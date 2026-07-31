"""Turn arbitrary cache kwargs into a stable, portable key.

Kwargs are the flexibility axis of this cache, so nothing here — and nothing in
``core``, ``index``, or ``intervals`` — may know a domain-specific kwarg name.
The only requirement placed on a caller is that values canonicalize
*deterministically*.

Determinism means the same kwargs produce the same key in another process, on
another machine, next year. That rules out :func:`hash` (PYTHONHASHSEED),
insertion order, ``repr`` of arbitrary objects, and locale-dependent formatting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final

from .errors import InvalidKwargError
from .intervals import ensure_utc

RESERVED_KWARGS: Final[frozenset[str]] = frozenset(
    {"start", "end", "mode", "columns", "frame"}
)
"""Names the cache API uses for control parameters, so they cannot also be cache
kwargs. Using one raises rather than silently shadowing it."""

_HASH_LENGTH: Final[int] = 32


def _canonical_value(value: Any, *, path: str) -> list[Any]:
    """Render a kwarg value as a deterministic ``[tag, payload]`` pair.

    The type tag is load-bearing: without it the int ``1`` and the string
    ``"1"`` would hash to the same key and quietly share a cache entry.

    A *structure* rather than a string, because string concatenation with
    separators is forgeable — a value containing the separator characters can
    impersonate additional kwargs. JSON encoding at the end escapes everything
    exactly once, so no value can break out of its own slot.
    """
    if isinstance(value, Enum):
        return _canonical_value(value.value, path=path)
    if value is None:
        return ["n", None]
    # bool before int: bool is an int subclass, and True would render as 1.
    if isinstance(value, bool):
        return ["b", value]
    if isinstance(value, int):
        return ["i", f"{value:d}"]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidKwargError(
                f"cache kwarg {path!r} is {value!r}; NaN and infinity have no "
                "stable identity and cannot key a cache entry"
            )
        # repr round-trips exactly for float and is locale-independent.
        return ["f", repr(value)]
    if isinstance(value, str):
        return ["s", value]
    if isinstance(value, Decimal):
        # Textual identity: Decimal("1.0") and Decimal("1") are *different* keys.
        return ["dec", str(value)]
    if isinstance(value, datetime):
        return ["dt", ensure_utc(value, label=f"cache kwarg {path!r}").isoformat()]
    if isinstance(value, date):
        return ["d", value.isoformat()]
    if isinstance(value, (list, tuple)):
        return [
            "l",
            [
                _canonical_value(item, path=f"{path}[{i}]")
                for i, item in enumerate(value)
            ],
        ]
    if isinstance(value, (set, frozenset)):
        raise InvalidKwargError(
            f"cache kwarg {path!r} is a {type(value).__name__}; sets have no "
            "reliable ordering across runs. Pass a sorted tuple instead."
        )
    if isinstance(value, dict):
        raise InvalidKwargError(
            f"cache kwarg {path!r} is a dict; nested mappings are not supported. "
            "Flatten it into separate kwargs."
        )
    raise InvalidKwargError(
        f"cache kwarg {path!r} has type {type(value).__name__}, which has no "
        "deterministic canonical form. Supported: str, int, float, bool, None, "
        "date, datetime, Decimal, Enum, and lists/tuples of those."
    )


def canonicalize(kwargs: dict[str, Any]) -> str:
    """Render the whole kwargs mapping as one deterministic string.

    Keys are sorted, so call-site keyword order is irrelevant. The result is
    JSON with sorted keys and no insignificant whitespace: unambiguous to parse
    back, and — the point — impossible for a value to forge, since every string
    is escaped inside its own slot rather than concatenated with separators.
    ``ensure_ascii`` keeps the bytes identical regardless of platform encoding.
    """
    for name in sorted(kwargs):
        if name in RESERVED_KWARGS:
            raise InvalidKwargError(
                f"{name!r} is reserved for the cache API and cannot be used as a "
                f"cache kwarg. Reserved names: {', '.join(sorted(RESERVED_KWARGS))}."
            )
        if not name:
            raise InvalidKwargError("cache kwarg names must be non-empty")
    tagged = {name: _canonical_value(kwargs[name], path=name) for name in kwargs}
    return json.dumps(tagged, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class CacheKey:
    """A cache identity: the caller's kwargs plus their stable digest."""

    kwargs: dict[str, Any]
    canonical: str
    digest: str

    @classmethod
    def build(cls, kwargs: dict[str, Any]) -> CacheKey:
        canonical = canonicalize(kwargs)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
        return cls(kwargs=dict(kwargs), canonical=canonical, digest=digest)

    @property
    def shard(self) -> str:
        """First path component, so a cache with many keys doesn't build one
        enormous directory."""
        return self.digest[:2]

    @property
    def relative_path(self) -> str:
        return f"{self.shard}/{self.digest}"

    def __str__(self) -> str:
        return f"{self.digest} ({self.canonical or 'no kwargs'})"
