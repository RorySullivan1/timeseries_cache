"""05 — Backends, testing, and tuning.

Everything above the `StorageBackend` protocol is coverage logic; everything
below it is bytes. That seam is what makes the cache testable without a
filesystem, portable to other storage, and tunable for your read pattern.

This one is aimed at whoever is wiring the cache into a project: how to test
code that uses it, how organising keys affects what you can cache, and which
knobs are worth touching.

Run: uv run python tutorials/05_backends_and_testing.py
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from timeseries_cache import (
    CacheKey,
    MemoryBackend,
    ParquetBackend,
    TimeseriesCache,
)

TS = pl.Datetime("us", "UTC")
BASE = datetime(2024, 1, 1, tzinfo=UTC)


def bars(n: int, start: int = 0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [BASE + timedelta(days=i) for i in range(start, start + n)],
            "close": [100.0 + i for i in range(start, start + n)],
        },
        schema={"ts": TS, "close": pl.Float64},
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        print("=" * 72)
        print("Testing: the memory backend needs no filesystem")
        print("=" * 72)
        cache = TimeseriesCache(MemoryBackend())
        cache.write(bars(3), ticker="AAPL")
        print(f"  rows: {cache.read(ticker='AAPL').frame.height}")
        print(
            "\n  Same class, same semantics, nothing on disk. Point your unit tests\n"
            "  at this and your integration tests at ParquetBackend. This repo's own\n"
            "  suite parametrizes every behavioral test over both, which is what\n"
            "  proves the coverage logic doesn't depend on storage details."
        )

        print("\n" + "=" * 72)
        print("Kwargs are the flexibility axis — nothing is hardcoded")
        print("=" * 72)
        store = MemoryBackend()
        cache = TimeseriesCache(store)
        cache.write(bars(2), ticker="AAPL", field="close", adjusted=True)
        cache.write(bars(2), ticker="AAPL", field="close", adjusted=False)
        cache.write(bars(2), desk="rates", book="EUR", tenor=10)
        print(f"  distinct keys stored: {len(list(store.digests()))}")
        print(
            "\n  `adjusted=True` and `adjusted=False` are different series. So is a\n"
            "  rates book with completely unrelated kwargs — the cache has no idea\n"
            "  what a ticker is, which is the point."
        )
        key = CacheKey.build({"ticker": "AAPL", "field": "close", "adjusted": True})
        print(f"\n  digest: {key.digest}")
        print(f"  canonical form: {key.canonical}")
        print(
            '\n  Values are type-tagged, so 1 and "1" never share an entry, and the\n'
            "  whole thing is JSON so no value can forge its way into another\n"
            "  kwarg's slot. Sorted keys mean call-site order is irrelevant:"
        )
        same = CacheKey.build({"adjusted": True, "field": "close", "ticker": "AAPL"})
        print(f"  reordered kwargs give the same digest: {key.digest == same.digest}")

        print("\n" + "=" * 72)
        print("Keys are self-describing on disk")
        print("=" * 72)
        disk = TimeseriesCache(ParquetBackend(root / "cache"))
        disk.write(bars(3), ticker="AAPL", field="close")
        layout = sorted(
            p.relative_to(root / "cache").as_posix()
            for p in (root / "cache").rglob("*")
            if p.is_file()
        )
        print("  " + "\n  ".join(layout))
        manifest = disk.manifest(ticker="AAPL", field="close")
        assert manifest is not None
        print(f"\n  manifest kwargs: {manifest.kwargs}")
        print(
            "\n  The kwargs are stored verbatim next to the hash, so a cache\n"
            "  directory explains itself and a digest collision surfaces as an\n"
            "  error rather than one series quietly serving another's rows."
        )

        print("\n" + "=" * 72)
        print("Tuning: row groups are the lever that matters")
        print("=" * 72)
        tuned = TimeseriesCache(
            ParquetBackend(
                root / "tuned",
                row_group_size=64_000,  # the unit the reader can skip
                compression="zstd",  # or "lz4" for faster, larger writes
                fsync=True,  # durability; see below
            )
        )
        tuned.write(bars(1_000), ticker="AAPL")
        lo = BASE + timedelta(days=400)
        window = tuned.read(start=lo, end=lo + timedelta(days=5), ticker="AAPL")
        print(f"  narrow read returned {window.frame.height} of 1000 rows")
        print(
            "\n  Every read here is a time range and row groups are what the parquet\n"
            "  reader can skip, so finer groups mean less over-reading. On a 2M-row\n"
            "  key, 64k cuts a narrow read from ~6.0ms to ~4.4ms at no write or\n"
            "  size cost. Lower it if your reads are consistently narrow; raise it\n"
            "  if they're consistently wide."
        )

        print("\n" + "=" * 72)
        print("Durability: writes land completely or not at all")
        print("=" * 72)
        print(
            "  Data and manifest are written to temp files and atomically renamed,\n"
            "  ordered so an interrupted write always *under-claims*:\n"
            "    growing   -> data first, manifest last  (rows nothing claims:\n"
            "                 harmless, costs a refetch)\n"
            "    shrinking -> manifest first, data last  (delete; the reverse would\n"
            "                 claim a range whose rows are gone — a silent hole)\n"
            "\n  fsync=False is faster but gives that up: after a power loss the\n"
            "  manifest may be durable while its rows are not. Reasonable only if\n"
            "  the cache is genuinely disposable."
        )

        print("\n" + "=" * 72)
        print("Sizing: one key is rewritten whole on every write")
        print("=" * 72)
        print(
            "  Reads scale — the time predicate pushes down. Writes scale with the\n"
            "  size of the *key*, not the size of the change. If a key grows large\n"
            "  enough for that to hurt, the answer is more keys: split by symbol,\n"
            "  by month, by whatever your access pattern slices on.\n"
            "\n  Also worth knowing: a write is read-modify-write with no lock, so\n"
            "  two writers to the same key can lose an update. Different keys are\n"
            "  fine, and so are concurrent readers."
        )

    print(
        "\nTakeaways:\n"
        "  * Use MemoryBackend in unit tests; parametrize over both backends when\n"
        "    the behavior under test is about the cache itself.\n"
        "  * Kwargs are open-ended and deterministic — organise keys around how you\n"
        "    actually query, since a key is the unit of rewrite.\n"
        "  * row_group_size is the read-path lever; fsync is the durability one.\n"
        "  * Back to 01_the_fetch_loop.py for the model everything rests on."
    )


if __name__ == "__main__":
    main()
