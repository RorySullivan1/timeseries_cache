"""The storage seam.

Everything above this protocol is pure coverage logic; everything below it is
bytes. Adding object storage, or porting the cache to another language, should
mean writing one of these — not touching ``core``.

Backends own **atomicity** (invariant 5). ``write`` receives the data and the
manifest together so a single implementation can guarantee the ordering, and the
safe order depends on which direction the update moves:

* **Growing** (the usual case — new rows, wider coverage): data first, manifest
  last. A crash between them leaves rows on disk that nothing claims, costing a
  refetch.
* **Shrinking** (``delete``, which removes rows *and* their coverage): manifest
  first, data last. The reverse would leave the manifest claiming a range whose
  rows are already gone — a read would then report "covered, and genuinely
  empty", which is precisely the silent hole invariant 5 exists to prevent.

Either way the interruptible middle state *under-claims*, so the cost of a crash
is always an unnecessary refetch rather than wrong data.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

import polars as pl

from ..index import Manifest
from ..keys import CacheKey


@runtime_checkable
class StorageBackend(Protocol):
    """Where a cache's bytes live."""

    def read_manifest(self, key: CacheKey) -> Manifest | None:
        """Return the stored manifest, or ``None`` if the key is unknown."""
        ...

    def scan(self, key: CacheKey) -> pl.LazyFrame | None:
        """Return a **lazy** handle on the key's rows, or ``None`` if absent.

        Lazy is not a stylistic preference. The read path filters on the
        timestamp column, and returning a ``LazyFrame`` is what lets that
        predicate push down into the parquet reader so only overlapping row
        groups are touched. An implementation that eagerly materializes here
        silently removes the cache's main performance property.
        """
        ...

    def write(
        self,
        key: CacheKey,
        frame: pl.DataFrame,
        manifest: Manifest,
        *,
        manifest_first: bool = False,
    ) -> None:
        """Persist rows and manifest, ordered so a crash under-claims.

        Set ``manifest_first`` when the update *reduces* what the manifest
        claims; see the module docstring for why the order has to flip.
        """
        ...

    def delete(self, key: CacheKey) -> None:
        """Remove all trace of a key. A no-op if it does not exist."""
        ...

    def digests(self) -> Iterator[str]:
        """Every key digest currently stored."""
        ...
