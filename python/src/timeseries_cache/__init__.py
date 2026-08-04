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
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from .backends import MemoryBackend, ParquetBackend, StorageBackend
from .core import (
    DEFAULT_TIMESTAMP_COLUMN,
    TIMESTAMP_DTYPE,
    ReadResult,
    RecastReport,
    SchemaPolicy,
    TimeseriesCache,
    WriteMode,
)
from .errors import (
    CacheKeyCollisionError,
    IndexContractError,
    InvalidIdentityError,
    InvalidKwargError,
    OverlappingWriteError,
    SchemaForcedWarning,
    SchemaMismatchError,
    TimeseriesCacheError,
    UnknownKeyError,
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
    "InvalidIdentityError",
    "InvalidKwargError",
    "Manifest",
    "MemoryBackend",
    "OverlappingWriteError",
    "PandasReadResult",
    "PandasTimeseriesCache",
    "ParquetBackend",
    "ReadResult",
    "RecastReport",
    "SchemaForcedWarning",
    "SchemaMismatchError",
    "SchemaPolicy",
    "StorageBackend",
    "TimeseriesCache",
    "TimeseriesCacheError",
    "UnknownKeyError",
    "WindowError",
    "WriteMode",
    "open_cache",
    "open_pandas_cache",
]


def open_cache(
    root: str | os.PathLike[str],
    *,
    timestamp_column: str = DEFAULT_TIMESTAMP_COLUMN,
    identity_columns: Sequence[str] = (),
    schema_policy: SchemaPolicy | str = SchemaPolicy.LOSSLESS,
    conform_schema: bool | None = None,
    staging_dir: str | os.PathLike[str] | Literal["auto"] | None = "auto",
) -> TimeseriesCache:
    """Open a parquet-backed cache rooted at ``root``.

    ``staging_dir`` defaults to ``"auto"``: when ``root`` looks like a network
    or DFS share, files are built and flushed on local disk and only the
    finished bytes cross the wire. A local cache is unaffected. Pass a path to
    pick the directory yourself, or ``None`` to always build beside the target.

    ``schema_policy`` decides how much latitude an incoming dtype gets against
    the one already stored — ``"lossless"`` (the default), ``"strict"``, or
    ``"force"``. See :class:`~timeseries_cache.core.SchemaPolicy`.

    The convenience wiring lives here rather than in ``core`` so the coverage
    logic never imports a concrete backend.
    """
    return TimeseriesCache(
        ParquetBackend(root, staging_dir=staging_dir),
        timestamp_column=timestamp_column,
        identity_columns=identity_columns,
        schema_policy=schema_policy,
        conform_schema=conform_schema,
    )


def open_pandas_cache(
    root: str | os.PathLike[str],
    *,
    timestamp_column: str = DEFAULT_TIMESTAMP_COLUMN,
    identity_columns: Sequence[str] = (),
    schema_policy: SchemaPolicy | str = SchemaPolicy.LOSSLESS,
    conform_schema: bool | None = None,
    staging_dir: str | os.PathLike[str] | Literal["auto"] | None = "auto",
) -> PandasTimeseriesCache:
    """Open a parquet-backed cache with the pandas facade.

    See :func:`open_cache` for ``staging_dir`` and ``schema_policy``.
    """
    from .pandas import PandasTimeseriesCache

    return PandasTimeseriesCache(
        ParquetBackend(root, staging_dir=staging_dir),
        timestamp_column=timestamp_column,
        identity_columns=identity_columns,
        schema_policy=schema_policy,
        conform_schema=conform_schema,
    )


def __getattr__(name: str) -> Any:
    """Expose the pandas facade without importing pandas unless it's asked for."""
    if name in {"PandasTimeseriesCache", "PandasReadResult"}:
        from . import pandas as _pandas

        return getattr(_pandas, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
