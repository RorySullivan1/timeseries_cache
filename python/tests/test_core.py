from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from timeseries_cache import (
    CacheKeyCollisionError,
    IndexContractError,
    Interval,
    InvalidKwargError,
    MemoryBackend,
    OverlappingWriteError,
    SchemaMismatchError,
    TimeseriesCache,
    WindowError,
    WriteMode,
)
from timeseries_cache.intervals import RESOLUTION

from .conftest import frame, ts

SERIES = {"ticker": "AAPL", "field": "close"}


def prices(result) -> list[float]:
    return result.frame["price"].to_list()


def days(result) -> list[datetime]:
    return result.frame["ts"].to_list()


class TestIndexContract:
    def test_rejects_naive_timestamps(self, cache: TimeseriesCache):
        naive = pl.DataFrame(
            {"ts": [datetime(2024, 1, 1)], "price": [1.0]},
            schema={"ts": pl.Datetime("us"), "price": pl.Float64},
        )
        with pytest.raises(IndexContractError, match="timezone-naive"):
            cache.write(naive, **SERIES)

    def test_rejects_a_missing_timestamp_column(self, cache: TimeseriesCache):
        with pytest.raises(IndexContractError, match="no 'ts' column"):
            cache.write(pl.DataFrame({"price": [1.0]}), **SERIES)

    def test_rejects_a_non_datetime_timestamp_column(self, cache: TimeseriesCache):
        bad = pl.DataFrame({"ts": [1, 2], "price": [1.0, 2.0]})
        with pytest.raises(IndexContractError, match="must be a Datetime"):
            cache.write(bad, **SERIES)

    def test_rejects_duplicate_timestamps(self, cache: TimeseriesCache):
        dupes = pl.DataFrame(
            {"ts": [ts(1), ts(1)], "price": [1.0, 2.0]},
            schema={"ts": pl.Datetime("us", "UTC"), "price": pl.Float64},
        )
        with pytest.raises(IndexContractError, match="duplicate timestamp"):
            cache.write(dupes, **SERIES)

    def test_sorts_out_of_order_input(self, cache: TimeseriesCache):
        cache.write(frame([5, 1, 3]), **SERIES)
        result = cache.read(**SERIES)
        assert days(result) == [ts(1), ts(3), ts(5)]

    def test_rejects_null_timestamps(self, cache: TimeseriesCache):
        nulls = pl.DataFrame(
            {"ts": [ts(1), None], "price": [1.0, 2.0]},
            schema={"ts": pl.Datetime("us", "UTC"), "price": pl.Float64},
        )
        with pytest.raises(IndexContractError, match="null timestamps"):
            cache.write(nulls, **SERIES)

    def test_rejects_subsecond_precision_that_storage_would_truncate(
        self, cache: TimeseriesCache
    ):
        nanos = pl.DataFrame(
            {"ts": [datetime(2024, 1, 1, tzinfo=UTC)], "price": [1.0]},
            schema={"ts": pl.Datetime("ns", "UTC"), "price": pl.Float64},
        ).with_columns(pl.col("ts") + pl.duration(nanoseconds=1))
        with pytest.raises(IndexContractError, match="sub-microsecond"):
            cache.write(nanos, **SERIES)

    def test_accepts_nanosecond_input_that_is_exactly_microseconds(
        self, cache: TimeseriesCache
    ):
        nanos = pl.DataFrame(
            {"ts": [datetime(2024, 1, 1, tzinfo=UTC)], "price": [1.0]},
            schema={"ts": pl.Datetime("ns", "UTC"), "price": pl.Float64},
        )
        cache.write(nanos, **SERIES)
        assert cache.read(**SERIES).frame.schema["ts"] == pl.Datetime("us", "UTC")

    def test_converts_other_timezones_to_utc(self, cache: TimeseriesCache):
        tokyo = pl.DataFrame(
            {
                "ts": [datetime(2024, 1, 1, 9, tzinfo=ZoneInfo("Asia/Tokyo"))],
                "price": [1.0],
            },
        ).with_columns(pl.col("ts").cast(pl.Datetime("us", "Asia/Tokyo")))
        cache.write(tokyo, **SERIES)
        assert days(cache.read(**SERIES)) == [datetime(2024, 1, 1, 0, tzinfo=UTC)]

    def test_dst_boundary_timestamps_stay_distinct(self, cache: TimeseriesCache):
        """1:30am US/Eastern happens twice on the fall-back day.

        Both instants are distinct in UTC, so both must survive as separate rows
        rather than colliding into a "duplicate timestamp" rejection.
        """
        eastern = ZoneInfo("America/New_York")
        before = datetime(2024, 11, 3, 1, 30, tzinfo=eastern, fold=0)
        after = datetime(2024, 11, 3, 1, 30, tzinfo=eastern, fold=1)
        assert before.astimezone(UTC) != after.astimezone(UTC)

        dst = pl.DataFrame(
            {
                "ts": [before.astimezone(UTC), after.astimezone(UTC)],
                "price": [1.0, 2.0],
            },
            schema={"ts": pl.Datetime("us", "UTC"), "price": pl.Float64},
        )
        cache.write(dst, **SERIES)
        assert cache.read(**SERIES).frame.height == 2


