"""Staging a build on local disk, chosen for you when the root looks remote.

Two failures on a real DFS share motivated this, and they are separate:

1. Without staging, the temp file is built *beside the target* — on the share —
   so parquet encoding and, worse, ``os.fsync`` run over the wire. A network
   redirector may simply refuse the fsync, and the write dies. Callers had to
   know to pass ``staging_dir`` to avoid it, which is knowledge the library
   should have rather than the caller.

2. Even *with* staging, the publish step fsync'd the copy it had just landed on
   the share — so one network fsync survived the fix that was supposed to
   eliminate it.

The rules below: ``"auto"`` stages locally exactly when the root looks remote
(never for a local cache, where it would turn a free rename into a copy), the
build-side fsync stays strict but explains itself, and the publish-side fsync
is best-effort because the share never really offered durability anyway.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

import pytest

from timeseries_cache.backends.parquet import (
    STAGING_SUBDIR,
    ParquetBackend,
)


class TestChoosingAStagingDirectory:
    def test_a_local_root_stages_nowhere(self, tmp_path):
        """The default must not pessimise the common case: building beside the
        target keeps the publish a same-volume rename with nothing to copy."""
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

    def test_none_forces_building_beside_the_target(self, tmp_path, monkeypatch):
        """An explicit opt-out, even on a share. Someone with a fast local
        share and a slow C: drive is entitled to make that call."""
        monkeypatch.setattr(
            ParquetBackend, "_looks_remote", staticmethod(lambda _: True)
        )
        assert ParquetBackend(tmp_path, staging_dir=None).staging_dir is None

    def test_detection_is_conservative_off_windows(self, tmp_path):
        """UNC paths and mapped drives are Windows shapes. Everywhere else the
        answer is "local", so behavior is exactly what it has always been."""
        if os.name != "nt":
            assert ParquetBackend._looks_remote(tmp_path) is False
            assert ParquetBackend._looks_remote(Path(r"\\server\share")) is False

    @pytest.mark.skipif(os.name != "nt", reason="UNC paths are a Windows shape")
    def test_a_unc_root_reads_as_remote(self):  # pragma: no cover - Windows only
        assert ParquetBackend._looks_remote(Path(r"\\server\share\cache")) is True


class TestTheFsyncThatCrossedTheWire:
    """Bug 2: staging moved the build off the share but left one fsync on it."""

    @staticmethod
    def _cross_volume(monkeypatch) -> None:
        """Make any cross-directory rename raise EXDEV, as a real
        local-disk-to-share rename does."""
        real_replace = os.replace

        def replace(src, dst, *args, **kwargs):
            if Path(src).parent != Path(dst).parent:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)

    def test_a_share_that_refuses_fsync_does_not_fail_the_write(
        self, tmp_path, monkeypatch
    ):
        """The reported error. SMB and DFS both refuse fsync on some servers,
        and the write has to survive it: the bytes came from a source already
        flushed locally, and atomicity rests on the rename, not this call."""
        self._cross_volume(monkeypatch)
        staging = tmp_path / "local"
        share = tmp_path / "share"
        share.mkdir()
        target = share / "data.parquet"

        real_flush = ParquetBackend._flush

        def refuse_on_the_share(path: Path) -> None:
            if Path(path).parent == share:
                raise OSError(errno.EBADF, "Bad file descriptor")
            real_flush(path)

        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(refuse_on_the_share))

        ParquetBackend._atomic_write(
            target, lambda p: p.write_bytes(b"payload"), staging_dir=staging
        )

        assert target.read_bytes() == b"payload"
        assert list(share.iterdir()) == [target], "no temp left on the share"
        assert not list(staging.iterdir()), "no temp left in staging"

    def test_the_local_build_is_still_flushed(self, tmp_path, monkeypatch):
        """Tolerating the remote fsync must not quietly drop the local one —
        that is the flush the crash guarantee actually rests on."""
        self._cross_volume(monkeypatch)
        staging = tmp_path / "local"
        share = tmp_path / "share"
        share.mkdir()

        flushed: list[Path] = []
        real_flush = ParquetBackend._flush

        def recording(path: Path) -> None:
            flushed.append(Path(path))
            real_flush(path)

        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(recording))

        ParquetBackend._atomic_write(
            share / "data.parquet", lambda p: p.write_bytes(b"x"), staging_dir=staging
        )

        assert any(p.parent == staging for p in flushed), (
            "the file built on local disk must still be fsync'd"
        )


class TestTheBuildSideFsyncStaysStrict:
    def test_a_build_directory_that_refuses_fsync_raises(self, tmp_path, monkeypatch):
        """Unlike the publish-side flush. If this one fails the data genuinely
        may not be on disk, and silently continuing would be the exact silent
        hole invariant 5 exists to forbid."""

        def refuse(path):
            raise OSError(errno.EBADF, "Bad file descriptor")

        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(refuse))

        with pytest.raises(OSError, match="could not flush"):
            ParquetBackend._atomic_write(
                tmp_path / "data.parquet", lambda p: p.write_bytes(b"x")
            )

    def test_the_error_says_what_to_do_about_it(self, tmp_path, monkeypatch):
        """A bare EBADF sent the user round three rounds of diagnosis. The
        message has to name the cause and the fix."""

        def refuse(path):
            raise OSError(errno.EBADF, "Bad file descriptor")

        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(refuse))

        with pytest.raises(OSError) as caught:
            ParquetBackend._atomic_write(
                tmp_path / "data.parquet", lambda p: p.write_bytes(b"x")
            )

        message = str(caught.value)
        assert "staging_dir" in message
        assert "fsync=False" in message

    def test_a_failed_flush_leaves_no_temp_behind(self, tmp_path, monkeypatch):
        def refuse(path):
            raise OSError(errno.EBADF, "Bad file descriptor")

        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(refuse))

        with pytest.raises(OSError):
            ParquetBackend._atomic_write(
                tmp_path / "data.parquet", lambda p: p.write_bytes(b"x")
            )

        assert list(tmp_path.iterdir()) == []


class TestTheWholeCacheOverASimulatedShare:
    def test_a_round_trip_survives_a_share_that_refuses_fsync(
        self, tmp_path, monkeypatch
    ):
        """End to end, through the cache rather than the backend's internals:
        write, merge, read, delete — with every network fsync refused."""
        import polars as pl

        from timeseries_cache import TimeseriesCache

        from .conftest import frame

        share = tmp_path / "share"
        share.mkdir()
        real_replace = os.replace

        def replace(src, dst, *args, **kwargs):
            if Path(src).parent != Path(dst).parent:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(src, dst, *args, **kwargs)

        real_flush = ParquetBackend._flush

        def refuse_on_the_share(path: Path) -> None:
            if share in Path(path).parents:
                raise OSError(errno.EBADF, "Bad file descriptor")
            real_flush(path)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(ParquetBackend, "_flush", staticmethod(refuse_on_the_share))

        series = {"ticker": "AAPL"}
        cache = TimeseriesCache(ParquetBackend(share, staging_dir=tmp_path / "local"))
        cache.write(frame([1, 2]), **series)
        cache.write(frame([3]), **series)

        assert cache.read(**series).frame.height == 3

        cache.delete(**series)
        assert cache.read(**series).frame.height == 0
        assert isinstance(cache.read(**series).frame, pl.DataFrame)

    def test_nothing_is_left_in_the_staging_directory(self, tmp_path, monkeypatch):
        from timeseries_cache import TimeseriesCache

        from .conftest import frame

        real_replace = os.replace

        def replace(src, dst, *args, **kwargs):
            if Path(src).parent != Path(dst).parent:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)

        share = tmp_path / "share"
        share.mkdir()
        staging = tmp_path / "local"
        cache = TimeseriesCache(ParquetBackend(share, staging_dir=staging))
        for day in range(1, 4):
            cache.write(frame([day]), ticker="AAPL")

        assert not list(staging.iterdir())
