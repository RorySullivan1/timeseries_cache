# 2026-08-03 15:50 · network-staging-and-null-typing

**Goal:** make writes survive a DFS-share root, and stop null columns from
corrupting a key's schema. Both reported from real use; both on PR #5.

## 1. Network / DFS share writes

User's `root` was a DFS shared drive, and the Windows write path kept failing
after the three portability fixes had already landed. They have delete rights,
which rules out the ACL cause, so the working theory is a transient holder plus
the many small writes parquet encoding pushes over SMB.

Two changes, both on `ParquetBackend`:

- **`staging_dir`** — build and fsync on local disk, then publish. The
  constraint that shapes everything: **a rename cannot cross volumes.**
  `os.replace` raises `EXDEV` rather than silently copying. So publishing is two
  steps — one streamed copy to a temp *beside* the target, then a same-volume
  rename. Copying straight onto the target would be simpler and is the one
  publish shape where a reader can observe a half-written parquet. Deliberately
  not done. Same-volume builds detect the absence of `EXDEV` and rename directly.
- **`replace_attempts` / `replace_backoff`** — retry the rename with doubling
  backoff. Safe because `os.replace` either happened or it didn't and the source
  survives either way. On exhaustion the error names attempts, elapsed, and what
  failing *every* attempt implies (permanent holder, or no delete rights — a
  rename **is** a delete) instead of a bare "Access is denied".

**Still inference.** The user never ran the diagnostic script, so the actual
cause on their share is unconfirmed. This targets the most probable one.

## 2. All-null columns must not vote on the schema

User: "the schema detected for nulls yields a string vs the value I need — I'd
like the cache schema to default to the stored cache and force new writes to use
that schema."

**Right instinct, dangerous as stated.** Answered no to the general form and
implemented the narrow one. The evidence, measured rather than assumed:

- pandas types `[None, None]` as `object`, which arrives as polars `String`.
  That is where the reported artifact comes from.
- Casting an **all-null** column is lossless in every direction, for every dtype.
- Casting a **populated** column leniently turns `'abc'` into `null` silently.
- Parquet round-trips the `Null` dtype, so "unknown" is storable.

So: **an all-null column never votes; a column with values is never cast.**
Reconciliation runs *before* the schema check, so the artifact never reads as a
schema change. Where nothing is stored yet the column stays `Null` — honestly
not yet known — and `_merge` promotes it when a write finally carries values
(the existing rows are all null, so the cast invents nothing).

The line matters more than the feature: a *partially* null column still raises.
`tests/test_null_typing.py` holds both halves; a change that makes the second
pass silently is data loss, not a relaxation.

## Gotchas & dead ends
- Wrote a hollow test (`test_it_works_whatever_the_stored_type_is`) that asserted
  nothing — it passed against a deliberately broken build. Rewrote as a real
  parametrized case over five dtypes. Every test here was verified to fail with
  reconciliation disabled before being accepted.
- Generalizing to "conform to the stored schema" is the obvious next step and is
  exactly wrong. The rule is safe *because* it rests on a checkable property
  (all-null casts are lossless), not because conforming sounds tidy.

## State at end
- PR #5 merged as `0812fdc` (squash); all six CI jobs green. 379 tests, ruff +
  mypy clean; main re-verified after the merge. Local branch deleted.
- `staging_dir`, `replace_attempts`, `replace_backoff` documented in
  `python/README.md` and tutorial 05; null-typing rule in CLAUDE.md, README, and
  tutorial 05.

## Open threads
- DFS write failure **unconfirmed fixed** — user has not run `diagnose_windows.py`.
- Merged branches still on origin; deletion 403s from this environment.
- Property-based tests for the interval algebra, per-key locking, second language
  port all still open.
