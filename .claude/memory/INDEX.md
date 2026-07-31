# MEMORY INDEX  ·  keep ≤ ~80 lines

## State            (rewrite in place — current truth only, ≤ ~10 lines)
- Greenfield. `CLAUDE.md` + `.claude/` exist; no source code, no `python/` dir, no toolchain.
- CLAUDE.md is a design charter, not a description of code. Its five invariants are unbuilt.
- Claude assets ported from `RorySullivan1/claudeBrain` (`example-project/.claude/` is the source to copy from).

## Decisions        (append-only; supersede, never delete)
- [2026-07-31] Repo is language-partitioned at top level, Python first — each language a self-contained impl of one shared contract — because the deliverable is a copyable template, not a package. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Coverage is tracked in a per-key manifest, separate from stored rows, so "fetched and legitimately empty" is distinguishable from "never fetched". This is the core design bet. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Public interval convention is closed `[start, end]` to match pandas `.loc`; internals use the same convention throughout. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Write modes take an *explicit* window, never one inferred from the incoming data's min/max — that's what makes `replace_window` able to delete stale rows. — sessions/2026-07-31-1841-bootstrap-claude-md.md
- [2026-07-31] Ported python-development/review/maintenance, financial-timeseries-analysis, session-memory, and an adapted python-developer agent; skipped coding-standards (C#/VSTO-heavy) and the python whole-stack brief (redundant). — sessions/2026-07-31-1841-bootstrap-claude-md.md

## Threads          (open items; remove when closed)
- `python/` is empty — layout, module split, and all five invariants are unimplemented.
- Manifest schema (serialization of covered intervals) specified in prose only; pin it in the first implementation session.
- Hooks invoke `python`, not `python3`; will silently no-op on a `python3`-only machine.

## Log              (append-only pointers)
- 2026-07-31 1841 | bootstrap CLAUDE.md + claudeBrain asset port | sessions/2026-07-31-1841-bootstrap-claude-md.md
