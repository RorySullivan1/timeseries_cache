# timeseries-cache (Python)

A lightweight cache for datetime-indexed data, addressed by arbitrary keyword
arguments, with surgical overwrite and explicit coverage tracking.

Polars is the storage and query engine; pandas is a first-class boundary type.

## Install

```bash
uv sync --dev                 # development
pip install -e ".[pandas]"    # with the pandas facade
```

## Tutorials

Five walkthroughs in [`tutorials/`](tutorials/) — the fetch loop, surgical
overwrite, trade data with repeating timestamps, the two facades, and wiring the
cache into a project. Start with
[01 — The fetch loop](tutorials/01-the-fetch-loop.md).

Their code blocks are extracted and executed by the test suite, so the examples
are guaranteed to run against the code in this repo.

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
- **The stored dtypes are the key's schema**, and an incoming column conforms to
  them where that loses nothing. See below.

## The stored schema wins

Whatever produced a batch inferred its dtypes from that batch alone. A window
whose prices all happen to be whole numbers arrives as `Int64` against a key
holding `Float64`; a column that came back entirely null arrives as `String`,
because that is what pandas' `object` becomes. None of that is a schema change,
and failing the write over it is the wrong answer.

So the key's stored dtype wins and the batch conforms to it:

```python
cache.write(prices, **series)  # price: Float64
cache.write(whole_numbers, **series)  # Int64    -> stored Float64
cache.write(quoted_numbers, **series)  # "102.50" -> 102.5
cache.write(all_null_batch, **series)  # String   -> stored Float64
```

Where nothing is stored yet, an all-null column is kept as `Null` — honestly
*not yet known* — so a later write carrying real values settles it:

```python
cache.write(all_null_batch, **series)  # price: Null  ("unknown")
cache.write(real_batch, **series)  # price: Float64, settled
```

### What it will not do

Conforming is safe only because "lossless" is **checked against the values
actually present** rather than assumed. Three gates; failing any raises
`SchemaMismatchError`:

| gate | what it stops |
|---|---|
| the strict cast must succeed | `"cheap"` → `Float64` becoming `null` |
| the null count must not rise | anything quietly dropping out |
| exact types must round-trip | `1.5` → `1`, `5` → `True`, µs → ms, integers past 2⁵³ |

The third gate is the one that earns its keep: polars performs every one of
those conversions **without raising**, so a plain "cast everything to the stored
type" would take them all silently.

Text sits outside the round-trip rule deliberately — `"1.50"` parsed to `1.5`
and printed back is `"1.5"`, a difference in spelling, not in data.

Conforming settles dtypes and nothing else. An added or dropped column is a
migration, not an inference artifact, and is still refused.

To demand exact dtypes on every write instead:

```python
cache = open_cache(root, conform_schema=False)
```

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

## Network and DFS shares

If `root` is a UNC path or a mapped network drive, **stage locally**:

```python
cache = open_cache(
    r"\\server\share\cache", staging_dir=r"C:\Users\me\AppData\Local\tscache"
)
```

Parquet encoding and the fsync then happen on local disk, and only finished
bytes cross the wire — as one streamed copy rather than the many small writes
encoding otherwise pushes over SMB.

Publishing still takes two steps, because **a rename cannot cross volumes**:
`os.replace` raises `EXDEV` rather than silently copying. So the finished file is
copied to a temp *beside* the target and renamed there. Readers still only ever
see the whole old file or the whole new one — the copy lands under a name
nothing looks for, and the rename within the share is atomic as always.

```
staging_dir/  build ──fsync──►  finished file
                                     │  one streamed copy
share/        .data.parquet.tmp ◄────┘
                     │  atomic rename, same volume
              data.parquet
```

### The rename can still be refused

On Windows a file cannot be replaced while *anything* holds it open, and on a
share plenty of things transiently do: DFS Replication, the file server's
indexer, antivirus, another client that just read the same key. The rename
retries with backoff to ride those out:

```python
ParquetBackend(root, replace_attempts=5, replace_backoff=0.1)  # ~1.5s of trying
```

Raise `replace_attempts` if your share is slow to let go. If it fails on *every*
attempt, the holder is permanent rather than transient — the error says so, and
the usual causes are:

- **No delete rights.** Replacing a file needs delete permission on it, or
  delete-child on its directory. A share ACL'd "create and write, but not
  delete" — a common corporate default — lets every step of a write succeed
  except the rename, and blocks cleaning up the temp file too. No library
  change can work around this; the directory needs Modify.
- **Another writer.** See the single-writer limitation below. On a shared drive
  that stops being hypothetical.

**Staging fixes the write path, not the read path.** Reads still go straight to
the share, and the row-group skipping the cache is tuned around gives back much
less over a network. If a shared cache is a convenience rather than a
requirement, a local `root` is faster in both directions — cache locally,
publish results to the share.

## Known limitations

- **Single-writer.** A write is read-modify-write with no lock. Concurrent
  writers to the *same key* can lose an update; concurrent writers to different
  keys are fine, as are concurrent readers. Serialize writes per key if you need
  more than that.
- **Whole-key rewrite.** Every write rewrites the key's parquet file. Reads scale
  well (the time predicate pushes down); writes scale with the size of the key,
  not the size of the change. Partition into more keys, or across time, if a
  single key grows large enough for that to hurt.
- **The column set is fixed per key.** Adding or dropping one is refused rather
  than migrated — delete the key or migrate it deliberately. The same goes for
  `identity_columns`: a key can't change its notion of row identity in place.
  Dtypes are the exception: an incoming column conforms to the stored one where
  that is lossless (above), but the *stored* type never changes.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
uv run pytest tests/test_core.py::TestReplaceWindow -q   # a single class
```

Behavioral tests are parametrized over the memory and parquet backends, and the
core scenarios run through both the polars and pandas facades.

CI runs all four commands on Python 3.11, 3.12, and 3.13, on both Linux and
Windows, for every push to `main` and every pull request. Windows is covered
because POSIX and Windows disagree about open files — POSIX lets you replace or
unlink one, Windows refuses — and that difference reaches the write path.
