from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from timeseries_cache import Interval, IntervalSet, WindowError
from timeseries_cache.intervals import RESOLUTION, ensure_utc

from .conftest import ts


class TestInterval:
    def test_rejects_naive_datetimes(self):
        with pytest.raises(WindowError, match="timezone-aware"):
            Interval(datetime(2024, 1, 1), ts(2))

    def test_rejects_inverted_bounds(self):
        with pytest.raises(WindowError, match="start must be <= end"):
            Interval(ts(5), ts(1))

    def test_single_instant_is_valid(self):
        assert Interval(ts(1), ts(1)).contains(ts(1))

    def test_normalizes_to_utc(self):
        from datetime import timezone

        eastern = timezone(timedelta(hours=-5))
        interval = Interval(
            datetime(2024, 1, 1, 19, tzinfo=eastern),
            datetime(2024, 1, 2, 19, tzinfo=eastern),
        )
        assert interval.start == datetime(2024, 1, 2, 0, tzinfo=UTC)

    def test_bounds_are_closed_on_both_ends(self):
        interval = Interval(ts(1), ts(3))
        assert interval.contains(ts(1))
        assert interval.contains(ts(3))
        assert not interval.contains(ts(3) + RESOLUTION)

    def test_touching_intervals_overlap_at_the_shared_instant(self):
        assert Interval(ts(1), ts(3)).overlaps(Interval(ts(3), ts(5)))
        assert not Interval(ts(1), ts(3)).overlaps(Interval(ts(3) + RESOLUTION, ts(5)))


class TestEnsureUtc:
    def test_rejects_naive(self):
        with pytest.raises(WindowError):
            ensure_utc(datetime(2024, 1, 1))


class TestIntervalSetNormalization:
    def test_merges_overlapping(self):
        merged = IntervalSet.of(Interval(ts(1), ts(5)), Interval(ts(3), ts(8)))
        assert merged.intervals == (Interval(ts(1), ts(8)),)

    def test_merges_intervals_touching_at_the_resolution(self):
        # [1, 3] and [3+1us, 5] have nothing between them in a discrete domain.
        merged = IntervalSet.of(
            Interval(ts(1), ts(3)), Interval(ts(3) + RESOLUTION, ts(5))
        )
        assert merged.intervals == (Interval(ts(1), ts(5)),)

    def test_keeps_genuinely_separated_intervals_apart(self):
        merged = IntervalSet.of(
            Interval(ts(1), ts(3)), Interval(ts(3) + 2 * RESOLUTION, ts(5))
        )
        assert len(merged) == 2

    def test_is_order_independent(self):
        forward = IntervalSet.of(Interval(ts(1), ts(2)), Interval(ts(5), ts(6)))
        backward = IntervalSet.of(Interval(ts(5), ts(6)), Interval(ts(1), ts(2)))
        assert forward == backward

    def test_hull_spans_gaps(self):
        covered = IntervalSet.of(Interval(ts(1), ts(2)), Interval(ts(9), ts(10)))
        assert covered.hull == Interval(ts(1), ts(10))

    def test_empty_hull_is_none(self):
        assert IntervalSet.empty().hull is None


class TestSubtraction:
    def test_punching_a_hole_leaves_two_pieces_excluding_the_endpoints(self):
        result = IntervalSet.of(Interval(ts(1), ts(10))).subtract(
            Interval(ts(4), ts(6))
        )
        assert result.intervals == (
            Interval(ts(1), ts(4) - RESOLUTION),
            Interval(ts(6) + RESOLUTION, ts(10)),
        )

    def test_subtracting_a_superset_empties_the_set(self):
        result = IntervalSet.of(Interval(ts(3), ts(5))).subtract(
            Interval(ts(1), ts(10))
        )
        assert not result

    def test_trimming_from_the_left(self):
        result = IntervalSet.of(Interval(ts(1), ts(10))).subtract(
            Interval(ts(1), ts(4))
        )
        assert result.intervals == (Interval(ts(4) + RESOLUTION, ts(10)),)

    def test_disjoint_subtraction_is_a_no_op(self):
        original = IntervalSet.of(Interval(ts(1), ts(3)))
        assert original.subtract(Interval(ts(8), ts(9))) == original

    def test_round_trip_union_then_subtract(self):
        base = IntervalSet.of(Interval(ts(1), ts(10)))
        cut = Interval(ts(4), ts(6))
        assert base.subtract(cut).union(cut) == base


class TestGaps:
    def test_reports_the_uncovered_middle(self):
        covered = IntervalSet.of(Interval(ts(1), ts(3)), Interval(ts(8), ts(10)))
        gaps = covered.gaps_within(Interval(ts(1), ts(10)))
        assert gaps.intervals == (Interval(ts(3) + RESOLUTION, ts(8) - RESOLUTION),)

    def test_no_coverage_means_the_whole_window_is_missing(self):
        window = Interval(ts(1), ts(5))
        assert IntervalSet.empty().gaps_within(window).intervals == (window,)

    def test_full_coverage_means_no_gaps(self):
        covered = IntervalSet.of(Interval(ts(1), ts(10)))
        assert covered.covers(Interval(ts(3), ts(6)))
        assert not covered.gaps_within(Interval(ts(3), ts(6)))

    def test_gaps_are_clipped_to_the_window(self):
        covered = IntervalSet.of(Interval(ts(5), ts(6)))
        gaps = covered.gaps_within(Interval(ts(1), ts(10)))
        assert gaps.hull == Interval(ts(1), ts(10))
        assert len(gaps) == 2


class TestIntersection:
    def test_clips_to_the_window(self):
        covered = IntervalSet.of(Interval(ts(1), ts(10)))
        assert covered.intersection(Interval(ts(4), ts(20))).intervals == (
            Interval(ts(4), ts(10)),
        )

    def test_disjoint_intersection_is_empty(self):
        covered = IntervalSet.of(Interval(ts(1), ts(3)))
        assert not covered.intersection(Interval(ts(5), ts(9)))


class TestSerialization:
    def test_round_trips_through_the_manifest_payload(self):
        original = IntervalSet.of(Interval(ts(1), ts(3)), Interval(ts(8), ts(10)))
        assert IntervalSet.from_payload(original.to_payload()) == original

    def test_empty_round_trips(self):
        assert IntervalSet.from_payload(IntervalSet.empty().to_payload()) == (
            IntervalSet.empty()
        )
