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
    InvalidIdentityError,
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


def _has_adjacent_duplicate(frame: pl.DataFrame, columns: Sequence[str]) -> bool:
    """Duplicate check for columns already known to be sorted.

    On sorted data a repeat can only sit next to its twin, so one linear pass
    answers it — no hash set over every row, which is what ``n_unique`` costs.
    A composite key repeats only when *every* component matches its predecessor.
    """
    return bool(
        frame.select(
            pl.all_horizontal(
                [pl.col(name) == pl.col(name).shift(1) for name in columns]
            ).any()
        ).item()
    )


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

    Args:
        backend: Where the bytes live.
        timestamp_column: The designated UTC timestamp column.
        identity_columns: Extra columns that, together with the timestamp,
            identify a row. Empty (the default) means the timestamp alone is the
            identity and must be unique. Supplying them — ``("trade_id",)`` for
            trade data, where many prints share a timestamp — makes
            ``(timestamp, *identity_columns)`` the unit of uniqueness *and* the
            unit an ``upsert`` overwrites. Coverage stays purely time-based
            either way; only row identity changes.
        conform_schema: When true (the default), a column whose dtype differs
            from the one already stored is cast to the stored dtype, provided
            the cast is **provably lossless** for the values present. A batch
            that infers ``Int64`` where the key holds ``Float64`` then lands
            instead of raising. A cast that would lose anything still raises
            :class:`~timeseries_cache.errors.SchemaMismatchError`. Set false to
            require every write to arrive already matching the stored dtypes.
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        timestamp_column: str = DEFAULT_TIMESTAMP_COLUMN,
        identity_columns: Sequence[str] = (),
        conform_schema: bool = True,
    ) -> None:
        if timestamp_column in identity_columns:
            raise InvalidIdentityError(
                f"{timestamp_column!r} is the timestamp column and is always part "
                "of a row's identity; list only the *extra* columns in "
                "identity_columns"
            )
        if len(set(identity_columns)) != len(identity_columns):
            raise InvalidIdentityError(
                f"identity_columns has repeats: {list(identity_columns)}"
            )
        self.backend = backend
        self.timestamp_column = timestamp_column
        self.identity_columns = tuple(identity_columns)
        self.conform_schema = conform_schema

    @property
    def row_key(self) -> tuple[str, ...]:
        """The columns that identify a row: the timestamp plus any identity
        columns. This is what uniqueness is enforced on and what an ``upsert``
        matches against."""
        return (self.timestamp_column, *self.identity_columns)

    # ---------------------------------------------------------------- helpers

    def _key(self, kwargs: dict[str, Any]) -> CacheKey:
        return CacheKey.build(kwargs)

    def _manifest(self, key: CacheKey) -> Manifest | None:
        manifest = self.backend.read_manifest(key)
        if manifest is not None:
            manifest.verify(key)
            manifest.verify_identity(self.identity_columns)
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

        row_key = self.row_key
        if missing := [c for c in self.identity_columns if c not in frame.columns]:
            raise InvalidIdentityError(
                f"identity column(s) {missing} are not in the frame; available: "
                f"{frame.columns}"
            )
        for name in self.identity_columns:
            if frame.select(pl.col(name).is_null().any()).item():
                raise InvalidIdentityError(
                    f"identity column {name!r} contains nulls; a null cannot "
                    "identify a row"
                )

        # Sort on the full row key, not just the timestamp: with identity
        # columns the stored order has to be deterministic among rows sharing a
        # timestamp, or a round-trip wouldn't be stable and the adjacent-
        # duplicate check below would miss pairs that aren't neighbours.
        # Callers usually hand over ordered data, and checking is a linear pass
        # where sorting again is not.
        if not self._is_sorted_by(frame, row_key):
            frame = frame.sort(row_key)

        if frame.height > 1 and _has_adjacent_duplicate(frame, row_key):
            unique = frame.select(pl.struct(row_key).n_unique()).item()
            duplicates = frame.height - unique
            if self.identity_columns:
                raise IndexContractError(
                    f"{duplicates} row(s) repeat the identity {row_key}. "
                    "Timestamps may repeat, but the timestamp together with "
                    "identity_columns must be unique."
                )
            raise IndexContractError(
                f"{column!r} has {duplicates} duplicate timestamp(s). The index "
                "must be unique; pass identity_columns if rows legitimately "
                "share a timestamp (e.g. trades), or de-duplicate before writing."
            )
        # Mark the invariant so downstream merges can rely on it without a
        # re-check; every path out of this function is sorted.
        return frame.with_columns(pl.col(column).set_sorted())

    @staticmethod
    def _is_sorted_by(frame: pl.DataFrame, columns: Sequence[str]) -> bool:
        """Whether the frame is already ordered by the given columns."""
        if len(columns) == 1:
            return bool(frame.get_column(columns[0]).is_sorted())
        # No composite `is_sorted`, so compare against the sorted key columns.
        keys = frame.select(columns)
        return keys.equals(keys.sort(list(columns)))

    @staticmethod
    def _schema_of(frame: pl.DataFrame) -> dict[str, str]:
        return {name: str(dtype) for name, dtype in frame.schema.items()}

    @staticmethod
    def _is_all_null(frame: pl.DataFrame, column: str) -> bool:
        if frame.height == 0:
            return True
        return frame.get_column(column).null_count() == frame.height

    def _reconcile_null_typing(
        self, frame: pl.DataFrame, stored: pl.Schema | None
    ) -> pl.DataFrame:
        """Stop a column of nothing but nulls from voting on the schema.

        A batch where a column came back entirely null carries no information
        about that column's type, but something still has to name a dtype, and
        what gets named is an accident of the boundary: pandas turns ``[None,
        None]`` into ``object``, which becomes polars ``String``. Left alone
        that either fixes a key's schema as ``String`` on the first write and
        rejects every real write after it, or arrives later and gets rejected
        as a schema change. Neither is a schema change — both are inference
        artifacts.

        So an all-null column never gets a vote. It takes the stored dtype where
        one is known, and is stored as ``Null`` — honestly "not yet known" —
        where one is not, so that a later write carrying real values can settle
        it. Casting an all-null column is lossless in every direction, which is
        what makes this safe; a column *with* values is never touched, so a
        genuine disagreement still raises.
        """
        casts: list[pl.Expr] = []
        for name, dtype in frame.schema.items():
            if name in self.row_key or not self._is_all_null(frame, name):
                continue
            known: pl.DataType | type[pl.DataType] = pl.Null
            if stored is not None and name in stored:
                known = stored[name]
            if known != dtype:
                casts.append(pl.col(name).cast(known))
        return frame.with_columns(casts) if casts else frame

    @staticmethod
    def _is_exact(dtype: pl.DataType) -> bool:
        """Types whose values have exactly one representation.

        Within this family, casting back is a fair test of losslessness. Text is
        deliberately outside it: ``"1.50"`` parsed to ``1.5`` and formatted back
        is ``"1.5"``, which is a difference in spelling, not in data.
        """
        return dtype.is_numeric() or dtype.is_temporal() or dtype == pl.Boolean

    @staticmethod
    def _round_trips(column: pl.Series, target: pl.DataType) -> bool:
        """Does casting to ``target`` and back reproduce every value exactly?

        The honest test of "lossless for the values present", and the reason
        conforming can be a default. It catches everything a strict cast waves
        through: polars truncates ``1.5`` to ``1`` for Float64 -> Int64, maps
        ``5`` to ``True`` for Int64 -> Boolean, drops sub-millisecond precision
        for Datetime("us") -> Datetime("ms"), and loses the low bits of an
        integer past 2**53 through Float64. None of those raise; all of them
        fail to round-trip, so all of them are refused.

        A pair polars cannot cast *back* is unverifiable rather than proven
        lossless, and this answers False for it — refusing is the conservative
        direction, and the only one consistent with conforming being a default.
        """
        try:
            returned = column.cast(target).cast(column.dtype)
        except pl.exceptions.PolarsError:
            return False
        return bool(returned.eq_missing(column).all())

    def _conform_to_stored(
        self, frame: pl.DataFrame, stored: pl.Schema | None
    ) -> pl.DataFrame:
        """Cast columns to the dtypes already stored, where that loses nothing.

        The stored schema is the key's schema; a batch's inferred dtypes are a
        guess made by whatever produced the batch. A vendor window whose values
        all happen to be whole numbers infers ``Int64`` against a key holding
        ``Float64``, and a partially-null column can land on a different type
        from one fetch to the next. None of that is a schema change, and having
        it fail the write is the wrong answer.

        So an incoming column defers to the stored dtype — but only when the
        conversion is **provably lossless for the values actually present**:

        - the strict cast must succeed, so ``"cheap"`` -> ``Float64`` still
          raises rather than becoming null (the failure mode that makes a blunt
          "just cast everything" dangerous);
        - nothing may become null that was not null already;
        - between types with exact representations it must also round-trip, so
          ``1.5`` -> ``Int64`` is refused rather than silently truncated.

        Anything that fails those tests is a genuine disagreement and raises.
        """
        if stored is None:
            return frame
        casts: list[pl.Expr] = []
        for name, dtype in frame.schema.items():
            if name not in stored:
                continue
            target = stored[name]
            if target == dtype or target == pl.Null:
                continue
            column = frame.get_column(name)
            if dtype == pl.Null:
                # No values to lose; the all-null path already settled this.
                casts.append(pl.col(name).cast(target))
                continue
            try:
                converted = column.cast(target)
            except pl.exceptions.PolarsError:
                raise SchemaMismatchError(
                    f"{name!r} arrived as {dtype} and the key stores {target}, "
                    f"and the values do not convert. Fix the source's types, or "
                    f"pass conform_schema=False to require an exact match."
                ) from None
            if converted.null_count() != column.null_count():
                raise SchemaMismatchError(
                    f"{name!r} arrived as {dtype} and the key stores {target}; "
                    f"converting would turn "
                    f"{converted.null_count() - column.null_count()} value(s) "
                    f"into null. Refusing rather than losing them."
                )
            if (
                self._is_exact(dtype)
                and self._is_exact(target)
                and not self._round_trips(column, target)
            ):
                raise SchemaMismatchError(
                    f"{name!r} arrived as {dtype} and the key stores {target}, "
                    f"but the values do not survive the conversion — it would "
                    f"change them (truncating 1.5 to 1, 5 to True, or a "
                    f"microsecond to a millisecond). Send the column as "
                    f"{target}, or store it as {dtype} under a new key."
                )
            casts.append(pl.col(name).cast(target))
        return frame.with_columns(casts) if casts else frame

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
            # A stored `Null` column means every row written so far was null, so
            # the type was never established. A write that finally carries values
            # settles it rather than conflicting with it.
            if stored[name] == str(pl.Null):
                continue
            if stored[name] != incoming[name]:
                details.append(
                    f"{name!r} is {incoming[name]}, stored as {stored[name]}"
                )
        if not details:
            return
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
        """Always project the row key, without duplicating it.

        Identity columns come back whether or not they were asked for, same as
        the timestamp: a projection that dropped them would hand the caller rows
        it cannot tell apart.
        """
        chosen = list(self.row_key)
        chosen.extend(c for c in columns if c not in self.row_key)
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
        live = existing.collect_schema() if existing is not None else None
        if not schemaless_empty:
            # Settle all-null columns against what is already known *before*
            # checking, so an inference artifact never reads as a schema change.
            incoming = self._reconcile_null_typing(incoming, live)
            if self.conform_schema:
                # The stored dtypes are the key's schema; a batch's inferred
                # dtypes are a guess. Defer to the former where nothing is lost.
                incoming = self._conform_to_stored(incoming, live)
            stored = (
                {name: str(dtype) for name, dtype in live.items()}
                if live is not None
                else (manifest.schema if manifest else None)
            )
            self._check_schema(stored, incoming)

        merged = self._merge(existing, incoming, window, mode, schemaless_empty)
        # Drop the scan before handing anything to the backend. `merged` is
        # already materialized, so nothing below needs it — but on Windows a
        # file cannot be replaced while a handle to it is open, and this scan
        # points at the very file the backend is about to move the new one onto.
        # POSIX doesn't care (the old inode survives its last handle), which is
        # exactly why leaving it alive looks harmless on Linux and fails there.
        del existing

        base = manifest or Manifest.new(
            key,
            timestamp_column=self.timestamp_column,
            identity_columns=self.identity_columns,
        )
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

        # Promote any column the stored data left as `Null` — every row written
        # so far was null, and this write finally names the type. The existing
        # rows are all null in that column, so the cast invents nothing.
        stored_schema = existing_lazy.collect_schema()
        promotions = [
            pl.col(name).cast(incoming.schema[name])
            for name, dtype in stored_schema.items()
            if dtype == pl.Null and incoming.schema.get(name, pl.Null) != pl.Null
        ]
        if promotions:
            existing_lazy = existing_lazy.with_columns(promotions)

        # Align column order so the concat is unambiguous; schema equality was
        # already enforced above.
        incoming = incoming.select(stored_schema.names())

        if mode is WriteMode.REPLACE_WINDOW:
            kept = existing_lazy.filter(
                ~pl.col(column).is_between(
                    pl.lit(window.start, dtype=TIMESTAMP_DTYPE),
                    pl.lit(window.end, dtype=TIMESTAMP_DTYPE),
                    closed="both",
                )
            )
        elif mode is WriteMode.UPSERT:
            # Anti-join on the *row key*, not the timestamp alone. With identity
            # columns this is the whole point: two trades sharing a timestamp are
            # different rows, so an incoming trade must displace only its own
            # prior version, not every print at that instant.
            row_key = list(self.row_key)
            kept = existing_lazy.join(
                incoming.lazy().select(row_key), on=row_key, how="anti"
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
            pl.concat([kept, incoming.lazy()], how="vertical")
            .sort(list(self.row_key))
            .collect()
        )

        # Cheaper than `n_unique()` (which builds a hash set over every row):
        # on sorted data a repeat can only be adjacent.
        if merged.height > 1 and _has_adjacent_duplicate(merged, self.row_key):
            # pragma: no cover - defensive; the modes above should prevent it
            raise IndexContractError(
                f"merge produced rows repeating the identity {self.row_key} "
                f"under mode {mode.value}; this is a bug in the cache, not in "
                "your data"
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
