"""Writing to a network or DFS share without ever renaming anything.

Four rounds of failures on a real share drove this, and each one was a different
way for the same idea to break: *publish by replacing the live file*. In order,
they were an fsync on the share refused outright, an fsync on a read-only
descriptor, a rename refused because Windows would not replace a held file, and
a rename refused as ``ERROR_NOT_SAME_DEVICE`` when the build was staged onto
another volume.

Patching them one at a time was the mistake. Replacing a file needs the target
to be unheld, deletable, and on the same volume — three things a share is
entitled to deny — while *creating* a file needs none of them. So a write now
publishes a new numbered generation and the newest valid manifest wins.

What that buys, and what these tests hold:

* no rename or replace anywhere in the write path;
* a half-written file is invisible rather than corrupting, because a generation
  is not real until its manifest parses;
* cleanup may fail freely — a share that refuses deletes costs disk, not writes.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

import pytest

from timeseries_cache import TimeseriesCache
from timeseries_cache.backends.parquet import (
    DATA_PREFIX,
    MANIFEST_PREFIX,
    STAGING_SUBDIR,
    ParquetBackend,
)

from .conftest import frame, ts

SERIES = {"ticker": "AAPL"}


def only_key(root: Path) -> Path:
    return next(root.glob("*/*"))


class TestNothingIsEverRenamed:
    """The property the whole layout exists for."""

    def test_a_write_never_calls_os_replace(self, tmp_path, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError(f"os.replace called: {args}")

        monkeypatch.setattr(os, "replace", forbidden)

        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1, 2]), **SERIES)
        cache.write(frame([3, 4]), **SERIES)
        cache.delete(start=ts(1), end=ts(1), **SERIES)

        assert cache.read(**SERIES).frame.height == 3

    def test_a_write_never_calls_os_rename(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            os,
            "rename",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("rename")),
        )
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1]), **SERIES)
        assert cache.read(**SERIES).frame.height == 1

    def test_a_second_write_touches_none_of_the_first_write_s_files(self, tmp_path):
        """The reason a half-written file cannot corrupt anything: publishing
        writes names that did not exist, so no reader is looking at them."""
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1]), **SERIES)
        directory = only_key(tmp_path / "cache")
        before = {p.name: p.read_bytes() for p in directory.iterdir()}

        cache.write(frame([2]), **SERIES)

        after = {p.name for p in directory.iterdir()}
        assert after - set(before), "the second write created new files"
        for name, content in before.items():
            if name in after:
                assert (directory / name).read_bytes() == content, (
                    f"{name} was modified in place"
                )


class TestGenerations:
    def test_the_newest_generation_serves(self, tmp_path):
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1]), **SERIES)
        cache.write(frame([2]), **SERIES)

        directory = only_key(tmp_path / "cache")
        assert max(ParquetBackend._generations(directory)) == 2
        assert cache.read(**SERIES).frame.height == 2

    def test_an_unparseable_newest_generation_is_skipped(self, tmp_path):
        """The property that replaces the atomic rename: an interrupted write
        leaves a generation that cannot be believed, so it is not."""
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1]), start=ts(1), end=ts(1), **SERIES)
        cache.write(frame([2]), start=ts(2), end=ts(2), **SERIES)

        directory = only_key(tmp_path / "cache")
        newest = max(ParquetBackend._generations(directory))
        ParquetBackend._manifest_path(directory, newest).write_text('{"digest": ')

        reopened = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        assert reopened.read(**SERIES).frame.height == 1, "fell back a generation"
        # Bounded, because an unbounded read spans only the coverage hull and so
        # can never report a gap. The point is that the lost window is *unknown*
        # again rather than claimed-and-empty.
        assert reopened.read(start=ts(1), end=ts(2), **SERIES).missing

    def test_an_orphaned_data_file_is_invisible(self, tmp_path):
        """Rows with no manifest claiming them are not part of the cache."""
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1]), **SERIES)

        directory = only_key(tmp_path / "cache")
        orphan = ParquetBackend._data_path(directory, 99)
        orphan.write_bytes(b"not even parquet")

        assert cache.read(**SERIES).frame.height == 1

    def test_a_superseded_generation_is_cleaned_up(self, tmp_path):
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        for day in range(1, 6):
            cache.write(frame([day]), **SERIES)

        directory = only_key(tmp_path / "cache")
        assert len(ParquetBackend._generations(directory)) <= 2, (
            "steady state is the live generation plus one kept for readers"
        )

    def test_cleanup_failing_does_not_fail_the_write(self, tmp_path, monkeypatch):
        """The point of publishing by create: a share that refuses deletes —
        locked file, or an ACL without delete rights — costs disk, not writes."""
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        for day in (1, 2):
            cache.write(frame([day]), **SERIES)

        real_unlink = Path.unlink

        def refuse(self, *args, **kwargs):
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(Path, "unlink", refuse)

        cache.write(frame([3]), **SERIES)  # must not raise
        monkeypatch.setattr(Path, "unlink", real_unlink)

        assert cache.read(**SERIES).frame.height == 3

    def test_a_generation_number_is_never_reused(self, tmp_path):
        """Reusing one would overwrite the evidence of a failed write — and be
        a replace, which is the thing this design does not do."""
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1]), **SERIES)

        directory = only_key(tmp_path / "cache")
        # A wedged generation from the future, with a manifest that won't parse.
        ParquetBackend._manifest_path(directory, 7).write_text("{ truncated")

        cache.write(frame([2]), **SERIES)
        assert ParquetBackend._manifest_path(directory, 7).read_text() == "{ truncated"
        assert max(ParquetBackend._generations(directory)) > 7


class TestReadingAnOlderLayout:
    def test_a_pre_generational_cache_still_reads(self, tmp_path):
        """Caches written before this layout must not become unreadable."""
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1, 2]), start=ts(1), end=ts(2), **SERIES)

        directory = only_key(tmp_path / "cache")
        generation = max(ParquetBackend._generations(directory))
        ParquetBackend._manifest_path(directory, generation).rename(
            directory / "manifest.json"
        )
        ParquetBackend._data_path(directory, generation).rename(
            directory / "data.parquet"
        )

        reopened = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        assert reopened.read(**SERIES).frame.height == 2
        assert reopened.read(start=ts(1), end=ts(2), **SERIES).is_complete

    def test_it_migrates_on_the_next_write(self, tmp_path):
        cache = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        cache.write(frame([1]), **SERIES)
        directory = only_key(tmp_path / "cache")
        generation = max(ParquetBackend._generations(directory))
        ParquetBackend._manifest_path(directory, generation).rename(
            directory / "manifest.json"
        )
        ParquetBackend._data_path(directory, generation).rename(
            directory / "data.parquet"
        )

        reopened = TimeseriesCache(ParquetBackend(tmp_path / "cache"))
        reopened.write(frame([2]), **SERIES)
        reopened.write(frame([3]), **SERIES)

        assert reopened.read(**SERIES).frame.height == 3
        assert not (directory / "manifest.json").exists(), "legacy pair cleaned up"


class TestChoosingAStagingDirectory:
    def test_a_local_root_stages_nowhere(self, tmp_path):
        """Writing in place is right for a local cache — there is nothing to
        gain from a copy when the target name is already unique."""
        assert ParquetBackend(tmp_path).staging_dir is None

    def test_a_remote_looking_root_stages_under_the_system_temp_dir(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            ParquetBackend, "_looks_remote", staticmethod(lambda _: True)
        )
        backend = ParquetBackend(tmp_path)
        assert backend.staging_dir == Path(tempfile.gettempdir()) / STAGING_SUBDIR

    def test_an_explicit_directory_wins_over_auto_detection(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            ParquetBackend, "_looks_remote", staticmethod(lambda _: True)
        )
        chosen = tmp_path / "c_temp"
        assert ParquetBackend(tmp_path, staging_dir=chosen).staging_dir == chosen

    def test_none_forces_writing_in_place(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ParquetBackend, "_looks_remote", staticmethod(lambda _: True)
        )
        assert ParquetBackend(tmp_path, staging_dir=None).staging_dir is None

    def test_detection_is_conservative_off_windows(self, tmp_path):
        if os.name != "nt":
            assert ParquetBackend._looks_remote(tmp_path) is False
            assert ParquetBackend._looks_remote(Path(r"\\server\share")) is False

    @pytest.mark.skipif(os.name != "nt", reason="UNC paths are a Windows shape")
    def test_a_unc_root_reads_as_remote(self):  # pragma: no cover - Windows only
        assert ParquetBackend._looks_remote(Path(r"\\server\share\cache")) is True


class TestStaging:
    def test_the_build_happens_locally_and_one_copy_crosses(
        self, tmp_path, monkeypatch
    ):
        import shutil

        copies: list[tuple[Path, Path]] = []
        real_copyfile = shutil.copyfile

        def recording(src, dst, *args, **kwargs):
            copies.append((Path(src), Path(dst)))
            return real_copyfile(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copyfile", recording)

        staging = tmp_path / "local"
        share = tmp_path / "share"
        cache = TimeseriesCache(ParquetBackend(share, staging_dir=staging))
        cache.write(frame([1]), **SERIES)

        assert copies, "staging should copy finished bytes to the target"
        assert all(src.parent == staging for src, _ in copies)
        assert all(share in dst.parents for _, dst in copies)
        assert not list(staging.iterdir()), "staging is left clean"

    def test_the_copy_lands_under_its_final_name(self, tmp_path):
        """No temp file beside the target and no rename after it: the
        generation number already makes the name unique and unwatched."""
        staging = tmp_path / "local"
        share = tmp_path / "share"
        TimeseriesCache(ParquetBackend(share, staging_dir=staging)).write(
            frame([1]), **SERIES
        )

        names = sorted(p.name for p in only_key(share).iterdir())
        assert all(name.startswith((MANIFEST_PREFIX, DATA_PREFIX)) for name in names), (
            names
        )
        assert not any(name.endswith(".tmp") for name in names)

    def test_the_staging_directory_is_created_if_missing(self, tmp_path):
        staging = tmp_path / "does" / "not" / "exist"
        TimeseriesCache(ParquetBackend(tmp_path / "share", staging_dir=staging)).write(
            frame([1]), **SERIES
        )
        assert staging.is_dir()

    def test_a_failed_build_leaves_the_published_state_intact(self, tmp_path):
        staging = tmp_path / "local"
        share = tmp_path / "share"

        class BuildFails(ParquetBackend):
            armed = False

            def _create(self, target, produce):  # type: ignore[override]
                if self.armed and target.name.startswith(DATA_PREFIX):

                    def explode(_path):
                        raise RuntimeError("boom")

                    produce = explode
                super()._create(target, produce)

        backend = BuildFails(share, staging_dir=staging)
        cache = TimeseriesCache(backend)
        cache.write(frame([1]), **SERIES)

        backend.armed = True
        with pytest.raises(RuntimeError):
            cache.write(frame([2]), **SERIES)

        backend.armed = False
        assert cache.read(**SERIES).frame.height == 1
        assert not list(staging.iterdir()), "no half-built file left behind"


class TestFsyncOnAShare:
    """A network redirector may refuse fsync outright — SMB and DFS both do on
    some servers. Where the flush happens decides whether that is fatal."""

    def test_no_flush_on_the_share_is_ever_strict(self, tmp_path, monkeypatch):
        """The wiring that makes a refusing share survivable.

        Asserted on the *call*, not by faking the outcome: a fake that swallowed
        the error itself would prove nothing, since swallowing is the behavior
        under test. If any share-side flush were strict, this raises.
        """
        staging = tmp_path / "local"
        share = tmp_path / "share"
        real_flush = ParquetBackend._flush
        calls: list[tuple[Path, bool]] = []

        def recording(path: Path, *, strict: bool) -> None:
            calls.append((Path(path), strict))
            if share in Path(path).parents and strict:
                raise AssertionError(f"strict flush on the share: {path}")
            real_flush(path, strict=strict)

        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(recording))

        cache = TimeseriesCache(ParquetBackend(share, staging_dir=staging))
        cache.write(frame([1, 2]), **SERIES)
        cache.write(frame([3]), **SERIES)

        assert cache.read(**SERIES).frame.height == 3
        assert any(share in path.parents for path, _ in calls), "share was flushed"
        assert any(path.parent == staging and strict for path, strict in calls), (
            "the local build must still be flushed strictly"
        )

    def test_the_local_build_is_still_flushed(self, tmp_path, monkeypatch):
        """Tolerating the remote flush must not quietly drop the local one —
        that is the flush the crash guarantee rests on."""
        staging = tmp_path / "local"
        flushed: list[Path] = []
        real_flush = ParquetBackend._flush

        def recording(path: Path, *, strict: bool) -> None:
            flushed.append(Path(path))
            real_flush(path, strict=strict)

        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(recording))

        TimeseriesCache(ParquetBackend(tmp_path / "share", staging_dir=staging)).write(
            frame([1]), **SERIES
        )

        assert any(p.parent == staging for p in flushed)

    def test_a_strict_flush_failure_says_what_to_do(self, tmp_path, monkeypatch):
        """Someone who forces staging off onto a share should get the fix, not
        a bare EBADF — that errno cost three rounds of diagnosis."""
        target = tmp_path / "file"
        target.write_bytes(b"x")

        def refuse(*args, **kwargs):
            raise OSError(errno.EBADF, "Bad file descriptor")

        monkeypatch.setattr(os, "fsync", refuse)

        with pytest.raises(OSError, match="staging_dir") as caught:
            ParquetBackend._flush(target, strict=True)
        assert "fsync=False" in str(caught.value)

    def test_a_best_effort_flush_failure_is_swallowed(self, tmp_path, monkeypatch):
        target = tmp_path / "file"
        target.write_bytes(b"x")

        def refuse(*args, **kwargs):
            raise OSError(errno.EBADF, "Bad file descriptor")

        monkeypatch.setattr(os, "fsync", refuse)

        ParquetBackend._flush(target, strict=False)  # must not raise


class TestTheWholeCacheOverASimulatedShare:
    def test_a_round_trip_survives_every_hostile_thing_a_share_does(
        self, tmp_path, monkeypatch
    ):
        """Refuses fsync, refuses deletes, and would refuse any rename. The
        cache has to work anyway."""
        share = tmp_path / "share"
        staging = tmp_path / "local"
        real_flush = ParquetBackend._flush

        def refuse_flush(path: Path, *, strict: bool) -> None:
            if share in Path(path).parents:
                # What SMB/DFS do. Strict here would be a failed write, which is
                # exactly what must not happen.
                if strict:
                    raise AssertionError(f"strict flush on the share: {path}")
                return
            real_flush(path, strict=strict)

        def refuse_rename(*args, **kwargs):
            raise AssertionError("the write path must not rename")

        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(refuse_flush))
        monkeypatch.setattr(os, "replace", refuse_rename)

        cache = TimeseriesCache(ParquetBackend(share, staging_dir=staging))
        cache.write(frame([1, 2]), start=ts(1), end=ts(2), **SERIES)
        cache.write(frame([3, 4]), start=ts(3), end=ts(4), **SERIES)
        assert cache.read(**SERIES).frame.height == 4

        cache.delete(start=ts(1), end=ts(1), **SERIES)
        result = cache.read(start=ts(1), end=ts(4), **SERIES)
        assert result.frame.height == 3
        assert result.missing, "the deleted window is unknown again, not covered"

        reopened = TimeseriesCache(ParquetBackend(share, staging_dir=staging))
        assert reopened.read(**SERIES).frame.height == 3
        assert not list(staging.iterdir())

    def test_a_share_with_no_delete_rights_at_all_still_works(
        self, tmp_path, monkeypatch
    ):
        """The configuration no rename-based design can survive: create and
        write permitted, delete refused."""
        share = tmp_path / "share"
        cache = TimeseriesCache(ParquetBackend(share, staging_dir=tmp_path / "local"))
        cache.write(frame([1]), **SERIES)

        def refuse(self, *args, **kwargs):
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(Path, "unlink", refuse)

        for day in (2, 3, 4):
            cache.write(frame([day]), **SERIES)

        assert cache.read(**SERIES).frame.height == 4
