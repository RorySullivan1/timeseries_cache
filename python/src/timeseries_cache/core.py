"""The cache itself: polars in, polars out.

Depends on :class:`~timeseries_cache.backends.base.StorageBackend` only — never
on a concrete backend, so the coverage logic here is the same code whether the
bytes land in parquet, in memory, or somewhere not written yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import polars as pl

from .backends.base import StorageBackend
from .errors import (
    IndexContractError,
    OverlappingWriteError,
    SchemaMismatchError,
    WindowError,
)
from .index import Manifest
from .intervals import Interval, IntervalSet, ensure_utc
from .keys import CacheKey

TIMESTAMP_DTYPE = pl.Datetime("us", "UTC")
"""Canonical storage dtype for the timestamp column.

Microseconds, not nanoseconds, so the interval algebra (which runs on Python
``datetime``) and the stored data share one resolution. A mismatch there is a
silent off-by-one-tick bug in coverage; sub-microsecond input is rejected rather
than truncated.
"""

DEFAULT_TIMESTAMP_COLUMN = "ts"


def _has_adjacent_duplicate(frame: pl.DataFrame, column: str) -> bool:
    """Duplicate check for a column already known to be sorted.

    On sorted data a repeat can only sit next to its twin, so one linear pass
    answers it — no hash set over every row, which is what ``n_unique`` costs.
    """
    return bool(frame.select((pl.col(column) == pl.col(column).shift(1)).any()).item())


class WriteMode(StrEnum):
    """How a write reconciles with what the key already holds."""

    UPSERT = "upsert"
    """Incoming rows replace matching timestamps; existing rows outside the
    incoming index survive."""

    REPLACE_WINDOW = "replace_window"
    """Delete *everything* in the window, then insert. The scalpel: because the
    window is explicit, a corrected refetch removes stale rows the new data no
    longer contains."""

    APPEND_ONLY = "append_only"
    """Reject any write overlapping existing coverage — for sources where an
    overlap means a bug upstream."""


@dataclass(frozen=True)
class ReadResult:
    """Rows, plus what the cache does *not* know about the requested range."""

    frame: pl.DataFrame
    requested: Interval | None
    missing: IntervalSet

    @property
    def is_complete(self) -> bool:
        """True when the cache covered the whole request.

        ``False`` with an empty :attr:`frame` means "never fetched"; ``True``
        with an empty frame means "fetched, and there genuinely is nothing".
        Collapsing those two is the bug this cache exists to prevent.
        """
        return not self.missing


class TimeseriesCache:
    """A datetime-indexed cache addressed by arbitrary keyword arguments.

    Reserved names (``start``, ``end``, ``mode``, ``columns``, ``frame``) are
    control parameters and cannot double as cache kwargs; passing one raises
    :class:`~timeseries_cache.errors.InvalidKwargError`.
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        timestamp_column: str = DEFAULT_TIMESTAMP_COLUMN,
    ) -> None:
        self.backend = backend
        self.timestamp_column = timestamp_column

    # ---------------------------------------------------------------- helpers

    def _key(self, kwargs: dict[str, Any]) -> CacheKey:
        return CacheKey.build(kwargs)

    def _manifest(self, key: CacheKey) -> Manifest | None:
        manifest = self.backend.read_manifest(key)
        if manifest is not None:
            manifest.verify(key)
        return manifest

    def _is_schemaless_empty(self, frame: pl.DataFrame) -> bool:
        """An empty frame that doesn't even carry the timestamp column.

        Allowed on purpose: it is how a caller says "I fetched this window and
        upstream had nothing", without needing to invent a schema.
        """
        return frame.height == 0 and self.timestamp_column not in frame.columns

    def _canonicalize(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Enforce the index contract, returning a sorted, UTC-microsecond frame."""
        column = self.timestamp_column
        if column not in frame.columns:
            raise IndexContractError(
                f"frame has no {column!r} column; available: {frame.columns}. "
                "Every cached object is datetime-indexed."
            )

        dtype = frame.schema[column]
        if not isinstance(dtype, pl.Datetime):
            raise IndexContractError(
                f"{column!r} must be a Datetime column; got {dtype}"
            )
        if dtype.time_zone is None:
            raise IndexContractError(
                f"{column!r} is timezone-naive. Naive timestamps are rejected "
                "rather than assumed to be UTC — localize explicitly, e.g. "
                f'.with_columns(pl.col("{column}").dt.replace_time_zone("UTC"))'
            )

        if dtype.time_unit == "ns":
            truncating = frame.select(
                (pl.col(column).cast(pl.Int64) % 1_000 != 0).any()
            ).item()
            if truncating:
                raise IndexContractError(
                    f"{column!r} carries sub-microsecond precision, which this "
                    "cache stores at microsecond resolution. Round or truncate "
                    "deliberately before writing rather than losing it silently."
                )

        frame = frame.with_columns(
            pl.col(column).dt.convert_time_zone("UTC").cast(TIMESTAMP_DTYPE)
        )

        if frame.select(pl.col(column).is_null().any()).item():
            raise IndexContractError(f"{column!r} contains null timestamps")

        # Callers usually hand over data that is already in order; checking is a
        # linear pass, whereas sorting it again is not.
        if not frame.get_column(column).is_sorted():
            frame = frame.sort(column)

        if frame.height > 1 and _has_adjacent_duplicate(frame, column):
            duplicates = frame.height - frame.select(pl.col(column).n_unique()).item()
            raise IndexContractError(
                f"{column!r} has {duplicates} duplicate timestamp(s). The index "
                "must be unique; aggregate or de-duplicate before writing."
            )
        # Mark the invariant so downstream merges can rely on it without a
        # re-check; every path out of this function is sorted.
        return frame.with_columns(pl.col(column).set_sorted())

    @staticmethod
    def _schema_of(frame: pl.DataFrame) -> dict[str, str]:
        return {name: str(dtype) for name, dtype in frame.schema.items()}

    def _check_schema(self, stored: dict[str, str] | None, frame: pl.DataFrame) -> None:
        """Compare an incoming frame against the schema already on disk.

        ``stored`` comes from the *live* frame's schema where one exists, not
        from the manifest's stringified copy: comparing manifest strings would
        turn a polars release that changes a dtype's ``repr`` into a wave of
        spurious mismatches on caches that never changed.
        """
        if not stored:
            return
        incoming = self._schema_of(frame)
        if incoming == stored:
            return
        stored_cols, new_cols = set(stored), set(incoming)
        details: list[str] = []
        if missing := sorted(stored_cols - new_cols):
            details.append(f"missing columns {missing}")
        if added := sorted(new_cols - stored_cols):
            details.append(f"unexpected columns {added}")
        for name in sorted(stored_cols & new_cols):
            if stored[name] != incoming[name]:
                details.append(
                    f"{name!r} is {incoming[name]}, stored as {stored[name]}"
                )
        raise SchemaMismatchError(
            "incoming frame does not match the stored schema: "
            + "; ".join(details)
            + ". Delete the key or migrate it deliberately."
        )

    def _resolve_write_window(
        self,
        frame: pl.DataFrame,
        start: datetime | None,
        end: datetime | None,
        mode: WriteMode,
        *,
        schemaless_empty: bool,
    ) -> Interval:
        column = self.timestamp_column
        if (start is None) != (end is None):
            raise WindowError(
                "pass both start and end, or neither — a half-open window is "
                "ambiguous about what coverage is being claimed"
            )

        if start is not None and end is not None:
            window = Interval(
                ensure_utc(start, label="start"), ensure_utc(end, label="end")
            )
            if not schemaless_empty and frame.height:
                lo = frame.select(pl.col(column).min()).item()
                hi = frame.select(pl.col(column).max()).item()
                if lo < window.start or hi > window.end:
                    raise WindowError(
                        f"frame spans [{lo.isoformat()}, {hi.isoformat()}] which "
                        f"falls outside the declared window {window}. Widen the "
                        "window or trim the data — writing rows outside the range "
                        "you claim to cover makes coverage a lie."
                    )
            return window

        if mode is WriteMode.REPLACE_WINDOW:
            raise WindowError(
                "replace_window requires an explicit start and end. Deriving the "
                "window from the data's min/max would defeat the mode's purpose: "
                "the whole point is to delete stale rows the new data no longer "
                "contains."
            )
        if schemaless_empty or frame.height == 0:
            raise WindowError(
                "an empty write must declare the window it covers. That is how "
                "the cache records 'fetched, and there was genuinely nothing' as "
                "distinct from 'never fetched'."
            )
        return Interval(
            frame.select(pl.col(column).min()).item(),
            frame.select(pl.col(column).max()).item(),
        )

    # ------------------------------------------------------------------- read

    def read(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        columns: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> ReadResult:
        """Return the rows in ``[start, end]`` plus the subranges not covered.

        Bounds are **closed**. Omitting both defaults to the key's covered hull,
        so an unbounded read reports internal gaps rather than an unbounded one.
        """
        key = self._key(kwargs)
        manifest = self._manifest(key)

        if manifest is None:
            window = self._window_or_none(start, end, None)
            return ReadResult(
                frame=pl.DataFrame(),
                requested=window,
                missing=IntervalSet.of(window) if window else IntervalSet.empty(),
            )

        window = self._window_or_none(start, end, manifest.coverage.hull)
        lazy = self.backend.scan(key)
        if lazy is None:
            # No data file yet — a key covered only by empty writes. Validate
            # against the manifest anyway, so a typo doesn't sit silent until
            # the day real rows arrive.
            if columns is not None and manifest.schema:
                self._reject_unknown_columns(columns, set(manifest.schema))
            frame = pl.DataFrame()
        else:
            if window is not None:
                # Pushed into the parquet reader: only overlapping row groups
                # are touched. Do not "simplify" this into a collect-then-filter.
                lazy = lazy.filter(
                    pl.col(self.timestamp_column).is_between(
                        pl.lit(window.start, dtype=TIMESTAMP_DTYPE),
                        pl.lit(window.end, dtype=TIMESTAMP_DTYPE),
                        closed="both",
                    )
                )
            if columns is not None:
                self._reject_unknown_columns(
                    columns, set(lazy.collect_schema().names())
                )
                lazy = lazy.select(self._selection(columns))
            frame = lazy.collect()

        missing = (
            manifest.coverage.gaps_within(window) if window else IntervalSet.empty()
        )
        return ReadResult(frame=frame, requested=window, missing=missing)

    def _selection(self, columns: Iterable[str]) -> list[str]:
        """Always project the timestamp column, without duplicating it."""
        chosen = [self.timestamp_column]
        chosen.extend(c for c in columns if c != self.timestamp_column)
        return chosen

    @staticmethod
    def _reject_unknown_columns(requested: Iterable[str], available: set[str]) -> None:
        """Fail on a bad projection here rather than leaving it to polars.

        A ``ColumnNotFoundError`` escaping would break the pandas facade's
        promise that its callers never see a polars type.
        """
        if unknown := [name for name in requested if name not in available]:
            raise SchemaMismatchError(
                f"unknown column(s) {sorted(set(unknown))}; this key holds "
                f"{sorted(available)}"
            )

    @staticmethod
    def _window_or_none(
        start: datetime | None, end: datetime | None, hull: Interval | None
    ) -> Interval | None:
        """Resolve the requested window, filling omitted bounds from the hull.

        An unbounded request has no meaningful notion of "missing" — the gaps
        would run to infinity — so it degrades to the covered hull and reports
        internal gaps only.
        """
        lo = ensure_utc(start, label="start") if start is not None else None
        hi = ensure_utc(end, label="end") if end is not None else None
        anchor = lo if lo is not None else hi
        if lo is None:
            lo = hull.start if hull else None
        if hi is None:
            hi = hull.end if hull else None
        if lo is None or hi is None:
            return None
        if lo > hi:
            if start is not None and end is not None:
                # Both bounds are the caller's; an inverted range is their bug.
                return Interval(lo, hi)
            # One bound was filled from a hull that lies entirely on the other
            # side of it — e.g. read(start=X) where everything covered predates
            # X. Collapse onto the bound the caller actually gave, so the answer
            # is "nothing here, and that instant is unknown" rather than a crash.
            assert anchor is not None
            return Interval(anchor, anchor)
        return Interval(lo, hi)

    def coverage(self, **kwargs: Any) -> IntervalSet:
        """What the cache knows it has fetched for this key."""
        manifest = self._manifest(self._key(kwargs))
        return manifest.coverage if manifest else IntervalSet.empty()

    def manifest(self, **kwargs: Any) -> Manifest | None:
        return self._manifest(self._key(kwargs))

    # ------------------------------------------------------------------ write

    def write(
        self,
        frame: pl.DataFrame,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        mode: WriteMode | str = WriteMode.UPSERT,
        **kwargs: Any,
    ) -> Interval:
        """Merge ``frame`` into the key and record the window it covers.

        Returns the window now marked covered.
        """
        mode = WriteMode(mode)
        key = self._key(kwargs)
        manifest = self._manifest(key)

        schemaless_empty = self._is_schemaless_empty(frame)
        incoming = frame if schemaless_empty else self._canonicalize(frame)
        window = self._resolve_write_window(
            incoming, start, end, mode, schemaless_empty=schemaless_empty
        )

        if mode is WriteMode.APPEND_ONLY:
            overlap = (
                manifest.coverage if manifest else IntervalSet.empty()
            ).intersection(window)
            if overlap:
                raise OverlappingWriteError(
                    f"append_only write over {window} overlaps existing coverage "
                    f"{overlap}. Use upsert or replace_window if overwriting is "
                    "intended."
                )

        existing = self.backend.scan(key) if manifest is not None else None
        if not schemaless_empty:
            stored = (
                {name: str(dtype) for name, dtype in existing.collect_schema().items()}
                if existing is not None
                else (manifest.schema if manifest else None)
            )
            self._check_schema(stored, incoming)

        merged = self._merge(existing, incoming, window, mode, schemaless_empty)

        base = manifest or Manifest.new(key, timestamp_column=self.timestamp_column)
        updated = base.updated(
            coverage=base.coverage.union(window),
            schema=self._schema_of(merged) if merged.width else base.schema,
            row_count=merged.height,
        )
        # Even a zero-column result is written: for an empty, schema-less first
        # write there are no rows to store, but the coverage claim is the point.
        self.backend.write(key, merged, updated)
        return window

    def _merge(
        self,
        existing_lazy: pl.LazyFrame | None,
        incoming: pl.DataFrame,
        window: Interval,
        mode: WriteMode,
        schemaless_empty: bool,
    ) -> pl.DataFrame:
        column = self.timestamp_column

        if existing_lazy is None:
            return pl.DataFrame() if schemaless_empty else incoming

        if schemaless_empty:
            if mode is WriteMode.REPLACE_WINDOW:
                # An empty replace over a window is a deletion of that window.
                return existing_lazy.filter(
                    ~pl.col(column).is_between(
                        pl.lit(window.start, dtype=TIMESTAMP_DTYPE),
                        pl.lit(window.end, dtype=TIMESTAMP_DTYPE),
                        closed="both",
                    )
                ).collect()
            return existing_lazy.collect()

        # Align column order so the concat is unambiguous; schema equality was
        # already enforced above.
        incoming = incoming.select(existing_lazy.collect_schema().names())

        if mode is WriteMode.REPLACE_WINDOW:
            kept = existing_lazy.filter(
                ~pl.col(column).is_between(
                    pl.lit(window.start, dtype=TIMESTAMP_DTYPE),
                    pl.lit(window.end, dtype=TIMESTAMP_DTYPE),
                    closed="both",
                )
            )
        elif mode is WriteMode.UPSERT:
            kept = existing_lazy.join(
                incoming.lazy().select(column), on=column, how="anti"
            )
        else:  # APPEND_ONLY — coverage was already proven disjoint
            kept = existing_lazy

        # Measured, not assumed: `merge_sorted` looks like the obvious win here
        # (both sides are already ordered) but benchmarks slower than concat+sort
        # across every size and overlap shape tried on polars 1.43 — 27ms vs 18ms
        # appending to a 2M-row key. Polars' sort has a fast path for
        # nearly-ordered input that beats the interleave. Re-measure before
        # swapping this; don't swap it on principle.
        merged = (
            pl.concat([kept, incoming.lazy()], how="vertical").sort(column).collect()
        )

        # Cheaper than `n_unique()` (which builds a hash set over every row):
        # on a sorted column a repeat can only be adjacent.
        if merged.height > 1 and _has_adjacent_duplicate(merged, column):
            # pragma: no cover - defensive; the modes above should prevent it
            raise IndexContractError(
                f"merge produced duplicate timestamp(s) under mode "
                f"{mode.value}; this is a bug in the cache, not in your data"
            )
        return merged

    # ----------------------------------------------------------------- delete

    def delete(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        """Drop a window's rows *and* its coverage, or the whole key.

        Removing coverage alongside the rows is what makes this the inverse of a
        write: the range goes back to "unknown", so the next read asks for it.
        """
        key = self._key(kwargs)
        manifest = self._manifest(key)
        if manifest is None:
            return

        if start is None and end is None:
            self.backend.delete(key)
            return
        if (start is None) != (end is None):
            raise WindowError("pass both start and end, or neither, to delete()")

        window = Interval(
            ensure_utc(start, label="start"),  # type: ignore[arg-type]
            ensure_utc(end, label="end"),  # type: ignore[arg-type]
        )
        lazy = self.backend.scan(key)
        remaining = (
            lazy.filter(
                ~pl.col(self.timestamp_column).is_between(
                    pl.lit(window.start, dtype=TIMESTAMP_DTYPE),
                    pl.lit(window.end, dtype=TIMESTAMP_DTYPE),
                    closed="both",
                )
            ).collect()
            if lazy is not None
            else pl.DataFrame()
        )
        updated = manifest.updated(
            coverage=manifest.coverage.subtract(window),
            schema=manifest.schema,
            row_count=remaining.height,
        )
        # `manifest_first` because this update *shrinks* what the cache claims.
        # The usual data-first order would, if interrupted, leave the manifest
        # claiming a range whose rows are already gone — and a read of that range
        # would then answer "covered, and genuinely empty", which is the silent
        # hole invariant 5 forbids. Manifest first means an interruption merely
        # drops coverage early, so the range is refetched.
        self.backend.write(key, remaining, updated, manifest_first=True)
