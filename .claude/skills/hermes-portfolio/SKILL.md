---
name: hermes-portfolio
description: Build and assess a portfolio of strategies by correlation — step 4 of the research framework. Use when the user has more than one working strategy, asks whether their strategies are diversified or redundant, wants to know what to research next to fill a gap, or mentions portfolio construction, correlation, or uncorrelated returns.
---

# Hermes: build the portfolio (step 4)

A single strategy is a single point of failure, however good its backtest. The goal is a
portfolio of **uncorrelated** return streams: risk-adjusted return scales with the number
of genuinely independent bets, so *N* uncorrelated streams cut portfolio volatility by
roughly √N while *N* variations on one edge cut it by nothing.

That makes "is this strategy good?" the wrong second question. The right one is **"is it
good *and* does it do something my existing strategies don't?"**

```bash
.venv/bin/hermes correlate                      # newest run per strategy
.venv/bin/hermes correlate <key> <key> …        # specific runs
.venv/bin/hermes correlate --frequency W        # weekly returns — steadier, needs longer history
```

Read three things from the output:

- **the matrix** — pairwise return correlation;
- **`diversification ratio`** — an equal-weight blend's volatility over the mean
  individual volatility. 1.00 means you own one bet in several costumes. Perfectly
  uncorrelated streams give ~1/√N. **Below** 1/√N means some pair is negatively
  correlated and actively cancelling, which is real and good;
- **flagged pairs** — ≥0.70 is effectively one bet; ≤0.30 genuinely diversifies.

A pair reported as *not measurable* has too little shared history to correlate. That is
not "uncorrelated" — it is unknown, and must not be counted as diversification.

## What correlation reveals that per-strategy metrics cannot

Two strategies can look completely different in code and be the same bet. Long-only trend
following on three large-cap tech names will show ~0.9 no matter how differently the
entries are written, because the position is "equities go up" three times. The diversifier
usually comes from changing something structural, in rough order of effect:

1. **Asset class or instrument** — the single biggest lever. The *same* strategy on gold
   instead of equities is often genuinely uncorrelated, and costs nothing to test.
2. **Direction** — a book with no short exposure has one regime it cannot survive. Check
   the `regime` axis: profit arriving only in Bull regimes is a correlation problem, not
   a strategy problem.
3. **Holding period** — intraday and multi-week edges answer to different drivers.
4. **Mechanism** — mean reversion against trend following.

Tweaking parameters, swapping one indicator for another, or adding a filter changes
correlation **least**. If a variant correlates ≥0.9 with its parent, it is a replacement
candidate, not an addition — keep the better one and spend the effort on a new bet.

## Deciding what to research next (step 5)

This is where the loop restarts. Combine the correlation matrix with the backlog:

```bash
.venv/bin/hermes correlate --json           # where the redundancy is
.venv/bin/hermes ideas --open               # what is available to work on
.venv/bin/hermes ideas --hit-rate           # which sources have been paying off
```

Name the **gap** before picking the idea: an asset class you have no exposure to, the
short side, a holding period you do not cover. Then choose the open idea that fills it,
rather than the one with the best-looking backtest — a fifth correlated strategy adds
risk and no return, and a mediocre uncorrelated one may add more than an excellent
redundant one.

## Honest limits

- Correlation here is measured on **backtest** equity curves over whatever history they
  share. Check the shared-period count; a confident number over 30 overlapping days is
  not a confident number.
- Correlations **move**, and they move together in crises. A 0.2 in calm markets can go
  to 0.8 in a drawdown, which is exactly when it matters. Treat these figures as the
  optimistic case.
- It says nothing about capital. Legs competing for the same account is
  `PortfolioBacktest` (one shared Account) — run that before believing a blend is
  tradeable at size.

Completion criterion: the user knows which of their strategies are redundant, what their
diversification ratio is, and the named gap the next piece of research should fill.
