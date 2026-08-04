"""Two escape hatches for a key whose stored dtype is the wrong one.

Conforming (``tests/test_schema_conform.py``) refuses anything it cannot prove
lossless, which is right by default and wrong when the caller already knows the
stored dtype is a mistake and wants the data in anyway.

- ``schema_policy="force"`` makes *this batch* fit the stored dtype, accepting
  the loss. It is never silent: whatever it costs comes back as a
  ``SchemaForcedWarning``.
- ``recast()`` fixes the *stored dtype itself*, once, so nothing has to be
  forced again.

The second is almost always the better answer, and the warning says so. These
tests hold the line that forcing stays loud and recasting stays deliberate.
"""

from __future__ import annotations

import polars as pl
import pytest

from timeseries_cache import (
    IndexContractError,
    InvalidIdentityError,
    SchemaForcedWarning,
    SchemaMismatchError,
    TimeseriesCache,
    UnknownKeyError,
)

from .conftest import ts

SERIES = {"ticker": "AAPL", "field": "close"}
TS = pl.Datetime("us", "UTC")


def typed(days: list[int], values: list[object], dtype: pl.DataType) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts": [ts(d) for d in days], "price": pl.Series("price", values, dtype=dtype)},
        schema={"ts": TS, "price": dtype},
    )


