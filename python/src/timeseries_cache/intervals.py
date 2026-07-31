"""Closed-interval algebra over UTC datetimes.

This module is the cache's coverage arithmetic and deliberately imports no
dataframe library — it reasons about *time ranges*, never about rows.

Two conventions hold everywhere and are the source of most would-be bugs:

* Intervals are **closed on both ends**, ``[start, end]``. Both endpoints are
  part of the interval.
* The time domain is **discrete at one microsecond** (:data:`RESOLUTION`), which
  is what makes subtraction closable: ``[0, 10] - [3, 5]`` is
  ``[0, 3-1us]`` and ``[5+1us, 10]``, not a pair of half-open intervals we have
  no way to represent. It also makes "touching" well defined, so ``[0, 3us]``
  and ``[4us, 9us]`` merge into one run instead of leaving a phantom gap.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import WindowError

RESOLUTION = timedelta(microseconds=1)
"""The atom of the time domain. Storage precision is microseconds (see keys of
the index contract in CLAUDE.md), so coverage arithmetic uses the same unit."""


def ensure_utc(value: datetime, *, label: str = "timestamp") -> datetime:
    """Return ``value`` as a UTC datetime, rejecting naive input.

    Naive datetimes are rejected rather than assumed to be UTC: guessing here is
    how a whole cache silently shifts by the author's local offset.
    """
    if not isinstance(value, datetime):
        raise WindowError(
            f"{label} must be a datetime; got {type(value).__name__} {value!r}"
        )
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise WindowError(
            f"{label} must be timezone-aware; got naive {value!r}. "
            "Localize it explicitly (e.g. .replace(tzinfo=UTC)) before passing it."
        )
    return value.astimezone(UTC)


@dataclass(frozen=True, order=True)
class Interval:
    """A closed time range ``[start, end]``. Both endpoints are included."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = ensure_utc(self.start, label="interval start")
        end = ensure_utc(self.end, label="interval end")
        if start > end:
            raise WindowError(
                f"interval start must be <= end; got start={start!r} end={end!r}"
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def overlaps(self, other: Interval) -> bool:
        """True when the two closed intervals share at least one instant."""
        return self.start <= other.end and other.start <= self.end

    def contains(self, moment: datetime) -> bool:
        return self.start <= ensure_utc(moment) <= self.end

    def __str__(self) -> str:
        return f"[{self.start.isoformat()}, {self.end.isoformat()}]"


def _merge(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Sort and coalesce intervals that overlap or touch at the resolution."""
    ordered = sorted(intervals)
    if not ordered:
        return ()
    merged: list[Interval] = [ordered[0]]
    for candidate in ordered[1:]:
        current = merged[-1]
        # `+ RESOLUTION` is what makes back-to-back runs coalesce: in a discrete
        # domain [a, t] and [t+1us, b] have nothing between them.
        if candidate.start <= current.end + RESOLUTION:
            if candidate.end > current.end:
                merged[-1] = Interval(current.start, candidate.end)
        else:
            merged.append(candidate)
    return tuple(merged)


def _subtract_one(minuend: Interval, subtrahend: Interval) -> list[Interval]:
    """``minuend`` minus ``subtrahend``, yielding zero, one, or two intervals."""
    if not minuend.overlaps(subtrahend):
        return [minuend]
    remainder: list[Interval] = []
    if subtrahend.start > minuend.start:
        remainder.append(Interval(minuend.start, subtrahend.start - RESOLUTION))
    if subtrahend.end < minuend.end:
        remainder.append(Interval(subtrahend.end + RESOLUTION, minuend.end))
    return remainder


@dataclass(frozen=True)
class IntervalSet:
    """A normalized set of disjoint, sorted, closed intervals.

    Instances are always normalized on construction, so equality is structural:
    two sets covering the same instants compare equal regardless of how they
    were assembled.
    """

    intervals: tuple[Interval, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "intervals", _merge(self.intervals))

    @classmethod
    def empty(cls) -> IntervalSet:
        return cls(())

    @classmethod
    def of(cls, *intervals: Interval) -> IntervalSet:
        return cls(tuple(intervals))

    def __bool__(self) -> bool:
        return bool(self.intervals)

    def __iter__(self) -> Iterator[Interval]:
        return iter(self.intervals)

    def __len__(self) -> int:
        return len(self.intervals)

    @property
    def hull(self) -> Interval | None:
        """The single interval spanning everything covered, gaps included."""
        if not self.intervals:
            return None
        return Interval(self.intervals[0].start, self.intervals[-1].end)

    def union(self, other: IntervalSet | Interval) -> IntervalSet:
        added = (other,) if isinstance(other, Interval) else other.intervals
        return IntervalSet(self.intervals + tuple(added))

    def subtract(self, other: IntervalSet | Interval) -> IntervalSet:
        removed = (other,) if isinstance(other, Interval) else other.intervals
        remaining = list(self.intervals)
        for cut in removed:
            step: list[Interval] = []
            for piece in remaining:
                step.extend(_subtract_one(piece, cut))
            remaining = step
        return IntervalSet(tuple(remaining))

    def intersection(self, window: Interval) -> IntervalSet:
        clipped = [
            Interval(max(piece.start, window.start), min(piece.end, window.end))
            for piece in self.intervals
            if piece.overlaps(window)
        ]
        return IntervalSet(tuple(clipped))

    def gaps_within(self, window: Interval) -> IntervalSet:
        """The parts of ``window`` this set does *not* cover.

        This is the read path's whole reason for existing: the caller fetches
        these ranges and nothing else.
        """
        return IntervalSet.of(window).subtract(self)

    def covers(self, window: Interval) -> bool:
        return not self.gaps_within(window)

    def to_payload(self) -> list[list[str]]:
        """Serialize for the manifest as ISO-8601 pairs."""
        return [[i.start.isoformat(), i.end.isoformat()] for i in self.intervals]

    @classmethod
    def from_payload(cls, payload: Iterable[Iterable[str]]) -> IntervalSet:
        return cls(
            tuple(
                Interval(datetime.fromisoformat(start), datetime.fromisoformat(end))
                for start, end in (tuple(pair) for pair in payload)
            )
        )

    def __str__(self) -> str:
        if not self.intervals:
            return "IntervalSet(empty)"
        return "IntervalSet(" + ", ".join(str(i) for i in self.intervals) + ")"
