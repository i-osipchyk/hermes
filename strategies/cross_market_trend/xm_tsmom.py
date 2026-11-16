# Parameters: 1  (lookback)
#
# Cross-market time-series momentum, baseline (idea 771bd086; Moskowitz/Ooi/Pedersen 2012).
#
#   Universe: tickers/xm_trend.json -- 8 equity-index CFDs, 3 spot metals, 7 FX majors on
#             Pepperstone, run as one capital pool (`hermes run xm_tsmom --universe xm_trend`).
#             Excluded: Treasury CFDs (history only 2023-09..2025-09) and every rolled-futures
#             commodity (crude, NatGas, softs, grains, copper): their raw CFD series carry roll
#             gaps the holder is cash-adjusted for, so a backtest would book them as P&L.
#   Signal:   sign of the trailing `lookback`-day close-to-close return, read on the first
#             daily bar of each month (monthly rebalance is a hard-coded constant).
#   Position: always in the market, long or short; flipped only when the sign flips.
#   Size:     notional = equity x LEG_VOL / (60-day realised vol, annualised), set at entry and
#             not resized while the sign holds. LEG_VOL is a constant, not a knob.
#
# Costs: spread per market (Pepperstone typical, rounded up); FX also $3.50/lot a side (Razor).
# Financing is the cost that matters on multi-month holds: indices/metals long pay USD policy
# rate + 2.5%, shorts receive (rate - 2.5%) -- the USD rate stands in for every market's local
# benchmark. FX swap is modelled as a flat 1% markup each side and ignores the rate
# differential (carry), which is a known simplification. Non-FX CFDs book P&L as if
# USD-quoted, i.e. GER40/JPN225/... behave as currency-hedged positions.

from __future__ import annotations

import math
from datetime import UTC, date, datetime

from hermes import Backtest, NotionalCash, Parameter, Strategy, Symbol, Timeframe
from hermes.data import PepperstoneSource
from hermes.execution import (
    CostModel,
    FinancingModel,
    PerLotCommission,
    RateChange,
    SlippageModel,
    SpreadModel,
)
from hermes.indicators import Indicator

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1d")
VOL_WINDOW = 60
LEG_VOL = 0.025          # annualised vol budget per market, as a fraction of equity
ADMIN_FEE = 0.025        # CFD financing markup over the benchmark (indices/metals)
FX_MARKUP = 0.01

# Approximate effective fed funds (USD benchmark) -- date it took effect, annual rate.
USD_RATE: tuple[tuple[date, float], ...] = (
    (date(2005, 1, 1), 0.0225), (date(2006, 1, 1), 0.0425), (date(2007, 1, 1), 0.0525),
    (date(2007, 10, 1), 0.045), (date(2008, 1, 1), 0.030), (date(2008, 4, 1), 0.020),
    (date(2008, 11, 1), 0.0025), (date(2009, 1, 1), 0.001), (date(2015, 12, 17), 0.004),
    (date(2016, 12, 15), 0.0065), (date(2017, 6, 15), 0.0115), (date(2017, 12, 14), 0.014),
    (date(2018, 6, 14), 0.019), (date(2018, 12, 20), 0.024), (date(2019, 8, 1), 0.021),
    (date(2019, 11, 1), 0.016), (date(2020, 3, 16), 0.001), (date(2022, 3, 17), 0.0033),
    (date(2022, 5, 5), 0.0083), (date(2022, 6, 16), 0.0158), (date(2022, 7, 28), 0.0233),
    (date(2022, 9, 22), 0.0308), (date(2022, 11, 3), 0.0383), (date(2022, 12, 15), 0.0433),
    (date(2023, 2, 2), 0.0458), (date(2023, 3, 23), 0.0483), (date(2023, 5, 4), 0.0508),
    (date(2023, 7, 27), 0.0533), (date(2024, 9, 19), 0.0483), (date(2024, 11, 8), 0.0458),
    (date(2024, 12, 19), 0.0433), (date(2025, 9, 18), 0.0408), (date(2025, 10, 30), 0.0383),
    (date(2025, 12, 11), 0.0358),
)

