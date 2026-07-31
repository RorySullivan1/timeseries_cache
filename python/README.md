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

## Write modes

All three take an **explicit** target window rather than deriving one from the
incoming data's min/max.

| Mode | Effect |
|---|---|
| `upsert` (default) | Incoming rows replace matching timestamps; rows outside the incoming index survive. |
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

- Timestamps are **tz-aware UTC**, sorted, unique. Naive input is rejected, never
  silently localized. Other zones are converted.
- Storage resolution is **microseconds**; sub-microsecond input is rejected
  rather than truncated.
- Ranges are **closed on both ends**, `[start, end]` — matching both
  `is_between(closed="both")` and pandas `.loc`.
- Cache kwargs are open-ended. Values must canonicalize deterministically: `str`,
  `int`, `float`, `bool`, `None`, `date`, `datetime`, `Decimal`, `Enum`, and
  lists/tuples of those. `start`, `end`, `mode`, `columns`, and `frame` are
  reserved for control parameters.
- A write lands completely or not at all: data first, manifest last. An
  interrupted write may leave rows nothing claims (costing a refetch); it will
  not leave coverage claiming rows that aren't there.

## Layout

```
<root>/<shard>/<digest>/manifest.json   # kwargs, coverage intervals, schema
<root>/<shard>/<digest>/data.parquet
```

The manifest keeps the kwargs verbatim, so a cache directory is self-describing
and a digest collision surfaces as an error rather than one series quietly
serving another's rows.

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
  migrated. Delete the key or migrate it deliberately.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
uv run pytest tests/test_core.py::TestReplaceWindow -q   # a single class
```

Behavioral tests are parametrized over the memory and parquet backends, and the
core scenarios run through both the polars and pandas facades.