class TestCoverageVersusEmptiness:
    """Invariant 3 — the distinction the whole cache exists for."""

    def test_unknown_range_reports_itself_as_missing(self, cache: TimeseriesCache):
        result = cache.read(start=ts(1), end=ts(5), **SERIES)
        assert result.frame.height == 0
        assert not result.is_complete
        assert result.missing.intervals == (Interval(ts(1), ts(5)),)

    def test_fetched_but_genuinely_empty_range_is_complete(
        self, cache: TimeseriesCache
    ):
        cache.write(pl.DataFrame(), start=ts(1), end=ts(5), **SERIES)
        result = cache.read(start=ts(1), end=ts(5), **SERIES)
        assert result.frame.height == 0
        assert result.is_complete
        assert not result.missing

    def test_empty_write_needs_an_explicit_window(self, cache: TimeseriesCache):
        with pytest.raises(WindowError, match="must declare the window"):
            cache.write(pl.DataFrame(), **SERIES)

    def test_holiday_gap_inside_covered_range_is_not_missing(
        self, cache: TimeseriesCache
    ):
        # Days 2 and 3 have no rows, but the range was fetched.
        cache.write(frame([1, 4]), start=ts(1), end=ts(4), **SERIES)
        result = cache.read(start=ts(1), end=ts(4), **SERIES)
        assert prices(result) == [101.0, 104.0]
        assert result.is_complete

    def test_partial_coverage_reports_only_the_real_gaps(self, cache: TimeseriesCache):
        cache.write(frame([1, 2]), start=ts(1), end=ts(2), **SERIES)
        cache.write(frame([9, 10]), start=ts(9), end=ts(10), **SERIES)
        result = cache.read(start=ts(1), end=ts(10), **SERIES)
        assert result.missing.intervals == (
            Interval(ts(2) + RESOLUTION, ts(9) - RESOLUTION),
        )

    def test_the_fetch_loop_converges(self, cache: TimeseriesCache):
        """Read, fill the reported gaps, read again — nothing left missing."""
        cache.write(frame([1]), start=ts(1), end=ts(1), **SERIES)
        cache.write(frame([10]), start=ts(10), end=ts(10), **SERIES)

        first = cache.read(start=ts(1), end=ts(10), **SERIES)
        for gap in first.missing:
            cache.write(pl.DataFrame(), start=gap.start, end=gap.end, **SERIES)

        assert cache.read(start=ts(1), end=ts(10), **SERIES).is_complete


class TestUpsert:
    def test_overlapping_rows_are_replaced_not_duplicated(self, cache: TimeseriesCache):
        cache.write(frame([1, 2, 3]), **SERIES)
        cache.write(frame([2, 3], price=900.0), **SERIES)
        result = cache.read(**SERIES)
        assert days(result) == [ts(1), ts(2), ts(3)]
        assert prices(result) == [101.0, 902.0, 903.0]

    def test_non_overlapping_rows_survive(self, cache: TimeseriesCache):
        cache.write(frame([1, 2]), **SERIES)
        cache.write(frame([5, 6]), **SERIES)
        assert days(cache.read(**SERIES)) == [ts(1), ts(2), ts(5), ts(6)]

    def test_coverage_accumulates(self, cache: TimeseriesCache):
        cache.write(frame([1, 4]), **SERIES)
        cache.write(frame([4, 8]), **SERIES)
        assert cache.coverage(**SERIES).intervals == (Interval(ts(1), ts(8)),)

    def test_derives_its_window_from_the_data(self, cache: TimeseriesCache):
        window = cache.write(frame([2, 7]), **SERIES)
        assert window == Interval(ts(2), ts(7))

    def test_derived_windows_leave_a_gap_between_separate_writes(
        self, cache: TimeseriesCache
    ):
        """The trap that makes explicit windows worth the keystrokes.

        A derived window spans only [min, max] of the rows written. Two writes
        of adjacent daily bars therefore cover two points, not the span between
        them — and the cache correctly reports the middle as unknown.
        """
        cache.write(frame([1]), **SERIES)
        cache.write(frame([3]), **SERIES)
        assert len(cache.coverage(**SERIES)) == 2
        assert cache.read(start=ts(1), end=ts(3), **SERIES).missing

        # Declaring the window is how you say "I fetched the whole range".
        cache.write(frame([1, 3]), start=ts(1), end=ts(3), **SERIES)
        assert cache.read(start=ts(1), end=ts(3), **SERIES).is_complete


