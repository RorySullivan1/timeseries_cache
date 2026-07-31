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
    ) -> None:
        self._cache = TimeseriesCache(backend, timestamp_column=timestamp_column)

    @property
    def timestamp_column(self) -> str:
        return self._cache.timestamp_column

    @property
    def backend(self) -> StorageBackend:
        return self._cache.backend

    # -------------------------------------------------------------- adapters

    def _to_polars(self, frame: pd.DataFrame) -> pl.DataFrame:
        column = self.timestamp_column
        if frame.empty and frame.index.nlevels == 1 and frame.columns.empty:
            # The "fetched, and upstream had nothing" case — no schema to carry.
            return pl.DataFrame()

        if isinstance(frame.index, pd.MultiIndex):
            raise IndexContractError(
                "a MultiIndex has no single timestamp to key on; reset the "
                "extra levels into columns or into cache kwargs"
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

        return pl.from_pandas(frame)

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
            empty = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name=column))
            return empty
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