class TestForcingAWrite:
    def test_unconvertible_values_become_null_instead_of_raising(
        self, cache: TimeseriesCache
    ):
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.warns(SchemaForcedWarning, match="1 value.*became null"):
            cache.write(
                typed([2, 3], ["cheap", "102.5"], pl.String),
                schema_policy="force",
                **SERIES,
            )

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [101.5, None, 102.5]

    def test_a_truncating_conversion_goes_through(self, cache: TimeseriesCache):
        cache.write(typed([1], [101], pl.Int64), **SERIES)
        with pytest.warns(SchemaForcedWarning, match="1 value.*changed"):
            cache.write(typed([2], [1.5], pl.Float64), schema_policy="force", **SERIES)

        assert cache.read(**SERIES).frame["price"].to_list() == [101, 1]

    def test_the_warning_names_the_column_and_both_dtypes(self, cache: TimeseriesCache):
        """A warning that doesn't say what it cost is barely better than
        silence, which is the thing this policy must not be."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.warns(SchemaForcedWarning) as caught:
            cache.write(
                typed([2], ["cheap"], pl.String), schema_policy="force", **SERIES
            )

        message = str(caught[0].message)
        assert "'price'" in message
        assert "String" in message and "Float64" in message
        assert "recast()" in message, "the warning should point at the real fix"

    def test_a_lossless_forced_write_warns_about_nothing(self, cache: TimeseriesCache):
        """Forcing is not an admission that loss happened — only that it would
        be accepted. Warning anyway would train callers to ignore it."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with warnings_as_errors():
            cache.write(typed([2], [102], pl.Int64), schema_policy="force", **SERIES)

        assert cache.read(**SERIES).frame["price"].to_list() == [101.5, 102.0]

    def test_the_cache_default_can_be_force(self, backend):
        cache = TimeseriesCache(backend, schema_policy="force")
        cache.write(typed([1], [101], pl.Int64), **SERIES)
        with pytest.warns(SchemaForcedWarning):
            cache.write(typed([2], [1.5], pl.Float64), **SERIES)

        assert cache.read(**SERIES).frame["price"].to_list() == [101, 1]

    def test_a_per_write_policy_overrides_the_cache_default(self, backend):
        cache = TimeseriesCache(backend, schema_policy="force")
        cache.write(typed([1], [101], pl.Int64), **SERIES)
        with pytest.raises(SchemaMismatchError):
            cache.write(
                typed([2], [1.5], pl.Float64), schema_policy="lossless", **SERIES
            )

    def test_forcing_does_not_retype_the_key(self, cache: TimeseriesCache):
        """Forcing bends the batch to the key, never the key to the batch —
        otherwise one bad write would redefine the column for good."""
        cache.write(typed([1], [101], pl.Int64), **SERIES)
        with pytest.warns(SchemaForcedWarning):
            cache.write(typed([2], [1.5], pl.Float64), schema_policy="force", **SERIES)

        manifest = cache.manifest(**SERIES)
        assert manifest is not None
        assert manifest.schema["price"] == str(pl.Int64)

    def test_added_columns_are_still_refused_under_force(self, cache: TimeseriesCache):
        """Force is about dtypes. A column with nowhere to go is not a dtype
        problem and forcing must not quietly invent a home for it."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        wider = typed([2], [102.5], pl.Float64).with_columns(
            pl.lit(1.0).alias("volume")
        )
        with pytest.raises(SchemaMismatchError, match="unexpected columns"):
            cache.write(wider, schema_policy="force", **SERIES)


class TestRecast:
    def test_retypes_the_stored_column(self, cache: TimeseriesCache):
        """The fix for a key whose first write typed a column wrongly."""
        cache.write(typed([1, 2], [101, 102], pl.Int64), **SERIES)
        report = cache.recast({"price": pl.Float64}, **SERIES)

        assert report.retyped == {"price": ("Int64", "Float64")}
        assert not report.lost_anything

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [101.0, 102.0]

    def test_later_writes_then_conform_to_the_new_type(self, cache: TimeseriesCache):
        """The point of recasting rather than forcing: it fixes the key once,
        and the writes that were failing simply start working."""
        cache.write(typed([1], [101], pl.Int64), **SERIES)
        with pytest.raises(SchemaMismatchError):
            cache.write(typed([2], [102.5], pl.Float64), **SERIES)

        cache.recast({"price": pl.Float64}, **SERIES)
        cache.write(typed([2], [102.5], pl.Float64), **SERIES)

        assert cache.read(**SERIES).frame["price"].to_list() == [101.0, 102.5]

    def test_coverage_survives_a_recast(self, cache: TimeseriesCache):
        """A dtype says nothing about which ranges have been fetched. Losing
        coverage here would put a hole in the one thing the cache guarantees."""
        cache.write(typed([1], [101], pl.Int64), start=ts(1), end=ts(9), **SERIES)
        before = cache.coverage(**SERIES)

        cache.recast({"price": pl.Float64}, **SERIES)

        assert cache.coverage(**SERIES) == before
        assert cache.read(start=ts(1), end=ts(9), **SERIES).is_complete

    def test_a_lossy_recast_is_refused_by_default(self, cache: TimeseriesCache):
        cache.write(typed([1, 2], [1.5, 2.5], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="would change 2 value"):
            cache.recast({"price": pl.Int64}, **SERIES)

        assert cache.read(**SERIES).frame.schema["price"] == pl.Float64, (
            "a refused recast must leave the key exactly as it was"
        )

    def test_force_accepts_the_loss_and_reports_it(self, cache: TimeseriesCache):
        cache.write(typed([1, 2], [1.5, 2.0], pl.Float64), **SERIES)
        report = cache.recast({"price": pl.Int64}, force=True, **SERIES)

        assert report.altered == {"price": 1}, "only 1.5 changed; 2.0 did not"
        assert report.lost_anything
        assert cache.read(**SERIES).frame["price"].to_list() == [1, 2]

    def test_force_counts_values_it_nulls(self, cache: TimeseriesCache):
        cache.write(typed([1, 2], ["cheap", "2.5"], pl.String), **SERIES)
        report = cache.recast({"price": pl.Float64}, force=True, **SERIES)

        assert report.nulled == {"price": 1}
        assert cache.read(**SERIES).frame["price"].to_list() == [None, 2.5]

    def test_adds_a_column_filled_with_null(self, cache: TimeseriesCache):
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        report = cache.recast(add={"volume": pl.Int64}, **SERIES)

        assert report.added == {"volume": "Int64"}
        result = cache.read(**SERIES)
        assert result.frame.schema["volume"] == pl.Int64
        assert result.frame["volume"].to_list() == [None]

    def test_an_added_column_makes_the_wider_write_land(self, cache: TimeseriesCache):
        """The migration path for a source that grew a column."""
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        wider = typed([2], [102.5], pl.Float64).with_columns(
            pl.lit(5, dtype=pl.Int64).alias("volume")
        )
        with pytest.raises(SchemaMismatchError, match="unexpected columns"):
            cache.write(wider, **SERIES)

        cache.recast(add={"volume": pl.Int64}, **SERIES)
        cache.write(wider, **SERIES)

        assert cache.read(**SERIES).frame["volume"].to_list() == [None, 5]

    def test_drops_a_column(self, cache: TimeseriesCache):
        frame = typed([1], [101.5], pl.Float64).with_columns(pl.lit("x").alias("stale"))
        cache.write(frame, **SERIES)
        report = cache.recast(drop=["stale"], **SERIES)

        assert report.dropped == ("stale",)
        assert "stale" not in cache.read(**SERIES).frame.columns

    def test_retype_add_and_drop_compose(self, cache: TimeseriesCache):
        frame = typed([1], [101], pl.Int64).with_columns(pl.lit("x").alias("stale"))
        cache.write(frame, **SERIES)
        report = cache.recast(
            {"price": pl.Float64}, add={"volume": pl.Int64}, drop=["stale"], **SERIES
        )

        assert report.retyped and report.added and report.dropped
        result = cache.read(**SERIES)
        assert result.frame.columns == ["ts", "price", "volume"]
        assert result.frame.schema["price"] == pl.Float64


class TestRecastRefusals:
    def test_an_unknown_key_is_an_error_not_a_no_op(self, cache: TimeseriesCache):
        with pytest.raises(UnknownKeyError, match="no schema to recast"):
            cache.recast({"price": pl.Float64}, **SERIES)

    def test_a_column_that_is_not_stored_cannot_be_retyped(
        self, cache: TimeseriesCache
    ):
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="not stored"):
            cache.recast({"volume": pl.Int64}, **SERIES)

    def test_a_column_that_is_stored_cannot_be_added(self, cache: TimeseriesCache):
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="already stored"):
            cache.recast(add={"price": pl.Int64}, **SERIES)

    def test_the_timestamp_column_is_off_limits(self, cache: TimeseriesCache):
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.raises(IndexContractError, match="timestamp column"):
            cache.recast({"ts": pl.Datetime("ms", "UTC")}, **SERIES)
        with pytest.raises(IndexContractError, match="timestamp column"):
            cache.recast(drop=["ts"], **SERIES)

    def test_an_identity_column_cannot_be_dropped(self, backend):
        cache = TimeseriesCache(backend, identity_columns=("trade_id",))
        book = pl.DataFrame(
            {"ts": [ts(1)], "trade_id": ["T1"], "price": [1.0]},
            schema={"ts": TS, "trade_id": pl.String, "price": pl.Float64},
        )
        cache.write(book, **SERIES)
        with pytest.raises(InvalidIdentityError, match="what a row is"):
            cache.recast(drop=["trade_id"], **SERIES)

    def test_an_empty_recast_is_refused(self, cache: TimeseriesCache):
        cache.write(typed([1], [101.5], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="at least one"):
            cache.recast(**SERIES)


class TestRecastAndRowIdentity:
    def test_retyping_an_identity_column_restores_the_stored_order(self, backend):
        """Sort order is by row key, so retyping a component can reorder rows:
        "10" sorts before "9" as text and after it as a number. Leaving the
        stored frame out of order would break the merge path's assumptions."""
        cache = TimeseriesCache(backend, identity_columns=("trade_id",))
        book = pl.DataFrame(
            {
                "ts": [ts(1), ts(1)],
                "trade_id": ["10", "9"],
                "price": [1.0, 2.0],
            },
            schema={"ts": TS, "trade_id": pl.String, "price": pl.Float64},
        )
        cache.write(book, **SERIES)
        cache.recast({"trade_id": pl.Int64}, **SERIES)

        result = cache.read(**SERIES)
        assert result.frame["trade_id"].to_list() == [9, 10]
        assert result.frame["price"].to_list() == [2.0, 1.0]

    def test_a_forced_recast_that_collides_two_ids_is_caught(self, backend):
        """Truncating 1.0 and 1.7 to Int64 makes both 1, so two rows that were
        distinct now claim the same identity. Storing that would silently break
        every later upsert against the key."""
        cache = TimeseriesCache(backend, identity_columns=("trade_id",))
        book = pl.DataFrame(
            {"ts": [ts(1), ts(1)], "trade_id": [1.0, 1.7], "price": [1.0, 2.0]},
            schema={"ts": TS, "trade_id": pl.Float64, "price": pl.Float64},
        )
        cache.write(book, **SERIES)
        with pytest.raises(IndexContractError, match="repeat the identity"):
            cache.recast({"trade_id": pl.Int64}, force=True, **SERIES)

    def test_a_forced_recast_that_nulls_an_identity_is_caught(self, backend):
        """A null cannot identify a row, so this has to fail rather than store
        an unaddressable one."""
        cache = TimeseriesCache(backend, identity_columns=("trade_id",))
        book = pl.DataFrame(
            {"ts": [ts(1)], "trade_id": ["T1"], "price": [1.0]},
            schema={"ts": TS, "trade_id": pl.String, "price": pl.Float64},
        )
        cache.write(book, **SERIES)
        with pytest.raises(InvalidIdentityError, match="contains nulls"):
            cache.recast({"trade_id": pl.Int64}, force=True, **SERIES)


