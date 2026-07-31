# 2026-07-31 20:21 · composite-row-identity

**Goal:** allow duplicate timestamps keyed by identity columns

**Driver:** user's real use case is trade data — many prints share a timestamp and
are told apart by a trade id. Asked for two things: timestamps may repeat when a
unique id is supplied, *and* that composite is what identifies data to overwrite.

## Design
- `TimeseriesCache(..., identity_columns=("trade_id",))`, exposed as `cache.row_key
  == (timestamp_column, *identity_columns)`. Default `()` keeps today's behavior
  exactly — all 251 prior tests passed untouched, which is the evidence for that.
- Constructor-level, not per-call: identity is a property of how a key's data is
  shaped, not of an individual read. Different shapes = different cache instances,
  and the manifest keeps them from colliding.
- **The upsert anti-join keys on `row_key`.** This is the requirement, not a detail:
  joining on the timestamp alone means correcting one trade wipes every other print
  at that instant.
- Sorting moved to the full row key. Two reasons, both load-bearing: rows sharing a
  timestamp need a deterministic order or round-trips aren't stable, and the cheap
  adjacent-pair duplicate check is only valid if equal keys end up neighbours.
- Identity columns are always projected on read, like the timestamp — handing back
  rows the caller can't tell apart is useless. Rejected if absent or null.
- **Coverage stays purely time-based.** Identity changes what a *row* is, never what
  a *range* means, so `replace_window` / `delete` / gap reporting are untouched.
  Consequence to keep in mind: dropping a busted trade needs `replace_window` over
  its instant — an upsert has nothing to overwrite it with.
- Manifest records `identity_columns` and `verify_identity` refuses a mismatch in
  either direction. Two answers to "is this the same row" is exactly how an upsert
  silently destroys rows it should have kept.

## Gotchas & dead ends
- `from_json` uses `payload.get("identity_columns", ())`, not `[...]`. Manifests
  written before the field behaved as timestamp-only, which *is* the default, so
  back-compat needs no FORMAT_VERSION bump. There's a test that deletes the field
  from a serialized manifest to prove it.
- Polars has no composite `is_sorted`, so `_is_sorted_by` falls back to comparing
  the key columns against their sorted selves for multi-column keys. Still cheaper
  than an unconditional re-sort, and the single-column path keeps the fast native check.
- `_has_adjacent_duplicate` now takes a *sequence* and uses `all_horizontal` over
  per-column shift comparisons — a composite repeats only when every component does.
- Considered accepting a pandas `MultiIndex` (ts, trade_id) since that's a natural
  shape for this data. Didn't: it would change the returned frame's shape and the
  facade is meant to be a thin adapter. Kept the rejection but rewrote the error to
  point at `identity_columns`.

## State at end
- 314 tests green (251 unchanged + 63 new), ruff + `mypy --strict` clean on 3.11/3.12/3.13.
- Pushed to `claude/ci-workflow`; PR #2 now carries both CI and this feature, retitled
  and rewritten to cover both. 3.12 already green in CI at time of writing.
- Verified end-to-end against trade-shaped data: three prints at 14:30:00, correcting
  one leaves the other two alone; `replace_window` drops a busted trade without
  reopening the range.

## Open threads
- PR #2 not merged yet.
- Old branch `claude/timeseries-cache-template-pq0v1e` still needs deleting in the
  GitHub UI (403 from this environment's git proxy).
- Identity is fixed per key, like schema — no in-place migration path. Documented as
  a limitation rather than solved.
