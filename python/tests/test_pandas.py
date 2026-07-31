"""The pandas facade.

Behavioral parity is covered by running the same scenarios as ``test_core``;
what is unique here is the boundary itself — index round-trip, dtype backing,
and the promise that polars never leaks through.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from timeseries_cache import (
    IndexContractError,
    Interval,
    OverlappingWriteError,
    WindowError,
    WriteMode,
)
from timeseries_cache.pandas import PandasTimeseriesCache

from .conftest import ts

SERIES = {"ticker": "AAPL", "field": "close"}


@pytest.fixture
def pcache(backend) -> PandasTimeseriesCache:
    return PandasTimeseriesCache(backend)


def pframe(days: list[int], *, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {"price": [price + d for d in days]},
        index=pd.DatetimeIndex([ts(d) for d in days], tz="UTC", name="ts"),
    )


class TestRoundTrip:
    def test_frame_comes_back_identical(self, pcache: PandasTimeseriesCache):
        original = pframe([1, 2, 3])
        pcache.write(original, **SERIES)
        assert_frame_equal(pcache.read(**SERIES).frame, original)

    def test_index_name_and_timezone_survive(self, pcache: PandasTimeseriesCache):
        pcache.write(pframe([1]), **SERIES)
        index = pcache.read(**SERIES).frame.index
        assert isinstance(index, pd.DatetimeIndex)
        assert index.name == "ts"
        assert str(index.tz) == "UTC"

    def test_dtypes_are_numpy_backed_not_arrow_backed(
        self, pcache: PandasTimeseriesCache
    ):
        """Arrow-backed nulls behave differently from np.nan, which would
        silently change downstream pct_change / rolling / dropna."""
        pcache.write(pframe([1, 2]), **SERIES)
        frame = pcache.read(**SERIES).frame
        assert frame["price"].dtype == "float64"
        assert not isinstance(frame["price"].dtype, pd.ArrowDtype)

    def test_accepts_a_timestamp_column_instead_of_an_index(
        self, pcache: PandasTimeseriesCache
    ):
        flat = pframe([1, 2]).reset_index()
        pcache.write(flat, **SERIES)
        assert pcache.read(**SERIES).frame.index.name == "ts"

    def test_empty_result_still_has_a_datetime_index(
        self, pcache: PandasTimeseriesCache
    ):
        result = pcache.read(start=ts(1), end=ts(2), **SERIES)
        assert result.frame.empty
        assert isinstance(result.frame.index, pd.DatetimeIndex)

    def test_empty_and_non_empty_reads_share_an_index_dtype(
        self, pcache: PandasTimeseriesCache
    ):
        """Otherwise the two won't line up on concat or comparison — an
        undated empty DatetimeIndex defaults to second resolution."""
        empty = pcache.read(start=ts(1), end=ts(2), **SERIES).frame
        pcache.write(pframe([1]), **SERIES)
        filled = pcache.read(**SERIES).frame
        assert empty.index.dtype == filled.index.dtype
        assert pd.concat([empty, filled]).index.dtype == filled.index.dtype

    def test_timestamps_with_no_data_columns_are_not_mistaken_for_emptiness(
        self, pcache: PandasTimeseriesCache
    ):
        """`DataFrame.empty` is True for any frame with no *columns*, even one
        carrying a full index. Treating that as "upstream had nothing" would
        drop every timestamp while still claiming the window as covered."""
        bare = pd.DataFrame(index=pd.DatetimeIndex([ts(1), ts(2)], tz="UTC", name="ts"))
        assert bare.empty  # the trap
        pcache.write(bare, start=ts(1), end=ts(2), **SERIES)

        result = pcache.read(start=ts(1), end=ts(2), **SERIES)
        assert result.frame.index.tolist() == [ts(1), ts(2)]
        assert result.is_complete


class TestBoundaryRejections:
    def test_rejects_a_naive_index(self, pcache: PandasTimeseriesCache):
        naive = pd.DataFrame(
            {"price": [1.0]},
            index=pd.DatetimeIndex([datetime(2024, 1, 1)], name="ts"),
        )
        with pytest.raises(IndexContractError, match="timezone-naive"):
            pcache.write(naive, **SERIES)

    def test_rejects_sub_microsecond_precision(self, pcache: PandasTimeseriesCache):
        nanos = pd.DataFrame(
            {"price": [1.0]},
            index=pd.DatetimeIndex(
                [pd.Timestamp("2024-01-01T00:00:00.000000001Z")], name="ts"
            ),
        )
        with pytest.raises(IndexContractError, match="sub-microsecond"):
            pcache.write(nanos, **SERIES)

    def test_accepts_nanosecond_dtype_without_sub_microsecond_values(
        self, pcache: PandasTimeseriesCache
    ):
        # The check must fire on truncation, not on the dtype alone — plenty of
        # ns-typed data is microsecond-exact.
        frame = pframe([1])
        frame.index = frame.index.astype("datetime64[ns, UTC]")
        assert frame.index.dtype == "datetime64[ns, UTC]"
        pcache.write(frame, **SERIES)
        assert pcache.read(**SERIES).frame.shape == (1, 1)

    def test_rejects_a_multiindex(self, pcache: PandasTimeseriesCache):
        multi = pd.DataFrame(
            {"price": [1.0]},
            index=pd.MultiIndex.from_tuples(
                [(datetime(2024, 1, 1, tzinfo=UTC), "AAPL")], names=["ts", "ticker"]
            ),
        )
        with pytest.raises(IndexContractError, match="MultiIndex"):
            pcache.write(multi, **SERIES)

    def test_rejects_an_ambiguous_timestamp(self, pcache: PandasTimeseriesCache):
        both = pframe([1]).assign(ts=[ts(1)])
        with pytest.raises(IndexContractError, match="both a DatetimeIndex"):
            pcache.write(both, **SERIES)

    def test_rejects_a_frame_with_no_timestamp_at_all(
        self, pcache: PandasTimeseriesCache
    ):
        with pytest.raises(IndexContractError, match="needs a DatetimeIndex"):
            pcache.write(pd.DataFrame({"price": [1.0, 2.0]}), **SERIES)

    def test_rejects_duplicate_timestamps(self, pcache: PandasTimeseriesCache):
        dupes = pd.DataFrame(
            {"price": [1.0, 2.0]},
            index=pd.DatetimeIndex([ts(1), ts(1)], tz="UTC", name="ts"),
        )
        with pytest.raises(IndexContractError, match="duplicate timestamp"):
            pcache.write(dupes, **SERIES)


class TestSemanticParity:
    """The same behaviors test_core asserts, seen through the facade."""

    def test_unknown_range_is_missing(self, pcache: PandasTimeseriesCache):
        result = pcache.read(start=ts(1), end=ts(5), **SERIES)
        assert not result.is_complete
        assert result.missing.intervals == (Interval(ts(1), ts(5)),)

    def test_fetched_but_empty_range_is_complete(self, pcache: PandasTimeseriesCache):
        pcache.write(pd.DataFrame(), start=ts(1), end=ts(5), **SERIES)
        result = pcache.read(start=ts(1), end=ts(5), **SERIES)
        assert result.frame.empty
        assert result.is_complete

    def test_upsert_replaces_overlapping_rows(self, pcache: PandasTimeseriesCache):
        pcache.write(pframe([1, 2, 3]), **SERIES)
        pcache.write(pframe([2, 3], price=900.0), **SERIES)
        assert pcache.read(**SERIES).frame["price"].tolist() == [101.0, 902.0, 903.0]

    def test_replace_window_removes_stale_rows(self, pcache: PandasTimeseriesCache):
        pcache.write(pframe([1, 2, 3, 4, 5]), **SERIES)
        pcache.write(
            pframe([2, 4], price=900.0),
            start=ts(2),
            end=ts(4),
            mode=WriteMode.REPLACE_WINDOW,
            **SERIES,
        )
        assert pcache.read(**SERIES).frame.index.day.tolist() == [1, 2, 4, 5]

    def test_append_only_rejects_overlap(self, pcache: PandasTimeseriesCache):
        pcache.write(pframe([1, 5]), mode=WriteMode.APPEND_ONLY, **SERIES)
        with pytest.raises(OverlappingWriteError):
            pcache.write(pframe([3, 8]), mode=WriteMode.APPEND_ONLY, **SERIES)

    def test_window_delete_reopens_the_range(self, pcache: PandasTimeseriesCache):
        pcache.write(pframe([1, 2, 3]), **SERIES)
        pcache.delete(start=ts(2), end=ts(2), **SERIES)
        result = pcache.read(start=ts(1), end=ts(3), **SERIES)
        assert result.frame.index.day.tolist() == [1, 3]
        assert result.missing.intervals == (Interval(ts(2), ts(2)),)

    def test_inclusive_slicing(self, pcache: PandasTimeseriesCache):
        pcache.write(pframe([1, 2, 3, 4, 5]), **SERIES)
        result = pcache.read(start=ts(2), end=ts(4), **SERIES)
        assert result.frame.index.day.tolist() == [2, 3, 4]

    def test_column_projection(self, pcache: PandasTimeseriesCache):
        wide = pframe([1, 2]).assign(volume=1.0)
        pcache.write(wide, **SERIES)
        assert pcache.read(columns=["volume"], **SERIES).frame.columns.tolist() == [
            "volume"
        ]

    def test_half_declared_window_is_rejected(self, pcache: PandasTimeseriesCache):
        with pytest.raises(WindowError, match="both start and end"):
            pcache.write(pframe([1]), start=ts(1), **SERIES)


class TestNoPolarsLeak:
    def test_read_returns_pandas(self, pcache: PandasTimeseriesCache):
        pcache.write(pframe([1]), **SERIES)
        assert isinstance(pcache.read(**SERIES).frame, pd.DataFrame)

    def test_errors_are_package_errors_not_polars_errors(
        self, pcache: PandasTimeseriesCache
    ):
        from timeseries_cache.errors import TimeseriesCacheError

        with pytest.raises(TimeseriesCacheError) as caught:
            pcache.write(pd.DataFrame({"price": [1.0]}), **SERIES)
        assert "polars" not in type(caught.value).__module__

    def test_duplicate_column_names_raise_a_package_error(
        self, pcache: PandasTimeseriesCache
    ):
        """`pl.from_pandas` raises a bare polars ValueError on these."""
        from timeseries_cache.errors import TimeseriesCacheError

        dupes = pframe([1, 2])
        dupes.insert(1, "price", [1.0, 2.0], allow_duplicates=True)
        with pytest.raises(TimeseriesCacheError) as caught:
            pcache.write(dupes, **SERIES)
        assert "polars" not in type(caught.value).__module__

    def test_unknown_column_on_read_raises_a_package_error(
        self, pcache: PandasTimeseriesCache
    ):
        from timeseries_cache.errors import TimeseriesCacheError

        pcache.write(pframe([1]), **SERIES)
        with pytest.raises(TimeseriesCacheError) as caught:
            pcache.read(columns=["nope"], **SERIES)
        assert "polars" not in type(caught.value).__module__

    def test_facade_owns_no_coverage_logic(self):
        """Guards the layering rule: if a fix would have to land in both
        facades, it belongs in core instead."""
        import inspect

        from timeseries_cache import pandas as facade

        source = inspect.getsource(facade)
        for leaked in ("IntervalSet.of", "gaps_within", "WriteMode.REPLACE_WINDOW"):
            assert leaked not in source
