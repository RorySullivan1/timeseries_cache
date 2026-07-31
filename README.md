# timeseries_cache

A lightweight caching template for datetime-indexed data, organized by language.

It is a template, not a service: a small reference implementation to copy into a
project and adapt. Correctness of the **coverage bookkeeping** is the product; the
storage format is an implementation detail.

| Language | Status | Docs |
|---|---|---|
| [Python](python/) | Complete | [`python/README.md`](python/README.md) |

## The idea

Storing rows tells you what data exists. It cannot tell you whether a range with no
rows was *fetched and legitimately empty* — a market holiday, a delisted symbol — or
simply *never requested*. Those need opposite responses, so this cache tracks coverage
separately from the rows, and a read reports the subranges it does not know about:

```python
result = cache.read(start=lo, end=hi, ticker="AAPL", field="close")
for gap in result.missing:            # only the real holes, not the whole range
    cache.write(fetch(gap.start, gap.end), start=gap.start, end=gap.end,
                ticker="AAPL", field="close")
```

Cache identity is arbitrary keyword arguments, so nothing in the cache knows what a
"ticker" is. Writes take an explicit window, which is what lets `replace_window`
delete stale rows a corrected refetch no longer contains — the case that inference
gets wrong.

See [`CLAUDE.md`](CLAUDE.md) for the invariants every language port must uphold.