class TestThroughThePandasFacade:
    def test_recast_takes_pandas_dtypes(self, backend):
        import pandas as pd

        from timeseries_cache.pandas import PandasTimeseriesCache

        cache = PandasTimeseriesCache(backend)
        cache.write(
            pd.DataFrame(
                {"price": [101]}, index=pd.DatetimeIndex([ts(1)], tz="UTC", name="ts")
            ).astype({"price": "int64"}),
            **SERIES,
        )
        report = cache.recast({"price": "float64"}, **SERIES)

        assert report.retyped == {"price": ("Int64", "Float64")}
        out = cache.read(**SERIES).frame
        assert out["price"].dtype == "float64"

    def test_a_bad_dtype_name_is_not_a_polars_error(self, backend):
        import pandas as pd

        from timeseries_cache.pandas import PandasTimeseriesCache

        cache = PandasTimeseriesCache(backend)
        cache.write(
            pd.DataFrame(
                {"price": [101.0]}, index=pd.DatetimeIndex([ts(1)], tz="UTC", name="ts")
            ),
            **SERIES,
        )
        with pytest.raises(ValueError, match="not a pandas dtype"):
            cache.recast({"price": "flooat64"}, **SERIES)

    def test_forcing_through_the_facade_warns_without_leaking_polars(self, backend):
        import pandas as pd

        from timeseries_cache.pandas import PandasTimeseriesCache

        cache = PandasTimeseriesCache(backend)

        def frame(day: int, value: object, dtype: str) -> pd.DataFrame:
            return pd.DataFrame(
                {"price": [value]},
                index=pd.DatetimeIndex([ts(day)], tz="UTC", name="ts"),
            ).astype({"price": dtype})

        cache.write(frame(1, 101, "int64"), **SERIES)
        with pytest.warns(SchemaForcedWarning):
            cache.write(frame(2, 1.5, "float64"), schema_policy="force", **SERIES)

        assert cache.read(**SERIES).frame["price"].tolist() == [101, 1]


class TestTheDeprecatedSpelling:
    def test_conform_schema_still_works_but_warns(self, backend):
        with pytest.deprecated_call(match="schema_policy"):
            cache = TimeseriesCache(backend, conform_schema=False)
        assert cache.schema_policy == "strict"

    def test_conform_schema_and_schema_policy_together_are_refused(self, backend):
        with pytest.raises(ValueError, match="not both"):
            TimeseriesCache(backend, schema_policy="force", conform_schema=True)


def warnings_as_errors():
    """`pytest.warns(None)` was removed in pytest 8; this is the replacement
    idiom for 'assert this block warns about nothing'."""
    import warnings
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        with warnings.catch_warnings():
            warnings.simplefilter("error", SchemaForcedWarning)
            yield

    return _ctx()
