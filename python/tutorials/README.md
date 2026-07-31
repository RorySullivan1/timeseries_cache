# Tutorials

Five walkthroughs, each covering one use case. Read them in order the first time
— each builds on the last — or jump to whichever matches what you're doing.

| | Covers | Read it when |
|---|---|---|
| [01 — The fetch loop](01-the-fetch-loop.md) | Coverage vs. emptiness; driving a fetch loop off `missing`; why an empty range still gets recorded | You're putting the cache in front of a vendor or API for the first time |
| [02 — Surgical overwrite](02-surgical-overwrite.md) | `upsert` / `replace_window` / `append_only`, `delete`, and why the target window is explicit | Upstream restates data, or a row that used to exist shouldn't any more |
| [03 — Trade data](03-trade-data.md) | `identity_columns`: many rows per timestamp, correcting one without disturbing its neighbours | Your rows aren't one-per-timestamp — trades, quotes, order events |
| [04 — Two facades](04-two-facades.md) | polars core vs. pandas facade, what the boundary guarantees, and the lazy read path | Deciding which facade to use, or touching the read path |
| [05 — Backends and testing](05-backends-and-testing.md) | Memory backend for tests, organising keys, tuning, durability, sizing | Wiring the cache into a project, or testing code that uses it |

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

The code here is executed, not just displayed. `tests/test_tutorials.py` pulls
every ```` ```python ```` block out of each page, concatenates them in document
order, and runs the result — so an API change that breaks an example breaks CI.

Two conventions follow from that, and both are worth keeping if you add a page:

- **Blocks build on each other** rather than each standing alone. Together they
  form one runnable script.
- **Claims are spelled as `assert`s.** Where the prose says "T1 and T2 are
  untouched", the code asserts it. That way the test checks the narrative rather
  than merely checking that nothing raised.

Blocks fenced as `text` or `py` are skipped, for snippets quoted from elsewhere
in the repo. A new tutorial is picked up automatically, but it needs a row in the
table above — that's asserted too, as are the `Next:` links between pages.
