"""A lightweight cache for datetime-indexed data.

Two facades over one core::

    from timeseries_cache import open_cache, open_pandas_cache

    cache = open_cache("/var/cache/prices")            # polars in, polars out
    pcache = open_pandas_cache("/var/cache/prices")    # pandas in, pandas out

Both are addressed by arbitrary keyword arguments::

    result = cache.read(start=lo, end=hi, ticker="AAPL", field="close")
    for gap in result.missing:            # fetch only what isn't known yet
        cache.write(fetch(gap), start=gap.start, end=gap.end,
                    ticker="AAPL", field="close")
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from .backends import MemoryBackend, ParquetBackend, StorageBackend
from .core import (
    DEFAULT_TIMESTAMP_COLUMN,
    TIMESTAMP_DTYPE,
    ReadResult,
    TimeseriesCache,
    WriteMode,
)
from .errors import (
    CacheKeyCollisionError,
    IndexContractError,
    InvalidKwargError,
    OverlappingWriteError,
    SchemaMismatchError,
    TimeseriesCacheError,
    WindowError,
)
from .index import Manifest
from .intervals import Interval, IntervalSet
from .keys import RESERVED_KWARGS, CacheKey

if TYPE_CHECKING:  # pragma: no cover
    from .pandas import PandasReadResult, PandasTimeseriesCache

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_TIMESTAMP_COLUMN",
    "RESERVED_KWARGS",
    "TIMESTAMP_DTYPE",
    "CacheKey",
    "CacheKeyCollisionError",
    "IndexContractError",
    "Interval",
    "IntervalSet",
    "InvalidKwargError",
    "Manifest",
    "MemoryBackend",
    "OverlappingWriteError",
    "PandasReadResult",
    "PandasTimeseriesCache",
    "ParquetBackend",
    "ReadResult",
    "SchemaMismatchError",
    "StorageBackend",
    "TimeseriesCache",
    "TimeseriesCacheError",
    "WindowError",
    "WriteMode",
    "open_cache",
    "open_pandas_cache",
]


def open_cache(
    root: str | os.PathLike[str],
    *,
    timestamp_column: str = DEFAULT_TIMESTAMP_COLUMN,
) -> TimeseriesCache:
    """Open a parquet-backed cache rooted at ``root``.

    The convenience wiring lives here rather than in ``core`` so the coverage
    logic never imports a concrete backend.
    """
    return TimeseriesCache(ParquetBackend(root), timestamp_column=timestamp_column)


def open_pandas_cache(
    root: str | os.PathLike[str],
    *,
    timestamp_column: str = DEFAULT_TIMESTAMP_COLUMN,
) -> PandasTimeseriesCache:
    """Open a parquet-backed cache with the pandas facade."""
    from .pandas import PandasTimeseriesCache

    return PandasTimeseriesCache(
        ParquetBackend(root), timestamp_column=timestamp_column
    )


def __getattr__(name: str) -> Any:
    """Expose the pandas facade without importing pandas unless it's asked for."""
    if name in {"PandasTimeseriesCache", "PandasReadResult"}:
        from . import pandas as _pandas

        return getattr(_pandas, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
