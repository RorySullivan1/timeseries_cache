"""In-process backend, for tests and for callers who want a cache that dies
with the process.

Deliberately shares no code with the parquet backend: running the same
behavioral suite against both is what proves the coverage logic doesn't depend
on storage details.
"""

from __future__ import annotations

from collections.abc import Iterator

import polars as pl

from ..index import Manifest
from ..keys import CacheKey


class MemoryBackend:
    """Holds frames and manifests in a dict."""

    def __init__(self) -> None:
        self._frames: dict[str, pl.DataFrame] = {}
        self._manifests: dict[str, Manifest] = {}

    def read_manifest(self, key: CacheKey) -> Manifest | None:
        return self._manifests.get(key.digest)

    def scan(self, key: CacheKey) -> pl.LazyFrame | None:
        frame = self._frames.get(key.digest)
        return None if frame is None else frame.lazy()

    def write(self, key: CacheKey, frame: pl.DataFrame, manifest: Manifest) -> None:
        # `clone` so a caller mutating the frame it handed us cannot reach into
        # the cache afterwards.
        if frame.width:
            self._frames[key.digest] = frame.clone()
        else:
            self._frames.pop(key.digest, None)
        self._manifests[key.digest] = manifest

    def delete(self, key: CacheKey) -> None:
        self._frames.pop(key.digest, None)
        self._manifests.pop(key.digest, None)

    def digests(self) -> Iterator[str]:
        return iter(sorted(self._manifests))
