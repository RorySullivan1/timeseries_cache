# 05 — Backends, testing, and tuning

*For whoever is wiring the cache into a project.*

---

Everything above the `StorageBackend` protocol is coverage logic; everything
below it is bytes. That seam is what makes the cache testable without a
filesystem, portable to other storage, and tunable for your read pattern.

## Testing: the memory backend needs no filesystem

```python
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from timeseries_cache import CacheKey, MemoryBackend, ParquetBackend, TimeseriesCache

TS = pl.Datetime("us", "UTC")
BASE = datetime(2024, 1, 1, tzinfo=UTC)
root = Path(tempfile.mkdtemp())


def bars(n: int, start: int = 0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [BASE + timedelta(days=i) for i in range(start, start + n)],
            "close": [100.0 + i for i in range(start, start + n)],
        },
        schema={"ts": TS, "close": pl.Float64},
    )


cache = TimeseriesCache(MemoryBackend())
cache.write(bars(3), ticker="AAPL")
assert cache.read(ticker="AAPL").frame.height == 3
```

Same class, same semantics, nothing on disk. Point your unit tests at this and
your integration tests at `ParquetBackend`.

This repo's own suite parametrizes every behavioral test over both, which is
what proves the coverage logic doesn't depend on storage details:

```py
# tests/conftest.py
@pytest.fixture(params=["memory", "parquet"])
def backend(request, tmp_path):
    if request.param == "memory":
        return MemoryBackend()
    return ParquetBackend(tmp_path / "cache")
```

## Kwargs are the flexibility axis

Nothing in the cache knows what a ticker is.

```python
store = MemoryBackend()
cache = TimeseriesCache(store)

cache.write(bars(2), ticker="AAPL", field="close", adjusted=True)
cache.write(bars(2), ticker="AAPL", field="close", adjusted=False)
cache.write(bars(2), desk="rates", book="EUR", tenor=10)

assert len(list(store.digests())) == 3
```

`adjusted=True` and `adjusted=False` are different series. So is a rates book
with completely unrelated kwargs.

Keys are deterministic and order-independent:

```python
a = CacheKey.build({"ticker": "AAPL", "field": "close", "adjusted": True})
b = CacheKey.build({"adjusted": True, "field": "close", "ticker": "AAPL"})
assert a.digest == b.digest

# Values are type-tagged, so these never collide:
assert CacheKey.build({"n": 1}).digest != CacheKey.build({"n": "1"}).digest
assert CacheKey.build({"n": 1}).digest != CacheKey.build({"n": True}).digest
```

The canonical form is JSON, so no value can forge its way into another kwarg's
slot no matter what characters it contains:

```python
forged = CacheKey.build({"a": 'x", "b": "y'})
target = CacheKey.build({"a": "x", "b": "y"})
assert forged.digest != target.digest
```

Supported value types: `str`, `int`, `float`, `bool`, `None`, `date`,
`datetime`, `Decimal`, `Enum`, and lists/tuples of those. Sets and dicts are
refused — they have no reliable ordering across runs. `start`, `end`, `mode`,
`columns` and `frame` are reserved for control parameters.

## Keys are self-describing on disk

```python
disk = TimeseriesCache(ParquetBackend(root / "cache"))
disk.write(bars(3), ticker="AAPL", field="close")

names = sorted(p.name for p in (root / "cache").rglob("*") if p.is_file())
assert names == ["data-00000001.parquet", "manifest-00000001.json"]

manifest = disk.manifest(ticker="AAPL", field="close")
assert manifest is not None
assert manifest.kwargs == {"ticker": "AAPL", "field": "close"}
```

Layout is `<root>/<shard>/<digest>/`, sharded by the digest's first byte so a
cache with many keys doesn't build one enormous directory. The kwargs are stored
verbatim next to the hash, so a digest collision surfaces as an error rather
than one series quietly serving another's rows.

**Files are numbered, and nothing is ever renamed or overwritten.** Each write
creates the next generation and the newest manifest that parses is the one a
reader uses:

```python
disk.write(bars(4), ticker="AAPL", field="close")

generations = sorted(p.name for p in (root / "cache").rglob("manifest-*.json"))
assert generations[-1] == "manifest-00000002.json"
assert disk.read(ticker="AAPL", field="close").frame.height == 4
```

Why that matters is in the next section.

## Tuning

All three knobs are on the backend:

