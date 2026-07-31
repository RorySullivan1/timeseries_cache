---
name: python-maintenance
description: Expert Python maintenance work — debugging, refactoring, fixing bugs, upgrading dependencies, modernizing legacy code, improving performance, and resolving technical debt in existing Python codebases. Use this skill whenever the user wants to debug an error, fix a bug, refactor code, modernize legacy Python, upgrade a dependency or Python version, improve performance, eliminate deprecation warnings, fix flaky tests, or clean up technical debt. Trigger on phrases like "fix this bug", "debug this error", "why is this slow", "refactor this", "upgrade to Python 3.12", "this is broken", "stack trace", "deprecation warning", "memory leak", "modernize this", or any work on existing Python code that already runs (or used to). Distinct from new development — use this when the code exists and needs to change.
---

# Python Maintenance

Expert Python maintenance: debugging, refactoring, modernizing, and resolving technical debt in existing code. The job is to change running code without breaking it — which requires more discipline than writing new code from scratch.

## Core principles

- **Reproduce before you fix.** A bug you can't reproduce is a bug you can't verify you fixed. Get a failing test or a reliable repro first.
- **Smallest change that solves the problem.** Maintenance is not the time for sweeping rewrites. Surgical fixes are reviewable; rewrites are risky.
- **Preserve behavior unless the behavior is the bug.** If a function has been returning `None` on missing input for years, code probably depends on that. Changing it is a behavior change, not a fix.
- **Match existing patterns.** Even if the codebase's style is dated, don't introduce a new style for one function. That just creates two patterns to maintain.
- **Tests are leverage.** Before refactoring, ensure there are tests covering the behavior you want to preserve. If there aren't, write them first — they're the safety net.

## Debugging workflow

When the user shares an error or unexpected behavior:

1. **Read the actual error.** Stack traces tell you exactly where the failure happened. Don't guess based on the description.
2. **Confirm the repro.** "Run X with input Y, expect Z, get W." If any of those pieces are missing, ask.
3. **Form a hypothesis** before making changes. "I think this is happening because…" — then verify, don't assume.
4. **Bisect when stuck.** Comment things out, add prints, run with `python -X dev`, use `pdb` / `breakpoint()`. Narrow until the failure point is unambiguous.
5. **Fix the cause, not the symptom.** Wrapping a buggy function in try/except hides the bug; it doesn't fix it.
6. **Add a regression test.** Whatever caused the bug should fail without the fix. Otherwise it'll come back.

### Common Python debugging traps

- **It works on my machine** — Python version, dependency versions, locale, timezone, filesystem case sensitivity, line endings.
- **The error is in import-time code** — circular imports, missing `__init__.py`, sys.path order. The traceback points at the import site, not the cause.
- **Encoding mismatches** — `UnicodeDecodeError` usually means a file isn't UTF-8 or `sys.stdout` is unexpected (Windows, Docker without `PYTHONIOENCODING`).
- **Mutable default arguments** — function appears to "remember" prior calls.
- **Late binding closures** — `[lambda: i for i in range(3)]` all return 2.
- **Async deadlocks** — sync code calling `asyncio.run` inside an already-running loop, or `await` on something that never resolves.
- **Pickling failures** — lambdas, local functions, open file handles aren't picklable.
- **Generator exhaustion** — iterating a generator twice gets nothing the second time.
- **Path issues** — relative paths depend on CWD, not script location. Use `Path(__file__).parent`.

## Refactoring workflow

1. **Confirm there are tests** covering the behavior you'll change. If not, write characterization tests first — tests that capture current behavior, even if that behavior is weird.
2. **Make small, reviewable commits.** Rename. Test. Extract. Test. Inline. Test. Don't bundle five refactors into one diff.
3. **Don't change behavior and structure in the same change.** Either you're refactoring (no behavior change, all tests still pass) or you're fixing (behavior changes, tests change). Mixing them makes review impossible.
4. **Use the type checker.** Run `mypy` / `pyright` after each change. It catches whole classes of refactor mistakes.
5. **Keep the public API stable** unless the user explicitly wants to break it. Internal refactors shouldn't ripple to callers.

### Refactor patterns worth knowing

- **Extract function** when a block has a clear single purpose and a name you'd want to use.
- **Replace conditional with polymorphism** *only* when the conditional appears in multiple places. One switch statement is fine.
- **Replace dict-of-dicts with dataclass/Pydantic model** when the dict has a fixed shape and is passed around.
- **Replace inheritance with composition** when subclasses override most of the parent or the "is-a" relationship is shaky.
- **Introduce parameter object** when a function takes 5+ arguments that travel together.
- **Replace magic value with named constant** for any literal that appears more than once or whose meaning isn't obvious.

## Modernization

