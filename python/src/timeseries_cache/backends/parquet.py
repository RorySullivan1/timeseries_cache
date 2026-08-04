"""Local-filesystem parquet backend — the default.

**Nothing here is ever renamed or replaced.** Each write creates a new numbered
generation and the newest valid manifest is what a reader uses::

    <root>/<shard>/<digest>/manifest-00000007.json
    <root>/<shard>/<digest>/data-00000007.parquet

That is the whole design, and it exists because the obvious alternative does not
work everywhere. Publishing by rename is the textbook way to make a write
atomic, and it needs the target to be replaceable — which on Windows means
unheld by every other process, and on a network or DFS share means the
redirector agreeing to a same-volume rename it is entitled to refuse
(``ERROR_NOT_SAME_DEVICE``) and an ACL granting delete, since replacing a file
*is* deleting one. A cache that stops working when any of those is missing is
not a robust cache.

Creating a file needs none of that. So:

* **Publishing is a create.** A new generation's data file is written under a
  name nothing is looking for yet, and the generation becomes real the instant
  its manifest file exists.
* **A half-written file is invisible, not corrupting.** Readers only consult
  ``data-N`` once ``manifest-N`` parses, and a truncated manifest does not
  parse, so an interrupted write leaves a generation that is skipped rather
  than one that lies.
* **The write-ordering rule disappears.** ``manifest_first`` mattered when a
  key's single pair of files was mutated in place; here nothing existing is
  touched, so growing and shrinking updates publish identically and both
  under-claim on a crash.
* **Cleanup is optional.** Old generations are deleted best-effort. A share that
  refuses the delete — locked file, no delete rights — costs disk space, never
  a failed write. This is the property that makes the cache usable on a share
  ACL'd "create and write, but not delete".

The cost is that a key briefly occupies two generations, since the previous one
is kept until the next write to keep readers that are mid-scan safe.
"""

from __future__ import annotations

import contextlib
import json
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

MANIFEST_PREFIX: Final[str] = "manifest-"
MANIFEST_SUFFIX: Final[str] = ".json"
DATA_PREFIX: Final[str] = "data-"
DATA_SUFFIX: Final[str] = ".parquet"
GENERATION_DIGITS: Final[int] = 8
"""Zero-padded so a plain directory listing sorts chronologically."""

LEGACY_MANIFEST: Final[str] = "manifest.json"
LEGACY_DATA: Final[str] = "data.parquet"
"""The pre-generational layout, read as generation 0.

Caches written by an earlier build keep working and migrate on their next write.
"""

KEEP_GENERATIONS: Final[int] = 1
"""How many superseded generations to leave behind.

One, not zero. A reader that has just resolved generation N and not yet opened
its parquet file would race a writer deleting N the moment N+1 lands; keeping
the previous generation makes that window harmless. Two live generations is the
price of never renaming.
"""

DEFAULT_ROW_GROUP_SIZE: Final[int] = 64_000
"""Rows per parquet row group.

Smaller than polars' default on purpose. Row groups are the unit the reader can
skip, and *every* read here is a time range, so finer groups mean less
over-reading. Measured on a 2M-row key: a 1,000-row window costs 4.1ms at 64k
versus 5.4ms at the default, a 50,000-row window 4.1ms versus 5.9ms, with write
time and file size unchanged. Below ~16k the write cost starts climbing and wide
reads get worse, so this is near the knee rather than as small as possible.
"""

STAGING_SUBDIR: Final[str] = "timeseries_cache_staging"
"""Directory under the system temp dir used when staging is chosen for you."""

DRIVE_REMOTE: Final[int] = 4
"""``GetDriveTypeW`` result for a network drive. From winbase.h; hardcoded
rather than pulled from pywin32, which the core deliberately does not depend on.
"""

DEFAULT_CREATE_ATTEMPTS: Final[int] = 5
DEFAULT_CREATE_BACKOFF: Final[float] = 0.1
"""Retry budget for creating a file.

Much less load-bearing than the rename retry it replaces — a create does not
contend with readers of an existing file — but a share still hands out
transient sharing violations from antivirus, indexers, and DFS Replication, and
riding those out is cheap. Retrying a create is safe because the name is unique
to this generation: nothing else is entitled to it.
"""


