# 03 — Trade data

*Many rows per timestamp, and correcting one of them without disturbing its
neighbours.*

---

Bar data has one row per timestamp. Trade data does not — a liquid name prints
dozens of times in the same microsecond, and what tells those prints apart is a
trade id, not the clock.

Naming that column makes row identity `(ts, trade_id)`. Two things follow, and
the second is the one that matters.

## Setup

```python
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from timeseries_cache import (
    IndexContractError,
    InvalidIdentityError,
    open_pandas_cache,
)

SERIES = dict(ticker="AAPL", feed="trades")
root = Path(tempfile.mkdtemp())

T0 = datetime(2024, 3, 5, 14, 30, 0, tzinfo=UTC)
T1 = datetime(2024, 3, 5, 14, 30, 1, tzinfo=UTC)


def prints(rows: list[tuple[datetime, str, float, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_id": [tid for _, tid, _, _ in rows],
            "price": [p for _, _, p, _ in rows],
            "size": [s for _, _, _, s in rows],
        },
        index=pd.DatetimeIndex([t for t, _, _, _ in rows], tz="UTC", name="ts"),
    )
```

## By default, repeated timestamps are refused

```python
plain = open_pandas_cache(root / "plain")

try:
    plain.write(prints([(T0, "T1", 189.10, 100), (T0, "T2", 189.11, 250)]), **SERIES)
    raise AssertionError("should have refused")
except IndexContractError as error:
    assert "identity_columns" in str(error)
```

That default is right for bars, where a duplicate timestamp is a bug. The error
message points at the way out for data where it isn't.

## Declare the identity and the clock is free to repeat

```python
cache = open_pandas_cache(root / "trades", identity_columns=("trade_id",))
assert cache.row_key == ("ts", "trade_id")

book = prints(
    [
        (T0, "T1", 189.10, 100),
        (T0, "T2", 189.11, 250),
        (T0, "T3", 189.10, 50),
        (T1, "T4", 189.15, 900),
    ]
)
assert not book.index.is_unique  # three prints share 14:30:00

cache.write(book, **SERIES)
assert len(cache.read(**SERIES).frame) == 4
```

## The point: correcting one print leaves the others alone

The exchange corrects T3's price to 189.12.

```python
cache.write(prints([(T0, "T3", 189.12, 50)]), start=T0, end=T0, **SERIES)

frame = cache.read(**SERIES).frame
assert frame["trade_id"].tolist() == ["T1", "T2", "T3", "T4"]
assert frame["price"].tolist() == [189.10, 189.11, 189.12, 189.15]
```

T1 and T2 share T3's timestamp and are untouched. Had identity been the
timestamp alone, this write would have wiped all three prints at 14:30:00 and
left only the correction.

A late print at an existing timestamp joins the others rather than replacing
anything:

```python
cache.write(prints([(T0, "T5", 189.09, 75)]), start=T0, end=T0, **SERIES)

assert cache.read(**SERIES).frame["trade_id"].tolist() == ["T1", "T2", "T3", "T5", "T4"]
```

Note the order: rows sort by the **full row key**, `(ts, trade_id)`. T5 slots in
among the 14:30:00 prints, and T4 stays last because it belongs to the later
timestamp — not because of its name. Sorting on the whole key is what makes the
stored order deterministic when timestamps repeat.

## Busting a trade still needs `replace_window`

T2 is busted. An `upsert` has nothing to overwrite it with — the same rule as
[tutorial 02](02-surgical-overwrite.md), unchanged by identity columns.

```python
survivors = cache.read(start=T0, end=T0, **SERIES).frame
survivors = survivors[survivors["trade_id"] != "T2"]

cache.write(survivors, start=T0, end=T0, mode="replace_window", **SERIES)

frame = cache.read(**SERIES).frame
assert frame["trade_id"].tolist() == ["T1", "T3", "T5", "T4"]
assert cache.read(start=T0, end=T1, **SERIES).is_complete  # no refetch triggered
```

Window semantics stay purely temporal: `replace_window` clears the whole instant
and reinstates exactly what you sent. **Identity changes what a *row* is, never
what a *range* means** — coverage, `replace_window` and `delete` are unaffected.

## The shape on the pandas side

The index stays the timestamp and may repeat; identity columns are ordinary
columns, not extra index levels.

```python
frame = cache.read(**SERIES).frame
assert isinstance(frame.index, pd.DatetimeIndex)
assert "trade_id" in frame.columns
```

They also come back on every read whether or not you project them — rows you
can't tell apart are useless:

```python
projected = cache.read(columns=["price"], **SERIES).frame
assert "trade_id" in projected.columns
```

A `MultiIndex` is rejected, with an error pointing at `identity_columns` as the
supported way to express that shape.

## Composite identities, and the guard against mixing them up

Pass several columns when one isn't enough:

```python
multi = open_pandas_cache(root / "multi", identity_columns=("venue", "trade_id"))
assert multi.row_key == ("ts", "venue", "trade_id")
```

A key remembers the identity it was written under, and refuses to be read under
a different one:

```python
wrong = open_pandas_cache(root / "trades")  # no identity_columns

try:
    wrong.read(**SERIES)
    raise AssertionError("should have refused")
except InvalidIdentityError as error:
    assert "was written with" in str(error)
```

Two answers to *"is this the same row"* is exactly how an `upsert` silently
destroys rows it should have kept.

## Takeaways

- `identity_columns=("trade_id",)` lets timestamps repeat and makes
  `(ts, trade_id)` the unit of uniqueness **and** of overwrite.
- Identity columns stay ordinary columns; the pandas index is still the
  timestamp, duplicates and all.
- They come back on every read whether or not you project them.
- Coverage, `replace_window` and `delete` remain purely time-based.

**Next:** [04 — Two facades](04-two-facades.md), for polars core and the pandas
boundary.