class TestReplaceWindow:
    def test_removes_stale_rows_the_new_data_no_longer_contains(
        self, cache: TimeseriesCache
    ):
        """The case inference gets wrong.

        Upstream corrected itself: day 3 should not exist. An upsert would leave
        it behind forever; replace_window deletes it because the window is
        declared, not derived from the incoming rows.
        """
        cache.write(frame([1, 2, 3, 4, 5]), **SERIES)
        cache.write(
            frame([2, 4], price=900.0),
            start=ts(2),
            end=ts(4),
            mode=WriteMode.REPLACE_WINDOW,
            **SERIES,
        )
        result = cache.read(**SERIES)
        assert days(result) == [ts(1), ts(2), ts(4), ts(5)]
        assert prices(result) == [101.0, 902.0, 904.0, 105.0]

    def test_leaves_rows_outside_the_window_untouched(self, cache: TimeseriesCache):
        cache.write(frame([1, 5, 9]), **SERIES)
        cache.write(
            frame([5], price=900.0),
            start=ts(4),
            end=ts(6),
            mode=WriteMode.REPLACE_WINDOW,
            **SERIES,
        )
        assert prices(cache.read(**SERIES)) == [101.0, 905.0, 109.0]

    def test_requires_an_explicit_window(self, cache: TimeseriesCache):
        with pytest.raises(WindowError, match="requires an explicit start and end"):
            cache.write(frame([1]), mode=WriteMode.REPLACE_WINDOW, **SERIES)

    def test_rejects_rows_outside_the_declared_window(self, cache: TimeseriesCache):
        with pytest.raises(WindowError, match="outside the declared window"):
            cache.write(
                frame([1, 9]),
                start=ts(1),
                end=ts(5),
                mode=WriteMode.REPLACE_WINDOW,
                **SERIES,
            )

    def test_empty_replace_clears_the_window_but_keeps_coverage(
        self, cache: TimeseriesCache
    ):
        cache.write(frame([1, 2, 3]), **SERIES)
        cache.write(
            pl.DataFrame(),
            start=ts(2),
            end=ts(3),
            mode=WriteMode.REPLACE_WINDOW,
            **SERIES,
        )
        result = cache.read(start=ts(1), end=ts(3), **SERIES)
        assert days(result) == [ts(1)]
        assert result.is_complete  # still fetched — just genuinely empty now

    def test_endpoints_are_inclusive(self, cache: TimeseriesCache):
        cache.write(frame([1, 2, 3]), **SERIES)
        cache.write(
            pl.DataFrame(),
            start=ts(1),
            end=ts(3),
            mode=WriteMode.REPLACE_WINDOW,
            **SERIES,
        )
        assert cache.read(**SERIES).frame.height == 0


class TestAppendOnly:
    def test_accepts_a_disjoint_window(self, cache: TimeseriesCache):
        cache.write(frame([1, 2]), mode=WriteMode.APPEND_ONLY, **SERIES)
        cache.write(frame([5, 6]), mode=WriteMode.APPEND_ONLY, **SERIES)
        assert days(cache.read(**SERIES)) == [ts(1), ts(2), ts(5), ts(6)]

    def test_rejects_an_overlapping_window(self, cache: TimeseriesCache):
        cache.write(frame([1, 5]), mode=WriteMode.APPEND_ONLY, **SERIES)
        with pytest.raises(OverlappingWriteError, match="overlaps existing coverage"):
            cache.write(frame([3, 8]), mode=WriteMode.APPEND_ONLY, **SERIES)

    def test_rejects_an_overlap_even_with_no_rows_in_common(
        self, cache: TimeseriesCache
    ):
        # Coverage overlaps although no timestamp is shared — an append-only
        # source producing this means something upstream is wrong.
        cache.write(frame([1, 10]), mode=WriteMode.APPEND_ONLY, **SERIES)
        with pytest.raises(OverlappingWriteError):
            cache.write(frame([4, 5]), mode=WriteMode.APPEND_ONLY, **SERIES)