class ParquetBackend:
    """Stores each key as numbered parquet + manifest generations.

    Args:
        root: Directory to store the cache under.
        compression: Parquet codec. The default trades write time for size —
            ``zstd`` is ~44ms/7.4MB per 2M rows against ``lz4``'s ~31ms/20MB —
            which suits data written once and read repeatedly. Switch to
            ``"lz4"`` if writes dominate your workload.
        row_group_size: See :data:`DEFAULT_ROW_GROUP_SIZE`. Lower it if reads
            are consistently narrow, raise it if they are consistently wide.
        fsync: Flush file contents before a generation is published. Disabling
            it is faster but gives up the crash guarantee: after a power loss a
            manifest may be durable while its rows are not.
        create_attempts: How many times to retry creating a file before giving
            up, for shares that hand out transient sharing violations. Set to 1
            to fail immediately.
        create_backoff: Seconds before the second attempt, doubling thereafter.
        staging_dir: Where to build a file before copying it into place.

            ``"auto"`` (the default) builds on local disk when ``root`` looks
            like a network or DFS share, and writes in place otherwise. Staging
            keeps parquet encoding *and the fsync* off the wire; only finished
            bytes cross, as one streamed copy.

            Pass a path to choose the directory yourself (``r"C:\\temp"``), or
            ``None`` to always write in place.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        compression: ParquetCompression = "zstd",
        row_group_size: int | None = DEFAULT_ROW_GROUP_SIZE,
        fsync: bool = True,
        create_attempts: int = DEFAULT_CREATE_ATTEMPTS,
        create_backoff: float = DEFAULT_CREATE_BACKOFF,
        staging_dir: str | os.PathLike[str] | Literal["auto"] | None = "auto",
    ) -> None:
        if create_attempts < 1:
            raise ValueError("create_attempts must be at least 1")
        self.root = Path(root)
        self.compression = compression
        self.row_group_size = row_group_size
        self.fsync = fsync
        self.create_attempts = create_attempts
        self.create_backoff = create_backoff
        self.staging_dir = self._resolve_staging_dir(staging_dir, self.root)

    # ------------------------------------------------------------- placement

    @classmethod
    def _resolve_staging_dir(
        cls,
        staging_dir: str | os.PathLike[str] | Literal["auto"] | None,
        root: Path,
    ) -> Path | None:
        """Turn the ``staging_dir`` argument into a directory, or ``None``.

        ``"auto"`` stages locally exactly when the root looks remote. Staging a
        local cache would only add a copy.
        """
        if staging_dir == "auto":
            if not cls._looks_remote(root):
                return None
            return Path(tempfile.gettempdir()) / STAGING_SUBDIR
        return Path(staging_dir) if staging_dir is not None else None

    @staticmethod
    def _looks_remote(root: Path) -> bool:
        """Whether ``root`` appears to live on a network or DFS share.

        Two shapes on Windows, and both matter because callers use both: a UNC
        path, and a mapped drive letter, which looks local until you ask the OS.
        Deliberately conservative — anything unclassifiable is treated as local.
        """
        if os.name != "nt":  # pragma: no cover - exercised on the Windows CI job
            return False
        try:
            resolved = str(root.resolve())
        except OSError:  # pragma: no cover - unreachable path, offline share
            resolved = str(root)
        if resolved.startswith("\\\\"):
            return True
        drive = os.path.splitdrive(resolved)[0]
        if not drive:
            return False
        try:  # pragma: no cover - Windows-only
            import ctypes

            # getattr, not `ctypes.windll`: `windll` is absent from the stubs
            # off Windows and present on it, so a literal attribute access needs
            # an ignore that mypy then flags as *unused* on the one platform
            # this runs.
            windll = getattr(ctypes, "windll")  # noqa: B009
            return bool(windll.kernel32.GetDriveTypeW(f"{drive}\\") == DRIVE_REMOTE)
        except Exception:  # pragma: no cover - defensive
            return False

    # ------------------------------------------------------------ generations

    def _dir(self, key: CacheKey) -> Path:
        return self.root / key.shard / key.digest

    @staticmethod
    def _manifest_path(directory: Path, generation: int) -> Path:
        if generation == 0:
            return directory / LEGACY_MANIFEST
        stamp = str(generation).zfill(GENERATION_DIGITS)
        return directory / f"{MANIFEST_PREFIX}{stamp}{MANIFEST_SUFFIX}"

    @staticmethod
    def _data_path(directory: Path, generation: int) -> Path:
        if generation == 0:
            return directory / LEGACY_DATA
        stamp = str(generation).zfill(GENERATION_DIGITS)
        return directory / f"{DATA_PREFIX}{stamp}{DATA_SUFFIX}"

    @staticmethod
    def _generations(directory: Path) -> list[int]:
        """Every generation with a manifest file, newest first.

        Name-based rather than content-based: finding the candidates must not
        cost a read of each one.
        """
        try:
            entries = list(directory.iterdir())
        except OSError:
            return []
        found: list[int] = []
        for entry in entries:
            name = entry.name
            if not (
                name.startswith(MANIFEST_PREFIX) and name.endswith(MANIFEST_SUFFIX)
            ):
                continue
            token = name[len(MANIFEST_PREFIX) : -len(MANIFEST_SUFFIX)]
            if token.isdigit():
                found.append(int(token))
        return sorted(found, reverse=True)

    @staticmethod
    def _load(path: Path) -> Manifest | None:
        """Read a manifest, or ``None`` if this generation is not real.

        "Not real" covers the file being absent and the file being incomplete.
        The second is the important one and is what replaces the atomic rename:
        a manifest interrupted mid-write cannot parse as JSON — the closing
        brace is what is missing — so the generation is skipped and the reader
        falls back to the previous one.

        Only structural failures are swallowed. A manifest from a *future*
        format version is a real error and still raises: guessing at a layout
        this build does not understand is how a cache silently serves wrong
        data.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return Manifest.from_json(raw)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, ValueError) and "format_version" in str(error):
                raise
            return None

    def _current(self, directory: Path) -> tuple[int, Manifest] | None:
        """The newest generation whose manifest is complete."""
        for generation in self._generations(directory):
            manifest = self._load(self._manifest_path(directory, generation))
            if manifest is not None:
                return generation, manifest
        legacy = self._load(directory / LEGACY_MANIFEST)
        if legacy is not None:
            return 0, legacy
        return None

    # -------------------------------------------------------------- protocol

    def read_manifest(self, key: CacheKey) -> Manifest | None:
        current = self._current(self._dir(key))
        return current[1] if current is not None else None

    def scan(self, key: CacheKey) -> pl.LazyFrame | None:
        directory = self._dir(key)
        current = self._current(directory)
        if current is None:
            return None
        path = self._data_path(directory, current[0])
        if not path.exists():
            # A generation with no data file is a schema-less empty write: the
            # coverage claim is the whole content.
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
        """Publish a new generation.

        ``manifest_first`` is accepted for the protocol and deliberately unused.
        It exists to order the two halves of an in-place update so the
        interruptible middle state under-claims; publishing a fresh generation
        has no such middle state, because nothing is visible until the manifest
        lands and the manifest is always last. Growing and shrinking updates are
        therefore the same operation here.
        """
        directory = self._dir(key)
        directory.mkdir(parents=True, exist_ok=True)

        current = self._current(directory)
        generation = self._next_generation(directory, current[0] if current else 0)

        if frame.width:
            self._create(
                self._data_path(directory, generation),
                lambda tmp: frame.write_parquet(
                    tmp,
                    compression=self.compression,
                    row_group_size=self.row_group_size,
                ),
            )

        def write_manifest(tmp: Path) -> None:
            tmp.write_text(manifest.to_json(), encoding="utf-8")

        # Last, always: this is the step that makes the generation visible.
        self._create(self._manifest_path(directory, generation), write_manifest)
        self._cleanup(directory, generation)

    def delete(self, key: CacheKey) -> None:
        shutil.rmtree(self._dir(key), ignore_errors=True)

    def digests(self) -> Iterator[str]:
        if not self.root.exists():
            return
        for shard in sorted(self.root.iterdir()):
            if not shard.is_dir():
                continue
            for entry in sorted(shard.iterdir()):
                if self._generations(entry) or (entry / LEGACY_MANIFEST).exists():
                    yield entry.name

    # ----------------------------------------------------------------- write

    def _next_generation(self, directory: Path, current: int) -> int:
        """The next free generation number.

        Skips any number already taken, including one left by a writer whose
        manifest is unparseable — reusing it would overwrite the evidence and,
        worse, be a replace.
        """
        taken = set(self._generations(directory))
        generation = max([current, *taken], default=0) + 1
        while generation in taken:
            generation += 1
        return generation

    def _create(self, target: Path, produce: Callable[[Path], None]) -> None:
        """Put a file at ``target``, which must not already exist.

        Either written straight there, or built in ``staging_dir`` and copied.
        No rename either way: ``target`` carries a generation number nothing is
        looking for yet, so a reader cannot see it half-written, and the file
        only starts mattering once this generation's manifest exists.
        """
        if self.staging_dir is None:
            self._attempt(lambda: self._produce_and_flush(target, produce, strict=True))
            return

        self.staging_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=self.staging_dir, prefix=f".{target.name}.", suffix=".tmp"
        )
        os.close(descriptor)
        built = Path(name)
        try:
            # Encode and flush on local disk; only finished bytes cross the wire.
            self._produce_and_flush(built, produce, strict=True)
            self._attempt(lambda: self._copy_and_flush(built, target))
        except BaseException:
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)
            raise
        finally:
            with contextlib.suppress(OSError):
                built.unlink(missing_ok=True)

    def _produce_and_flush(
        self, target: Path, produce: Callable[[Path], None], *, strict: bool
    ) -> None:
        try:
            produce(target)
            if self.fsync:
                self._flush(target, strict=strict)
        except BaseException:
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)
            raise

    def _copy_and_flush(self, built: Path, target: Path) -> None:
        try:
            # One streamed copy of a finished file, rather than the many small
            # writes parquet encoding would otherwise push across the wire.
            shutil.copyfile(built, target)
            if self.fsync:
                self._flush(target, strict=False)
        except BaseException:
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)
            raise

    def _attempt(self, action: Callable[[], None]) -> None:
        """Run ``action``, riding out transient sharing violations."""
        delay = self.create_backoff
        last: OSError | None = None
        for attempt in range(1, self.create_attempts + 1):
            try:
                action()
                return
            except PermissionError as error:
                # WinError 5 (access denied) and 32 (sharing violation) both
                # land here, and on a share both are usually momentary.
                last = error
                if attempt == self.create_attempts:
                    break
                time.sleep(delay)
                delay *= 2
        raise PermissionError(
            f"could not create a file after {self.create_attempts} attempt(s): "
            f"{last}. Unlike replacing a file, creating one needs no delete "
            "rights — so this points at write permission on the directory, or "
            "a scanner holding new files open. Raise create_attempts if the "
            "holder is slow rather than permanent."
        ) from last

    @staticmethod
    def _flush(path: Path, *, strict: bool) -> None:
        """Force a file's contents to storage.

        Opened ``"r+b"``, not ``"rb"``: the descriptor handed to fsync must be
        *writable*. POSIX permits fsync on a read-only descriptor, but Windows'
        ``os.fsync`` is ``_commit()``, which rejects a read-only handle with
        EBADF.

        ``strict`` distinguishes the two call sites, and they must not be
        conflated. Flushing where the file is *built* establishes durability and
        a failure there is real. Flushing a copy that has landed on a share is
        best-effort: its source was already durable locally, nothing reads the
        file until this generation's manifest exists, and a network redirector
        is entitled to refuse fsync outright.
        """
        try:
            with open(path, "r+b") as written:
                written.flush()
                os.fsync(written.fileno())
        except OSError as error:
            if not strict:
                return
            raise OSError(
                f"could not flush {path.name} in {path.parent}: {error}. A "
                "filesystem that refuses fsync is almost always a network or "
                'DFS share; build on local disk instead (staging_dir=r"C:\\temp", '
                'or leave it "auto"), or pass fsync=False to give up the crash '
                "guarantee deliberately."
            ) from error

    def _cleanup(self, directory: Path, published: int) -> None:
        """Drop superseded generations. Entirely best-effort.

        A file that cannot be deleted — held open by a reader, or a share
        without delete rights — costs disk space and nothing else. That is the
        whole reason publishing is a create: cleanup is allowed to fail.
        """
        for generation in self._generations(directory):
            if generation > published - 1 - KEEP_GENERATIONS:
                continue
            with contextlib.suppress(OSError):
                self._manifest_path(directory, generation).unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                self._data_path(directory, generation).unlink(missing_ok=True)
        if published > KEEP_GENERATIONS:
            # The pre-generational pair, once it is far enough behind.
            for legacy in (directory / LEGACY_MANIFEST, directory / LEGACY_DATA):
                with contextlib.suppress(OSError):
                    legacy.unlink(missing_ok=True)