# Typical Pepperstone spread in price points, rounded up.
SPREAD = {
    "US500": 0.5, "US2000": 0.3, "GER40": 1.2, "UK100": 1.0, "JPN225": 8.0, "AUS200": 1.0,
    "HK50": 10.0, "EUSTX50": 1.5, "XAUUSD": 0.15, "XAGUSD": 0.02, "XPTUSD": 2.5,
    "EURUSD": 0.00002, "GBPUSD": 0.00003, "AUDUSD": 0.00003, "NZDUSD": 0.00004,
    "USDCAD": 0.00003, "USDCHF": 0.00003, "USDJPY": 0.003,
}
FX = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"}


def financing(ticker: str) -> FinancingModel:
    if ticker in FX:
        return FinancingModel(annual_rate=FX_MARKUP, short_rate=FX_MARKUP)
    return FinancingModel(
        annual_rate=USD_RATE[0][1] + ADMIN_FEE,
        short_rate=ADMIN_FEE - USD_RATE[0][1],
        schedule=tuple(RateChange(d, r + ADMIN_FEE, ADMIN_FEE - r) for d, r in USD_RATE),
    )


def cost_model(ticker: str) -> CostModel:
    return CostModel(
        commission=PerLotCommission(per_lot=3.5 if ticker in FX else 0.0),
        spread=SpreadModel(points=SPREAD.get(ticker, 1.0)),
        slippage=SlippageModel(ticks=0.0),
        financing=financing(ticker),
    )


class Trend(Indicator):
    """Trailing `lookback`-day return and 60-day annualised realised vol, on closed bars."""

    def __init__(self, timeframe: Timeframe, lookback: int) -> None:
        super().__init__(timeframe)
        self.n = lookback

    @property
    def lookback(self) -> int:
        return max(self.n, VOL_WINDOW) + 1

    @property
    def outputs(self) -> tuple[str, ...]:
        return ("ret", "vol")

    def compute(self, bars):
        if len(bars) < self.lookback:
            return {"ret": None, "vol": None}
        closes = [b.close for b in bars[-self.lookback:]]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(-VOL_WINDOW, 0)]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return {"ret": closes[-1] / closes[-1 - self.n] - 1, "vol": math.sqrt(var * 252)}


class CrossMarketTsmom(Strategy):
    base_tf = D1

    def setup(self) -> None:
        lookback = int(self.param(Parameter(
            "lookback", 252, choices=(63, 126, 189, 252, 315),
            description="Trailing return window in daily bars (252 = 12 months)")))
        self.trend = self.use(Trend(D1, lookback))
        self._month: tuple[int, int] | None = None

    def on_bar(self, bar) -> None:
        month = (bar.timestamp.year, bar.timestamp.month)
        if month == self._month:
            return
        self._month = month
        v = self.indicator_value(self.trend)
        if v["ret"] is None or not v["vol"]:
            return
        want = 1 if v["ret"] > 0 else -1
        trades = self.venue.open_trades()
        if trades and all((1 if t.side.name == "BUY" else -1) == want for t in trades):
            return
        for t in trades:
            self.close(t)
        size = NotionalCash(self.venue.equity() * LEG_VOL / v["vol"])
        (self.buy if want > 0 else self.sell)(size, tag="long" if want > 0 else "short")


def build_backtest(**overrides) -> Backtest:
    symbol = overrides.pop("symbol", Symbol("US500", "pepperstone"))
    overrides.setdefault("cost_model", cost_model(symbol.ticker))
    strategy = overrides.pop("strategy", None) or CrossMarketTsmom()
    return Backtest(
        strategy=strategy,
        source=PepperstoneSource(),
        symbol=symbol,
        timeframes=[D1],
        # In-sample window. 2020-07-01 onward is the holdout (idea 771bd086): do not run
        # there until a candidate is frozen.
        start=overrides.pop("start", datetime(2006, 1, 1, tzinfo=UTC)),
        end=overrides.pop("end", datetime(2020, 7, 1, tzinfo=UTC)),
        starting_cash=overrides.pop("starting_cash", 1_000_000),
        **overrides,
    )


if __name__ == "__main__":
    build_backtest().run()
