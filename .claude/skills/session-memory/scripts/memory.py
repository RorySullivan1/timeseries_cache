#!/usr/bin/env python
"""session-memory — file-based project memory for Claude Code.

A small CLI that owns the mechanics of the session-memory skill: scaffolding
timestamped session logs, searching/listing them, printing the auto-loaded INDEX,
and backing the lifecycle hooks (SessionStart / PreCompact / Stop / UserPromptSubmit).

Stdlib only. The project root is resolved from $CLAUDE_PROJECT_DIR (set by Claude
Code) with a fallback to the current working directory, so the same script works
unchanged in any project that vendors it.

On macOS/Linux invoke with `python3`; on Windows use `python`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import sys
from datetime import datetime
from pathlib import Path

# --- paths -----------------------------------------------------------------

def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())

def memory_dir() -> Path:
    return project_root() / ".claude" / "memory"

def sessions_dir() -> Path:
    return memory_dir() / "sessions"

def index_path() -> Path:
    return memory_dir() / "INDEX.md"

# --- templates -------------------------------------------------------------

INDEX_TEMPLATE = """\
# MEMORY INDEX  ·  keep ≤ ~80 lines

## State            (rewrite in place — current truth only, ≤ ~10 lines)
-

## Decisions        (append-only; supersede, never delete)
-

## Threads          (open items; remove when closed)
-

## Log              (append-only pointers)
-
"""

SESSION_TEMPLATE = """\
# {date} · {slug}

**Goal:** {goal}

## What happened
-

## Gotchas & dead ends
-

## State at end
-

## Open threads
-
"""

# --- helpers ---------------------------------------------------------------

def read_stdin_json() -> dict:
    """Read the hook payload Claude Code passes on stdin. Never blocks fatally."""
    try:
        data = sys.stdin.read()
    except Exception:
        return {}
    if not data.strip():
        return {}
    try:
        return json.loads(data)
    except (ValueError, TypeError):
        return {}

def session_files() -> list[Path]:
    if not sessions_dir().is_dir():
        return []
    return sorted(
        (p for p in sessions_dir().glob("*.md") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )

# --- commands --------------------------------------------------------------

def cmd_init(_args) -> int:
    sessions_dir().mkdir(parents=True, exist_ok=True)
    if not index_path().exists():
        index_path().write_text(INDEX_TEMPLATE, encoding="utf-8")
        print(f"seeded {index_path()}")
    else:
        print(f"exists {index_path()}")
    return 0

def cmd_new(args) -> int:
    sessions_dir().mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    slug = re.sub(r"[^a-z0-9-]+", "-", args.slug.lower()).strip("-") or "session"
    name = f"{now:%Y-%m-%d-%H%M}-{slug}.md"
    path = sessions_dir() / name
    if path.exists():
        print(f"exists {path}", file=sys.stderr)
        return 1
    path.write_text(
        SESSION_TEMPLATE.format(date=f"{now:%Y-%m-%d %H:%M}", slug=slug, goal=args.goal or ""),
        encoding="utf-8",
    )
    print(str(path))
    return 0

def cmd_search(args) -> int:
    needle = args.query.lower()
    hits = 0
    for path in sorted(session_files(), key=lambda p: p.name):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if needle in line.lower():
                rel = path.relative_to(project_root())
                print(f"{rel.as_posix()}:{n}: {line.strip()}")
                hits += 1
    if not hits:
        print(f"no matches for {args.query!r}", file=sys.stderr)
    return 0

def cmd_list(args) -> int:
    files = session_files()
    if not files:
        print("no session logs yet", file=sys.stderr)
        return 0
    for path in files[: args.limit]:
        print(path.relative_to(project_root()).as_posix())
    return 0

def cmd_index(_args) -> int:
    """SessionStart: print INDEX so it loads into context. Silent if absent."""
    if index_path().exists():
        sys.stdout.write(index_path().read_text(encoding="utf-8", errors="replace"))
    return 0

def cmd_precompact_hook(_args) -> int:
    """PreCompact: stdout is added to context. Remind before detail is lost."""
    if not memory_dir().is_dir():
        return 0
    print(
        "session-memory: context is about to be compacted. Before it is, persist "
        "anything a future session would have to reconstruct — a decision, a non-obvious "
        "fix, a dead end, a next step — with "
        "`python .claude/skills/session-memory/scripts/memory.py new --slug <slug> --goal <goal>`, "
        "then update .claude/memory/INDEX.md (rewrite State; append Decisions/Log). "
        "Detail not captured now will be summarized away."
    )
    return 0

def stop_nudge_marker(session_id: str) -> Path:
    """Per-session marker so the Stop nudge fires at most once per session."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    return Path(tempfile.gettempdir()) / f"claude-session-memory-nudged-{safe}.flag"

