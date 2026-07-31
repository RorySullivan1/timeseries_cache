# timeseries-cache (Python)

A lightweight cache for datetime-indexed data, addressed by arbitrary keyword
arguments, with surgical overwrite and explicit coverage tracking.

Polars is the storage and query engine; pandas is a first-class boundary type.

## Install

```bash
uv sync --dev                 # development
pip install -e ".[pandas]"    # with the pandas facade
```

## The idea

Storing rows tells you what data exists. It cannot tell you whether a range with
no rows was *fetched and legitimately empty* — a market holiday, a delisted
symbol — or simply *never requested*. Those need opposite responses, so this
cache records **coverage** separately from the rows.

That turns the usual "check cache, else fetch everything" into a loop that asks
upstream only for what is genuinely unknown:

```python
from timeseries_cache import open_pandas_cache

cache = open_pandas_cache("/var/cache/prices")
series = dict(ticker="AAPL", field="close", vendor="bbg")

result = cache.read(start=lo, end=hi, **series)
for gap in result.missing:  # only the real holes
    cache.write(fetch(gap.start, gap.end), start=gap.start, end=gap.end, **series)

result = cache.read(start=lo, end=hi, **series)
assert result.is_complete
```

An empty fetch is recorded, not discarded — write an empty frame with the window
it covers and that range stops being asked for:

```python
cache.write(pd.DataFrame(), start=holiday, end=holiday, **series)
```

## Rows that share a timestamp

By default the timestamp identifies a row and must be unique. Plenty of data isn't
shaped that way — trades print many times at the same instant, each with its own
id. Name the disambiguating column(s) and the timestamp is free to repeat:

```python
cache = open_pandas_cache(root, identity_columns=("trade_id",))
cache.write(book, **series)  # DatetimeIndex with repeats: fine
```

Identity becomes `(ts, trade_id)`, and that is what an `upsert` matches on — so
correcting one trade leaves its neighbours at the same instant alone:

```python
# T1, T2, T3 all print at 14:30:00. Correct only T3.
cache.write(corrected_t3, start=t, end=t, **series)
# -> T3 updated; T1 and T2 untouched
```

Pass several columns for a composite (`("venue", "trade_id")`). The index stays the
timestamp on the pandas side — identity columns are ordinary columns, not extra
index levels — and they come back on every read whether or not you project them.

A key records the identity it was written under; opening it with a different one
raises rather than quietly changing what "the same row" means.

**Coverage is unaffected.** Identity columns change what a *row* is, never what a
*range* means, so `replace_window`, `delete`, and gap reporting stay purely
temporal. To actually drop a busted trade, `replace_window` over its instant —
`upsert` has nothing to overwrite it with.

## Write modes

All three take an **explicit** target window rather than deriving one from the
incoming data's min/max.

| Mode | Effect |
|---|---|
| `upsert` (default) | Incoming rows replace those with a matching row key; rows outside the incoming set survive. |
| `replace_window` | Delete *everything* in `[start, end]`, then insert. |
| `append_only` | Reject any write overlapping existing coverage. |

`replace_window` is the reason the window is explicit. When upstream corrects
itself and a row should no longer exist, an `upsert` leaves the stale row behind
forever — there is nothing in the new data to overwrite it with. Declaring the
window deletes it:

```python
cache.write(corrected, start=t0, end=t1, mode="replace_window", **series)
```

## Contract

- Timestamps are **tz-aware UTC** and sorted. Naive input is rejected, never silently
  localized. Other zones are converted.
- A row is identified by `(timestamp, *identity_columns)`. With no identity columns —
  the default — that is the timestamp alone, and it must be unique.
- Storage resolution is **microseconds**; sub-microsecond input is rejected
  rather than truncated.
- Ranges are **closed on both ends**, `[start, end]` — matching both
  `is_between(closed="both")` and pandas `.loc`.
- Cache kwargs are open-ended. Values must canonicalize deterministically: `str`,
  `int`, `float`, `bool`, `None`, `date`, `datetime`, `Decimal`, `Enum`, and
  lists/tuples of those. `start`, `end`, `mode`, `columns`, and `frame` are
  reserved for control parameters.
- A write lands completely or not at all. Growing updates write data first and
  the manifest last; `delete`, which *shrinks* what is claimed, writes the
  manifest first. Either way an interruption leaves the cache under-claiming, so
  the cost is a refetch — never coverage claiming rows that aren't there.
- Cache kwargs canonicalize to JSON before hashing, so no value can forge its
  way into another kwarg's slot no matter what characters it contains.

## Layout

```
<root>/<shard>/<digest>/manifest.json   # kwargs, coverage intervals, schema
<root>/<shard>/<digest>/data.parquet
```

The manifest keeps the kwargs verbatim, so a cache directory is self-describing
and a digest collision surfaces as an error rather than one series quietly
serving another's rows.

## Tuning

The defaults suit a cache written once and read often. All three knobs are on
the backend:

```python
from timeseries_cache import TimeseriesCache
from timeseries_cache.backends import ParquetBackend

backend = ParquetBackend(
    root,
    row_group_size=64_000,  # the unit the reader can skip
    compression="zstd",  # or "lz4": ~30% faster writes, ~2.7x bigger files
    fsync=True,  # see the durability note below
)
cache = TimeseriesCache(backend)
```

**`row_group_size` is the one that matters.** Every read here is a time range,
and row groups are what the parquet reader can skip, so finer groups mean less
over-reading. Measured against a 2M-row key:

| | polars default | 64k (this default) |
|---|---|---|
| read 1,000-row window | ~6.0ms | ~4.4ms |
| read 50,000-row window | ~6.0ms | ~4.4ms |
| read 500,000-row window | ~9.5ms | ~9.6ms |
| append 100 rows | ~170ms | ~170ms |

Narrow and mid-width reads get ~1.35x for free; wide reads and writes are
unchanged. Going below ~16k starts costing write time and hurting wide reads.

**`fsync=False`** saves roughly 24ms per write but gives up the crash guarantee:
after a power loss the manifest may be durable while its rows are not, which is
the one state the write ordering exists to prevent. Reasonable if the cache is
genuinely disposable; not otherwise.

Writes are dominated by rewriting the key's whole parquet file (~50ms per 2M
rows, before compression choice). If that hurts, the answer is more keys — see
below.

## Known limitations

- **Single-writer.** A write is read-modify-write with no lock. Concurrent
  writers to the *same key* can lose an update; concurrent writers to different
  keys are fine, as are concurrent readers. Serialize writes per key if you need
  more than that.
- **Whole-key rewrite.** Every write rewrites the key's parquet file. Reads scale
  well (the time predicate pushes down); writes scale with the size of the key,
  not the size of the change. Partition into more keys, or across time, if a
  single key grows large enough for that to hurt.
- **Schema is fixed per key.** Adding or retyping a column is refused rather than
  migrated. Delete the key or migrate it deliberately. The same goes for
  `identity_columns`: a key can't change its notion of row identity in place.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
uv run pytest tests/test_core.py::TestReplaceWindow -q   # a single class
```

Behavioral tests are parametrized over the memory and parquet backends, and the
core scenarios run through both the polars and pandas facades.

CI runs all four commands on Python 3.11, 3.12, and 3.13 for every push to `main`
and every pull request.
