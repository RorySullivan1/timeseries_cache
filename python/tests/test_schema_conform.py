"""A batch's dtypes are a guess; the stored dtypes are the key's schema.

Whatever produced an incoming batch inferred its types from the values in that
batch alone. A window whose prices all happen to be whole numbers infers
``Int64`` against a key holding ``Float64``; a partially-null column can land on
a different type from one fetch to the next. None of that is a schema change,
and failing the write over it is the wrong answer.

So an incoming column defers to the stored dtype — but only where the
conversion is provably lossless *for the values actually present*. Three tests
gate it, and the tests below are organized around them:

1. the strict cast must succeed — ``"cheap"`` has no Float64 reading;
2. nothing may become null that was not null already;
3. between types with exact representations the values must round-trip, which
   is what catches the conversions polars performs silently: 1.5 -> 1,
   5 -> True, microseconds -> milliseconds, and integers past 2**53.

`schema_policy` picks how much latitude the batch gets: "lossless" (default) is
the above, "strict" demands an exact match, and "force" converts anyway and
accepts the loss (``tests/test_schema_force.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest

from timeseries_cache import Interval, SchemaMismatchError, TimeseriesCache

from .conftest import ts

SERIES = {"ticker": "AAPL", "field": "close"}
TS = pl.Datetime("us", "UTC")


def typed(days: list[int], values: list[object], dtype: pl.DataType) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts": [ts(d) for d in days], "price": pl.Series("price", values, dtype=dtype)},
        schema={"ts": TS, "price": dtype},
    )


class TestLosslessDifferencesLand:
    def test_int_batch_into_a_float_key(self, cache: TimeseriesCache):
        """The everyday case: a window whose values are all whole numbers."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        cache.write(typed([2], [102], pl.Int64), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [101.5, 102.0]

    def test_float_batch_into_an_int_key_when_the_values_are_whole(
        self, cache: TimeseriesCache
    ):
        cache.write(typed([1], [101], pl.Int64), **SERIES)
        cache.write(typed([2], [102.0], pl.Float64), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Int64
        assert result.frame["price"].to_list() == [101, 102]

    def test_numeric_strings_parse_into_a_float_key(self, cache: TimeseriesCache):
        """A vendor that quotes its numbers. Formatting is not data: '102.50'
        and 102.5 are the same value, so the difference in spelling is not a
        reason to refuse the write."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        cache.write(typed([2], ["102.50"], pl.String), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [101.5, 102.5]

    def test_a_partially_null_column_lands_when_its_values_convert(
        self, cache: TimeseriesCache
    ):
        """The reported case. Nulls beside real values are not the problem —
        unconvertible values are, and there are none here."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        cache.write(typed([2, 3], [None, "102.5"], pl.String), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [101.5, None, 102.5]

    def test_numbers_format_into_a_string_key(self, cache: TimeseriesCache):
        cache.write(typed([1], ["a"], pl.String), **SERIES)
        cache.write(typed([2], [1.5], pl.Float64), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame["price"].to_list() == ["a", "1.5"]

    def test_a_coarser_stored_time_unit_when_nothing_is_truncated(self, backend):
        cache = TimeseriesCache(backend)
        stored = pl.Datetime("ms", "UTC")
        cache.write(typed([1], [ts(1)], stored), **SERIES)
        cache.write(typed([2], [ts(2)], TS), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == stored
        assert result.frame["price"].to_list() == [ts(1), ts(2)]

    def test_conforming_applies_to_identity_columns_too(self, backend):
        """A key's notion of a trade id has one type, whatever a batch infers."""
        cache = TimeseriesCache(backend, identity_columns=("trade_id",))

        def book(day: int, trade_id: object, dtype: pl.DataType) -> pl.DataFrame:
            return pl.DataFrame(
                {
                    "ts": [ts(day)],
                    "trade_id": pl.Series("trade_id", [trade_id], dtype=dtype),
                    "price": [1.0],
                },
                schema={"ts": TS, "trade_id": dtype, "price": pl.Float64},
            )

        cache.write(book(1, "7", pl.String), **SERIES)
        cache.write(book(2, 8, pl.Int64), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["trade_id"] == pl.String
        assert result.frame["trade_id"].to_list() == ["7", "8"]


class TestLossyDifferencesStillRaise:
    """The three gates. Each of these conversions is one polars performs
    without complaint, which is exactly why conforming cannot simply cast."""

    def test_unparseable_text_is_refused(self, cache: TimeseriesCache):
        """Gate 1. A lenient cast would make 'cheap' null — the silent data
        loss this whole design exists to avoid."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="do not convert"):
            cache.write(typed([2], ["cheap"], pl.String), **SERIES)

    def test_a_fractional_value_is_not_truncated_into_an_int_key(
        self, cache: TimeseriesCache
    ):
        """Gate 3. `pl.Series([1.5]).cast(pl.Int64)` returns 1 and raises
        nothing, so only the round trip catches this."""
        cache.write(typed([1], [101], pl.Int64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="do not survive"):
            cache.write(typed([2], [1.5], pl.Float64), **SERIES)

    def test_an_integer_past_the_float_mantissa_is_refused(
        self, cache: TimeseriesCache
    ):
        """Gate 3, the subtle one: 2**53 + 1 has no Float64 representation and
        comes back as 2**53."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="do not survive"):
            cache.write(typed([2], [2**53 + 1], pl.Int64), **SERIES)

    def test_a_non_boolean_integer_is_refused(self, cache: TimeseriesCache):
        """Gate 3. Polars maps every nonzero integer to True."""
        cache.write(typed([1], [True], pl.Boolean), **SERIES)
        with pytest.raises(SchemaMismatchError, match="do not survive"):
            cache.write(typed([2], [5], pl.Int64), **SERIES)

    def test_zero_and_one_are_still_accepted_as_boolean(self, cache: TimeseriesCache):
        cache.write(typed([1], [True], pl.Boolean), **SERIES)
        cache.write(typed([2], [0], pl.Int64), **SERIES)

        assert cache.read(**SERIES).frame["price"].to_list() == [True, False]

    def test_sub_millisecond_precision_is_not_dropped(self, backend):
        """Gate 3 across time units, where truncation is silent and permanent."""
        cache = TimeseriesCache(backend)
        cache.write(typed([1], [ts(1)], pl.Datetime("ms", "UTC")), **SERIES)
        precise = datetime(2024, 1, 3, 0, 0, 0, 123456, tzinfo=ts(1).tzinfo)
        with pytest.raises(SchemaMismatchError, match="do not survive"):
            cache.write(typed([2], [precise], TS), **SERIES)

    def test_a_column_that_cannot_convert_at_all_is_refused(
        self, cache: TimeseriesCache
    ):
        """Polars has no String -> Boolean cast; the error must still be the
        cache's, not a raw polars traceback."""
        cache.write(typed([1], [True], pl.Boolean), **SERIES)
        with pytest.raises(SchemaMismatchError, match="do not convert"):
            cache.write(typed([2], ["yes"], pl.String), **SERIES)

    def test_added_and_dropped_columns_are_still_refused(self, cache: TimeseriesCache):
        """Conforming settles dtypes, never the set of columns — a column with
        nowhere to go is a migration, not an inference artifact."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        wider = typed([2], [102.5], pl.Float64).with_columns(
            pl.lit(1.0).alias("volume")
        )
        with pytest.raises(SchemaMismatchError, match="unexpected columns"):
            cache.write(wider, **SERIES)
        with pytest.raises(SchemaMismatchError, match="missing columns"):
            cache.write(
                typed([3], [103.5], pl.Float64).drop("price"),
                start=ts(3),
                end=ts(3),
                **SERIES,
            )


class TestOptingOut:
    def test_strict_demands_an_exact_match(self, backend):
        cache = TimeseriesCache(backend, schema_policy="strict")
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="stored as"):
            cache.write(typed([2], [102], pl.Int64), **SERIES)

    def test_all_null_settling_still_applies_under_strict(self, backend):
        """The two rules are independent. An all-null column carries no type at
        all, so its reconciliation is not a conversion and is not opted out of."""
        cache = TimeseriesCache(backend, schema_policy="strict")
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        cache.write(typed([2], [None], pl.String), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [101.5, None]


class TestThroughThePandasFacade:
    def test_an_int_column_lands_in_a_float_key(self, backend):
        import pandas as pd

        from timeseries_cache.pandas import PandasTimeseriesCache

        cache = PandasTimeseriesCache(backend)

        def frame(day: int, value: object, dtype: str) -> pd.DataFrame:
            # Note the astype rather than a pre-built Series: passing one with
            # its own RangeIndex would align against the DatetimeIndex and
            # quietly produce NaN.
            return pd.DataFrame(
                {"price": [value]},
                index=pd.DatetimeIndex([ts(day)], tz="UTC", name="ts"),
            ).astype({"price": dtype})

        cache.write(frame(1, 101.5, "float64"), **SERIES)
        cache.write(frame(2, 102, "int64"), **SERIES)

        out = cache.read(**SERIES).frame
        assert out["price"].dtype == "float64"
        assert out["price"].tolist() == [101.5, 102.0]

    def test_a_lossy_batch_raises_the_cache_s_own_error(self, backend):
        """Polars must not leak through the facade, in values or in exceptions."""
        import pandas as pd

        from timeseries_cache.pandas import PandasTimeseriesCache

        cache = PandasTimeseriesCache(backend)
        whole = pd.DataFrame(
            {"price": [101]}, index=pd.DatetimeIndex([ts(1)], tz="UTC", name="ts")
        ).astype({"price": "int64"})
        fractional = pd.DataFrame(
            {"price": [1.5]}, index=pd.DatetimeIndex([ts(2)], tz="UTC", name="ts")
        ).astype({"price": "float64"})
        cache.write(whole, **SERIES)
        with pytest.raises(SchemaMismatchError) as caught:
            cache.write(fractional, **SERIES)
        assert "polars" not in str(caught.value).lower()


def test_a_matching_frame_is_left_exactly_as_it_is(cache: TimeseriesCache):
    """The overwhelmingly common path: dtypes already agree, nothing is cast."""
    first = typed([1], [101.5], pl.Float64)
    cache.write(first, **SERIES)
    cache.write(typed([2], [102.5], pl.Float64), **SERIES)

    result = cache.read(**SERIES)
    assert result.frame.schema == first.schema
    assert result.frame["price"].to_list() == [101.5, 102.5]


def test_conforming_does_not_change_the_covered_window(cache: TimeseriesCache):
    """Conforming settles dtypes. Coverage stays purely temporal, and a
    conformed write claims exactly the window it was given — no more."""
    cache.write(typed([1], [101.5], pl.Float64), start=ts(1), end=ts(2), **SERIES)
    window = cache.write(typed([2], [102], pl.Int64), start=ts(2), end=ts(2), **SERIES)

    assert (window.start, window.end) == (ts(2), ts(2))
    assert cache.read(start=ts(1), end=ts(2), **SERIES).is_complete
    assert cache.read(start=ts(1), end=ts(4), **SERIES).missing.intervals == (
        Interval(ts(2) + timedelta(microseconds=1), ts(4)),
    )
