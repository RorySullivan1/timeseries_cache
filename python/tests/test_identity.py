"""Rows identified by ``(timestamp, *identity_columns)``.

The motivating case is trade data: many prints share a timestamp, and the trade
id is what tells them apart. That makes the composite the unit of uniqueness
*and* the unit an ``upsert`` overwrites — replacing one trade must not disturb
its neighbours at the same instant.

Coverage stays purely time-based throughout; only row identity changes.
"""

from __future__ import annotations

import polars as pl
import pytest

from timeseries_cache import (
    IndexContractError,
    Interval,
    InvalidIdentityError,
    SchemaMismatchError,
    TimeseriesCache,
    WriteMode,
)

from .conftest import ts

SERIES = {"ticker": "AAPL", "feed": "trades"}
SCHEMA = {
    "ts": pl.Datetime("us", "UTC"),
    "trade_id": pl.String,
    "price": pl.Float64,
}


def trades(rows: list[tuple[int, str, float]]) -> pl.DataFrame:
    """``(day, trade_id, price)`` triples — several may share a day."""
    return pl.DataFrame(
        {
            "ts": [ts(day) for day, _, _ in rows],
            "trade_id": [tid for _, tid, _ in rows],
            "price": [price for _, _, price in rows],
        },
        schema=SCHEMA,
    )


@pytest.fixture
def cache(backend) -> TimeseriesCache:
    return TimeseriesCache(backend, identity_columns=("trade_id",))


def ids(result) -> list[str]:
    return result.frame["trade_id"].to_list()


class TestConfiguration:
    def test_row_key_is_timestamp_plus_identity(self, cache: TimeseriesCache):
        assert cache.row_key == ("ts", "trade_id")

    def test_default_row_key_is_the_timestamp_alone(self, backend):
        assert TimeseriesCache(backend).row_key == ("ts",)

    def test_rejects_the_timestamp_as_an_identity_column(self, backend):
        with pytest.raises(InvalidIdentityError, match="always part"):
            TimeseriesCache(backend, identity_columns=("ts",))

    def test_rejects_repeated_identity_columns(self, backend):
        with pytest.raises(InvalidIdentityError, match="repeats"):
            TimeseriesCache(backend, identity_columns=("a", "a"))

    def test_supports_a_composite_identity(self, backend):
        cache = TimeseriesCache(backend, identity_columns=("venue", "trade_id"))
        assert cache.row_key == ("ts", "venue", "trade_id")


class TestDuplicateTimestamps:
    def test_rows_may_share_a_timestamp(self, cache: TimeseriesCache):
        cache.write(trades([(1, "a", 10.0), (1, "b", 11.0), (2, "c", 12.0)]), **SERIES)
        result = cache.read(**SERIES)
        assert result.frame.height == 3
        assert result.frame["ts"].to_list() == [ts(1), ts(1), ts(2)]

    def test_the_same_timestamp_still_needs_distinct_ids(self, cache: TimeseriesCache):
        with pytest.raises(IndexContractError, match="repeat the identity"):
            cache.write(trades([(1, "a", 10.0), (1, "a", 11.0)]), **SERIES)

    def test_without_identity_columns_duplicates_are_still_rejected(self, backend):
        """The default is unchanged — and the message points at the way out."""
        plain = TimeseriesCache(backend)
        frame = pl.DataFrame(
            {"ts": [ts(1), ts(1)], "price": [1.0, 2.0]},
            schema={"ts": pl.Datetime("us", "UTC"), "price": pl.Float64},
        )
        with pytest.raises(IndexContractError, match="identity_columns"):
            plain.write(frame, **SERIES)

    def test_identity_columns_must_be_present(self, cache: TimeseriesCache):
        frame = pl.DataFrame(
            {"ts": [ts(1)], "price": [1.0]},
            schema={"ts": pl.Datetime("us", "UTC"), "price": pl.Float64},
        )
        with pytest.raises(InvalidIdentityError, match="not in the frame"):
            cache.write(frame, **SERIES)

    def test_identity_columns_may_not_be_null(self, cache: TimeseriesCache):
        frame = pl.DataFrame(
            {"ts": [ts(1)], "trade_id": [None], "price": [1.0]}, schema=SCHEMA
        )
        with pytest.raises(InvalidIdentityError, match="cannot"):
            cache.write(frame, **SERIES)

    def test_ordering_among_shared_timestamps_is_deterministic(
        self, cache: TimeseriesCache
    ):
        cache.write(trades([(1, "c", 3.0), (1, "a", 1.0), (1, "b", 2.0)]), **SERIES)
        assert ids(cache.read(**SERIES)) == ["a", "b", "c"]


class TestUpsertMatchesOnTheComposite:
    def test_replaces_only_the_matching_row(self, cache: TimeseriesCache):
        """The whole point.

        Keying on the timestamp alone, correcting trade 'b' would wipe 'a' and
        'c' — they share its instant but are different trades.
        """
        cache.write(trades([(1, "a", 10.0), (1, "b", 11.0), (1, "c", 12.0)]), **SERIES)
        cache.write(trades([(1, "b", 99.0)]), start=ts(1), end=ts(1), **SERIES)

        result = cache.read(**SERIES)
        assert ids(result) == ["a", "b", "c"]
        assert result.frame["price"].to_list() == [10.0, 99.0, 12.0]

    def test_adds_a_new_trade_at_an_existing_timestamp(self, cache: TimeseriesCache):
        cache.write(trades([(1, "a", 10.0)]), **SERIES)
        cache.write(trades([(1, "b", 11.0)]), start=ts(1), end=ts(1), **SERIES)
        assert ids(cache.read(**SERIES)) == ["a", "b"]

    def test_the_same_id_at_a_different_timestamp_is_a_different_row(
        self, cache: TimeseriesCache
    ):
        cache.write(trades([(1, "a", 10.0)]), **SERIES)
        cache.write(trades([(2, "a", 20.0)]), **SERIES)
        result = cache.read(**SERIES)
        assert result.frame.height == 2
        assert result.frame["ts"].to_list() == [ts(1), ts(2)]


