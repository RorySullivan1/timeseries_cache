# Tutorials

Runnable, self-contained walkthroughs. Each is a plain script that narrates what
it is doing and why, writes to a temporary directory, and cleans up after itself.

```bash
uv sync --dev
uv run python tutorials/01_the_fetch_loop.py
```

Read them in order the first time — each builds on the last — or jump to the one
that matches what you're doing.

| | Covers | Read it when |
|---|---|---|
| [`01_the_fetch_loop.py`](01_the_fetch_loop.py) | Coverage vs. emptiness; driving a fetch loop off `missing`; why an empty range still gets recorded | You're wiring the cache in front of a vendor or API for the first time |
| [`02_surgical_overwrite.py`](02_surgical_overwrite.py) | `upsert` / `replace_window` / `append_only`, `delete`, and why the target window is explicit | Upstream restates data, or a row that used to exist shouldn't any more |
| [`03_trade_data.py`](03_trade_data.py) | `identity_columns`: many rows per timestamp, correcting one without disturbing its neighbours | Your rows aren't one-per-timestamp — trades, quotes, order events |
| [`04_two_facades.py`](04_two_facades.py) | polars core vs. pandas facade, what the boundary guarantees, and the lazy read path | Deciding which facade to use, or touching the read path |
| [`05_backends_and_testing.py`](05_backends_and_testing.py) | Memory backend for tests, organising keys, tuning, durability, sizing | Wiring the cache into a project, or testing code that uses it |

## The one idea to take away

Storing rows cannot tell you what you have already asked for. A range with no
rows might be a holiday you fetched and got nothing for, or a range you have
never requested — and those want opposite responses. So a key records the
intervals it has covered alongside its rows, and `read()` returns both the slice
and the subranges it knows nothing about:

```python
result = cache.read(start=lo, end=hi, ticker="AAPL", field="close")
for gap in result.missing:  # only the real holes
    cache.write(
        fetch(gap.start, gap.end),
        start=gap.start,
        end=gap.end,
        ticker="AAPL",
        field="close",
    )
```

Everything else in these tutorials is a consequence of that.

## Keeping them honest

`tests/test_tutorials.py` runs every script in this directory and fails if any
exits non-zero, so a change to the API that breaks an example breaks CI. If you
add a tutorial here, it is picked up automatically — no registration needed.
