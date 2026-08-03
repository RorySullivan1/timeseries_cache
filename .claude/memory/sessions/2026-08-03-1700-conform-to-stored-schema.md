# 2026-08-03 17:00 · conform-to-stored-schema

**Goal:** make the stored dtypes win, so inferred type differences stop failing
writes. PR #6.

## What happened

Last round I answered "default new writes to the stored schema?" with a narrow
version — only an all-null column defers — and explicitly warned against
generalizing it. The user came back: *"The mixed schema still does not land. I
would like the new schemas to default to the stored schema."* That is the
request restated after hearing the objection, so it is their call, and the
narrow rule was in fact too narrow: it fixed the reported symptom (a null batch
inferring `String`) and left every other inferred difference raising.

Cases that were failing and should not have been:
- a window whose values are all whole numbers → `Int64` into a `Float64` key
- a partially-null column landing on a different type per fetch
- a vendor quoting its numbers → `"102.50"`

## The design

`conform_schema=True` (default). An incoming column is cast to the stored dtype
**only where lossless for the values actually present**. Three gates:

1. strict cast succeeds — `"cheap"` → `Float64` raises, never becomes null
2. null count must not rise
3. between *exact* types (numeric, temporal, boolean) the values must
   **round-trip**

**Gate 3 is the whole reason this can be a default**, and it is the part that is
easy to omit. Polars performs all of these *without raising*:

| conversion | polars result |
|---|---|
| `1.5` → Int64 | `1` |
| `5` → Boolean | `True` |
| Datetime us → ms | truncated |
| `2**53 + 1` → Float64 | `2**53` |

Strict-cast alone lets all four through. Casting to the target and back and
requiring `eq_missing` on every value is the honest test.

Text is deliberately outside the round-trip family: `"1.50"` → `1.5` → `"1.5"`
differs in spelling, not in data, so a String source is judged only by whether
it parses. Getting this wrong the other way (round-tripping everything) would
reject perfectly good vendor data.

Conforming settles dtypes only — added/dropped columns are still refused, and
the *stored* type never changes.

## Gotchas & dead ends
- First cut applied the round-trip check to `dtype.is_numeric()` only. Polars
  says `Boolean.is_numeric() == False`, so `5 → True` sailed through and I
  caught it in the manual matrix, not the tests. Widened to numeric ∪ temporal ∪
  boolean via `_is_exact`. **Lesson: enumerate the silent conversions first and
  test the predicate against them, rather than reaching for the obvious
  predicate.**
- `_round_trips` can hit a pair polars cannot cast *back*. Unverifiable ≠ proven
  lossless, so it returns False (refuse) rather than assuming.
- Wrote a pandas test as `pd.DataFrame({"c": pd.Series([v])}, index=DatetimeIndex(...))`
  — the Series' RangeIndex aligned against the DatetimeIndex and every value
  became NaN. Use `.astype()` on a plain list instead. Classic, and the failure
  looked like a cache bug.
- Verified the tests are load-bearing by flipping the default off: 30 of 42 fail.

## State at end
- PR #6 open on `claude/conform-to-stored-schema`, 423 tests, ruff + mypy clean
  on 3.11/3.13.
- Supersedes the CLAUDE.md note that said not to generalize the null rule; that
  note is now the three-gate rule.

## Open threads
- DFS write failure still unconfirmed fixed (user never ran the diagnostic).
- Merged branches still on origin; deletion 403s from here.
