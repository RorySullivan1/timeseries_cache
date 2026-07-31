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
import time
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

DEFAULT_REPLACE_ATTEMPTS: Final[int] = 5
DEFAULT_REPLACE_BACKOFF: Final[float] = 0.1
"""Retry budget for the final rename.

POSIX never needs this — a file there can be replaced while open. Windows
cannot, and on a network or DFS share the set of things that transiently hold a
file open is large and outside your control: DFS Replication, the file server's
indexer, antivirus, another client that just read the same key. Those clear in
milliseconds, so a short bounded retry converts a hard failure into a pause.

Bounded on purpose. A genuine permission problem should still surface quickly
rather than being buried under a minute of retries, and retrying cannot corrupt
anything: ``os.replace`` either happened or it didn't, and the source file is
still there either way.
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
        replace_attempts: How many times to try the final rename before giving
            up. Only relevant on Windows, where a file cannot be replaced while
            anything holds it open — and on a network or DFS share plenty of
            things transiently do: DFS Replication, the server's indexer,
            antivirus, another client. Each retry backs off, doubling from
            ``replace_backoff``. Set to 1 to fail immediately.
        replace_backoff: Seconds before the second attempt, doubling thereafter.
            The default gives up after roughly 1.5s of trying.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        compression: ParquetCompression = "zstd",
        row_group_size: int | None = DEFAULT_ROW_GROUP_SIZE,
        fsync: bool = True,
        replace_attempts: int = DEFAULT_REPLACE_ATTEMPTS,
        replace_backoff: float = DEFAULT_REPLACE_BACKOFF,
    ) -> None:
        if replace_attempts < 1:
            raise ValueError("replace_attempts must be at least 1")
        self.root = Path(root)
        self.compression = compression
        self.row_group_size = row_group_size
        self.fsync = fsync
        self.replace_attempts = replace_attempts
        self.replace_backoff = replace_backoff

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
                self._atomic_write(
                    directory / DATA_NAME,
                    write_data,
                    fsync=self.fsync,
                    attempts=self.replace_attempts,
                    backoff=self.replace_backoff,
                )
            else:
                # A schema-less empty write: there is no frame to store, only a
                # coverage claim. Drop any stale data file rather than leave rows
                # the manifest no longer describes.
                (directory / DATA_NAME).unlink(missing_ok=True)

        def put_manifest() -> None:
            self._atomic_write(
                directory / MANIFEST_NAME,
                write_manifest,
                fsync=self.fsync,
                attempts=self.replace_attempts,
                backoff=self.replace_backoff,
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
        target: Path,
        produce: Callable[[Path], None],
        *,
        fsync: bool = True,
        attempts: int = DEFAULT_REPLACE_ATTEMPTS,
        backoff: float = DEFAULT_REPLACE_BACKOFF,
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
            ParquetBackend._replace(
                tmp_path, target, attempts=attempts, backoff=backoff
            )
            if fsync:
                ParquetBackend._fsync_dir(target.parent)
        except BaseException:
            # Suppressed: on Windows, unlinking a file that still has an open
            # handle raises PermissionError, and an exception raised *inside* an
            # except block replaces the one being handled. Without this, a
            # failed cleanup hides whatever actually went wrong — you get
            # "permission denied" on a temp file instead of the real cause.
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _replace(
        source: Path,
        target: Path,
        *,
        attempts: int = DEFAULT_REPLACE_ATTEMPTS,
        backoff: float = DEFAULT_REPLACE_BACKOFF,
    ) -> None:
        """Atomically move ``source`` onto ``target``, retrying transient locks.

        POSIX replaces an open file happily — the old inode survives until its
        last handle closes — so the retry never engages there. Windows refuses
        while anything holds the target open, and on a network or DFS share the
        holder is often a service you don't control and that lets go a moment
        later.

        On exhaustion the error carries the attempt count and elapsed time,
        because *which* it was matters: failing every attempt over 1.5s points
        at a permanent holder or a permission problem, while succeeding on a
        later attempt points at a transient one.
        """
        delay = backoff
        started = time.monotonic()
        last: OSError | None = None

        for attempt in range(1, attempts + 1):
            try:
                os.replace(source, target)
                return
            except PermissionError as error:
                # Covers both WinError 5 (access denied) and 32 (sharing
                # violation); Python maps both onto PermissionError.
                last = error
                if attempt == attempts:
                    break
                time.sleep(delay)
                delay *= 2

        elapsed = time.monotonic() - started
        raise PermissionError(
            f"could not move {source.name} onto {target.name} after "
            f"{attempts} attempt(s) over {elapsed:.1f}s: {last}. "
            "On Windows a file cannot be replaced while anything holds it open. "
            "Failing every attempt points at a permanent holder rather than a "
            "transient one — a permission problem (replacing a file needs delete "
            "rights on it, or delete-child on its directory), DFS Replication, "
            "or another process with the file open. Raise replace_attempts if "
            "the holder is slow rather than permanent."
        ) from last

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
