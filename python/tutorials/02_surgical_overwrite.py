"""02 — Surgical overwrite: the three write modes, and why the window is explicit.

Vendors restate. Sometimes a value changes; sometimes a row that used to exist
should no longer exist at all. That second case is the one naive caching gets
wrong, and it is why every write here takes an **explicit target window** rather
than inferring one from the incoming data's min/max.

Run: uv run python tutorials/02_surgical_overwrite.py
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from timeseries_cache import OverlappingWriteError, open_cache

SERIES = dict(ticker="AAPL", field="close")
TS = pl.Datetime("us", "UTC")


def day(d: int) -> datetime:
    return datetime(2024, 1, d, tzinfo=UTC)


def bars(rows: list[tuple[int, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts": [day(d) for d, _ in rows], "close": [c for _, c in rows]},
        schema={"ts": TS, "close": pl.Float64},
    )


def show(cache, label: str) -> None:
    frame = cache.read(**SERIES).frame
    pairs = list(zip(frame["ts"].to_list(), frame["close"].to_list(), strict=True))
    rendered = ", ".join(f"{t:%d}:{c:g}" for t, c in pairs)
    print(f"  {label:<26} {rendered}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        print("=" * 72)
        print("upsert (the default) — replace matching rows, keep the rest")
        print("=" * 72)
        cache = open_cache(root / "a")
        cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
        show(cache, "initial")
        cache.write(bars([(2, 999), (4, 104)]), **SERIES)
        show(cache, "after upsert 2,4")
        print(
            "\n  Day 2 was replaced, day 4 added, days 1 and 3 untouched. This is\n"
            "  what you want for ordinary incremental loading."
        )

        print("\n" + "=" * 72)
        print("The trap: upsert cannot delete")
        print("=" * 72)
        cache = open_cache(root / "b")
        cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
        show(cache, "initial")
        print("  Upstream restates: day 2 never traded. It sends back only 1 and 3.")
        cache.write(bars([(1, 101), (3, 103)]), **SERIES)
        show(cache, "after upsert 1,3")
        print(
            "\n  Day 2 is STILL THERE. There was nothing in the incoming data to\n"
            "  overwrite it with, and inferring 'delete anything between min and\n"
            "  max that I didn't send' would be a guess — a dangerous one, since\n"
            "  a partial refetch legitimately sends a subset."
        )

        print("\n" + "=" * 72)
        print("replace_window — the scalpel")
        print("=" * 72)
        cache = open_cache(root / "c")
        cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
        show(cache, "initial")
        cache.write(
            bars([(1, 101), (3, 103)]),
            start=day(1),
            end=day(3),
            mode="replace_window",
            **SERIES,
        )
        show(cache, "after replace [1,3]")
        print(
            "\n  Day 2 is gone. Declaring the window says 'this range is now exactly\n"
            "  what I'm handing you' — so the cache can delete what's absent. The\n"
            "  window is explicit precisely so this is never inferred."
        )

        print("\n" + "=" * 72)
        print("replace_window respects its bounds")
        print("=" * 72)
        cache = open_cache(root / "d")
        cache.write(bars([(1, 101), (5, 105), (9, 109)]), **SERIES)
        show(cache, "initial")
        cache.write(
            bars([(5, 555)]),
            start=day(4),
            end=day(6),
            mode="replace_window",
            **SERIES,
        )
        show(cache, "after replace [4,6]")
        print("\n  Days 1 and 9 are outside the window and survive untouched.")

        print("\n" + "=" * 72)
        print("An empty replace_window is a deletion that stays covered")
        print("=" * 72)
        cache = open_cache(root / "e")
        cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
        cache.write(
            pl.DataFrame(), start=day(2), end=day(2), mode="replace_window", **SERIES
        )
        show(cache, "after empty replace [2,2]")
        result = cache.read(start=day(1), end=day(3), **SERIES)
        print(f"  still complete over [1,3]: {result.is_complete}")
        print(
            "\n  Day 2's row is gone, but the range is still *covered* — the cache\n"
            "  now knows day 2 is legitimately empty and won't refetch it."
        )

        print("\n" + "=" * 72)
        print("delete — the opposite: forget the range entirely")
        print("=" * 72)
        cache = open_cache(root / "f")
        cache.write(bars([(1, 101), (2, 102), (3, 103)]), **SERIES)
        cache.delete(start=day(2), end=day(2), **SERIES)
        show(cache, "after delete [2,2]")
        result = cache.read(start=day(1), end=day(3), **SERIES)
        print(f"  missing after delete: {result.missing}")
        print(
            "\n  Compare with the empty replace_window above. Both remove the row;\n"
            "  replace_window says 'I checked, it's empty', delete says 'forget I\n"
            "  ever asked'. Only the latter comes back as a gap to refetch."
        )

        print("\n" + "=" * 72)
        print("append_only — refuse overlap outright")
        print("=" * 72)
        cache = open_cache(root / "g")
        cache.write(bars([(1, 101), (2, 102)]), mode="append_only", **SERIES)
        show(cache, "initial")
        cache.write(bars([(3, 103)]), mode="append_only", **SERIES)
        show(cache, "after appending 3")
        try:
            cache.write(bars([(2, 999)]), mode="append_only", **SERIES)
        except OverlappingWriteError as error:
            print(f"\n  rewriting day 2 raised: {type(error).__name__}")
            print(
                "  For immutable sources — an exchange feed, an audit log — an\n"
                "  overlap means something upstream is wrong, and you want to hear\n"
                "  about it rather than silently accept a rewrite."
            )

    print(
        "\nTakeaways:\n"
        "  * upsert replaces matching rows and can never delete.\n"
        "  * replace_window declares a range and makes it exactly what you sent —\n"
        "    the only mode that removes rows a restatement dropped.\n"
        "  * An empty replace_window empties a range but keeps it covered;\n"
        "    delete() removes the coverage too, so the range is refetched.\n"
        "  * append_only turns an unexpected overlap into an error.\n"
        "  * Next: 03_trade_data.py — when one timestamp holds many rows."
    )


if __name__ == "__main__":
    main()