class TestOtherModesAreUnaffected:
    def test_replace_window_still_clears_by_time(self, cache: TimeseriesCache):
        """Window semantics stay time-based: a corrected refetch of an interval
        replaces every trade in it, whatever the ids."""
        cache.write(
            trades([(1, "a", 1.0), (2, "b", 2.0), (2, "c", 3.0), (3, "d", 4.0)]),
            **SERIES,
        )
        cache.write(
            trades([(2, "z", 99.0)]),
            start=ts(2),
            end=ts(2),
            mode=WriteMode.REPLACE_WINDOW,
            **SERIES,
        )
        assert ids(cache.read(**SERIES)) == ["a", "z", "d"]

    def test_append_only_still_rejects_an_overlapping_window(
        self, cache: TimeseriesCache
    ):
        from timeseries_cache import OverlappingWriteError

        cache.write(trades([(1, "a", 1.0)]), mode=WriteMode.APPEND_ONLY, **SERIES)
        with pytest.raises(OverlappingWriteError):
            cache.write(trades([(1, "b", 2.0)]), mode=WriteMode.APPEND_ONLY, **SERIES)

    def test_delete_removes_every_row_in_the_window(self, cache: TimeseriesCache):
        cache.write(trades([(1, "a", 1.0), (2, "b", 2.0), (2, "c", 3.0)]), **SERIES)
        cache.delete(start=ts(2), end=ts(2), **SERIES)
        assert ids(cache.read(start=ts(1), end=ts(3), **SERIES)) == ["a"]

    def test_coverage_is_still_purely_time_based(self, cache: TimeseriesCache):
        cache.write(trades([(1, "a", 1.0), (1, "b", 2.0)]), **SERIES)
        assert cache.coverage(**SERIES).intervals == (Interval(ts(1), ts(1)),)

    def test_an_empty_covered_range_still_reads_as_complete(
        self, cache: TimeseriesCache
    ):
        cache.write(pl.DataFrame(), start=ts(1), end=ts(5), **SERIES)
        result = cache.read(start=ts(1), end=ts(5), **SERIES)
        assert result.frame.height == 0
        assert result.is_complete


class TestManifestRemembersIdentity:
    def test_manifest_records_it(self, cache: TimeseriesCache):
        cache.write(trades([(1, "a", 1.0)]), **SERIES)
        manifest = cache.manifest(**SERIES)
        assert manifest is not None
        assert manifest.identity_columns == ("trade_id",)

    def test_reading_with_the_wrong_identity_raises(self, backend):
        """Otherwise the two caches disagree about what "the same row" means,
        and an upsert under the wrong one silently destroys rows."""
        TimeseriesCache(backend, identity_columns=("trade_id",)).write(
            trades([(1, "a", 1.0), (1, "b", 2.0)]), **SERIES
        )
        plain = TimeseriesCache(backend)
        with pytest.raises(InvalidIdentityError, match="was written with"):
            plain.read(**SERIES)

    def test_writing_with_the_wrong_identity_raises(self, backend):
        TimeseriesCache(backend).write(
            pl.DataFrame(
                {"ts": [ts(1)], "price": [1.0]},
                schema={"ts": pl.Datetime("us", "UTC"), "price": pl.Float64},
            ),
            **SERIES,
        )
        keyed = TimeseriesCache(backend, identity_columns=("trade_id",))
        with pytest.raises(InvalidIdentityError, match="was written with"):
            keyed.write(trades([(2, "a", 1.0)]), **SERIES)

    def test_old_manifests_without_the_field_default_to_timestamp_only(self):
        """Back-compat: manifests predating identity columns behaved exactly as
        an empty identity does, so a missing field must read that way."""
        import json

        from timeseries_cache.index import Manifest
        from timeseries_cache.keys import CacheKey

        written = Manifest.new(
            CacheKey.build(SERIES),
            timestamp_column="ts",
            identity_columns=("trade_id",),
        )
        payload = json.loads(written.to_json())
        del payload["identity_columns"]
        assert Manifest.from_json(json.dumps(payload)).identity_columns == ()


class TestProjection:
    def test_identity_columns_always_come_back(self, cache: TimeseriesCache):
        """A projection that dropped them would hand back rows the caller
        cannot tell apart."""
        cache.write(trades([(1, "a", 1.0), (1, "b", 2.0)]), **SERIES)
        result = cache.read(columns=["price"], **SERIES)
        assert result.frame.columns == ["ts", "trade_id", "price"]

    def test_asking_for_them_does_not_duplicate_them(self, cache: TimeseriesCache):
        cache.write(trades([(1, "a", 1.0)]), **SERIES)
        result = cache.read(columns=["trade_id", "price"], **SERIES)
        assert result.frame.columns == ["ts", "trade_id", "price"]

    def test_unknown_column_still_raises(self, cache: TimeseriesCache):
        cache.write(trades([(1, "a", 1.0)]), **SERIES)
        with pytest.raises(SchemaMismatchError, match="unknown column"):
            cache.read(columns=["nope"], **SERIES)
