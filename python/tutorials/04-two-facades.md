# 04 — Two facades

*polars underneath, pandas at the boundary — and the one part of the read path
that isn't a style choice.*

---

The cache stores and queries with polars, and offers pandas as a first-class
boundary type. They are two classes rather than one class with a `frame=`
switch, so your type checker knows what comes back.

## Same storage, two return types

```python
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import polars as pl

from timeseries_cache import (
    MemoryBackend,
    PandasTimeseriesCache,
    TimeseriesCache,
    open_cache,
    open_pandas_cache,
)

SERIES = dict(ticker="AAPL", field="close")
TS = pl.Datetime("us", "UTC")
root = Path(tempfile.mkdtemp())
base = datetime(2024, 1, 1, tzinfo=UTC)

frame = pl.DataFrame(
    {
        "ts": [base + timedelta(days=i) for i in range(3)],
        "close": [101.0, 102.0, 103.0],
    },
    schema={"ts": TS, "close": pl.Float64},
)

open_cache(root / "shared").write(frame, **SERIES)

assert isinstance(open_cache(root / "shared").read(**SERIES).frame, pl.DataFrame)
assert isinstance(open_pandas_cache(root / "shared").read(**SERIES).frame, pd.DataFrame)
```

The same directory, read through either facade. Nothing about the storage
differs — the facade only decides what lands in your hands.

## What the pandas boundary guarantees

```python
out = open_pandas_cache(root / "shared").read(**SERIES).frame

assert isinstance(out.index, pd.DatetimeIndex)
assert out.index.name == "ts"
assert str(out.index.tz) == "UTC"
assert out["close"].dtype == "float64"
assert not isinstance(out["close"].dtype, pd.ArrowDtype)
```

That last line is deliberate. Conversion is **numpy-backed**: arrow-backed
pandas has different null semantics from `np.nan`, which quietly changes how
`pct_change`, `rolling` and `dropna` behave downstream — exactly the code a
price cache feeds.

A frame written and read back compares equal, index name, dtype and tz
included:

```python
original = pd.DataFrame(
    {"close": [1.5, 2.5]},
    index=pd.DatetimeIndex([base, base + timedelta(days=1)], tz="UTC", name="ts"),
)
rt = open_pandas_cache(root / "roundtrip")
rt.write(original, **SERIES)

pd.testing.assert_frame_equal(rt.read(**SERIES).frame, original)
```

## Polars never leaks through the pandas facade

```python
from timeseries_cache.errors import TimeseriesCacheError

pandas_cache = open_pandas_cache(root / "shared")
try:
    pandas_cache.read(columns=["nope"], **SERIES)
    raise AssertionError("should have refused")
except TimeseriesCacheError as error:
    assert "polars" not in type(error).__module__
```

Not a polars `ColumnNotFoundError`. A caller who only imported the pandas facade
should never have to reason about polars — in return values *or* in tracebacks.

## The read path is lazy, and that is load-bearing

```python
big = pl.DataFrame(
    {
        "ts": [base + timedelta(minutes=i) for i in range(200_000)],
        "close": [float(i) for i in range(200_000)],
    },
    schema={"ts": TS, "close": pl.Float64},
)
wide = open_cache(root / "wide")
wide.write(big, ticker="BIG")

from timeseries_cache.keys import CacheKey

scan = wide.backend.scan(CacheKey.build({"ticker": "BIG"}))
assert isinstance(scan, pl.LazyFrame)  # not a DataFrame

lo = base + timedelta(minutes=100_000)
narrow = wide.read(start=lo, end=lo + timedelta(minutes=10), ticker="BIG")
assert narrow.frame.height == 11  # 11 rows out of 200,000
```

`backend.scan()` returns a `LazyFrame` so the time predicate can be pushed into
the parquet reader, and only row groups overlapping the range are touched.

Collapsing that into a full read followed by a filter would be a **performance
regression, not a refactor** — every read this cache serves is a time range.
That is also why `DEFAULT_ROW_GROUP_SIZE` is finer than polars' default; see
[tutorial 05](05-backends-and-testing.md).

## Both facades take a backend

`open_cache` and `open_pandas_cache` are conveniences that build a
`ParquetBackend` for you. The classes themselves take any backend, so they can
share one:

```python
shared = MemoryBackend()
as_polars = TimeseriesCache(shared)
as_pandas = PandasTimeseriesCache(shared)

as_polars.write(frame, **SERIES)
assert len(as_pandas.read(**SERIES).frame) == 3
```

The convenience constructors live in `__init__.py` rather than `core.py`, which
is what keeps the core free of any concrete backend import while callers still
get a one-liner.

## Which one should you use?

- **pandas facade** if downstream code is pandas. The conversion happens once at
  the boundary and you keep numpy semantics.
- **polars facade** if you're writing new analysis, or the frames are large
  enough that a conversion per read matters.

They are interchangeable per call site — the same cache directory serves both.

## Takeaways

- `TimeseriesCache` is polars in/out; `PandasTimeseriesCache` is pandas in/out
  with a `DatetimeIndex`. Same storage, same semantics.
- Two classes, not a `frame=` flag, so return types stay static.
- pandas conversion is numpy-backed to preserve `np.nan` semantics.
- Reads stay lazy so the time predicate pushes down. Don't undo that.

**Next:** [05 — Backends and testing](05-backends-and-testing.md).
