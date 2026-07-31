from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from timeseries_cache import MemoryBackend, ParquetBackend, TimeseriesCache

DAY = timedelta(days=1)


def ts(day: int, *, hour: int = 0, month: int = 1) -> datetime:
    """A terse UTC timestamp for tests: ``ts(3)`` is 2024-01-03T00:00Z."""
    return datetime(2024, month, day, hour, tzinfo=UTC)


def frame(days: list[int], *, price: float = 100.0) -> pl.DataFrame:
    """A minimal well-formed cache frame over the given January days."""
    return pl.DataFrame(
        {
            "ts": [ts(d) for d in days],
            "price": [price + d for d in days],
        },
        schema={"ts": pl.Datetime("us", "UTC"), "price": pl.Float64},
    )


@pytest.fixture(params=["memory", "parquet"])
def backend(request: pytest.FixtureRequest, tmp_path):
    """Every behavioral test runs against both backends.

    The coverage logic is supposed to be storage-independent; running one suite
    over both is what actually proves it.
    """
    if request.param == "memory":
        return MemoryBackend()
    return ParquetBackend(tmp_path / "cache")


@pytest.fixture
def cache(backend) -> TimeseriesCache:
    return TimeseriesCache(backend)
