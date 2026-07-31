# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# timeseries_cache

A **lightweight caching template for datetime-indexed data**, organized by language. It
is a template, not a service: the deliverable is a small, dependency-light reference
implementation that another project copies in and adapts. Correctness of the *coverage
bookkeeping* is the product; the storage format is an implementation detail.

**Status: greenfield.** Only this file, `README.md`, and `.claude/` exist. Everything
below the "Intended layout" heading is the design to build toward, not code that exists
yet. When you implement a piece, keep this file in sync — and when a section here
conflicts with committed code, the code is wrong until someone says otherwise.

## Intended layout

Language-partitioned at the top level, Python first. Each language directory is a
self-contained implementation of the same contract, with its own build config.

```
python/
  src/timeseries_cache/
    core.py        # TimeseriesCache facade: read / write / delete / coverage
    keys.py        # arbitrary kwargs -> stable cache key + storage path
    index.py       # per-key manifest: covered intervals, schema, row counts
    intervals.py   # interval algebra: merge, subtract, gaps
    backends/      # StorageBackend protocol + parquet (default) + memory (tests)
    errors.py
  tests/
  pyproject.toml
<future-language>/ # same contract, same concepts, idiomatic to that language
```

## The invariants

These are the reason the project exists. Everything else is negotiable.

### 1. Every cached object is datetime-indexed

The index is tz-aware **UTC**, monotonically increasing, and unique. Naive input is
rejected at the boundary, never silently localized. This holds for every backend and
every language port — code may assume it after validation and must enforce it before
storing.

### 2. Cache identity is the kwargs, and the kwargs are open-ended

Callers identify a series with arbitrary keyword arguments — `ticker="AAPL",
field="close", vendor="bbg", adjusted=True` — and the cache turns that mapping into a
deterministic key. **Nothing in `core`, `keys`, or `index` may hardcode a
domain-specific kwarg name.** Flexibility here is a hard requirement, so:

- Normalize before hashing: sort keys, canonicalize scalar values, reject unhashable
  or non-deterministic ones (dicts, sets, objects without a stable repr).
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

- **`upsert`** (default) — incoming rows replace matching timestamps; existing rows
  outside the incoming index survive.
- **`replace_window`** — delete *everything* in `[start, end]`, then insert. The
  scalpel. Because the window is explicit, a corrected partial refetch can remove
  stale rows that the new data no longer contains — the case inference gets wrong.
- **`append_only`** — reject any write overlapping existing coverage. For
  append-only sources where an overlap means a bug upstream.

Interval convention: the public API is **closed on both ends, `[start, end]`**, matching
pandas `.loc` slicing. The interval algebra in `intervals.py` uses the same convention
throughout — mixing conventions between the API and the internals is the bug this note
exists to prevent.

### 5. A write either lands completely or not at all

Data files are written to a temporary path and atomically renamed; the manifest is
updated **last**. An interrupted write may leave an orphaned temp file (harmless,
garbage-collectable) but must never leave the manifest claiming coverage whose rows
aren't on disk. Prefer over-fetching after a crash to serving a silent hole.

## Design constraints

- **Light by default.** The core targets stdlib + pandas. Parquet/pyarrow and any
  remote filesystem are optional extras behind the backend protocol — core logic must
  not import a concrete backend.
- **The backend protocol is the porting seam.** Adding object storage, or porting to
  another language, should touch the backend and the serialization of the manifest,
  not the coverage logic.
- **The on-disk layout, manifest schema, and key-hashing scheme are a compatibility
  surface.** Changing any of them invalidates existing caches. Treat it as a caller
  decision, not an implementation detail.

## Tooling (target)

Not yet wired up. When creating `python/pyproject.toml`, use these so the commands
below stay true:

| Task | Command (from `python/`) |
|---|---|
| Install (dev) | `uv sync --dev` |
| Lint + format | `uv run ruff check . && uv run ruff format .` |
| Type check | `uv run mypy src` |
| Tests | `uv run pytest` |
| A single test | `uv run pytest tests/test_core.py::test_replace_window_removes_stale_rows` |

Python 3.11+, ruff (88 cols), mypy on `src`, pytest.

## Testing expectations

The cache's bugs live in the seams, not the happy path. Any change to coverage or
overwrite semantics needs cases for: an empty-but-covered range vs. an unknown range,
partial-window replacement, overlapping upsert, out-of-order and duplicate timestamps,
tz-naive input, DST boundaries, and an interrupted write leaving the manifest
consistent. Use the memory backend for logic, and exercise at least the round-trip
against the real filesystem backend.

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
is explicit, the UTC index contract, and the rule that no core module may hardcode a
kwarg name.
