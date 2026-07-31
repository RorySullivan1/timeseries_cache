# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# timeseries_cache

A **lightweight caching template for datetime-indexed data**, organized by language. It
is a template, not a service: the deliverable is a small, dependency-light reference
implementation that another project copies in and adapts. Correctness of the *coverage
bookkeeping* is the product; the storage format is an implementation detail.

**Status:** the Python implementation is complete and green (237 tests). No other
language exists yet. The invariants below are implemented, not aspirational — when a
section here conflicts with the code, that is a bug in one of them; say which.

## Layout

Language-partitioned at the top level, Python first. Each language directory is a
self-contained implementation of the same contract, with its own build config.

```
python/
  tutorials/       # markdown walkthroughs; their code blocks are executed by tests
  src/timeseries_cache/
    core.py        # TimeseriesCache: read / write / delete / coverage (polars in, polars out)
    pandas.py      # PandasTimeseriesCache: the same API, pandas in, pandas out
    keys.py        # arbitrary kwargs -> stable cache key + storage path
    index.py       # per-key manifest: covered intervals, schema, row counts
    intervals.py   # interval algebra: merge, subtract, gaps
    backends/      # StorageBackend protocol + parquet (default) + memory (tests)
    errors.py
  tests/         # parametrized over both backends and both facades
  pyproject.toml
<future-language>/ # same contract, same concepts, idiomatic to that language
```

The convenience constructors (`open_cache`, `open_pandas_cache`) live in
`__init__.py`, not `core.py` — that is what keeps `core` free of any concrete backend
import while callers still get a one-liner.

## The invariants

These are the reason the project exists. Everything else is negotiable.

### 1. Every cached object is datetime-indexed

Storage is **polars**, so "indexed" means a designated timestamp column — by convention
`ts` — that is tz-aware **UTC** and sorted ascending. Naive input is rejected at the
boundary, never silently localized. This holds for every backend and every language
port; code may assume it after validation and must enforce it before storing.

A pandas `DatetimeIndex` is a presentation concern, not a storage one — parquet stores a
column either way. The pandas facade (below) sets and restores the index at the boundary.

**Row identity is `(timestamp, *identity_columns)`.** By default `identity_columns` is
empty and the timestamp alone identifies a row, so it must be unique. Supplying them —
`identity_columns=("trade_id",)` — lets timestamps repeat, which is the real shape of
trade data, and makes the composite the unit of uniqueness. `row_key` on the cache is
that tuple; sorting, duplicate detection, and `upsert` matching all use it, never the
timestamp alone. Identity columns may not be null, and are always projected on read
even when not requested — rows the caller can't tell apart are useless.

A key remembers the identity it was written with, and reading it under a different one
raises. Two answers to "is this the same row" is how an `upsert` silently destroys
rows it should have kept.

**Coverage is time-based regardless.** Identity columns change what a *row* is; they
never change what a *range* means. Window semantics — coverage, `replace_window`,
`delete` — stay purely temporal.

### 2. Cache identity is the kwargs, and the kwargs are open-ended

Callers identify a series with arbitrary keyword arguments — `ticker="AAPL",
field="close", vendor="bbg", adjusted=True` — and the cache turns that mapping into a
deterministic key. **Nothing in `core`, `keys`, or `index` may hardcode a
domain-specific kwarg name.** Flexibility here is a hard requirement, so:

- Normalize before hashing: sort keys, canonicalize scalar values, reject unhashable
  or non-deterministic ones (dicts, sets, objects without a stable repr).
- **Canonicalize to a structure, then JSON — never string concatenation.** Joining
  `name=value` pairs with separators is forgeable: a string value containing the
  separators impersonates extra kwargs, produces an identical canonical string, and
  so slips past the manifest's collision check too. Values carry a type tag (`1` and
  `"1"` must not collide) and JSON escaping keeps each one inside its own slot.
- The same kwargs must produce the same key across processes and machines — no
  `hash()`, no insertion-order dependence, no locale-dependent formatting.
- Kwargs are stored verbatim in the manifest alongside the hash, so a cache directory
  is self-describing and a key collision is detectable rather than silent.

### 3. "No data" and "never fetched" are different things

This is the central correctness problem. A key's manifest records the **intervals the
cache has actually covered**, independent of which rows exist. A range that was
fetched and legitimately came back empty (a holiday, a delisted symbol) is *covered*
and must not be refetched; a range never requested is *unknown*. Storing rows alone
cannot distinguish these, which is why the manifest — not the data files — is the
source of truth for coverage.

`read()` therefore returns both the slice and the unknown subintervals within the
requested range, so a caller's fetch loop asks upstream only for real gaps.

### 4. Surgical overwrite is explicit, never inferred

Write modes, all taking an explicit target window rather than deriving one from the
incoming data's min/max:

