"""The per-key manifest — the cache's source of truth for what is *known*.

Invariant 3 lives here. Stored rows tell you what data exists; they cannot tell
you whether a range with no rows was *fetched and legitimately empty* (a market
holiday, a delisted symbol) or simply *never requested*. Those need opposite
responses, so coverage is recorded explicitly, alongside the rows but
independent of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final

from .errors import CacheKeyCollisionError
from .intervals import IntervalSet
from .keys import CacheKey

FORMAT_VERSION: Final[int] = 1
"""Bumped only for a breaking change to the on-disk manifest. Reading a
manifest from the future is an error, not something to guess at."""


def _jsonable(value: Any) -> Any:
    """Best-effort rendering of a kwarg value for the manifest's human-readable
    copy. Identity never depends on this — that is ``canonical``'s job."""
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class Manifest:
    """What the cache knows about one key."""

    digest: str
    canonical: str
    kwargs: dict[str, Any]
    timestamp_column: str
    coverage: IntervalSet
    schema: dict[str, str]
    row_count: int
    created_at: datetime
    updated_at: datetime
    format_version: int = FORMAT_VERSION

    @classmethod
    def new(
        cls,
        key: CacheKey,
        *,
        timestamp_column: str,
        now: datetime | None = None,
    ) -> Manifest:
        stamp = now or datetime.now(UTC)
        return cls(
            digest=key.digest,
            canonical=key.canonical,
            kwargs=dict(key.kwargs),
            timestamp_column=timestamp_column,
            coverage=IntervalSet.empty(),
            schema={},
            row_count=0,
            created_at=stamp,
            updated_at=stamp,
        )

    def updated(
        self,
        *,
        coverage: IntervalSet,
        schema: dict[str, str],
        row_count: int,
        now: datetime | None = None,
    ) -> Manifest:
        return replace(
            self,
            coverage=coverage,
            schema=dict(schema),
            row_count=row_count,
            updated_at=now or datetime.now(UTC),
        )

    def verify(self, key: CacheKey) -> None:
        """Fail loudly if this manifest belongs to different kwargs.

        The canonical string is stored precisely so a digest collision surfaces
        as an error instead of one series quietly serving another's rows.
        """
        if self.canonical != key.canonical:
            raise CacheKeyCollisionError(
                f"digest {key.digest} already holds kwargs {self.canonical!r}, "
                f"but was addressed with {key.canonical!r}. Refusing to read or "
                "overwrite another series' data."
            )

    def to_json(self) -> str:
        payload = {
            "format_version": self.format_version,
            "digest": self.digest,
            "canonical": self.canonical,
            "kwargs": {k: _jsonable(v) for k, v in self.kwargs.items()},
            "timestamp_column": self.timestamp_column,
            "coverage": self.coverage.to_payload(),
            "schema": self.schema,
            "row_count": self.row_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        payload = json.loads(raw)
        version = int(payload.get("format_version", 0))
        if version > FORMAT_VERSION:
            raise ValueError(
                f"manifest format_version {version} is newer than this build "
                f"supports ({FORMAT_VERSION}); upgrade timeseries-cache"
            )
        return cls(
            digest=payload["digest"],
            canonical=payload["canonical"],
            kwargs=payload["kwargs"],
            timestamp_column=payload["timestamp_column"],
            coverage=IntervalSet.from_payload(payload["coverage"]),
            schema=payload["schema"],
            row_count=int(payload["row_count"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            format_version=version,
        )
