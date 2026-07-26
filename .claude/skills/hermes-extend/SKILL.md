---
name: hermes-extend
description: Add a custom Indicator, DataSource, or ExecutionVenue to Hermes following its interfaces and conventions. Use when the user wants a new indicator, a new data provider/source, a broker or live execution venue, or otherwise to extend the library.
---

# Hermes: extend the library

Grow Hermes at one of its three seams. The pattern is the same each time: **subclass the
interface, mirror an existing implementation, follow the conventions, add a test** — only
the target changes. Read the interface and the mirror from the repo; don't work from
memory.

## Pick the seam

- **Indicator** → interface `src/hermes/indicators/base.py`; mirror `SMA` in
  `common.py`; wrap a TA library via `LibraryIndicator`.
  Conventions: `compute(bars)` is a **pure function** over the visible series (closed
  history **plus the current Forming Bar** — the engine passes `bars_for_compute()`);
  declare `lookback` (drives warmup/Lead-in) and return `None` per line until warm;
  multi-line indicators name their `outputs`.

- **DataSource** → interface `src/hermes/data/source.py`; mirror `BinanceSource`
  (public REST, cache) or `YFinanceSource` (split-adjust).
  Conventions: normalise to UTC `Bar`s; fetch the base timeframe and let the engine
  aggregate up (for a cTrader-style provider whose higher-TF bars are misaligned, rebuild
  from 1h — see `ctrader_source.py` and ADR-0006); use `BarCache` for fetch-once; build a
  correct `Instrument` with its `SessionCalendar` (and `day_anchor` for CFDs).

- **ExecutionVenue** (live; out of v1 scope) → interface `src/hermes/execution/venue.py`;
  mirror `SimulatedVenue`.
  Conventions: implement the same interface so a Strategy runs unchanged (ADR-0001);
  translate Hermes's logical Trades onto the venue's own model.

## Steps

1. Read the chosen interface **and** its mirror implementation in full.
2. Implement the subclass, matching the mirror's shape and the conventions above.
3. Export it from the package `__init__` if it's part of the public surface.
4. Add a test alongside `tests/`, mirroring the closest existing test
   (`test_indicators.py`, `test_cache.py`, `test_ctrader.py`) — prefer pure/offline
   coverage of the logic over anything needing live network.
5. Run `.venv/bin/python -m pytest` and `ruff check`.

Completion criterion: the new subclass implements every abstract method, has a passing
test, and lint is clean.