```python
tuned = TimeseriesCache(
    ParquetBackend(
        root / "tuned",
        row_group_size=64_000,  # the unit the reader can skip
        compression="zstd",  # or "lz4": ~30% faster writes, ~2.7x bigger
        fsync=True,  # durability; see below
    )
)
tuned.write(bars(1_000), ticker="AAPL")

lo = BASE + timedelta(days=400)
assert tuned.read(start=lo, end=lo + timedelta(days=5), ticker="AAPL").frame.height == 6
```

**`row_group_size` is the one that matters.** Every read here is a time range and
row groups are what the parquet reader can skip, so finer groups mean less
over-reading. Measured against a 2M-row key:

| | polars default | 64k (this default) |
|---|---|---|
| read 1,000-row window | ~6.0ms | ~4.4ms |
| read 50,000-row window | ~6.0ms | ~4.4ms |
| read 500,000-row window | ~9.5ms | ~9.6ms |
| append 100 rows | ~170ms | ~170ms |

Narrow and mid-width reads get ~1.35x for free. Below ~16k, write time starts
climbing and wide reads get worse.

## Caching onto a network or DFS share

**A write never renames or replaces a file**, and that is what makes a share
workable. Publishing by rename — the textbook way to make a write atomic — needs
the target to be unheld by every other process, deletable (replacing a file *is*
deleting one), and on the same volume as the temp. A share is entitled to refuse
all three, and refusing any one of them fails the write.

Creating a file needs none of that. So a write puts down `data-<n>.parquet` and
then `manifest-<n>.json`, both under names nothing is looking at yet, and the
generation becomes real the moment the manifest lands. An interrupted write
leaves a manifest that doesn't parse, which readers skip — so a partial write is
invisible rather than corrupting. Cleanup of old generations is best-effort: a
share that refuses deletes costs disk space, never a failed write.

If `root` is a UNC path or mapped drive, the build also happens locally
**automatically** — the backend detects a remote root and builds under the
system temp dir, so parquet encoding and the fsync stay off the wire. The
explicit form, for choosing the directory yourself:

```python
networked = TimeseriesCache(
    ParquetBackend(
        root / "pretend_share",
        staging_dir=root / "local_staging",
    )
)
networked.write(bars(50), ticker="AAPL")
networked.write(bars(50, start=50), ticker="AAPL")

assert networked.read(ticker="AAPL").frame.height == 100
assert not list((root / "local_staging").iterdir())  # staging is left clean
```

Parquet encoding and the fsync happen on local disk; one streamed copy of the
finished file crosses the wire, landing directly under its final name. No temp
beside the target, no rename — the generation number already makes the name
unique and unwatched.

The fsync is the part that matters most. A network redirector may refuse
`os.fsync` outright — SMB and DFS both do on some servers, which surfaces as
`Bad file descriptor` on an otherwise ordinary write. Building locally moves
that call onto a filesystem that answers it, and the one flush that still
happens on the share (of the copy that has just landed) is best-effort for
exactly that reason.

One thing staging does *not* fix: reads still go to the share, and the
row-group skipping the cache is tuned around gives back much less over a
network. A local `root` is faster in both directions where that's an option.

## The stored schema wins

Whatever produced a batch inferred its dtypes from that batch alone. A window
whose values are all whole numbers arrives as `Int64`; one that came back
entirely null arrives as `String`, because that is what pandas' `object`
becomes. Neither is a schema change, so the stored dtype wins and the batch
conforms to it:

```python
typed = TimeseriesCache(MemoryBackend())
typed.write(bars(2), ticker="AAPL")  # close: Float64


def one(day: int, value: object, dtype: pl.DataType) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts": [BASE + timedelta(days=day)], "close": [value]},
        schema={"ts": TS, "close": dtype},
    )


typed.write(one(2, 102, pl.Int64), ticker="AAPL")  # Int64  -> Float64
typed.write(one(3, "103.50", pl.String), ticker="AAPL")  # text   -> 103.5
typed.write(one(4, None, pl.String), ticker="AAPL")  # all-null nulls out

out = typed.read(ticker="AAPL").frame
assert out.schema["close"] == pl.Float64
assert out["close"].to_list() == [100.0, 101.0, 102.0, 103.5, None]
```

Where nothing is stored yet, an all-null column is kept as `Null` — honestly
*not yet known* — and the first write carrying values settles it.

### It is checked, not assumed

Conforming would be dangerous as a plain cast, because polars performs plenty of
lossy conversions **without raising**: `1.5` becomes `1`, `5` becomes `True`, a
microsecond becomes a millisecond. So every conversion has to clear three gates
— the strict cast succeeds, no new nulls appear, and between exact types the
values round-trip:

