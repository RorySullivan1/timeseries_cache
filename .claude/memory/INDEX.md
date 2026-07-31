# MEMORY INDEX  ·  keep ≤ ~80 lines

## State            (rewrite in place — current truth only, ≤ ~10 lines)
- `python/` is complete: 335 tests green, ruff + `mypy --strict` clean on 3.11/3.12/3.13. No other language port.
- PRs #1 and #2 merged; main = `193b392`. PR #3 open on `claude/tutorials` (runnable examples).
- Row identity is `(timestamp, *identity_columns)`; default `()` = timestamp alone, unchanged behavior.
- Remote branch deletion returns 403 from this environment's git proxy — must be done in the GitHub UI.
- CLAUDE.md now describes shipped code, not a plan. All five invariants are implemented.
- Frame layer: polars core + thin pandas facade. Consumers are mostly pandas.
- Claude assets ported from `RorySullivan1/claudeBrain` (`example-project/.claude/` is the source to copy from).

## Decisions        (append-only; supersede, never delete)
- [2026-07-31] Repo is language-partitioned at top level, Python first — each language a self-contained impl of one shared contract — because the deliverable is a copyable template, not a package. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Coverage is tracked in a per-key manifest, separate from stored rows, so "fetched and legitimately empty" is distinguishable from "never fetched". This is the core design bet. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Public interval convention is closed `[start, end]` to match pandas `.loc`; internals use the same convention throughout. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Write modes take an *explicit* window, never one inferred from the incoming data's min/max — that's what makes `replace_window` able to delete stale rows. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Ported python-development/review/maintenance, financial-timeseries-analysis, session-memory, and an adapted python-developer agent; skipped coding-standards (C#/VSTO-heavy) and the python whole-stack brief (redundant). — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Frame layer is **polars core + pandas facade**, not pandas. Decisive reason: `scan_parquet` + pushed-down time predicate on the read path, which is the whole game for a `[start, end]` API, plus no-index removes the write-mode bug class. Asymmetry sealed it — polars→pandas is one boundary call, pandas→polars later means retrofitting the read path. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Two typed facades (`TimeseriesCache` polars / `PandasTimeseriesCache` pandas) rather than a `frame=` param, so return types stay static for the type checker. Consumers are mostly pandas, so the facade is first-class, not an afterthought. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] pandas conversion is numpy-backed by default (no `use_pyarrow_extension_array`): arrow-backed nulls differ from `np.nan` and would silently change downstream `pct_change`/`rolling`/`dropna`. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Time domain is discrete at 1 microsecond; storage dtype is `Datetime("us","UTC")`. This is what makes closed-interval subtraction closable and "touching" well-defined. Sub-microsecond input is rejected, not truncated. — sessions/2026-07-31-1908-python-implementation.md
- [2026-07-31] Kwarg canonicalization is type-tagged (`i:1` vs `s:1`), bool checked before int, sets/dicts rejected. Untagged, `1` and `"1"` would silently share a cache entry. — sessions/2026-07-31-1908-python-implementation.md
- [2026-07-31] Schema checks compare the *live* polars schema of the stored frame; the manifest's schema strings are informational only. A string comparison would make a polars `repr` change invalidate every cache on disk. — sessions/2026-07-31-1908-python-implementation.md
- [2026-07-31] `open_cache`/`open_pandas_cache` live in `__init__.py`, not `core.py`, so `core` never imports a concrete backend while callers still get a one-liner. — sessions/2026-07-31-1908-python-implementation.md
- [2026-07-31] Kwargs canonicalize to a tagged *structure* serialized as JSON, superseding the `name=value&...` string join, which was forgeable: a value containing the separators produced an identical canonical string and served another series' rows. Digests changed as a result. — sessions/2026-07-31-2001-review-fixes.md
- [2026-07-31] Write ordering rule is "the interruptible middle state must under-claim", not "manifest last": growing updates write data first, shrinking ones (`delete`) write the manifest first. — sessions/2026-07-31-2001-review-fixes.md
- [2026-07-31] `DEFAULT_ROW_GROUP_SIZE = 64_000` — row groups are the parquet skip unit and every read is a time range, so finer groups buy ~1.35x on narrow reads for free. This is the read path's real lever. — sessions/2026-07-31-2001-review-fixes.md
- [2026-07-31] Kept `concat + sort` over `merge_sorted` in `_merge`: measured faster in 5 of 6 shapes on polars 1.43 despite both inputs being sorted. Re-measure on a polars upgrade; don't swap on principle. — sessions/2026-07-31-2001-review-fixes.md
- [2026-07-31] Mypy's `python_version` is deliberately unpinned: pinning it to the 3.11 floor makes 3.12/3.13 fail on numpy's own stubs. The 3.11 CI job guarantees the floor instead. Don't re-add the pin. — sessions/2026-07-31-2012-merge-and-ci.md
- [2026-07-31] No `uv.lock` committed, so CI resolves fresh and an upstream release can turn it red with no local change. Intended for a template — early warning beats a lock that hides it. — sessions/2026-07-31-2012-merge-and-ci.md
- [2026-07-31] Row identity is `(timestamp, *identity_columns)`, set per cache instance; the upsert anti-join, sorting, and duplicate detection all key on it. Driven by trade data, where correcting one print must not wipe the others at that instant. — sessions/2026-07-31-2021-composite-row-identity.md
- [2026-07-31] Coverage stays purely time-based under composite identity: identity changes what a *row* is, never what a *range* means. `replace_window`/`delete`/gaps untouched. — sessions/2026-07-31-2021-composite-row-identity.md
- [2026-07-31] The manifest records `identity_columns` and refuses a mismatch either way; missing field reads as timestamp-only, so no FORMAT_VERSION bump was needed. — sessions/2026-07-31-2021-composite-row-identity.md
- [2026-07-31] Tutorials are markdown, superseding the first pass as runnable scripts. `tests/test_tutorials.py` extracts each ```python block, concatenates in document order and executes it, so examples can't rot. Prose claims are written as `assert`s so the test validates the narrative. — sessions/2026-07-31-2037-tutorials.md
- [2026-07-31] `ruff format` formats Python inside markdown fences, so tutorial code is style-checked by the existing format gate — markdown is not outside the linter's reach. — sessions/2026-07-31-2037-tutorials.md

## Threads          (open items; remove when closed)
- Hooks invoke `python`, not `python3`; will silently no-op on a `python3`-only machine.
- PR #3 (tutorials) not merged. Merged branches still on origin — deletion 403s from here, needs the GitHub UI.
- Interval algebra + cache semantics were fuzzed once by hand and came back clean; worth wiring in as property tests rather than a one-off.
- No second language port; the backend protocol + manifest JSON are the intended seam.
- Accepted (not bugs): single-writer per key, whole-key rewrite per write, schema and identity fixed per key.

## Log              (append-only pointers)
- 2026-07-31 1841 | bootstrap CLAUDE.md + claudeBrain asset port | sessions/2026-07-31-1841-bootstrap-claude-md.md
- 2026-07-31 1908 | Python implementation: coverage manifest, write modes, both facades | sessions/2026-07-31-1908-python-implementation.md
- 2026-07-31 2001 | review fixes: key forgery, delete ordering, row-group tuning | sessions/2026-07-31-2001-review-fixes.md
- 2026-07-31 2012 | merge PR #1 to main; CI workflow on PR #2 | sessions/2026-07-31-2012-merge-and-ci.md
- 2026-07-31 2021 | composite row identity for repeating timestamps | sessions/2026-07-31-2021-composite-row-identity.md
- 2026-07-31 2037 | runnable tutorials, tested in CI | sessions/2026-07-31-2037-tutorials.md
