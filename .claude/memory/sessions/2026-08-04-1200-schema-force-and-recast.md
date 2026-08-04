# 2026-08-04 12:00 · schema-force-and-recast

**Goal:** escape hatches for a key whose *stored* dtype is the wrong one. PR #7,
merged as `b613335`.

## What the user actually asked for

> "there needs to be a way to force a new append to fit the previously set
> schema (even if its wrong) ... we could also have a method to rework the cache
> to fit a peculiar schema."

Two halves of one problem, and they map onto opposite directions:

- **force** — bend *the batch* to the key
- **recast** — bend *the key* to reality

Third request in a row on schema handling. The arc is worth remembering: I
answered the first with the narrowest safe rule (all-null columns only), the
user said it was still too narrow, I widened it to provably-lossless conforming,
and the user then pointed out that provable losslessness still can't help when
the stored dtype is simply wrong. Each objection was right. **The lesson is that
"safe default" and "no escape hatch" are different things** — the gate was
correct, what was missing was a documented way past it.

## The design

`schema_policy` (StrEnum, replaces `conform_schema` which survives as a
deprecated alias): `strict` / `lossless` (default) / `force`. Settable on the
cache, overridable per `write()`.

`force` casts with `strict=False` and skips the round-trip gate. Three rules
keep it from being the silent corruption the gate exists to prevent:

1. **Never silent about real loss** — `SchemaForcedWarning` names the column,
   both dtypes, and *separate counts* for nulled vs. altered values.
2. **A forced write that loses nothing warns about nothing.** Otherwise callers
   filter the category and the one that mattered goes unread. This has a test.
3. **Forcing never retypes the key.** One bad batch must not redefine a column.

`recast(dtypes=, add=, drop=, force=)` rewrites the stored frame and manifest.
It is also the migration path for column-set changes a write refuses.

## The two non-obvious invariants

Neither was in the request; both would have bitten.

- **Coverage is untouched by a recast.** A dtype says nothing about which ranges
  have been fetched. Dropping coverage here would put a hole in the one thing
  the cache guarantees.
- **Retyping a row-key column must re-canonicalize.** Sort order is by row key,
  so a retype *reorders rows* — `"10"` sorts before `"9"` as text and after it
  as a number — and a forced retype can collide two distinct ids (`1.0` and
  `1.7` both truncate to `1`). Either would silently break every later upsert.
  `recast` runs `_canonicalize` whenever a row-key column is retyped, so both
  surface as errors.

## Gotchas & dead ends
- First draft of the collision test used String ids `"1"` / `"1.7"` → Int64.
  They *null* rather than collide, so it hit the null-identity check instead.
  Float64 `1.0` / `1.7` is the case that actually collides. Kept both as tests.
- `pytest.warns(None)` is gone in pytest 8; "asserts nothing warns" needs
  `warnings.catch_warnings` + `simplefilter("error", Category)`.
- Pandas facade takes pandas dtypes and translates by converting an **empty
  `pd.Series`** rather than a lookup table — the answer is by construction what
  the facade would produce, and there is no table to drift on a release.
- Verified load-bearing: disabling the force and recast guards fails 14 tests.

## State at end
- main = `b613335`, 487 tests, ruff + mypy clean, re-verified after merge.
- PRs #1-#7 all merged. No branch in flight.

## Open threads
- DFS write failure still unconfirmed fixed (user never ran the diagnostic).
- Merged branches still on origin; deletion 403s from here.
- User mentioned `scrAdmin` / gallery variant `CONFIRM_BlankVertical` — **not in
  this repo**. Belongs to another of their repos (pypptx? an xl* one?); asked
  which, unanswered so far.
