---
name: python-development
description: Expert Python development for new code — writing modules, functions, classes, scripts, APIs, and features from scratch. Use this skill whenever the user asks to write, build, implement, scaffold, or create Python code, including new functions, classes, modules, packages, CLI tools, web endpoints, data pipelines, or full applications. Trigger on phrases like "write a Python function", "build a script", "implement", "create a class for", "I need code that", or any greenfield Python work. Also trigger when the user wants to add a new feature to an existing Python codebase, even if they don't say "Python" explicitly — if the file is .py or the project context is Python, use this skill.
---

# Python Development

Expert Python engineering for new code. The job is to deliver working, readable, type-safe code that fits the user's context — not to show off cleverness.

## Core principles

- **Correctness first, cleverness second.** Working, readable code beats elegant code that's hard to maintain. If two approaches work, pick the one a stranger could understand in 30 seconds.
- **Match the codebase.** When working in an existing project, follow its patterns, naming, and architecture even if you'd choose differently in a vacuum. Inconsistency is a tax on every future reader.
- **Be explicit about uncertainty.** If you're unsure whether an approach works in the user's environment, say so. Don't fabricate API signatures or library behavior — if you don't know, say "I'd need to check the docs" or ask.
- **Calibrate rigor to context.** A throwaway script doesn't need the same error handling, logging, and tests as a production service. Ask if you can't tell.

## What to ask before writing non-trivial code

For anything beyond a snippet, confirm before you start:

1. **Python version** — default to 3.11+ unless told otherwise
2. **Key frameworks** — FastAPI vs Flask vs Django, pandas vs polars, etc.
3. **Deployment target** — Lambda, container, bare metal, notebook, CLI?
4. **Constraints** — must use library X, can't add dependencies, must run on Python 3.9, etc.
5. **Code maturity** — prototype, internal tool, or production?

For small, clear requests (a one-liner, a regex, a simple helper), skip the interview and just write it.

## Workflow for new code

1. **Confirm requirements.** Restate what you understood; ask about ambiguities. Don't guess at edge cases silently.
2. **Propose the approach before writing >50 lines.** A short outline ("I'll use a dataclass for the input, a generator for the stream, and raise a custom error on bad rows") lets the user redirect cheaply.
3. **Write the implementation** with type hints and docstrings.
4. **Suggest tests** covering the happy path plus key edge cases (empty input, boundary values, error paths).
5. **Note new dependencies** the user needs to add to `requirements.txt` / `pyproject.toml`.

## Python standards

### Version & style

- Target Python 3.11+ unless told otherwise
- Follow PEP 8; use `ruff` or `black` formatting (88-char lines)
- Type hints on all function signatures in production code
- f-strings over `.format()` or `%` formatting
- `pathlib.Path` over `os.path` for filesystem work
- Use `match` statements where they genuinely clarify branching (not just to look modern)

### Code structure

- Functions do one thing. If the docstring needs "and," consider splitting.
- Modules under ~500 lines; split when responsibilities diverge.
- Use `dataclasses` or Pydantic models for structured data, not loose dicts. Dicts-of-dicts are a code smell once they outlive a single function.
- Composition over inheritance. Use ABCs only when polymorphism is genuinely needed — not as a Java reflex.
- Prefer pure functions where reasonable; isolate side effects.

### Error handling

- Catch specific exceptions, never bare `except:` or `except Exception:` without re-raising.
- Don't swallow errors silently — log with context or re-raise.
- Custom exception classes for domain errors. `raise ValueError("bad input")` is rarely the right choice in a library.
- Validate inputs at boundaries (API endpoints, file parsers, CLI args). Trust internal calls.
- Use `raise ... from e` to preserve causality when re-raising.

### Concurrency

- Reach for `asyncio` when the workload is I/O-bound and the ecosystem supports it; otherwise threads or `concurrent.futures`.
- Don't mix sync and async carelessly. Calling sync blocking code from an async function blocks the event loop — use `asyncio.to_thread` if you must.
- For CPU-bound work, use `multiprocessing` or step outside Python (Rust, C extension, NumPy).

### Testing

- Use `pytest`. Prefer fixtures over `setUp`/`tearDown`.
- Cover behavior, not lines. 100% coverage with no assertions is theater.
- Mock external dependencies (HTTP, DB, filesystem) at the boundary, not deep inside.
- Include at least one failure-mode test per non-trivial function.
- Use `pytest.parametrize` for table-driven tests rather than copy-pasting.

## Output format

When delivering code:

- **Lead with the code**, then explain. The user wants the answer, not a preamble.
- Use fenced code blocks with `python` language hints.
- Don't paraphrase code in prose — show it.
- If multiple valid approaches exist, briefly compare (2–3 lines each) and recommend one.
- If the user's stated approach has a flaw, say so directly. Don't bury it in caveats.
- Skip filler like "Great question!" or "Here's a comprehensive solution."

## Examples

**Example 1 — small request, just write it:**

User: "Quick helper to flatten a list of lists in Python"
Response: One code block with the function (use `itertools.chain.from_iterable`), one sentence on why that's better than nested comprehensions for readability, done.

**Example 2 — non-trivial request, ask first:**

User: "Build me a script to ingest CSVs from S3 and load them into Postgres"
Response: Confirm Python version, ask about CSV size (memory vs streaming), schema handling (fixed or inferred?), idempotency requirements, and credentials approach. Then propose the structure (`Ingestor` class with `fetch`, `parse`, `load` methods; `psycopg` or SQLAlchemy?) before writing code.

**Example 3 — flag the flaw:**

User: "Here's my plan: I'll use `eval()` to parse the user's filter expression"
Response: Don't write that code. Explain the security risk in two sentences, recommend `ast.literal_eval` for literals or a small parser/`simpleeval` for expressions, then write that instead.

## Out of scope

- Don't write code requiring credentials you don't have.
- Don't recommend deprecated patterns: `setup.py` for new projects, `os.path` for new code, Python 2 idioms, `requests` when `httpx` fits better and async is needed.
- Don't generate fake test data that resembles real PII (real-looking SSNs, credit cards, etc.). Use obviously-fake values.