class TestWindowValidation:
    def test_rejects_a_half_declared_window(self, cache: TimeseriesCache):
        with pytest.raises(WindowError, match="both start and end"):
            cache.write(frame([1]), start=ts(1), **SERIES)

    def test_rejects_rows_outside_a_declared_window(self, cache: TimeseriesCache):
        with pytest.raises(WindowError, match="outside the declared window"):
            cache.write(frame([1, 9]), start=ts(1), end=ts(5), **SERIES)

    def test_a_window_wider_than_the_data_is_allowed(self, cache: TimeseriesCache):
        """Claiming more coverage than you have rows for is the normal case."""
        cache.write(frame([3]), start=ts(1), end=ts(5), **SERIES)
        assert cache.coverage(**SERIES).intervals == (Interval(ts(1), ts(5)),)


class TestRead:
    def test_slices_inclusively(self, cache: TimeseriesCache):
        cache.write(frame([1, 2, 3, 4, 5]), **SERIES)
        result = cache.read(start=ts(2), end=ts(4), **SERIES)
        assert days(result) == [ts(2), ts(3), ts(4)]

    def test_unbounded_read_defaults_to_the_covered_hull(self, cache: TimeseriesCache):
        cache.write(frame([2, 8]), **SERIES)
        result = cache.read(**SERIES)
        assert result.requested == Interval(ts(2), ts(8))
        assert result.is_complete

    def test_open_ended_start_uses_the_hull(self, cache: TimeseriesCache):
        cache.write(frame([2, 8]), **SERIES)
        result = cache.read(end=ts(5), **SERIES)
        assert result.requested == Interval(ts(2), ts(5))

    def test_projects_requested_columns_and_always_keeps_the_timestamp(
        self, cache: TimeseriesCache
    ):
        wide = frame([1, 2]).with_columns(pl.lit(1.0).alias("volume"))
        cache.write(wide, **SERIES)
        result = cache.read(columns=["volume"], **SERIES)
        assert result.frame.columns == ["ts", "volume"]

    def test_asking_for_the_timestamp_column_does_not_duplicate_it(
        self, cache: TimeseriesCache
    ):
        cache.write(frame([1]), **SERIES)
        assert cache.read(columns=["ts", "price"], **SERIES).frame.columns == [
            "ts",
            "price",
        ]

    def test_unknown_column_raises_a_package_error(self, cache: TimeseriesCache):
        cache.write(frame([1]), **SERIES)
        with pytest.raises(SchemaMismatchError, match="unknown column"):
            cache.read(columns=["nope"], **SERIES)

    def test_start_beyond_all_coverage_returns_empty_and_unknown(
        self, cache: TimeseriesCache
    ):
        """Filling the missing bound from the hull must not invert the window."""
        cache.write(frame([1, 2]), **SERIES)
        result = cache.read(start=ts(20), **SERIES)
        assert result.frame.height == 0
        assert not result.is_complete

    def test_end_before_all_coverage_returns_empty_and_unknown(
        self, cache: TimeseriesCache
    ):
        cache.write(frame([10, 11]), **SERIES)
        result = cache.read(end=ts(2), **SERIES)
        assert result.frame.height == 0
        assert not result.is_complete

    def test_an_explicitly_inverted_window_is_the_callers_error(
        self, cache: TimeseriesCache
    ):
        cache.write(frame([1, 5]), **SERIES)
        with pytest.raises(WindowError, match="start must be <= end"):
            cache.read(start=ts(5), end=ts(1), **SERIES)

    def test_unknown_key_returns_an_empty_frame(self, cache: TimeseriesCache):
        assert cache.read(ticker="NOPE").frame.height == 0

    def test_keys_are_isolated(self, cache: TimeseriesCache):
        cache.write(frame([1]), ticker="AAPL")
        cache.write(frame([2]), ticker="MSFT")
        assert days(cache.read(ticker="AAPL")) == [ts(1)]
        assert days(cache.read(ticker="MSFT")) == [ts(2)]

    def test_kwargs_order_addresses_the_same_entry(self, cache: TimeseriesCache):
        cache.write(frame([1]), ticker="AAPL", field="close")
        assert cache.read(field="close", ticker="AAPL").frame.height == 1

    def test_scan_stays_lazy(self, cache: TimeseriesCache):
        """The read path's pushdown depends on the backend handing back a
        LazyFrame; an eager backend would silently remove it."""
        from timeseries_cache.keys import CacheKey

        cache.write(frame([1, 2]), **SERIES)
        scanned = cache.backend.scan(CacheKey.build(SERIES))
        assert isinstance(scanned, pl.LazyFrame)