def cmd_stop_hook(_args) -> int:
    """Stop: nudge once per session to capture it. Guarded against loops."""
    payload = read_stdin_json()
    if payload.get("stop_hook_active") is True:
        return 0  # already blocked once this turn; let the session stop
    if not memory_dir().is_dir():
        return 0
    # Block at most once per session: a marker keyed by session_id means we've
    # already nudged, so subsequent Stops (e.g. on "continue") pass silently.
    # Missing session_id → no dedup possible, fall back to nudging this once.
    session_id = str(payload.get("session_id") or "")
    if session_id:
        marker = stop_nudge_marker(session_id)
        if marker.exists():
            return 0
        try:
            marker.touch()
        except OSError:
            pass  # best-effort; a failed marker just means we may nudge again
    out = {
        "decision": "block",
        "reason": (
            "Before finishing: if this session produced something a future session "
            "would otherwise reconstruct — a decision, a non-obvious fix, a dead end, "
            "or a clear next step — capture it now with "
            "`python .claude/skills/session-memory/scripts/memory.py new --slug <slug> --goal <goal>` "
            "and update .claude/memory/INDEX.md (rewrite State; append Decisions/Log). "
            "If nothing substantial happened (routine Q&A, or obvious from the code), "
            "just stop without logging."
        ),
    }
    print(json.dumps(out))
    return 0

RECALL_RE = re.compile(
    r"\b(continue|last time|where did we|left off|pick up|resume|recap|catch me up)\b",
    re.IGNORECASE,
)

def cmd_prompt_hook(_args) -> int:
    """UserPromptSubmit: on a recall-style prompt, point at memory. Else silent."""
    payload = read_stdin_json()
    prompt = str(payload.get("prompt") or "")
    if not prompt or not RECALL_RE.search(prompt):
        return 0
    if not index_path().exists():
        return 0
    print(
        "session-memory: this looks like a continuation. Consult "
        ".claude/memory/INDEX.md for current State, Decisions, and open Threads, and "
        "`python .claude/skills/session-memory/scripts/memory.py search \"<topic>\"` "
        "for the relevant session log before proceeding."
    )
    return 0

# --- dispatch --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory.py", description="session-memory CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create memory/ and seed INDEX.md").set_defaults(func=cmd_init)

    sp = sub.add_parser("new", help="scaffold a timestamped session log")
    sp.add_argument("--slug", required=True, help="short kebab-case topic")
    sp.add_argument("--goal", default="", help="one-line goal for the session")
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser("search", help="search session logs")
    sp.add_argument("query")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("list", help="list recent session logs")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_list)

    sub.add_parser("index", help="print INDEX.md (SessionStart hook)").set_defaults(func=cmd_index)
    sub.add_parser("precompact-hook", help="PreCompact reminder").set_defaults(func=cmd_precompact_hook)
    sub.add_parser("stop-hook", help="Stop write reminder").set_defaults(func=cmd_stop_hook)
    sub.add_parser("prompt-hook", help="UserPromptSubmit recall").set_defaults(func=cmd_prompt_hook)
    return p

def _force_utf8() -> None:
    """Windows consoles default to cp1252; memory text (≤, ·, —) needs UTF-8 out."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())