- **`upsert`** (default) — incoming rows replace those with a matching **row key**
  (see invariant 1); existing rows outside the incoming set survive. With identity
  columns configured, correcting one trade must not disturb its neighbours at the
  same instant — which is why the anti-join is on `row_key`, not the timestamp.
- **`replace_window`** — delete *everything* in `[start, end]`, then insert. The
  scalpel. Because the window is explicit, a corrected partial refetch can remove
  stale rows that the new data no longer contains — the case inference gets wrong.
- **`append_only`** — reject any write overlapping existing coverage. For
  append-only sources where an overlap means a bug upstream.

Interval convention: the public API is **closed on both ends, `[start, end]`** — matching
both polars' `is_between(..., closed="both")` and pandas `.loc` slicing, so neither
facade has to adjust bounds. The interval algebra in `intervals.py` uses the same
convention throughout; mixing conventions between the API and the internals is the bug
this note exists to prevent.

### 5. A write either lands completely or not at all

Files are written to a temporary path and atomically renamed. The rule is that the
**interruptible middle state must under-claim**, which means the order depends on
direction:

- **Growing** (new rows, wider coverage) — data first, manifest last. A crash leaves
  rows nothing claims: harmless, costs a refetch.
- **Shrinking** (`delete`, which removes rows *and* their coverage) — manifest first,
  data last. Data-first here would leave the manifest claiming a range whose rows are
  gone, and a read would answer "covered, and genuinely empty" — the silent hole this
  invariant exists to forbid. `StorageBackend.write` takes `manifest_first` for this.

Prefer over-fetching after a crash to serving a silent hole, always.

## Frame layer: polars core, pandas facade

Polars is the storage and query engine; pandas is a supported boundary type, because
most consumers here are pandas.

- **Reads must use `pl.scan_parquet(...)` + a pushed-down time predicate**, not a full
  read followed by a filter. A cache whose entire read API is `[start, end]` gets its
  biggest win from only touching row groups that overlap the range. A change that
  collapses the lazy scan into an eager read is a performance regression, not a
  refactor — treat it as one. `DEFAULT_ROW_GROUP_SIZE` is the other half of that win:
  row groups are the skip unit, so it is deliberately finer than polars' default.
- **Benchmark before "optimizing" the merge path.** `merge_sorted` looks obviously
  right there (both sides are sorted) and measures *slower* than concat-then-sort on
  polars 1.43 — its sort has a fast path for nearly-ordered input. The write path is
  dominated by rewriting the key's parquet file, not by the merge.
- **Two typed facades over one core.** `TimeseriesCache` takes and returns
  `pl.DataFrame`; `PandasTimeseriesCache` takes and returns `pd.DataFrame` with a
  `DatetimeIndex`. Same methods, same semantics, static return types. Don't add a
  `frame=` parameter that makes the return type dynamic — it defeats the type checker
  at exactly the boundary where callers need it.
- **The facade is a boundary adapter, nothing more.** It sets/restores the index and
  converts; it owns no coverage logic, no interval math, no write-mode behavior. If a
  fix needs to land in both facades, it belongs in the core instead.
- **Convert to numpy-backed pandas by default** (`to_pandas()` without
  `use_pyarrow_extension_array=True`). Arrow-backed pandas has different null semantics
  from `np.nan`, which silently changes how downstream `pct_change`, `rolling`, and
  `dropna` behave — exactly the code this cache feeds.
- **Polars must not leak through the pandas facade**, in return values or in exceptions.
  A caller who only imported `PandasTimeseriesCache` should never see a `polars`
  traceback or type.

## Design constraints

- **Light by default.** The core targets stdlib + polars, which brings its own parquet
  reader — no pyarrow needed. `pyarrow` and `pandas` are a `[pandas]` extra, pulled in
  only by the facade; remote filesystems are a separate extra behind the backend
  protocol. Core logic must not import a concrete backend.
- **The backend protocol is the porting seam.** Adding object storage, or porting to
  another language, should touch the backend and the serialization of the manifest,
  not the coverage logic.
- **The on-disk layout, manifest schema, and key-hashing scheme are a compatibility
  surface.** Changing any of them invalidates existing caches. Treat it as a caller
  decision, not an implementation detail. `FORMAT_VERSION` in `index.py` exists for
  exactly this; a manifest from the future is an error, not something to guess at.
- **The manifest's schema strings are informational.** Schema checks compare the
  *live* polars schema of the stored frame, so a polars release that changes a dtype's
  `repr` doesn't invalidate every cache on disk. Don't reintroduce a string comparison.

### Accepted limitations

Documented in `python/README.md` and deliberate — don't "fix" them without asking:

- **Single-writer per key.** Writes are read-modify-write with no lock. Concurrent
  writers to the same key can lose an update.
- **Whole-key rewrite on write.** Reads scale (pushdown); writes scale with key size,
  not change size. The answer is more keys, not a rewrite of the storage model.
- **Schema is fixed per key.** Adding or retyping a column is refused, not migrated.

## Tooling