class TestReservedKwargs:
    """Control names must never silently become part of the cache identity.

    Some of them are bound parameters on a given method (``read`` has ``start``,
    ``end``, ``columns``), so Python routes those to the parameter. The ones that
    fall through to ``**kwargs`` must be refused rather than keyed on.
    """

    @pytest.mark.parametrize("name", ["mode", "frame"])
    def test_names_falling_through_to_kwargs_are_refused_by_read(
        self, cache: TimeseriesCache, name: str
    ):
        with pytest.raises(InvalidKwargError, match="reserved"):
            cache.read(**{name: "x"})

    @pytest.mark.parametrize("name", ["columns"])
    def test_names_falling_through_to_kwargs_are_refused_by_write(
        self, cache: TimeseriesCache, name: str
    ):
        with pytest.raises(InvalidKwargError, match="reserved"):
            cache.write(frame([1]), **{name: "x"})

    def test_a_bound_control_name_is_validated_not_keyed_on(
        self, cache: TimeseriesCache
    ):
        # `start` binds to the parameter, so it must fail as a bad *window*,
        # never quietly become part of the key.
        with pytest.raises(WindowError, match="must be a datetime"):
            cache.read(start="x", end=ts(2), **SERIES)


class TestSchema:
    def test_rejects_a_changed_dtype(self, cache: TimeseriesCache):
        cache.write(frame([1]), **SERIES)
        changed = frame([2]).with_columns(pl.col("price").cast(pl.Int64))
        with pytest.raises(SchemaMismatchError, match="stored as"):
            cache.write(changed, **SERIES)

    def test_rejects_an_added_column(self, cache: TimeseriesCache):
        cache.write(frame([1]), **SERIES)
        wider = frame([2]).with_columns(pl.lit(1.0).alias("volume"))
        with pytest.raises(SchemaMismatchError, match="unexpected columns"):
            cache.write(wider, **SERIES)

    def test_rejects_a_dropped_column(self, cache: TimeseriesCache):
        cache.write(frame([1]), **SERIES)
        with pytest.raises(SchemaMismatchError, match="missing columns"):
            cache.write(frame([2]).select("ts"), **SERIES)

    def test_column_order_is_not_a_mismatch(self, cache: TimeseriesCache):
        cache.write(frame([1]), **SERIES)
        cache.write(frame([2]).select("price", "ts"), **SERIES)
        assert cache.read(**SERIES).frame.height == 2

    def test_manifest_records_the_schema(self, cache: TimeseriesCache):
        cache.write(frame([1]), **SERIES)
        manifest = cache.manifest(**SERIES)
        assert manifest is not None
        assert set(manifest.schema) == {"ts", "price"}
        assert manifest.schema["price"] == "Float64"

    def test_schema_is_compared_against_live_data_not_manifest_strings(
        self, cache: TimeseriesCache
    ):
        """A polars release that changes a dtype's repr must not turn every
        existing cache into a wall of spurious mismatches."""
        from timeseries_cache.keys import CacheKey

        cache.write(frame([1]), **SERIES)
        stored = cache.manifest(**SERIES)
        assert stored is not None

        stale = stored.updated(
            coverage=stored.coverage,
            schema={"ts": "Datetime(some future repr)", "price": "Float64Legacy"},
            row_count=stored.row_count,
        )
        cache.backend.write(CacheKey.build(SERIES), cache.read(**SERIES).frame, stale)

        # Still accepted: the check reads the stored frame's real schema.
        cache.write(frame([2]), **SERIES)
        assert cache.read(**SERIES).frame.height == 2


