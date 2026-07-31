---
name: session-memory
description: >-
  Persist and recall project state across Claude Code sessions via a
  .claude/memory/ directory — a small auto-loaded INDEX.md plus append-only
  timestamped logs in sessions/. Use whenever the user references a past session
  ("what did we decide", "last time", "where did we leave off", "continue"), asks
  to record/log/save/update/check project memory, starts work in a repo that has
  .claude/memory/, or finishes a substantial session worth capturing. Trigger
  even if the user doesn't say the word "memory".
---

# Session Memory

Claude Code starts every session cold. This skill gives a project a file-based
memory so decisions, dead ends, and open threads carry over.

The design target is a long-context model, so the binding constraint is **not**
"will it fit." It's three other things: the standing token cost of whatever loads
on *every* session, signal-to-noise within that context, and **summarization
decay** — the slow loss of detail when a synthesized file is rewritten session
after session. Every rule below follows from those three.

So: a tiny `INDEX.md` that auto-loads each session and carries synthesized state,
plus full-fidelity `sessions/*.md` logs that are **append-only** and read on
demand. Detail is never destroyed; only one small part of the index is ever
rewritten.

## Layout

```
.claude/memory/
├── INDEX.md                              # auto-loaded every session — keep ≤ ~80 lines (~600 tokens)
└── sessions/
    └── YYYY-MM-DD-HHMM-slug.md           # append-only, full detail, read on demand
```

If `.claude/memory/` doesn't exist and memory is wanted, create it (see Setup).

> Commands below use `python` (this repo's interpreter); on macOS/Linux use `python3`.

## INDEX.md — four sections, two update rules

The whole anti-decay design lives here. Lossy rewriting is confined to one small
section; the high-value content is append-only and fully traceable.

- **State** — *rewrite in place.* The only synthesized, lossy part. Keep it to
  ≤ ~10 lines of current truth. This is the part you accept some decay on, which
  is why it's small and everything important also lives append-only below.
- **Decisions** — *append-only.* One line each: `[YYYY-MM-DD] decision — why — sessions/<file>.md`.
  Never edit or delete a decision. To reverse one, append a new line that
  supersedes it. This keeps your most valuable content decay-proof and makes the
  reasoning history auditable — which, given how often "why did we drop X" comes
  up, is the point.
- **Threads** — *mutable list.* Add open items; delete them when closed (the
  detail survives in the relevant log).
- **Log** — *append-only pointer index:* `YYYY-MM-DD HHMM | topic | sessions/<file>.md`.

Seed `INDEX.md` with exactly this:

```markdown
# MEMORY INDEX  ·  keep ≤ ~80 lines

## State            (rewrite in place — current truth only, ≤ ~10 lines)
-

## Decisions        (append-only; supersede, never delete)
-

## Threads          (open items; remove when closed)
-

## Log              (append-only pointers)
-
```

## Reading

- If the SessionStart hook is installed (see Setup), `INDEX.md` is already in
  context — don't re-read it. If it isn't, read `.claude/memory/INDEX.md` before
  doing project work.
- For specifics, locate the log and read it — don't bulk-load `sessions/`:
  ```bash
  python .claude/skills/session-memory/scripts/memory.py search "QUERY"
  python .claude/skills/session-memory/scripts/memory.py list --limit 10
  ```
  Reading several logs is fine when the task genuinely spans them. The rule is
  *load what's relevant, skip what isn't* — not minimize for its own sake.
- The live repo and the user's current words override memory on any conflict.

## Writing

Log a session when it produced something a future session would otherwise have to
reconstruct: a decision, a non-obvious fix, a dead end, or a clear next step. Skip
routine Q&A and anything obvious from the code.

```bash
python .claude/skills/session-memory/scripts/memory.py new --slug graph-api-scopes \
  --goal "settle the Graph API permission scope for taskmaster"
```

The script scaffolds the timestamped file and owns the log template (Goal /
What happened / Gotchas & dead ends / State at end / Open threads). Fill terse
bullets. Record *why*, not just *what* — a dead end ("tried X, failed because Y")
is worth more than a success, because it's the thing nobody can recover from the
code. Then do the index update below.

## Index discipline — the one step that keeps memory from rotting

After writing a log: rewrite **State** to match reality now; append any
**Decisions** and the **Log** pointer; prune resolved **Threads**. Append-only
sections only grow; only State is rewritten, so decay is bounded to ≤ ~10 lines.

When `INDEX.md` approaches its budget, don't compress in place — move the oldest
**Decisions** and **Log** lines into `sessions/ARCHIVE-YYYY.md` and leave a single
pointer. All compaction happens in the index; **never rewrite a log file.**

## Setup (one-time)

1. `python .claude/skills/session-memory/scripts/memory.py init` — creates
   `.claude/memory/sessions/` and seeds `INDEX.md` (template above).
2. Install the lifecycle hooks in `.claude/settings.json` (committable) or
   `~/.claude/settings.json` (all projects). SessionStart stdout becomes context,
   so this is what makes the index "always loaded" — and it loads *only* the
   index, never the logs:
   ```json
   {
     "hooks": {
       "SessionStart": [
         {
           "matcher": "startup|resume|clear|compact",
           "hooks": [
             { "type": "command",
               "command": "python",
               "args": ["${CLAUDE_PROJECT_DIR}/.claude/skills/session-memory/scripts/memory.py", "index"],
               "timeout": 10 }
           ]
         }
       ]
     }
   }
   ```
   This repo also wires **PreCompact**, **Stop**, and **UserPromptSubmit** to the
   same script (`precompact-hook` / `stop-hook` / `prompt-hook`) so the model is
   reminded to *persist* memory before context is lost and to *recall* it on
   "continue"-style prompts. See `.claude/settings.json` and
   `.claude/hooks/README.md`. Exec-form (`command` + `args`) is used for
   cross-platform reliability — shell-form `cat`/`$VAR` hooks are fragile on Windows.
3. Commit `.claude/memory/` for shared team memory, or gitignore it to keep it local.

The hooks live in settings, not this skill's frontmatter, because memory must load
at the very start of a session — before any skill has necessarily triggered.

## Scope vs CLAUDE.md / DECISIONS.md

Static conventions (style, test command, architecture rules) → `CLAUDE.md`,
loaded automatically. Evolving state and recent decisions → this skill's
`INDEX.md`. Long-form architecture decision records → `DECISIONS.md` if you keep
one; the index links to it rather than duplicating it.