| Task | Command (from `python/`) |
|---|---|
| Install (dev) | `uv sync --dev` |
| Lint + format | `uv run ruff check . && uv run ruff format .` |
| Type check | `uv run mypy src` |
| Tests | `uv run pytest` |
| A single test | `uv run pytest "tests/test_core.py::TestReplaceWindow::test_removes_stale_rows_the_new_data_no_longer_contains"` |

Python 3.11+, ruff (88 cols), mypy on `src`, pytest. Runtime deps: `polars` in the
core; `pandas` + `pyarrow` under a `[pandas]` extra. Install dev with the extra so the
facade's tests run.

`.github/workflows/ci.yml` runs all four commands on push to `main` and on every PR,
across Python 3.11/3.12/3.13 on **both ubuntu and windows**. Windows is not
decoration: POSIX lets you replace or unlink an open file and Windows refuses, and
that difference alone produced three bugs a Linux-only suite could not reach —
including writes to an existing key failing outright. Anything touching file
handles, `os.replace`, `fsync`, or temp files needs to be reasoned about for both.

No `uv.lock` is committed — a template should be re-resolved by whoever copies it —
so CI resolves fresh each run and an upstream release can turn it red without a
local change. That is intended: for a template, finding out early beats a lock that
hides it.

Mypy's target is deliberately **not** pinned in `pyproject.toml`. It checks against
whatever interpreter it runs under, so the 3.11 job is what guarantees the floor in
`requires-python`; pinning to the minimum instead makes newer runtimes fail on their
own dependencies' stubs.

## Testing expectations

The cache's bugs live in the seams, not the happy path. Any change to coverage or
overwrite semantics needs cases for: an empty-but-covered range vs. an unknown range,
partial-window replacement, overlapping upsert, out-of-order and duplicate timestamps,
tz-naive input, DST boundaries, and an interrupted write leaving the manifest
consistent. Use the memory backend for logic, and exercise at least the round-trip
against the real filesystem backend.

Anything touching row identity needs both configurations — timestamp-only *and*
identity columns — since the default path and the composite path take different
branches in sorting, duplicate detection, and the upsert join. `tests/test_identity.py`
holds the composite cases.

Both facades must be tested against the same behavioral cases — parametrize over them
rather than testing polars deeply and pandas shallowly. The pandas facade additionally
needs: index round-trip (a frame written and read back is `assert_frame_equal` to the
original, index name, dtype, and tz included), and that column dtypes come back
numpy-backed.

The `backend` fixture in `tests/conftest.py` is parametrized over memory and parquet,
so every behavioral test already runs twice; write tests against the fixture rather
than constructing a backend directly, unless the test is *about* one backend.

The tutorials in `tutorials/` are **markdown, and their code is executed**.
`tests/test_tutorials.py` extracts every ` ```python ` block from each page,
concatenates them in document order, and runs the result — so an API change that
breaks an example breaks the build. Two conventions follow and must be kept: blocks
build on each other into one runnable script, and claims the prose makes are spelled
as `assert`s, so the test checks the narrative rather than merely that nothing
raised. Blocks fenced ` ```py ` or ` ```text ` are skipped, for snippets quoted from
elsewhere in the repo.

Discovery is by glob, so a new page is covered automatically; it also needs a row in
`tutorials/README.md`, and its inter-page links must resolve — both asserted. `ruff
format` reaches inside the fences, so tutorial code is style-checked too. When you
change public API, expect to update the tutorials alongside the tests; prose that
still reads plausibly but no longer runs is worse than no example.

## Capabilities

Skills in `.claude/skills/` and the agent in `.claude/agents/` auto-load each session by
their `description:` — Claude selects them; you don't invoke them by hand. They cover
the Python lifecycle (write / review / maintain), time-series correctness in pandas
(alignment, resampling, tz handling, look-ahead), and cross-session memory. They were
ported from the `claudeBrain` factory repo; pull further assets from
`example-project/.claude/` there rather than writing new ones from scratch.

## Memory

State and decisions carry across sessions via the `session-memory` skill:
`.claude/memory/INDEX.md` (auto-loaded by the SessionStart hook in
`.claude/settings.json`) plus append-only `sessions/*.md` logs. Record design decisions
about the manifest schema and write semantics there — they're the ones future sessions
can't recover from the code.

## Conventions

- Branch per change; conventional-commit-style messages.
- `ruff format` before commit.
- Skill folder name always equals the skill's `name:` frontmatter.
- A new language port is a new top-level directory implementing the same invariants —
  not a variation on them.

## Compact Instructions

On compaction, preserve: the coverage-vs-emptiness distinction (invariant 3), the
closed-interval `[start, end]` convention, the three write modes and that their window
is explicit, the UTC timestamp-column contract, that row identity is
`(timestamp, *identity_columns)` while coverage stays purely time-based, the rule that
no core module may hardcode a kwarg name, and that the frame layer is polars core + a
thin pandas facade with pushed-down scans on the read path.
