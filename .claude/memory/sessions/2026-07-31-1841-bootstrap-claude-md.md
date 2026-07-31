# 2026-07-31 18:41 · bootstrap-claude-md

**Goal:** bootstrap CLAUDE.md and port claudeBrain assets

## What happened
- Repo was empty (README.md with a one-line title only). Wrote `CLAUDE.md` as a
  design charter rather than a description of existing code, and marked it explicitly
  as greenfield so future sessions don't mistake the spec for reality.
- Reviewed `RorySullivan1/claudeBrain` (a factory repo of reusable `.claude/` assets;
  its `example-project/.claude/` is the populated reference to copy from).
- Ported five skills + one agent + the session-memory hook wiring. See Decisions.

## Gotchas & dead ends
- `claudeBrain`'s `coding-standards` skill looked applicable but is half C#/VSTO
  examples and cross-references `VSTO-development` — dragging it in would have added
  dead pointers. `python-development` + `python-review` cover the same floor for a
  Python-only repo. Skipped deliberately, not overlooked.
- `context/python-project-instructions.md` (whole-stack brief) overlaps the python-*
  skills almost entirely by claudeBrain's own admission. Skipped to keep the repo lean.
- `financial-timeseries-analysis` needed editing on the way in: its description and
  "Out of scope" deferred to `quantitative-finance`, `backtesting-validation`, and
  `quant-code-review`, none of which exist here. Retargeted at `python-*` and CLAUDE.md.
- `agents/python-developer.md` was written for the example project's `tools/` layer and
  deferred to `finance-quantitative-developer`. Rewritten for `python/` and this repo's
  invariants; dropped the `model: sonnet` pin so it inherits the session model.

## Frame layer: polars vs pandas (settled this session)
- Asked as a direct question after the charter landed. Answer: **polars core + pandas
  facade**, consumers "mostly pandas downstream".
- Argument that decided it, in order: (1) `pl.scan_parquet(...).filter(is_between(...))`
  pushes the time predicate into the parquet reader — for an API whose only read verb is
  `[start, end]`, that's the biggest available lever and pandas can only approximate it;
  (2) polars has no index, so `replace_window`/`upsert` become explicit
  filter/concat/`unique(keep="last")`/sort instead of pandas index alignment, removing
  the bug class invariants 3–4 are most exposed to; (3) polars bundles its own parquet
  reader, so the core is *lighter* than pandas+pyarrow; (4) the asymmetry — polars→pandas
  is one boundary call, pandas→polars later is a read-path retrofit.
- Rejected `frame="pandas"|"polars"` parameter: makes the return type dynamic and defeats
  the type checker exactly at the caller boundary. Two typed facades instead.
- Gotcha worth remembering: `.to_pandas(use_pyarrow_extension_array=True)` gives
  arrow-backed dtypes whose null semantics differ from `np.nan`, which silently changes
  `pct_change`/`rolling`/`dropna` downstream. Default to numpy-backed.
- Closed `[start, end]` survives the switch unchanged — it happens to match both polars'
  `is_between(closed="both")` and pandas `.loc`, so neither facade adjusts bounds.

## State at end
- `CLAUDE.md`, `.claude/{skills,agents,settings.json,memory}` in place. No source code,
  no `python/` directory, no toolchain yet.
- `financial-timeseries-analysis` carries a syntax note: its examples stay pandas, but
  they're statements about semantics — translate the idiom, not the rule. Includes the
  pandas→polars mapping for the four idioms that come up most.
- session-memory hooks wired for SessionStart / UserPromptSubmit / PreCompact / Stop;
  `memory.py index` verified to run under `python3`.

## Open threads
- Nothing under `python/` exists yet — the layout, module split, and the five
  invariants in CLAUDE.md are unimplemented design.
- Hook `command` is `python`, matching claudeBrain's exec-form convention. On a machine
  where only `python3` is on PATH the hooks will silently no-op; revisit if that bites.
- The manifest schema (how covered intervals are serialized) is specified in prose only.
  First implementation session should pin it and record the decision here.
