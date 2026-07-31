"""Local-filesystem parquet backend — the default.

Layout, sharded by the digest's first byte so a cache with many keys does not
build one enormous directory::

    <root>/<shard>/<digest>/manifest.json
    <root>/<shard>/<digest>/data.parquet
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import polars as pl

from ..index import Manifest
from ..keys import CacheKey

MANIFEST_NAME = "manifest.json"
DATA_NAME = "data.parquet"


class ParquetBackend:
    """Stores each key as a parquet file plus a JSON manifest."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _dir(self, key: CacheKey) -> Path:
        return self.root / key.shard / key.digest

    def read_manifest(self, key: CacheKey) -> Manifest | None:
        path = self._dir(key) / MANIFEST_NAME
        if not path.exists():
            return None
        return Manifest.from_json(path.read_text(encoding="utf-8"))

    def scan(self, key: CacheKey) -> pl.LazyFrame | None:
        path = self._dir(key) / DATA_NAME
        if not path.exists():
            return None
        return pl.scan_parquet(path)

    def write(self, key: CacheKey, frame: pl.DataFrame, manifest: Manifest) -> None:
        directory = self._dir(key)
        directory.mkdir(parents=True, exist_ok=True)

        def write_data(tmp: Path) -> None:
            frame.write_parquet(tmp)

        def write_manifest(tmp: Path) -> None:
            tmp.write_text(manifest.to_json(), encoding="utf-8")

        # Data first, manifest last. See the note in base.py on why this order
        # is the safe one to be interrupted in.
        if frame.width:
            self._atomic_write(directory / DATA_NAME, write_data)
        else:
            # A schema-less empty write: there is no frame to store, only a
            # coverage claim. Drop any stale data file rather than leave rows
            # the manifest no longer describes.
            (directory / DATA_NAME).unlink(missing_ok=True)
        self._atomic_write(directory / MANIFEST_NAME, write_manifest)

    @staticmethod
    def _atomic_write(target: Path, produce: Callable[[Path], None]) -> None:
        """Write via a sibling temp file, fsync, then rename over the target.

        The rename is the atomic step: a reader sees either the whole old file
        or the whole new one, never a half-written parquet.
        """
        descriptor, name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        os.close(descriptor)
        tmp_path = Path(name)
        try:
            produce(tmp_path)
            with open(tmp_path, "rb") as written:
                os.fsync(written.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def delete(self, key: CacheKey) -> None:
        shutil.rmtree(self._dir(key), ignore_errors=True)

    def digests(self) -> Iterator[str]:
        if not self.root.exists():
            return
        for shard in sorted(self.root.iterdir()):
            if not shard.is_dir():
                continue
            for entry in sorted(shard.iterdir()):
                if (entry / MANIFEST_NAME).exists():
                    yield entry.name
