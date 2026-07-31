# 2026-07-31 21:23 · windows-portability

**Goal:** fix three Windows-only failures in the atomic write path

Reported by the user in three terse rounds ("bad file descriptor on os.fsync",
"moved to line 164 with os.O_RDONLY", "permission denied targeting the temp
file"). All three came from the same root cause family: **POSIX and Windows
disagree about open files**, and CI is ubuntu-only so none were reachable here.

## The three bugs, in order of consequence
1. **`write()` held a lazy scan over the file the backend was about to replace.**
   POSIX allows replacing an open file — the old inode survives its last handle —
   Windows refuses with `PermissionError`/WinError 5. So *every write after the
   first* failed on Windows. Fix: `del existing` once the merge has materialized.
2. **The cleanup path masked the real error.** `except BaseException:
   tmp_path.unlink(...)` — unlinking a file with an open handle raises
   PermissionError on Windows, and an exception raised *inside* an except block
   **replaces** the one being handled. So the user kept seeing "permission
   denied" on a temp file while the actual cause was discarded. This is why the
   reported fault appeared to "move" between rounds. Fix: `contextlib.suppress`.
3. **`os.fsync` got a read-only descriptor** (`open(tmp, "rb")`). POSIX permits
   it; Windows `os.fsync` is `_commit()` and rejects a read-only CRT handle with
   EBADF. Fix: `"r+b"`. Separately, `_fsync_dir` left `os.close` in an unguarded
   `finally`, so a platform quirk could break an otherwise-good write.

## How to test a platform you can't run
Every fix asserts the *invariant* rather than waiting for the failure, which is
the only way to get coverage from an ubuntu runner:
- Scan release: hold a `weakref` to every LazyFrame the backend hands out and
  assert none survive into `write()`. Works on any platform.
- fsync descriptor: monkeypatch `os.fsync`, record each fd's access mode via
  `fcntl`, require the *regular-file* ones to be writable. The directory fd is
  exempt — a directory can't be opened for writing, which is exactly why
  `_fsync_dir` is best-effort.
- Directory fsync: parametrize over open/fsync/close, force each to raise EBADF,
  assert the write still lands and leaves no temp file.

Each was verified to fail against the pre-fix code before being accepted.

## Gotchas & dead ends
- **Guessed twice before getting it right.** Round 1 (read-only fsync) I verified
  mechanically and was correct. Round 2 I reasoned about `_fsync_dir` and shipped
  a real but probably-unrelated fix — the reported line number didn't even match
  my file, which should have been the signal to ask for the traceback instead of
  inferring. Lesson: when the reported location doesn't match the code in front
  of you, stop and ask.
- The masked-exception bug (#2) is what made the earlier rounds so hard to
  diagnose. Fixing error *visibility* early would have found #1 faster than
  fixing suspected causes one at a time.
- `del existing` alone suffices; `existing = None; del existing` is redundant.
- `pull_request_read(get_status)` still reports "pending"/`total_count: 0` — it's
  the legacy commit-status API. Use `get_check_runs` + `mergeable_state`.

## State at end
- PR #3 merged as `4612191`; main verified green at 340 tests, ruff + mypy clean.
  Local branch deleted, subscription and check-in cancelled.

## Open threads
- **Add `windows-latest` to the CI matrix.** Three Windows bugs in three rounds,
  one breaking the core write path, all found by inspection from one-line reports.
  Two lines in `ci.yml`; expect it to surface more path/filesystem differences.
- Unverified: whether polars holds the parquet file open beyond the LazyFrame's
  lifetime. If the user still hits PermissionError on replace, that's next.
- All three merged branches still on origin — deletion 403s from this environment.
