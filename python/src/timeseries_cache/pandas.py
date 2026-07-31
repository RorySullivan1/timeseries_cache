"""The pandas boundary.

A thin adapter and nothing more: it converts frames and moves the timestamp
between an index and a column. It owns no coverage logic, no interval maths, and
no write-mode behavior — if a fix would have to land both here and in
:mod:`~timeseries_cache.core`, it belongs in core.

Requires the ``pandas`` extra (``pip install timeseries-cache[pandas]``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
import polars as pl

from .backends.base import StorageBackend
from .core import DEFAULT_TIMESTAMP_COLUMN, TimeseriesCache, WriteMode
from .errors import IndexContractError
from .index import Manifest
from .intervals import Interval, IntervalSet


@dataclass(frozen=True)
class PandasReadResult:
    """The pandas mirror of :class:`~timeseries_cache.core.ReadResult`."""

    frame: pd.DataFrame
    requested: Interval | None
    missing: IntervalSet

    @property
    def is_complete(self) -> bool:
        return not self.missing


class PandasTimeseriesCache:
    """Same cache, same semantics, pandas in and pandas out.

    A separate class rather than a ``frame=`` switch on
    :class:`~timeseries_cache.core.TimeseriesCache`: a parameter would make the
    return type dynamic and cost callers their type checking at exactly the
    boundary where it matters.
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        timestamp_column: str = DEFAULT_TIMESTAMP_COLUMN,
        identity_columns: Sequence[str] = (),
    ) -> None:
        self._cache = TimeseriesCache(
            backend,
            timestamp_column=timestamp_column,
            identity_columns=identity_columns,
        )

    @property
    def timestamp_column(self) -> str:
        return self._cache.timestamp_column

    @property
    def identity_columns(self) -> tuple[str, ...]:
        """Extra columns that, with the index, identify a row.

        They stay ordinary columns on the pandas side — the index remains the
        timestamp, and may legitimately repeat.
        """
        return self._cache.identity_columns

    @property
    def row_key(self) -> tuple[str, ...]:
        """The columns identifying a row: the timestamp plus identity columns."""
        return self._cache.row_key

    @property
    def backend(self) -> StorageBackend:
        return self._cache.backend

    # -------------------------------------------------------------- adapters

    def _to_polars(self, frame: pd.DataFrame) -> pl.DataFrame:
        column = self.timestamp_column
        # Both halves matter. `DataFrame.empty` is True whenever there are no
        # *columns*, even with a fully populated index — so testing it alone
        # would read a frame of bare timestamps as "upstream had nothing" and
        # drop every one of them while still claiming the window as covered.
        if frame.columns.empty and len(frame.index) == 0:
            # The "fetched, and upstream had nothing" case — no schema to carry.
            return pl.DataFrame()

        if duplicated := frame.columns[frame.columns.duplicated()].tolist():
            raise IndexContractError(
                f"frame has duplicate column name(s) {sorted(set(duplicated))}; "
                "rename them before writing"
            )

        if isinstance(frame.index, pd.MultiIndex):
            raise IndexContractError(
                "a MultiIndex has no single timestamp to key on. If the extra "
                "levels identify rows that share a timestamp (trade ids, say), "
                "reset them into columns and name them in identity_columns; "
                "otherwise move them into cache kwargs"
            )

        if isinstance(frame.index, pd.DatetimeIndex):
            if column in frame.columns:
                raise IndexContractError(
                    f"frame has both a DatetimeIndex and a {column!r} column; "
                    "drop one so the timestamp is unambiguous"
                )
            self._reject_submicrosecond(frame.index.dtype, frame.index)
            frame = frame.rename_axis(column).reset_index()
        elif column not in frame.columns:
            raise IndexContractError(
                f"frame needs a DatetimeIndex or a {column!r} column; got index "
                f"of type {type(frame.index).__name__} and columns "
                f"{list(frame.columns)}"
            )
        else:
            self._reject_submicrosecond(frame[column].dtype, frame[column])

        try:
            return pl.from_pandas(frame)
        except Exception as error:
            # Anything polars raises here is about the caller's frame, and a
            # polars traceback escaping would break the facade's promise that
            # its callers never see one.
            raise IndexContractError(
                f"frame could not be converted for storage: {error}"
            ) from error

    @staticmethod
    def _reject_submicrosecond(dtype: Any, values: Any) -> None:
        """Refuse nanosecond data that microsecond storage would truncate.

        pandas defaults to ``datetime64[ns]``, so this fires only when the
        values genuinely carry sub-microsecond detail — the case where silently
        rounding would lose information the caller may care about.
        """
        if getattr(dtype, "unit", None) != "ns":
            return
        index = pd.DatetimeIndex(values)
        if bool((index != index.floor("us")).any()):
            raise IndexContractError(
                "timestamps carry sub-microsecond precision, which this cache "
                "stores at microsecond resolution. Round explicitly (e.g. "
                '.dt.floor("us")) rather than losing it silently.'
            )

    def _to_pandas(self, frame: pl.DataFrame) -> pd.DataFrame:
        column = self.timestamp_column
        if frame.width == 0:
            # Spell the unit out: an undated `DatetimeIndex([])` resolves to
            # second resolution, so an empty read would come back with a
            # different index dtype from a non-empty one and refuse to line up
            # on concat or comparison.
            return pd.DataFrame(
                index=pd.DatetimeIndex([], dtype="datetime64[us, UTC]", name=column)
            )
        # numpy-backed on purpose: arrow-backed pandas has different null
        # semantics from np.nan, which silently changes how downstream
        # pct_change / rolling / dropna behave.
        out = frame.to_pandas(use_pyarrow_extension_array=False)
        if column in out.columns:
            out = out.set_index(column)
        return out

    # ------------------------------------------------------------------- API

    def read(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        columns: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> PandasReadResult:
        """Rows in ``[start, end]`` as a DatetimeIndex-ed frame, plus the gaps."""
        result = self._cache.read(start=start, end=end, columns=columns, **kwargs)
        return PandasReadResult(
            frame=self._to_pandas(result.frame),
            requested=result.requested,
            missing=result.missing,
        )

    def write(
        self,
        frame: pd.DataFrame,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        mode: WriteMode | str = WriteMode.UPSERT,
        **kwargs: Any,
    ) -> Interval:
        return self._cache.write(
            self._to_polars(frame), start=start, end=end, mode=mode, **kwargs
        )

    def delete(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        self._cache.delete(start=start, end=end, **kwargs)

    def coverage(self, **kwargs: Any) -> IntervalSet:
        return self._cache.coverage(**kwargs)

    def manifest(self, **kwargs: Any) -> Manifest | None:
        return self._cache.manifest(**kwargs)
