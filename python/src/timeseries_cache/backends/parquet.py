"""Local-filesystem parquet backend — the default.

Layout, sharded by the digest's first byte so a cache with many keys does not
build one enormous directory::

    <root>/<shard>/<digest>/manifest.json
    <root>/<shard>/<digest>/data.parquet
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final, Literal

import polars as pl

from ..index import Manifest
from ..keys import CacheKey

ParquetCompression = Literal["lz4", "uncompressed", "snappy", "gzip", "brotli", "zstd"]

MANIFEST_NAME = "manifest.json"
DATA_NAME = "data.parquet"

DEFAULT_ROW_GROUP_SIZE: Final[int] = 64_000
"""Rows per parquet row group.

Smaller than polars' default on purpose. Row groups are the unit the reader can
skip, and *every* read here is a time range, so finer groups mean less
over-reading. Measured on a 2M-row key: a 1,000-row window costs 4.1ms at 64k
versus 5.4ms at the default, a 50,000-row window 4.1ms versus 5.9ms, with write
time and file size unchanged. Below ~16k the write cost starts climbing and wide
reads get worse, so this is near the knee rather than as small as possible.
"""


class ParquetBackend:
    """Stores each key as a parquet file plus a JSON manifest.

    Args:
        root: Directory to store the cache under.
        compression: Parquet codec. The default trades write time for size —
            ``zstd`` is ~44ms/7.4MB per 2M rows against ``lz4``'s ~31ms/20MB —
            which suits data written once and read repeatedly. Switch to
            ``"lz4"`` if writes dominate your workload.
        row_group_size: See :data:`DEFAULT_ROW_GROUP_SIZE`. Lower it if reads
            are consistently narrow, raise it if they are consistently wide.
        fsync: Flush file contents and directory entries before returning.
            Disabling it is faster (~24ms per write on the machine this was
            measured on) but gives up the crash guarantee in invariant 5: after
            a power loss the manifest may be durable while its rows are not.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        compression: ParquetCompression = "zstd",
        row_group_size: int | None = DEFAULT_ROW_GROUP_SIZE,
        fsync: bool = True,
    ) -> None:
        self.root = Path(root)
        self.compression = compression
        self.row_group_size = row_group_size
        self.fsync = fsync

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

    def write(
        self,
        key: CacheKey,
        frame: pl.DataFrame,
        manifest: Manifest,
        *,
        manifest_first: bool = False,
    ) -> None:
        directory = self._dir(key)
        directory.mkdir(parents=True, exist_ok=True)

        def write_data(tmp: Path) -> None:
            frame.write_parquet(
                tmp,
                compression=self.compression,
                row_group_size=self.row_group_size,
            )

        def write_manifest(tmp: Path) -> None:
            tmp.write_text(manifest.to_json(), encoding="utf-8")

        def put_data() -> None:
            if frame.width:
                self._atomic_write(directory / DATA_NAME, write_data, fsync=self.fsync)
            else:
                # A schema-less empty write: there is no frame to store, only a
                # coverage claim. Drop any stale data file rather than leave rows
                # the manifest no longer describes.
                (directory / DATA_NAME).unlink(missing_ok=True)

        def put_manifest() -> None:
            self._atomic_write(
                directory / MANIFEST_NAME, write_manifest, fsync=self.fsync
            )

        # Order matters, and which order is safe depends on direction — see the
        # module docstring in base.py. Both orders leave an interruptible middle
        # state that under-claims.
        if manifest_first:
            put_manifest()
            put_data()
        else:
            put_data()
            put_manifest()

    @staticmethod
    def _atomic_write(
        target: Path, produce: Callable[[Path], None], *, fsync: bool = True
    ) -> None:
        """Write via a sibling temp file, fsync, then rename over the target.

        The rename is the atomic step: a reader sees either the whole old file
        or the whole new one, never a half-written parquet. The directory fsync
        afterwards is what makes the rename itself survive a power loss —
        without it the ordering guarantee above holds only against a process
        crash, not against the machine going down.
        """
        descriptor, name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        os.close(descriptor)
        tmp_path = Path(name)
        try:
            produce(tmp_path)
            if fsync:
                # "r+b", not "rb": the descriptor handed to fsync must be
                # *writable*. POSIX permits fsync on a read-only descriptor, but
                # on Windows os.fsync is _commit(), which rejects a read-only
                # CRT handle with EBADF — "Bad file descriptor" — so a read-only
                # open here works everywhere except the platform most likely to
                # be running it. r+b also avoids truncating what produce wrote.
                with open(tmp_path, "r+b") as written:
                    written.flush()
                    os.fsync(written.fileno())
            os.replace(tmp_path, target)
            if fsync:
                ParquetBackend._fsync_dir(target.parent)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Persist a directory entry (the rename), where the platform allows it.

        Entirely best-effort, and every step is guarded because platforms
        disagree at every step: Windows cannot open a directory as a file at
        all, macOS and several network filesystems accept the descriptor but
        refuse to fsync it, and a descriptor left in an odd state by either can
        make even the close fail. None of that should break a write that
        otherwise succeeded — the durability this buys is a refinement on top of
        the atomic rename, not something the write's correctness rests on.

        The file fsync in ``_atomic_write`` is deliberately *not* guarded like
        this. If that one fails, the data genuinely may not be on disk and the
        caller needs to hear about it.
        """
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:  # pragma: no cover - platform-dependent
            return  # e.g. Windows, where a directory has no file descriptor
        try:
            os.fsync(descriptor)
        except OSError:  # pragma: no cover - platform-dependent
            pass  # e.g. macOS and some network filesystems
        finally:
            # Guarded too: an unguarded close here turns a harmless
            # platform quirk into a failed write.
            with contextlib.suppress(OSError):
                os.close(descriptor)

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
