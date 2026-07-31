---
name: financial-timeseries-analysis
description: >-
  Financial time-series hygiene in pandas/numpy — returns vs prices,
  resampling/alignment, market calendars and missing data, stationarity,
  autocorrelation, volatility estimation (EWMA/GARCH), rolling windows, and
  building features without look-ahead. Use whenever the work touches a price or
  return series, a DatetimeIndex, resampling, rolling/expanding windows,
  volatility estimation, or lagged/rolling feature construction. Trigger on
  phrases like "compute returns", "resample to monthly", "rolling window",
  "estimate volatility", "EWMA", "stationarity", "lag this feature", "align
  these series", or "reindex to trading days". Defer generic Python idioms to
  python-development. In this repo it governs how cached frames are indexed,
  aligned, and sliced — the cache stores series, so its correctness rules apply
  to every read and write path.
---

> **Syntax note for this repo.** The examples below are pandas, but the storage
> layer is **polars** (see `CLAUDE.md`). Everything here is a statement about
> *semantics*, and the semantics carry over unchanged — translate the idiom, not
> the rule. The mapping that matters: a pandas `DatetimeIndex` is a sorted, unique,
> UTC `ts` column; `.loc[start:end]` is `filter(pl.col("ts").is_between(start, end,
> closed="both"))`; `shift(1)` is `.shift(1)` over a sorted frame (sort first —
> polars has no index to guarantee order for you); `rolling(w).std()` is
> `rolling_std(window_size=w, min_periods=...)`. The look-ahead rules in particular
> are engine-independent and are the reason this skill is here.

# Financial Time-Series Analysis

Correct handling of price and return series in pandas. The recurring danger is
**look-ahead bias** — letting information from time `t` leak into a value
timestamped `t` or earlier — and the recurring chore is getting alignment,
calendars, and resampling right so the series mean what you think they mean.

## Core principles

- **Compute returns the right way.** Simple `p.pct_change()` aggregates across
  assets; log `np.log(p).diff()` aggregates across time. The first observation is
  `NaN` — handle it explicitly, don't `fillna(0)` it into a fake flat day.
- **Every transform has a timestamp contract.** A value at index `t` may only use
  data available at or before `t`. Rolling stats, normalizations, and labels all
  break this by default unless you lag or shift deliberately.
- **Align, don't assume.** Two series rarely share an index. Join on the
  DatetimeIndex (`pd.concat(axis=1)` / `.align()`) and decide how to treat the
  gaps; never zip raw arrays of differing calendars positionally.
- **Calendars are not regular.** Markets skip weekends and holidays. A `freq="D"`
  reindex invents non-trading days; resampling needs the right closed/label side.
- **Missing data is a decision, not a default.** Forward-fill, drop, or
  interpolate are different assumptions with different biases — choose per series,
  and never forward-fill a *return* (fill the price, then recompute).

## Look-ahead: the failure mode to hunt

- **Centered windows leak the future.** `rolling(w, center=True)` and
  `ewm` without care use future points. Use trailing windows for anything that
  feeds a decision.
- **Normalize with trailing stats only.** A z-score using the full-sample mean/std
  leaks the future into every point. Use `expanding()` or trailing `rolling()`.
- **Shift features, not just intuition.** A signal computed from data through `t`
  can only act on the next bar: `signal.shift(1)` before aligning to forward
  returns. Off-by-one here silently inflates every result.
- **Resample on the close.** When downsampling, label and close on the period end
  (`resample("ME", label="right", closed="right")`) so a monthly bar doesn't
  include data stamped after its own label.

## Common operations (do them this way)

- **Returns:** `simple = px.pct_change()`, `logret = np.log(px).diff()`. Drop or
  mark the leading `NaN`.
- **Resample:** `px.resample("W-FRI").last()` for prices; sum log returns or
  compound simple returns (`(1+r).prod()-1`) when aggregating returns — never
  `.mean()` a return over a coarser period.
- **Align to a trading calendar:** reindex to an explicit trading-day index (e.g.
  from `pandas_market_calendars` if available), not `freq="B"`, which still
  includes holidays.
- **Rolling stats:** `ret.rolling(252).std() * np.sqrt(252)` for trailing annual
  vol; `min_periods` set so early values aren't computed on a partial window.
- **EWMA volatility:** `ret.ewm(span=n).std()` (RiskMetrics-style); for clustering
  beyond EWMA reach for a GARCH model via the `arch` package (justify + pin).

## Stationarity & dependence

- **Prices aren't stationary; returns usually are.** Model returns (or log
  prices' differences), not raw price levels, for most statistical work.
- **Test, don't assume.** ADF (`statsmodels.tsa.stattools.adfuller`) for a unit
  root; inspect ACF/PACF for autocorrelation before fitting AR/MA orders.
- **Heteroskedasticity is the norm.** Volatility clusters; use EWMA/GARCH or
  robust (HAC/Newey–West) standard errors rather than assuming constant variance.

## Checklist

- [ ] Returns computed with the right type (simple vs log) for the use, leading
      `NaN` handled honestly.
- [ ] No centered windows or full-sample stats feeding a decision; features
      `shift`ed so each value uses only past data.
- [ ] Series aligned on the DatetimeIndex; gaps handled by an explicit, justified
      rule (and returns never forward-filled).
- [ ] Resampling uses correct `closed`/`label` and the right aggregation
      (compound returns, last price).
- [ ] Reindexing respects the trading calendar — no invented non-trading days.
- [ ] `min_periods` set on rolling/expanding stats; partial-window values not
      trusted.

## Anti-patterns

- `rolling(w, center=True)` or full-sample z-scores in anything predictive.
- Forward-filling returns (fill the *price*, then recompute returns).
- `fillna(0)` on a leading return to dodge the `NaN`.
- Averaging returns when resampling instead of compounding/summing.
- `freq="D"`/`"B"` reindex that ignores market holidays.
- Aligning two series by position (`.values`) instead of by index.
- Modeling raw price levels with tools that assume stationarity.

## Out of scope

- **General pandas/Python idioms unrelated to time-series correctness** →
  `python-development`.
- **Reviewing existing code for defects** → `python-review`.
- **Cache identity, coverage bookkeeping, and overwrite semantics** → `CLAUDE.md`.
  This skill covers what a *correct series* looks like; the cache layer decides
  what is *stored* and *known*.
