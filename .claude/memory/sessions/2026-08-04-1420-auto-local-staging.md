# 2026-08-04 14:20 · auto-local-staging

**Goal:** the DFS fsync error came back. PR #8, merged; follow-up fix pushed
straight to main as `0856258`.

## Why the #5 staging work hadn't fixed it

Two reasons, and only the first was known:

1. **`staging_dir` defaulted to `None`.** Unless a caller knew to pass it, the
   temp was built beside the target — on the share — and fsync'd there. That is
   knowledge the library should carry, not the caller. Now defaults to `"auto"`.
2. **`_publish` fsync'd the copy it had just landed *on the share*.** One
   network fsync survived the fix meant to remove it, on exactly the path a
   share user takes. This is why staging looked like it hadn't worked.

Fix 2 is the interesting one: I shipped #5 believing staging moved *every*
flush off the wire, and never grepped for the second call site. **`grep -n
_flush` would have found it in five seconds.** When claiming a fix removes a
class of operation from a path, enumerate the call sites rather than reasoning
about the one you just edited.

## The rule that came out of it

Two fsyncs, two opposite rules, and conflating them is what caused the bug:

| flush | rule | why |
|---|---|---|
| build-side (`_atomic_write`) | **strict** | where durability is established |
| publish-side (`_publish`) | **suppressed** | source already durable locally; atomicity is the rename |

Now written into CLAUDE.md so any new fsync call site has to answer which it is.

`"auto"` detection: UNC path, or `GetDriveTypeW == DRIVE_REMOTE` via `ctypes`
(no pywin32). Windows-only, conservative — anything unclassifiable reads as
local, so a local cache keeps its free same-volume rename.

## Gotchas & dead ends
- **`ctypes.windll` is absent from the stubs off Windows and present on it**, so
  the `type: ignore[attr-defined]` mypy needs on Linux is flagged as *unused* on
  the only platform the code runs. All three Windows CI jobs failed on it.
  `getattr(ctypes, "windll")  # noqa: B009` type-checks the same on both.
  A green local Linux run is **not evidence** for platform-conditional code.
- Ran `ruff check --select RUF100 .` to test whether that noqa was unused — it
  reported yes, because `--select` in isolation disables every other rule, so
  B009 counted as "non-enabled". Removed the noqa, broke lint, put it back.
  **Don't test a noqa's necessity by narrowing the rule set.**
- The user merged PR #8 while the mypy fix was still in flight, so `c47bafa`
  landed on main red and main was broken for ~6 minutes. Cherry-picked onto main
  and pushed. A merged PR can't take follow-ups.

## State at end
- main = `0856258`, all six CI jobs green. 499 tests, 1 skipped (Windows-only
  UNC assertion, which does run on the Windows jobs).
- PRs #1-#8 all merged.

## Open threads
- **Still unconfirmed on the user's actual share.** Reproduced the shape (EXDEV
  plus a refusing fsync), not their environment. Asked for a traceback if it
  recurs; this is the fourth round on the same symptom.
- `scrAdmin` / gallery variant `CONFIRM_BlankVertical` — not in this repo,
  belongs to another of their repos. Asked which; unanswered.
- Merged branches still on origin; deletion 403s from here.
