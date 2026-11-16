# Raw bars + corporate-action events, adjusted at read time

Status: accepted (audit F16, G6). Applies to **daily (`1d`) bars from `YFinanceSource` and
`TiingoSource`**.

## Problem

Both providers return prices adjusted to *today's* basis and restate their whole history on
every new split or dividend. The bar cache stored those and fetched only missing ranges, so
bars cached before an ex-date and bars fetched after it sat on different bases and produced
a fake gap at the seam (≈ the dividend yield; 50% for a 2:1 split) — exactly what gap, dip
and value strategies read. Nothing recorded the basis or fetch date.

## Decision

* The cache holds what was **printed** (`<cache>/_raw/…`), which never changes, plus a small
  per-ticker table of dated **events** (`<cache>/_adjustments/<source>/<ticker>.json`):
  split ratio, cash dividend, and the raw close before the ex-date.
* Adjustment happens when bars are read (`data/adjustments.py::adjust_bars`). A new
  dividend adds one event and rewrites no bar.
* `adjust=` on the source: `"total"` (default — splits and dividends, what the sources
  served before), `"split"` (splits only: keeps real ex-dividend gaps), `"none"`.
* Total-return factor for earlier prices is `1 − dividend / prev_close` (the Yahoo/CRSP
  method, basis-invariant so a later split does not alter it); splits divide prices and
  multiply volume. A bar is "earlier" when its UTC date is before the ex-date.
* yfinance's *unadjusted* prices/volume/dividends are still split-restated, so they are
  un-split on ingest by every split with a later ex-date, giving true raw values.
* Tiingo: raw `open/high/low/close/volume` and `divCash`/`splitFactor` are used; the `adj*`
  fields are ignored. Fetches are padded back 10 days so an ex-date on the first requested
  day still sees its prior close.
* `DataSource.data_epoch(ticker)` = `<adjust>:<digest of the event table>`. It is part of
  `cache_key` (single symbol and universe) and the run is re-keyed after fetching, so a run
  is identified by the data it actually read and an identical request is a ledger hit.

## Consequences

* The first daily request for a ticker re-fetches (old adjusted `_1d` files are no longer
  read; delete `.hermes_cache/bars/{yfinance,tiingo}/*_1d.*` when convenient). Tiingo calls
  are paid, so expect one pass.
* A split/dividend the provider announces *after* the last fetch is unknown until the next
  fetch — recorded in the epoch, not hidden.
* Numbers can differ slightly from the provider's own adjusted series (own dividend factor,
  rounding). That is deliberate: our basis is explicit and stable.
* Not covered: intraday and weekly bars (a weekly bar spans ex-dates, intraday history is
  short) and the other sources keep their original path. The prior-close rule ignores a
  dividend and split on the same date (rare). The raw-vs-provider agreement was **not
  checked against live data** (offline session).