```python
import pytest

from timeseries_cache import SchemaMismatchError

whole = TimeseriesCache(MemoryBackend())
whole.write(one(1, 100, pl.Int64), ticker="AAPL")  # close: Int64

with pytest.raises(SchemaMismatchError):  # 1.5 would truncate to 1
    whole.write(one(2, 1.5, pl.Float64), ticker="AAPL")

with pytest.raises(SchemaMismatchError):  # 'cheap' would become null
    whole.write(one(3, "cheap", pl.String), ticker="AAPL")
```

Text sits outside the round-trip rule on purpose: `"1.50"` parsed to `1.5` and
printed back is `"1.5"`, a difference in spelling rather than in data. And
conforming settles dtypes only — an added or dropped column is a migration, not
an inference artifact, and is still refused.

### When the stored dtype is the wrong one

All of that assumes the key got its types right. Sometimes it didn't: the first
write that ever landed typed a column from a bad sample, and now correct data
can't get in. `schema_policy` picks the way out — `"lossless"` (the default
above), `"strict"` (exact match), or `"force"`.

`force` makes the batch fit whatever the key says, accepting the loss. It never
does so quietly:

```python
import warnings

from timeseries_cache import SchemaForcedWarning

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    whole.write(one(2, 1.5, pl.Float64), schema_policy="force", ticker="AAPL")

assert issubclass(caught[0].category, SchemaForcedWarning)
assert "1 value(s) changed" in str(caught[0].message)
assert whole.read(ticker="AAPL").frame["close"].to_list() == [100, 1]  # 1.5 -> 1
```

`recast()` fixes the *key* instead, once, so nothing has to be forced again —
and unlike a write, it can migrate the column set:

```python
report = whole.recast({"close": pl.Float64}, add={"volume": pl.Int64}, ticker="AAPL")

assert report.retyped == {"close": ("Int64", "Float64")}
assert report.added == {"volume": "Int64"}
assert not report.lost_anything

# The write that was failing now simply works.
wider = pl.DataFrame(
    {"ts": [BASE + timedelta(days=3)], "close": [103.5], "volume": [7]},
    schema={"ts": TS, "close": pl.Float64, "volume": pl.Int64},
)
whole.write(wider, ticker="AAPL")
assert whole.read(ticker="AAPL").frame["close"].to_list() == [100.0, 1.0, 103.5]
```

A recast that would lose data raises unless you pass `force=True`, which then
reports what it cost in `report.nulled` and `report.altered`. **Coverage is
untouched** either way — retyping a column says nothing about which ranges have
been fetched.

Prefer `recast` to `force`: forcing pays the loss on every write, a recast pays
it once. Through the pandas facade `recast` takes pandas dtypes
(`{"close": "float64"}`), so migrating a key never means importing polars.

## Durability

Data and manifest are published as a new generation, never written over the
live ones, so an interrupted write always **under-claims**.

Data is written first and the manifest last, always. There is no second case:
because a generation is published rather than mutated in place, a crash at any
point leaves the previous generation intact and the new one unreferenced. Both
growing and shrinking updates therefore under-claim, and a read of the lost
window answers *"unknown"* rather than *"covered, and genuinely empty"* — the
silent hole invariant 5 exists to forbid.

`fsync=False` is faster but gives that up: after a power loss the manifest may
be durable while its rows are not. Reasonable only if the cache is genuinely
disposable.

## Sizing, and the limits worth knowing

Reads scale — the time predicate pushes down. **Writes scale with the size of
the key**, not the size of the change, because a write rewrites the key's whole
parquet file. If a key grows large enough for that to hurt, the answer is more
keys: split by symbol, by month, by whatever your access pattern slices on.

Two other accepted limitations:

- **Single writer per key.** A write is read-modify-write with no lock, so two
  writers to the same key can lose an update. Different keys are fine, and so
  are concurrent readers.
- **Schema and identity are fixed per key.** Adding or retyping a column is
  refused rather than migrated; so is changing `identity_columns`.

## Porting to other storage

The `StorageBackend` protocol is the seam. Implement five methods —
`read_manifest`, `scan`, `write`, `delete`, `digests` — and the coverage logic
comes along unchanged. Two rules for an implementation:

- `scan` must return a **lazy** handle, or you lose predicate pushdown.
- `write` owns atomicity and honours `manifest_first`, per the table above.

## Takeaways

- Use `MemoryBackend` in unit tests; parametrize over both when the behavior
  under test is about the cache itself.
- Kwargs are open-ended and deterministic — organise keys around how you
  actually query, since a key is the unit of rewrite.
- `row_group_size` is the read-path lever; `fsync` is the durability one.

**Back to** [01 — The fetch loop](01-the-fetch-loop.md) for the model everything
rests on.
