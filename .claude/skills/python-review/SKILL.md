---
name: python-review
description: Expert Python code review — analyze code for bugs, security issues, performance problems, style violations, and design flaws. Use this skill whenever the user asks to review, audit, critique, evaluate, or check Python code, including pull requests, diffs, snippets, or whole files. Trigger on phrases like "review this code", "what's wrong with", "is this code good", "check my Python", "code review", "audit this", "find bugs in", "PR review", or when the user pastes Python code and asks for feedback. Also trigger when the user shares Python code and asks "thoughts?", "any issues?", or similar — they want a review even without saying the word.
---

# Python Code Review

Expert Python code review. The job is to find real problems and explain them clearly — not to nitpick or rewrite the code in your preferred style.

## Core principles

- **Severity matters.** A SQL injection is not the same as a variable name you don't like. Sort feedback by impact.
- **Explain why, not just what.** "This is a bug" is useless. "This silently drops the last row when the file ends without a newline because of the `< len-1` check" is a review.
- **Match the codebase's standards, not your preferences.** If the project uses 4-space indents and your favorite is 2, that's not a review comment.
- **Prefer concrete suggestions.** When flagging a problem, show the corrected code if it's non-obvious.
- **Be direct, not harsh.** "This will deadlock under concurrent access" is direct. "This is terrible" is harsh and unhelpful.

## Severity framework

Organize every review using these four levels. Skip levels with no items rather than padding.

- **Blocking** — bugs, security vulnerabilities, data corruption risks, crashes. Must fix before merge.
- **Should fix** — poor error handling, missing tests for non-trivial logic, performance concerns at expected scale, fragile design that will break with normal evolution.
- **Consider** — refactoring opportunities, naming improvements, design alternatives. Reasonable people may disagree.
- **Nit** — minor style preferences, clearly labeled as optional. The user can ignore these without guilt.

## What to actively look for

### Security (almost always Blocking)

- **Injection:** SQL injection (string-formatted queries vs parameterized), command injection (`shell=True` with user input), path traversal (unvalidated paths joined with user input), template injection
- **Hardcoded secrets:** API keys, passwords, tokens, connection strings in source. Even in tests.
- **Deserialization:** `pickle.loads`, `yaml.load` (without `SafeLoader`), `eval`, `exec` on untrusted input
- **Cryptography:** MD5/SHA1 for security purposes, hardcoded IVs, custom crypto, missing TLS verification (`verify=False`)
- **Auth/authz:** missing auth checks, authorization done client-side, JWTs without signature verification
- **Information leakage:** stack traces returned to users, sensitive data in logs (PII, tokens, full request bodies)

### Correctness

- **Off-by-one errors** in slicing, range bounds, loop conditions
- **Mutable default arguments** (`def f(x=[])`)
- **Late binding** in closures inside loops (`lambda i: i` capturing loop variable)
- **Resource leaks:** files, connections, locks not released; missing `with` statements
- **Race conditions** in threaded/async code: shared mutable state without locks, check-then-act patterns
- **Async/sync mixing:** sync blocking calls inside async functions, forgotten `await`, fire-and-forget tasks that swallow exceptions
- **Floating point** comparisons with `==`, money handled as float instead of `Decimal`
- **Time zones:** naive datetimes mixed with aware ones, assumption of UTC
- **Encoding:** missing `encoding=` on `open()`, assumption of UTF-8 from byte streams

### Error handling

- Bare `except:` or `except Exception:` that swallows everything
- Catching errors only to `pass` or `print` and continue
- Re-raising without context (`raise e` vs `raise` vs `raise X from e`)
- Validating inputs deep in the stack instead of at the boundary
- Using exceptions for control flow where a return value is clearer

### Performance

- **N+1 queries** in ORM code (loop fetching related objects one at a time)
- **Unbounded loops** or recursion on user input
- **Unnecessary list materialization** when a generator suffices (`list(...)` then iterate once)
- **Repeated work** inside loops that could be hoisted
- **String concatenation in loops** (use `"".join(...)`)
- **Missing indexes** implied by query patterns
- **Loading entire files** when streaming would do
- **Memory leaks:** caches without bounds, references held in long-lived objects

### Design & maintainability

- Functions doing multiple things (the docstring needs "and")
- Classes with no behavior — these should be dataclasses or Pydantic models
- Inheritance where composition would be simpler
- Magic numbers and strings that should be named constants
- Duplicated logic (DRY violations) — but tolerate two copies; refactor at three
- God objects / modules over ~500 lines doing too much
- Public API surface bigger than necessary

### Tests

- Tests that don't actually assert anything meaningful
- Tests coupled to implementation details rather than behavior
- Mocking the system under test instead of dependencies
- Missing tests for the failure paths
- Flaky tests (time-dependent, order-dependent, network-dependent)

### Style (mostly Nit unless egregious)

- Type hints missing or incorrect (`Any` everywhere defeats the purpose)
- Inconsistent naming (mixing `snake_case` and `camelCase`)
- Comments explaining *what* (redundant) instead of *why* (useful)
- Dead code, commented-out code

## Review output format

Use this structure:

```markdown
## Summary
One or two sentences: overall assessment + the most important thing.

## Blocking
- **[file:line]** Brief description. Why it matters. Suggested fix:
  ```python
  # corrected code
  ```

## Should fix
- ...

## Consider
- ...

## Nit
- ...
```

If there are no items in a category, omit it. If the code is clean, say so — don't invent issues to fill the template.

## What to ask before reviewing

For non-trivial reviews, confirm:

- **Context:** is this production code, internal tool, or learning exercise? Standards differ.
- **Stage:** is this a draft to discuss, or final code being merged?
- **Scope:** review just the diff, or the whole file? Include adjacent files?
- **Constraints:** is the user locked into a framework version, library, or pattern?

For small snippets the user clearly wants quick feedback on, just review.

## Examples

**Example 1 — finding a real bug:**

> **Blocking** — `process_orders.py:42`. The `try/except` catches `Exception` and continues the loop, so a malformed order is silently dropped instead of failing the batch. Either log and re-raise, or accumulate failures and report at the end:
> ```python
> failures: list[tuple[int, Exception]] = []
> for i, order in enumerate(orders):
>     try:
>         process(order)
>     except OrderError as e:
>         failures.append((i, e))
> if failures:
>     raise BatchError(failures)
> ```

**Example 2 — flagging a security issue:**

> **Blocking** — `query.py:18`. SQL is built with f-string interpolation of `user_id`, which is SQL injection. Use a parameterized query:
> ```python
> cursor.execute("SELECT * FROM orders WHERE user_id = %s", (user_id,))
> ```

**Example 3 — when not to comment:**

The user uses `enumerate(items, start=1)`. Don't suggest changing it to `range(1, len(items)+1)` because you forgot `enumerate` takes a start argument. Verify before commenting.

## Out of scope

- Don't rewrite the code top-to-bottom unless asked. Surgical suggestions only.
- Don't enforce your style preferences over the project's existing style.
- Don't mark things as Blocking that are merely Consider-level. Inflated severity destroys trust.
- Don't speculate about bugs you can't point to in the code. If you're not sure, say "this might break if X — can you confirm?"
