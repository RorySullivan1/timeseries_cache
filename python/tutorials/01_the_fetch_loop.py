"""01 — The fetch loop: why coverage is tracked separately from rows.

The idea this cache is built around: **storing rows cannot tell you what you have
already asked for.** A range with no rows might be a market holiday you fetched
and got nothing for, or a range you have simply never requested. Those want
opposite responses — one should never be refetched, the other must be.

So a key records the *intervals it has covered* alongside its rows, and `read()`
hands back both the slice and the subranges it knows nothing about. Your fetch
loop then asks upstream only for real holes.

Run: uv run python tutorials/01_the_fetch_loop.py
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from timeseries_cache import open_pandas_cache

SERIES = dict(ticker="AAPL", field="close", vendor="demo")

UPSTREAM_CALLS: list[str] = []


def fetch(start: datetime, end: datetime) -> pd.DataFrame:
    """A pretend vendor. Business days only — weekends come back empty."""
    UPSTREAM_CALLS.append(f"{start.isoformat()} .. {end.isoformat()}")
    days = pd.date_range(start, end, freq="D", tz="UTC")
    days = days[days.dayofweek < 5]
    return pd.DataFrame(
        {"close": [100.0 + d.day for d in days]},
        index=pd.DatetimeIndex(days, name="ts"),
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = open_pandas_cache(Path(tmp) / "prices")
        lo = datetime(2024, 1, 1, tzinfo=UTC)
        hi = datetime(2024, 1, 14, tzinfo=UTC)

        print("=" * 68)
        print("A cold cache: everything is unknown")
        print("=" * 68)
        result = cache.read(start=lo, end=hi, **SERIES)
        print(f"rows: {len(result.frame)}")
        print(f"complete: {result.is_complete}")
        print(f"missing: {result.missing}")
        print(
            "\nNote `missing` is the *whole* window. Nothing has been fetched, so\n"
            "nothing is known — which is different from knowing it's empty."
        )

        print("\n" + "=" * 68)
        print("Fill the gaps — and only the gaps")
        print("=" * 68)
        for gap in result.missing:
            cache.write(
                fetch(gap.start, gap.end), start=gap.start, end=gap.end, **SERIES
            )
        print(f"upstream calls so far: {UPSTREAM_CALLS}")

        result = cache.read(start=lo, end=hi, **SERIES)
        print(f"\nrows: {len(result.frame)}   complete: {result.is_complete}")
        print(result.frame.head(4))

        print("\n" + "=" * 68)
        print("The payoff: weekends are EMPTY but COVERED")
        print("=" * 68)
        saturday = datetime(2024, 1, 6, tzinfo=UTC)
        weekend = cache.read(start=saturday, end=saturday, **SERIES)
        print(f"2024-01-06 (a Saturday) -> rows: {len(weekend.frame)}")
        print(f"                           complete: {weekend.is_complete}")
        print(
            "\nZero rows, but `complete` is True: the cache asked, and there was\n"
            "genuinely nothing. A cache that only stored rows could not tell this\n"
            "apart from 'never asked', and would refetch that Saturday forever."
        )

        print("\n" + "=" * 68)
        print("Re-running the loop costs nothing")
        print("=" * 68)
        before = len(UPSTREAM_CALLS)
        again = cache.read(start=lo, end=hi, **SERIES)
        for gap in again.missing:
            cache.write(
                fetch(gap.start, gap.end), start=gap.start, end=gap.end, **SERIES
            )
        print(f"upstream calls added: {len(UPSTREAM_CALLS) - before}")
        print("The loop is idempotent — that's the whole point.")

        print("\n" + "=" * 68)
        print("Extending the range asks only for the new part")
        print("=" * 68)
        later = hi + timedelta(days=7)
        extended = cache.read(start=lo, end=later, **SERIES)
        print(f"missing when asking for a wider window: {extended.missing}")
        for gap in extended.missing:
            cache.write(
                fetch(gap.start, gap.end), start=gap.start, end=gap.end, **SERIES
            )
        print("\nall upstream calls made:\n  " + "\n  ".join(UPSTREAM_CALLS))
        print(
            "\nThe second call covers only the new tail — the already covered part\n"
            "was never requested again.\n"
            "\nNotice it starts one microsecond after the old coverage ended. Ranges\n"
            "here are closed on *both* ends, so the instant 2024-01-14T00:00:00 is\n"
            "already covered; the gap begins at the next representable instant. The\n"
            "cache's time domain is discrete at one microsecond, which is what lets\n"
            "closed intervals be subtracted without leaving anything ambiguous."
        )

        print("\n" + "=" * 68)
        print("Inspecting what a key knows")
        print("=" * 68)
        print(f"coverage: {cache.coverage(**SERIES)}")
        manifest = cache.manifest(**SERIES)
        assert manifest is not None
        print(f"rows stored: {manifest.row_count}")
        print(f"kwargs kept verbatim: {manifest.kwargs}")

    print(
        "\nTakeaways:\n"
        "  * read() returns (frame, missing) — drive your fetch loop off `missing`.\n"
        "  * An empty result with is_complete=True means 'genuinely nothing there'.\n"
        "    An empty result with is_complete=False means 'never asked'.\n"
        "  * Record empty fetches. That is what stops the refetch-forever loop.\n"
        "  * Next: 02_surgical_overwrite.py — what to do when upstream restates."
    )


if __name__ == "__main__":
    main()