Common upgrades worth proposing when touching old code (but ask first if scope isn't clear):

- `os.path` → `pathlib.Path`
- `%` formatting / `.format()` → f-strings
- `dict()` / `list()` constructors → `{}` / `[]`
- `Optional[X]` / `Union[X, Y]` → `X | None` / `X | Y` (Python 3.10+)
- `typing.List` / `typing.Dict` → `list` / `dict` (Python 3.9+)
- `setup.py` → `pyproject.toml`
- `requirements.txt` without pins → pinned with hashes, or `uv` / `poetry` lockfile
- `unittest` → `pytest` (only if there's appetite — mass test conversion is its own project)
- `requests` + threads → `httpx` + asyncio (only when async is actually beneficial)
- `pkg_resources` → `importlib.resources` / `importlib.metadata`
- `datetime.utcnow()` → `datetime.now(timezone.utc)` (the former is deprecated in 3.12+)

### Python version upgrades

When the user is upgrading Python (e.g., 3.9 → 3.12):

1. Check the [What's New](https://docs.python.org/3/whatsnew/) for each version between source and target. Note removed/deprecated APIs.
2. Run the existing test suite under the new version first. Most issues surface there.
3. Run with `-W error::DeprecationWarning` to catch deprecations as errors.
4. Common breakages: `asyncio` API changes, `distutils` removal in 3.12, `imp` module removal, `typing.io` / `typing.re` removal, stricter datetime parsing.
5. Update CI matrix to test both old and new versions during transition.

### Dependency upgrades

- **Read the changelog** before upgrading any non-trivial dependency. Don't just bump and pray.
- **Major versions** (1.x → 2.x) deserve scrutiny. Minor versions usually safe.
- **Run tests** before and after. If there are no tests, write a smoke test.
- **Check transitive dependencies** — `pip-audit` and `pip list --outdated` help.
- **Lockfile discipline** — every upgrade should regenerate the lockfile, not just edit it by hand.

## Performance work

1. **Measure first, optimize second.** Use `cProfile`, `py-spy`, `scalene`, or `time.perf_counter()`. Don't optimize what you haven't measured.
2. **Find the actual hot path.** Often it's not where you think. A loop you stare at may be 1% of runtime; a JSON parse you didn't notice is 70%.
3. **Algorithmic wins beat micro-optimizations.** O(n²) → O(n log n) dwarfs every "make this loop faster" trick.
4. **Cache thoughtfully.** `functools.lru_cache` for pure functions; explicit cache invalidation for anything that can change.
5. **Don't reach for C/Cython/Rust** until you've exhausted Python wins. Vectorize with NumPy, use `set` lookups, use generators to avoid materialization.
6. **Async helps I/O, not CPU.** A CPU-bound function won't run faster under asyncio.

## Output format

When the user shares broken code or asks for a fix:

1. **State the diagnosis** in one or two sentences. ("This fails because `process_row` mutates the row dict, and the dict is shared across iterations of the outer loop.")
2. **Show the fix** as a diff or replacement code block, not a rewrite of the whole file.
3. **Explain what changed and why.**
4. **Suggest a regression test** if the bug is non-trivial.
5. **Flag related issues** you noticed but didn't fix, separately. Don't silently expand scope.

## What to ask before maintenance work

- **What's the symptom?** Error message, wrong output, slow, crash?
- **What's the repro?** Exact command, input, expected vs actual.
- **Recent changes?** "It used to work" + "we upgraded X" is a strong signal.
- **Test coverage?** Are there tests around the area you'll change?
- **Risk tolerance?** Production hotfix vs leisurely cleanup are different jobs.

For obvious typos and trivial fixes, just fix them.

## Examples

**Example 1 — debug from a stack trace:**

User pastes a `KeyError: 'user_id'` traceback.
Don't speculate. Read the trace, identify the exact dict access, ask what input produced it, then either reproduce or reason from the code. The fix isn't `dict.get()` — that hides the bug. The fix is figuring out why `user_id` is missing when it shouldn't be, and either validating the input upstream or handling the legitimate case.

**Example 2 — refactor request:**

User: "Clean up this 200-line function."
First response: ask if there are tests. If yes, propose a sequence of small extractions with names. If no, suggest writing characterization tests first — refactoring untested code is a coin flip.

**Example 3 — performance complaint:**

User: "This script is slow."
Don't guess. Ask for the profile, or offer to add profiling. "Slow" without a number isn't actionable. Once you see the profile, the fix is usually obvious.

## Out of scope

- Don't rewrite working code because you'd structure it differently. That's not maintenance, that's vandalism.
- Don't bundle unrelated fixes into one change. Each fix should stand on its own.
- Don't suggest upgrades the user didn't ask for during a bug fix. Note them separately.
- Don't fix the symptom (catch and ignore the exception) when the cause is fixable.
