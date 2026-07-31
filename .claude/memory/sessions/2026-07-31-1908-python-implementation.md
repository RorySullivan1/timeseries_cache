# 2026-07-31 19:08 · python-implementation

**Goal:** implement the Python cache: coverage manifest, write modes, both facades

## What happened
- Built `python/` to the charter: `intervals` → `keys` → `index` → `backends` → `core`
  → `pandas`. 237 tests, ruff + mypy strict clean.
- Verified end-to-end with a realistic fetch loop (business-day upstream, weekends
  come back empty): first read reports one gap, one upstream call fills it, second
  read is complete, and a `replace_window` of a single day removes it without
  reopening the range for refetch.

## Design decisions made during implementation
- **Interval algebra is discrete at 1 microsecond** (`intervals.RESOLUTION`). This is
  what makes closed-interval subtraction closable: `[0,10] - [3,5]` is
  `[0, 3-1us]` and `[5+1us, 10]`, not two half-open intervals we can't represent.
  It also defines "touching", so back-to-back runs coalesce instead of leaving a
  phantom gap. Storage dtype is `Datetime("us","UTC")` to match exactly — a
  ns/us mismatch here would be a silent off-by-one-tick in coverage.
- **Sub-microsecond input is rejected, not truncated**, in both facades. Consistent
  with "naive input is rejected, never silently localized".
- **Type-tagged canonicalization** in `keys.py`: without tags `1` and `"1"` hash
  equal. bool is checked before int (bool is an int subclass). Sets and dicts are
  rejected outright — no deterministic ordering.
- **Reserved kwargs** (`start`/`end`/`mode`/`columns`/`frame`) raise if used as cache
  kwargs. Note this only bites for names that fall through to `**kwargs` on a given
  method; where the name *is* a bound parameter, Python routes it there and it gets
  window-validated instead. Both paths are tested.
- **Schema comparison uses the live polars schema of the stored frame**, not the
  manifest's stringified copy. Manifest strings are informational. Otherwise a polars
  release that changes a dtype `repr` would invalidate every cache on disk.
- **Unknown columns on read raise `SchemaMismatchError`** rather than letting polars'
  `ColumnNotFoundError` through — the facade promises no polars leaks in *exceptions*,
  not just return values.
- **Backends own atomicity**, receiving data + manifest in one `write` call so the
  ordering (data first, manifest last) lives in one place per backend.

## Gotchas & dead ends
- `read(start=X)` with everything covered *before* X filled `end` from the hull and
  produced an inverted interval → crash. Now collapses onto the caller's explicit
  bound, so the answer is "nothing here, that instant is unknown". Only an
  explicitly-inverted window (both bounds given) still raises.
- Four of the first test failures were wrong expectations, not bugs. The instructive
  one: `write(frame([1,2]))` then `write(frame([3,4]))` does *not* produce contiguous
  coverage — derived windows span only `[min,max]` of their own rows, so daily bars
  written separately leave real gaps between them. Correct behavior; now has a test
  documenting it as the reason explicit windows are worth the keystrokes.
- pandas 3.0.5 defaults to `datetime64[us]`, not `[ns]`. A test asserting the old
  default failed. The ns-truncation check must key off values, not dtype.
- ruff `UP042` wants `StrEnum` over `(str, Enum)` on 3.11+; `SIM115` flags
  `NamedTemporaryFile(delete=False)` — used `mkstemp` instead.

## State at end
- `python/` complete: 237 tests green, ruff + `mypy --strict` clean, uv-managed.
- CLAUDE.md updated from charter to description; limitations section added.
- `uv.lock` is gitignored — this is a template meant to be copied and re-resolved,
  not an application. Revisit if CI reproducibility matters more.

## Open threads
- Single-writer per key (read-modify-write, no lock); whole-key rewrite on every
  write; schema fixed per key. All three are documented as accepted, not oversights.
- No other language port exists. The backend protocol + manifest JSON are the seam.
- No CI workflow yet — the toolchain commands are in CLAUDE.md but nothing runs them
  on push.
