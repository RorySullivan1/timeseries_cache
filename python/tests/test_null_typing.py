"""A column of nothing but nulls carries no type information.

Something still has to name a dtype for such a column, and what gets named is an
accident of the boundary — pandas turns ``[None, None]`` into ``object``, which
becomes polars ``String``. Treating that as the column's type either fixes a
key's schema as ``String`` on the first write and rejects every real write after
it, or arrives later and gets rejected as a schema change.

Neither is a schema change. Both are inference artifacts, and the cache settles
them against what it already knows rather than letting a null batch vote.

The safety argument is that casting an all-null column is lossless in every
direction, so nothing can be invented or destroyed. A column *with* values is
never touched — that is the line between this and silently coercing data.
"""

from __future__ import annotations

import polars as pl
import pytest

from timeseries_cache import SchemaMismatchError, TimeseriesCache

from .conftest import ts

SERIES = {"ticker": "AAPL", "field": "close"}
TS = pl.Datetime("us", "UTC")


def typed(days: list[int], values: list[object], dtype: pl.DataType) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts": [ts(d) for d in days], "price": values},
        schema={"ts": TS, "price": dtype},
    )


class TestNullsDoNotVote:
    def test_an_all_null_batch_takes_the_stored_type(self, cache: TimeseriesCache):
        """The reported case: a window the vendor returned nulls for."""
        cache.write(typed([1, 2], [101.0, 102.0], pl.Float64), **SERIES)
        cache.write(typed([3, 4], [None, None], pl.String), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [101.0, 102.0, None, None]

    def test_a_first_all_null_write_leaves_the_type_open(self, cache: TimeseriesCache):
        """Stored as Null — honestly "not yet known" — rather than guessed.

        Guessing String here is what makes every later real write fail.
        """
        cache.write(typed([1, 2], [None, None], pl.String), **SERIES)

        manifest = cache.manifest(**SERIES)
        assert manifest is not None
        assert manifest.schema["price"] == str(pl.Null)

    def test_a_later_real_write_settles_an_open_type(self, cache: TimeseriesCache):
        cache.write(typed([1, 2], [None, None], pl.String), **SERIES)
        cache.write(typed([3, 4], [103.0, 104.0], pl.Float64), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [None, None, 103.0, 104.0]

    def test_the_settled_type_then_governs(self, cache: TimeseriesCache):
        cache.write(typed([1], [None], pl.String), **SERIES)
        cache.write(typed([2], [102.0], pl.Float64), **SERIES)
        cache.write(typed([3], [None], pl.String), **SERIES)  # null again

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Float64
        assert result.frame["price"].to_list() == [None, 102.0, None]

    @pytest.mark.parametrize(
        ("dtype", "value"),
        [
            (pl.Float64, 1.5),
            (pl.Int64, 7),
            (pl.String, "text"),
            (pl.Boolean, True),
            (pl.Datetime("us", "UTC"), ts(9)),
        ],
        ids=lambda p: str(p) if isinstance(p, pl.DataType) else "",
    )
    def test_it_holds_whatever_the_stored_type_is(self, backend, dtype, value):
        """Casting an all-null column is lossless for every dtype, which is the
        property the whole rule rests on."""
        cache = TimeseriesCache(backend)
        established = pl.DataFrame(
            {"ts": [ts(1)], "price": pl.Series("price", [value], dtype=dtype)},
            schema={"ts": TS, "price": dtype},
        )
        cache.write(established, **SERIES)

        # A later batch that came back empty, typed String by inference.
        cache.write(typed([2], [None], pl.String), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == dtype
        assert result.frame["price"].to_list() == [value, None]

    def test_an_all_null_column_never_widens_a_narrower_stored_type(
        self, cache: TimeseriesCache
    ):
        cache.write(typed([1], [7], pl.Int64), **SERIES)
        cache.write(typed([2], [None], pl.String), **SERIES)

        result = cache.read(**SERIES)
        assert result.frame.schema["price"] == pl.Int64, "Int64 must not become String"
        assert result.frame["price"].to_list() == [7, None]


class TestRealConflictsStillRaise:
    """The line: values are never coerced, only absent types are settled."""

    def test_a_genuine_type_change_is_refused(self, cache: TimeseriesCache):
        cache.write(typed([1, 2], [101.0, 102.0], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError, match="stored as Float64"):
            cache.write(typed([3, 4], ["cheap", "dear"], pl.String), **SERIES)

    def test_a_partially_null_column_is_not_coerced(self, cache: TimeseriesCache):
        """One real value is enough to make the column's type its own.

        This is the case that separates settling an unknown type from silently
        casting data — a lenient cast here would turn 'cheap' into null.
        """
        cache.write(typed([1, 2], [101.0, 102.0], pl.Float64), **SERIES)
        with pytest.raises(SchemaMismatchError):
            cache.write(typed([3, 4], [None, "cheap"], pl.String), **SERIES)

    def test_an_added_column_is_still_refused(self, cache: TimeseriesCache):
        cache.write(typed([1], [101.0], pl.Float64), **SERIES)
        wider = typed([2], [102.0], pl.Float64).with_columns(
            pl.lit(None, dtype=pl.String).alias("note")
        )
        with pytest.raises(SchemaMismatchError, match="unexpected columns"):
            cache.write(wider, **SERIES)


class TestThroughThePandasFacade:
    """Where the artifact actually originates: object dtype for [None, None]."""

    def test_pandas_none_column_takes_the_stored_type(self, backend):
        import pandas as pd

        from timeseries_cache.pandas import PandasTimeseriesCache

        cache = PandasTimeseriesCache(backend)

        def frame(days, values):
            return pd.DataFrame(
                {"price": values},
                index=pd.DatetimeIndex([ts(d) for d in days], tz="UTC", name="ts"),
            )

        cache.write(frame([1, 2], [101.0, 102.0]), **SERIES)
        nulls = frame([3, 4], [None, None])
        assert nulls["price"].dtype == object, "the artifact this test exists for"

        cache.write(nulls, **SERIES)

        out = cache.read(**SERIES).frame
        assert out["price"].dtype == "float64"
        assert out["price"].tolist()[:2] == [101.0, 102.0]
        assert out["price"].isna().tolist() == [False, False, True, True]

    def test_pandas_first_write_all_none_then_real_values(self, backend):
        import pandas as pd

        from timeseries_cache.pandas import PandasTimeseriesCache

        cache = PandasTimeseriesCache(backend)

        def frame(days, values):
            return pd.DataFrame(
                {"price": values},
                index=pd.DatetimeIndex([ts(d) for d in days], tz="UTC", name="ts"),
            )

        cache.write(frame([1, 2], [None, None]), **SERIES)
        cache.write(frame([3, 4], [103.0, 104.0]), **SERIES)

        out = cache.read(**SERIES).frame
        assert out["price"].dtype == "float64"
        assert out["price"].tolist()[2:] == [103.0, 104.0]
