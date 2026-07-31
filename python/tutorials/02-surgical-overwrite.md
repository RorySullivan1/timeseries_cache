# 02 — Surgical overwrite

*The three write modes, `delete`, and why the target window is always explicit.*

---

Vendors restate. Sometimes a value changes; sometimes a row that used to exist
should no longer exist at all.

That second case is what naive caching gets wrong, and it is why every write
here takes an **explicit target window** rather than inferring one from the
incoming data's min/max.

## Setup

```python
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from timeseries_cache import OverlappingWriteError, open_cache

SERIES = dict(ticker="AAPL", field="close")
TS = pl.Datetime("us", "UTC")
root = Path(tempfile.mkdtemp())


def day(d: int) -> datetime:
    return datetime(2024, 1, d, tzinfo=UTC)


def bars(rows: list[tuple[int, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts": [day(d) for d, _ in rows], "close": [c for _, c in rows]},
        schema={"ts": TS, "close": pl.Float64},
    )


def days_in(cache) -> list[int]:
    return [t.day for t in cache.read(**SERIES).frame["ts"].to_list()]
```

## `upsert` — the default

Incoming rows replace those with a matching row key; everything else survives.

```python
cache = open_cache(root / "a")
cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
cache.write(bars([(2, 999), (4, 104)]), **SERIES)

assert days_in(cache) == [1, 2, 3, 4]
assert cache.read(**SERIES).frame["close"].to_list() == [101, 999, 103, 104]
```

Day 2 replaced, day 4 added, days 1 and 3 untouched. This is what you want for
ordinary incremental loading.

## The trap: `upsert` cannot delete

Upstream restates — day 2 never traded — and sends back only days 1 and 3.

```python
cache = open_cache(root / "b")
cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
cache.write(bars([(1, 101), (3, 103)]), **SERIES)

assert days_in(cache) == [1, 2, 3]  # day 2 is STILL THERE
```

There was nothing in the incoming data to overwrite day 2 with. And inferring
*"delete anything between min and max that I didn't send"* would be a guess — a
dangerous one, since a partial refetch legitimately sends a subset.

## `replace_window` — the scalpel

Declaring the window says *"this range is now exactly what I'm handing you"*, so
the cache can delete what's absent.

```python
cache = open_cache(root / "c")
cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
cache.write(
    bars([(1, 101), (3, 103)]),
    start=day(1),
    end=day(3),
    mode="replace_window",
    **SERIES,
)

assert days_in(cache) == [1, 3]  # day 2 is gone
```

The window is explicit precisely so this is never inferred.

It respects its bounds — rows outside survive:

```python
cache = open_cache(root / "d")
cache.write(bars([(1, 101), (5, 105), (9, 109)]), **SERIES)
cache.write(bars([(5, 555)]), start=day(4), end=day(6), mode="replace_window", **SERIES)

assert days_in(cache) == [1, 5, 9]
assert cache.read(**SERIES).frame["close"].to_list() == [101, 555, 109]
```

And it refuses rows that fall outside the window you declared, rather than
quietly widening it:

```python
from timeseries_cache import WindowError

cache = open_cache(root / "d2")
try:
    cache.write(
        bars([(1, 101), (9, 109)]),
        start=day(1),
        end=day(5),
        mode="replace_window",
        **SERIES,
    )
    raise AssertionError("should have refused")
except WindowError:
    pass
```

## An empty `replace_window` empties a range but keeps it covered

```python
cache = open_cache(root / "e")
cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
cache.write(pl.DataFrame(), start=day(2), end=day(2), mode="replace_window", **SERIES)

assert days_in(cache) == [1, 3]
assert cache.read(start=day(1), end=day(3), **SERIES).is_complete
```

Day 2's row is gone, but the range is still *covered* — the cache now knows day
2 is legitimately empty and won't refetch it.

## `delete` — the opposite

```python
cache = open_cache(root / "f")
cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
cache.delete(start=day(2), end=day(2), **SERIES)

result = cache.read(start=day(1), end=day(3), **SERIES)
assert days_in(cache) == [1, 3]
assert not result.is_complete  # the range is unknown again
assert len(result.missing) == 1
```

Compare with the empty `replace_window` above. Both remove the row:

| | rows in range | coverage | next read |
|---|---|---|---|
| empty `replace_window` | removed | kept | "covered, genuinely empty" |
| `delete` | removed | removed | reported as a gap, refetched |

`replace_window` says *"I checked, it's empty"*. `delete` says *"forget I ever
asked"*.

## `append_only` — refuse overlap outright

```python
cache = open_cache(root / "g")
cache.write(bars([(1, 101), (2, 102)]), mode="append_only", **SERIES)
cache.write(bars([(3, 103)]), mode="append_only", **SERIES)
assert days_in(cache) == [1, 2, 3]

try:
    cache.write(bars([(2, 999)]), mode="append_only", **SERIES)
    raise AssertionError("should have refused")
except OverlappingWriteError:
    pass
```

For immutable sources — an exchange feed, an audit log — an overlap means
something upstream is wrong, and you want to hear about it rather than silently
accept a rewrite.

## Takeaways

- `upsert` replaces matching rows and can **never** delete.
- `replace_window` declares a range and makes it exactly what you sent — the only
  mode that removes rows a restatement dropped.
- An empty `replace_window` empties a range but keeps it covered; `delete()`
  removes the coverage too, so the range gets refetched.
- `append_only` turns an unexpected overlap into an error.

**Next:** [03 — Trade data](03-trade-data.md), for when one timestamp holds many
rows.