class TestDelete:
    def test_removes_the_whole_key(self, cache: TimeseriesCache):
        cache.write(frame([1, 2]), **SERIES)
        cache.delete(**SERIES)
        assert cache.manifest(**SERIES) is None
        assert not cache.coverage(**SERIES)

    def test_window_delete_removes_rows_and_coverage(self, cache: TimeseriesCache):
        cache.write(frame([1, 2, 3, 4, 5]), **SERIES)
        cache.delete(start=ts(2), end=ts(3), **SERIES)

        result = cache.read(start=ts(1), end=ts(5), **SERIES)
        assert days(result) == [ts(1), ts(4), ts(5)]
        # The range goes back to "unknown", so the next read asks for it again.
        assert result.missing.intervals == (Interval(ts(2), ts(3)),)

    def test_deleting_an_unknown_key_is_a_no_op(self, cache: TimeseriesCache):
        cache.delete(ticker="NOPE")

    def test_rejects_a_half_declared_window(self, cache: TimeseriesCache):
        cache.write(frame([1]), **SERIES)
        with pytest.raises(WindowError, match="both start and end"):
            cache.delete(start=ts(1), **SERIES)


class TestCollisionDetection:
    def test_manifest_kwargs_are_checked_against_the_request(
        self, cache: TimeseriesCache
    ):
        from timeseries_cache.keys import CacheKey

        cache.write(frame([1]), ticker="AAPL")

        # Forge a digest collision: same digest, different kwargs.
        real = CacheKey.build({"ticker": "AAPL"})
        impostor = CacheKey.build({"ticker": "MSFT"})
        object.__setattr__(impostor, "digest", real.digest)

        manifest = cache.backend.read_manifest(impostor)
        assert manifest is not None
        with pytest.raises(CacheKeyCollisionError, match="Refusing"):
            manifest.verify(impostor)


class TestAtomicity:
    def test_a_failed_write_leaves_prior_state_intact(self):
        class ExplodingBackend(MemoryBackend):
            armed = False

            def write(self, key, frame, manifest):
                if self.armed:
                    raise OSError("disk full")
                super().write(key, frame, manifest)

        backend = ExplodingBackend()
        cache = TimeseriesCache(backend)
        cache.write(frame([1, 2]), **SERIES)

        backend.armed = True
        with pytest.raises(OSError):
            cache.write(frame([5, 6]), **SERIES)

        result = cache.read(start=ts(1), end=ts(6), **SERIES)
        assert days(result) == [ts(1), ts(2)]
        assert result.missing  # the failed range is still unknown, not claimed

    def test_manifest_lands_after_data(self, tmp_path):
        """If the manifest write fails, the cache must under-claim, not over-claim.

        Rows may already be on disk; coverage must not mention them, so the next
        read refetches rather than serving a range nothing verified.
        """
        from timeseries_cache.backends.parquet import MANIFEST_NAME, ParquetBackend

        class ManifestFailsBackend(ParquetBackend):
            armed = False

            def _atomic_write(self, target, produce):  # type: ignore[override]
                if self.armed and target.name == MANIFEST_NAME:
                    raise OSError("disk full")
                ParquetBackend._atomic_write(target, produce)

        backend = ManifestFailsBackend(tmp_path / "cache")
        cache = TimeseriesCache(backend)
        cache.write(frame([1, 2]), **SERIES)

        backend.armed = True
        with pytest.raises(OSError):
            cache.write(frame([5, 6]), **SERIES)

        backend.armed = False
        coverage = cache.coverage(**SERIES)
        assert coverage.intervals == (Interval(ts(1), ts(2)),)
        assert cache.read(start=ts(1), end=ts(6), **SERIES).missing

    def test_atomic_write_cleans_up_after_a_failure(self, tmp_path):
        from timeseries_cache.backends.parquet import ParquetBackend

        target = tmp_path / "data.parquet"
        target.write_text("original")

        def explode(_path):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            ParquetBackend._atomic_write(target, explode)

        assert target.read_text() == "original"
        assert list(tmp_path.iterdir()) == [target]


class TestPersistence:
    def test_survives_a_new_cache_instance(self, tmp_path):
        from timeseries_cache import open_cache

        first = open_cache(tmp_path / "cache")
        first.write(frame([1, 2]), start=ts(1), end=ts(4), **SERIES)

        second = open_cache(tmp_path / "cache")
        result = second.read(start=ts(1), end=ts(4), **SERIES)
        assert days(result) == [ts(1), ts(2)]
        assert result.is_complete

    def test_digests_are_listed(self, tmp_path):
        from timeseries_cache import open_cache
        from timeseries_cache.keys import CacheKey

        cache = open_cache(tmp_path / "cache")
        cache.write(frame([1]), ticker="AAPL")
        cache.write(frame([1]), ticker="MSFT")

        expected = {
            CacheKey.build({"ticker": "AAPL"}).digest,
            CacheKey.build({"ticker": "MSFT"}).digest,
        }
        assert set(cache.backend.digests()) == expected
