"""Exception hierarchy for the cache.

Deliberately free of any dataframe import: these cross the pandas facade
boundary, so a caller who only imported ``PandasTimeseriesCache`` must never see
a ``polars`` type in a traceback.
"""

from __future__ import annotations


class TimeseriesCacheError(Exception):
    """Base class for every error this package raises."""


class InvalidKwargError(TimeseriesCacheError):
    """A cache kwarg is missing, reserved, or not deterministically hashable."""


class CacheKeyCollisionError(TimeseriesCacheError):
    """A stored manifest's kwargs disagree with the ones that produced its key.

    Raised rather than silently serving another series' data. The manifest keeps
    the kwargs verbatim precisely so this is detectable.
    """


class IndexContractError(TimeseriesCacheError):
    """The timestamp column violates the index contract.

    Missing, tz-naive, not UTC, null, unsorted-and-unsortable, non-unique, or
    carrying sub-microsecond precision that storage would silently truncate.
    """


class SchemaMismatchError(TimeseriesCacheError):
    """An incoming frame's schema disagrees with what the key already holds."""


class OverlappingWriteError(TimeseriesCacheError):
    """An ``append_only`` write overlaps coverage the cache already has."""


class WindowError(TimeseriesCacheError):
    """The target window is missing, inverted, or inconsistent with the data."""
