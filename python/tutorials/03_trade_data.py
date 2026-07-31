"""03 — Trade data: many rows per timestamp.

Bar data has one row per timestamp. Trade data does not — a liquid name prints
dozens of times in the same microsecond, and what tells those prints apart is a
trade id, not the clock.

Naming that column makes row identity `(ts, trade_id)`. Two things follow, and
the second is the one that matters: timestamps may repeat, and an `upsert`
matches on the *pair*, so correcting one print leaves its neighbours alone.

Run: uv run python tutorials/03_trade_data.py
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from timeseries_cache import IndexContractError, open_pandas_cache

SERIES = dict(ticker="AAPL", feed="trades")

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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        print("=" * 72)
        print("Without identity columns, repeated timestamps are refused")
        print("=" * 72)
        plain = open_pandas_cache(root / "plain")
        try:
            plain.write(
                prints([(T0, "T1", 189.10, 100), (T0, "T2", 189.11, 250)]), **SERIES
            )
        except IndexContractError as error:
            print(f"  {type(error).__name__}: {error}")
        print(
            "\n  That default is right for bars — a duplicate there is a bug. The\n"
            "  error tells you the way out for data where it isn't."
        )

        print("\n" + "=" * 72)
        print("Declare the identity and the clock is free to repeat")
        print("=" * 72)
        cache = open_pandas_cache(root / "trades", identity_columns=("trade_id",))
        book = prints(
            [
                (T0, "T1", 189.10, 100),
                (T0, "T2", 189.11, 250),
                (T0, "T3", 189.10, 50),
                (T1, "T4", 189.15, 900),
            ]
        )
        print(f"  index is unique: {book.index.is_unique}")
        cache.write(book, **SERIES)
        print(cache.read(**SERIES).frame.to_string())

        print("\n" + "=" * 72)
        print("The point: correcting one print leaves the others alone")
        print("=" * 72)
        print("  The exchange corrects T3's price to 189.12.")
        cache.write(prints([(T0, "T3", 189.12, 50)]), start=T0, end=T0, **SERIES)
        frame = cache.read(**SERIES).frame
        print(frame.to_string())
        print(
            "\n  T1 and T2 share T3's timestamp and are untouched. Had identity been\n"
            "  the timestamp alone, this write would have wiped all three prints at\n"
            "  14:30:00 and left only the correction."
        )

        print("\n" + "=" * 72)
        print("Adding a late print at an existing timestamp")
        print("=" * 72)
        cache.write(prints([(T0, "T5", 189.09, 75)]), start=T0, end=T0, **SERIES)
        print(cache.read(**SERIES).frame.to_string())
        print("\n  T5 joins the others rather than replacing anything.")

        print("\n" + "=" * 72)
        print("Busting a trade needs replace_window, not upsert")
        print("=" * 72)
        print("  T2 is busted. An upsert has nothing to overwrite it with, so:")
        survivors = cache.read(start=T0, end=T0, **SERIES).frame
        survivors = survivors[survivors["trade_id"] != "T2"]
        cache.write(survivors, start=T0, end=T0, mode="replace_window", **SERIES)
        print(cache.read(**SERIES).frame.to_string())
        print(
            "\n  Window semantics stay purely temporal: replace_window clears the\n"
            "  whole instant and reinstates exactly what you sent. Identity changes\n"
            "  what a *row* is, never what a *range* means."
        )
        result = cache.read(start=T0, end=T1, **SERIES)
        print(f"\n  still covered, no refetch triggered: {result.is_complete}")

        print("\n" + "=" * 72)
        print("Composite identities, and the guard against mixing them up")
        print("=" * 72)
        multi = open_pandas_cache(
            root / "multi", identity_columns=("venue", "trade_id")
        )
        print(f"  row key: {multi.row_key}")
        print(
            "\n  A key also remembers the identity it was written under. Opening the\n"
            "  trades cache without identity_columns:"
        )
        wrong = open_pandas_cache(root / "trades")
        try:
            wrong.read(**SERIES)
        except Exception as error:
            print(f"    {type(error).__name__}: {error}")
        print(
            "\n  Two answers to 'is this the same row' is how an upsert silently\n"
            "  destroys rows it should have kept, so it's refused."
        )

    print(
        "\nTakeaways:\n"
        "  * identity_columns=('trade_id',) lets timestamps repeat and makes\n"
        "    (ts, trade_id) the unit of uniqueness and of overwrite.\n"
        "  * Identity columns stay ordinary columns; the pandas index is still the\n"
        "    timestamp, and it may have duplicates.\n"
        "  * They come back on every read whether or not you project them.\n"
        "  * Coverage, replace_window and delete remain time-based.\n"
        "  * Next: 04_two_facades.py — polars core, pandas boundary."
    )


if __name__ == "__main__":
    main()
