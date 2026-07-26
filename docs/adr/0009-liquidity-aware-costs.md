# Liquidity-aware costs (maker vs taker)

Fills are tagged **maker** or **taker**, and the Cost Model charges accordingly.

- **Maker** — a resting limit order that provides liquidity: a **limit entry** or a
  **take-profit**. It fills at its own price, so it pays the **maker fee** (which may be a
  negative *rebate*) and takes **no slippage and no spread**.
- **Taker** — an order that crosses the book: a **market** order, a **stop** trigger
  (stop-loss, stop entry), or a **signal close**. It pays the **taker fee** plus slippage
  (and spread for bid-based instruments).

Before this, every fill paid the taker fee and full slippage, which made limit-order
strategies (e.g. FVG entries, take-profits) look far worse than a live account — a real
review found the taker-on-limit assumption, not the signal, was the first domino turning a
breakeven system deeply negative.

## Mechanics
- A `Liquidity` enum threads through `CommissionModel.commission(...)`,
  `SpreadModel/SlippageModel.adjust_fill(...)`, and `CostModel.fill_price(...)`, all
  defaulting to `TAKER` for backward compatibility.
- `MakerTakerCommission(maker_rate, taker_rate)` is the liquidity-aware commission;
  `maker_rate` may be negative (rebate → a credit to cash).
- `SimulatedVenue` decides the role per fill: limit entry / take-profit → maker;
  market / stop / stop-loss / signal close → taker.
- Defaults: crypto spot maker=taker=0.10%; USD-M perp maker 0.02% / taker 0.05% (set the
  maker rate negative to model a rebate tier).

## Consequences
- Slippage and spread remain embedded in the fill price (so `gross_pnl` reflects them);
  only their *application* is now conditioned on liquidity. Commission stays a separate
  cash line, and a maker rebate shows up as a negative `costs`.
- A stop-loss is modelled as a stop-market (taker with slippage), matching how most
  exchanges execute a protective stop.
