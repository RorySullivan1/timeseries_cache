# 01 — The fetch loop

*Coverage vs. emptiness, and why the cache records ranges it fetched even when
they came back empty.*

---

Storing rows cannot tell you what you have already asked for.

A range with no rows might be a market holiday you fetched and got nothing for,
or a range you have simply never requested. Those want opposite responses — one
should never be refetched, the other must be — and a cache that only stores rows
cannot tell them apart.

So a key records the **intervals it has covered** alongside its rows, and
`read()` hands back both the slice and the subranges it knows nothing about.

## Setup

A pretend vendor that only has business days, so weekends legitimately come back
empty:

```python
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from timeseries_cache import open_pandas_cache

SERIES = dict(ticker="AAPL", field="close", vendor="demo")
upstream_calls: list[str] = []


def fetch(start: datetime, end: datetime) -> pd.DataFrame:
    upstream_calls.append(f"{start.isoformat()} .. {end.isoformat()}")
    days = pd.date_range(start, end, freq="D", tz="UTC")
    days = days[days.dayofweek < 5]  # business days only
    return pd.DataFrame(
        {"close": [100.0 + d.day for d in days]},
        index=pd.DatetimeIndex(days, name="ts"),
    )


cache = open_pandas_cache(Path(tempfile.mkdtemp()) / "prices")
lo = datetime(2024, 1, 1, tzinfo=UTC)
hi = datetime(2024, 1, 14, tzinfo=UTC)
```

## A cold cache: everything is unknown

```python
result = cache.read(start=lo, end=hi, **SERIES)

assert len(result.frame) == 0
assert not result.is_complete
assert list(result.missing) == [result.requested]  # the whole window
```

`missing` is the entire requested window. Nothing has been fetched, so nothing
is known — which is different from knowing it is empty.

## Fill the gaps, and only the gaps

```python
for gap in result.missing:
    cache.write(fetch(gap.start, gap.end), start=gap.start, end=gap.end, **SERIES)

assert len(upstream_calls) == 1

result = cache.read(start=lo, end=hi, **SERIES)
assert result.is_complete
assert len(result.frame) == 10  # 10 business days in the range
```

## The payoff: weekends are empty *and* covered

```python
saturday = datetime(2024, 1, 6, tzinfo=UTC)
weekend = cache.read(start=saturday, end=saturday, **SERIES)

assert len(weekend.frame) == 0  # no rows
assert weekend.is_complete  # ...but we asked, and there were none
```

Zero rows with `is_complete == True` means *"the cache asked, and there was
genuinely nothing there"*. A cache that only stored rows could not distinguish
this from "never asked", and would refetch that Saturday forever.

The two states, side by side:

| | `frame` | `is_complete` | meaning |
|---|---|---|---|
| never fetched | empty | `False` | ask upstream |
| fetched, nothing there | empty | `True` | don't ask again |
| fetched, has data | rows | `True` | use it |

## The loop is idempotent

```python
before = len(upstream_calls)

again = cache.read(start=lo, end=hi, **SERIES)
for gap in again.missing:
    cache.write(fetch(gap.start, gap.end), start=gap.start, end=gap.end, **SERIES)

assert len(upstream_calls) == before  # nothing was refetched
```

Running the same loop twice costs nothing. That is the whole point.

## Extending the range asks only for the new part

```python
later = hi + timedelta(days=7)
extended = cache.read(start=lo, end=later, **SERIES)

for gap in extended.missing:
    cache.write(fetch(gap.start, gap.end), start=gap.start, end=gap.end, **SERIES)

assert len(upstream_calls) == before + 1  # one call, for the tail only
```

The second call covers only the new tail. Look closely at where it starts:

```python
gap = extended.missing.intervals[0]
assert gap.start == hi + timedelta(microseconds=1)
```

One microsecond after the old coverage ended. Ranges here are closed on **both**
ends, so the instant `2024-01-14T00:00:00` was already covered and the gap
begins at the next representable instant. The cache's time domain is discrete at
one microsecond, which is what lets closed intervals be subtracted without
leaving anything ambiguous.

## Inspecting what a key knows

```python
coverage = cache.coverage(**SERIES)
assert len(coverage) == 1  # one contiguous run

manifest = cache.manifest(**SERIES)
assert manifest is not None
assert manifest.kwargs == SERIES  # kept verbatim
assert manifest.row_count == len(cache.read(**SERIES).frame)
```

The kwargs are stored next to the hash, so a cache directory explains itself.

## Takeaways

- `read()` returns `(frame, missing)` — drive your fetch loop off `missing`.
- Empty + `is_complete=True` means *genuinely nothing there*; empty +
  `is_complete=False` means *never asked*.
- Record empty fetches. That is what stops the refetch-forever loop.

**Next:** [02 — Surgical overwrite](02-surgical-overwrite.md), for what to do
when upstream restates.
