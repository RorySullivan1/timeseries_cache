"""04 — Two facades: polars underneath, pandas at the boundary.

The cache stores and queries with polars, and offers pandas as a first-class
boundary type. They are two classes rather than one class with a `frame=`
switch, so your type checker knows what comes back.

This tutorial shows when to reach for each, what the pandas boundary guarantees,
and the one thing that is not a stylistic choice: the read path is lazy, so a
time-range read only touches the parts of the file that overlap it.

Run: uv run python tutorials/04_two_facades.py
"""

from __future__ import annotations

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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        base = datetime(2024, 1, 1, tzinfo=UTC)

        print("=" * 72)
        print("Same data, two return types")
        print("=" * 72)
        frame = pl.DataFrame(
            {
                "ts": [base + timedelta(days=i) for i in range(3)],
                "close": [101.0, 102.0, 103.0],
            },
            schema={"ts": TS, "close": pl.Float64},
        )
        polars_cache = open_cache(root / "shared")
        polars_cache.write(frame, **SERIES)

        print(
            "TimeseriesCache.read().frame  ->", type(polars_cache.read(**SERIES).frame)
        )
        pandas_cache = open_pandas_cache(root / "shared")
        print(
            "PandasTimeseriesCache          ->", type(pandas_cache.read(**SERIES).frame)
        )
        print(
            "\nThe same directory, read through either facade. Nothing about the\n"
            "storage differs — the facade only decides what lands in your hands."
        )

        print("\n" + "=" * 72)
        print("What the pandas boundary guarantees")
        print("=" * 72)
        out = pandas_cache.read(**SERIES).frame
        print(f"  index type : {type(out.index).__name__}")
        print(f"  index name : {out.index.name}")
        print(f"  index dtype: {out.index.dtype}")
        print(f"  close dtype: {out['close'].dtype}")
        print(
            "\n  numpy-backed on purpose. Arrow-backed pandas has different null\n"
            "  semantics from np.nan, which quietly changes how pct_change,\n"
            "  rolling and dropna behave downstream — exactly the code a price\n"
            "  cache feeds."
        )

        original = pd.DataFrame(
            {"close": [1.5, 2.5]},
            index=pd.DatetimeIndex(
                [base, base + timedelta(days=1)], tz="UTC", name="ts"
            ),
        )
        rt_cache = open_pandas_cache(root / "roundtrip")
        rt_cache.write(original, **SERIES)
        pd.testing.assert_frame_equal(rt_cache.read(**SERIES).frame, original)
        print("\n  A frame written and read back compares equal — index name, dtype")
        print("  and tz included.")

        print("\n" + "=" * 72)
        print("Polars never leaks through the pandas facade")
        print("=" * 72)
        try:
            pandas_cache.read(columns=["nope"], **SERIES)
        except Exception as error:
            module = type(error).__module__
            print(f"  {type(error).__name__} from {module}")
            print(
                "\n  Not a polars ColumnNotFoundError. A caller who only imported\n"
                "  the pandas facade should never have to reason about polars —\n"
                "  in return values or in tracebacks."
            )

        print("\n" + "=" * 72)
        print("The read path is lazy, and that is load-bearing")
        print("=" * 72)
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
        assert scan is not None
        lo = base + timedelta(minutes=100_000)
        plan = scan.filter(
            pl.col("ts").is_between(
                pl.lit(lo, dtype=TS),
                pl.lit(lo + timedelta(minutes=10), dtype=TS),
                closed="both",
            )
        ).explain()
        pushed = "SELECTION" in plan.upper() or "FILTER" in plan.upper()
        print(f"  backend.scan() returns: {type(scan).__name__}")
        print(f"  time predicate pushed into the scan: {pushed}")
        narrow = wide.read(start=lo, end=lo + timedelta(minutes=10), ticker="BIG")
        print(
            f"  reading 11 rows out of {big.height:,}: {narrow.frame.height} rows back"
        )
        print(
            "\n  The filter reaches the parquet reader, so only row groups that\n"
            "  overlap the range are touched. Collapsing that into a full read\n"
            "  followed by a filter would be a performance regression, not a\n"
            "  refactor — every read this cache serves is a time range."
        )

        print("\n" + "=" * 72)
        print("Choosing a facade, and bringing your own backend")
        print("=" * 72)
        shared = MemoryBackend()
        as_polars = TimeseriesCache(shared)
        as_pandas = PandasTimeseriesCache(shared)
        as_polars.write(frame, **SERIES)
        rows = len(as_pandas.read(**SERIES).frame)
        print(f"  written via polars, read via pandas: {rows} rows")
        print(
            "\n  Both facades take a backend, so they can share one. open_cache and\n"
            "  open_pandas_cache are just conveniences that build a ParquetBackend\n"
            "  for you — the core itself never imports a concrete backend."
        )

    print(
        "\nTakeaways:\n"
        "  * TimeseriesCache is polars in/out; PandasTimeseriesCache is pandas\n"
        "    in/out with a DatetimeIndex. Same storage, same semantics.\n"
        "  * Two classes, not a frame= flag, so return types stay static.\n"
        "  * pandas conversion is numpy-backed to keep np.nan semantics.\n"
        "  * Reads stay lazy so the time predicate pushes down. Don't undo that.\n"
        "  * Next: 05_backends_and_testing.py — memory backend, tuning, durability."
    )


if __name__ == "__main__":
    main()
