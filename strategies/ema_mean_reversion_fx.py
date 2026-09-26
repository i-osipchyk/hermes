# Parameters: 3  (<= 3 recommended; more reduces parameter_adjusted_sharpe)
#
# Mean-reversion on FX pairs (daily).  EMA50 is the gravity centre;
# a single resting limit order per side sits at the Nth-percentile distance
# of the last 50 bars' |close - EMA50| / EMA50.  That distance auto-adjusts
# each bar as recent volatility changes — no hand-tuning per pair required.
#
# Entry: limit at EMA ± dev_pct            (refreshed every bar)
# Exit:  take-profit at current EMA        (mean-reversion target, also trailing)
# Stop:  entry ± stop_mult × entry_dist    (caps trend-continuation losses)
#
# The EMADevPercentile indicator has lookback = 50 (EMA) + 50 (window) = 100,
# so the engine automatically fetches ~5 months of lead-in data before the
# trading start date — the strategy is fully warm on bar 1.

from __future__ import annotations

import math
from collections import deque
from datetime import UTC, datetime

from hermes import (
    ADX,
    EMA,
    Backtest,
    Parameter,
    OrderType,
    PortfolioBacktest,
    Side,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.core import Bar
from hermes.indicators.base import Indicator
from hermes.indicators.common import _ema_running
from hermes.execution import Order, OrderStatus
from hermes.execution.trade import Trade
from hermes.data import PepperstoneSource, YFinanceSource

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1d")

_YFINANCE_SYMBOLS = {
    # USD majors
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X",
    # EUR crosses
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X",
    "EURNZD": "EURNZD=X",
    "EURCHF": "EURCHF=X",
    # GBP crosses
    "GBPJPY": "GBPJPY=X",
    "GBPAUD": "GBPAUD=X",
    "GBPCAD": "GBPCAD=X",
    "GBPNZD": "GBPNZD=X",
    "GBPCHF": "GBPCHF=X",
    # JPY crosses
    "AUDJPY": "AUDJPY=X",
    "CADJPY": "CADJPY=X",
    "NZDJPY": "NZDJPY=X",
    "CHFJPY": "CHFJPY=X",
    # AUD/NZD crosses
    "AUDCAD": "AUDCAD=X",
    "AUDNZD": "AUDNZD=X",
    "AUDCHF": "AUDCHF=X",
    "NZDCAD": "NZDCAD=X",
    "NZDCHF": "NZDCHF=X",
}

PAIRS = list(_YFINANCE_SYMBOLS.keys())


# ---------------------------------------------------------------------------
# Custom indicator: rolling percentile of EMA deviation (% of EMA)
# ---------------------------------------------------------------------------

class EMADevPercentile(Indicator):
    """Rolling Nth-percentile of |close - EMA(ema_period)| / EMA, in percent.

    Outputs
    -------
    ema      : current EMA value
    dev_pct  : Nth percentile of the last ``window`` bars' % deviations from EMA
               (None until the window is full)
    """

    def __init__(
        self,
        timeframe: Timeframe,
        ema_period: int = 50,
        window: int = 50,
        percentile: float = 95.0,
        *,
        mode: str | None = None,
    ) -> None:
        super().__init__(timeframe, mode=mode)
        self.ema_period = ema_period
        self.window = window
        self.percentile = percentile
        self._k = 2.0 / (ema_period + 1)
        self._ema: float | None = None
        self._devs: deque[float] = deque(maxlen=window)

    @property
    def lookback(self) -> int:
        # Engine fetches at least this many bars before start — guarantees
        # both the EMA and the deviation window are warm on bar 1.
        return self.ema_period + self.window

    @property
    def outputs(self) -> tuple[str, ...]:
        return ("ema", "dev_pct")

    # -- incremental path ---------------------------------------------------

    def precompute(self, bars: list[Bar]) -> None:
        closes = [b.close for b in bars]
        emas = _ema_running(closes, self.ema_period)
        self._ema = emas[-1]
        self._devs.clear()
        for c, e in zip(closes, emas):
            if e is not None:
                self._devs.append(abs(c - e) / e * 100.0)
        self._current = self._value()

    def on_bar_closed(self, bars: list[Bar]) -> None:
        if self._ema is None:
            self.precompute(bars)
            return
        close = bars[-1].close
        self._ema = close * self._k + self._ema * (1 - self._k)
        self._devs.append(abs(close - self._ema) / self._ema * 100.0)
        self._current = self._value()

    def on_forming_bar(self, bars: list[Bar]) -> None:
        if self._ema is None:
            self._current = {"ema": None, "dev_pct": None}
            return
        close = bars[-1].close
        tent_ema = close * self._k + self._ema * (1 - self._k)
        tent_dev = abs(close - tent_ema) / tent_ema * 100.0
        tent_devs = list(self._devs) + [tent_dev]
        p = self._pct(tent_devs) if len(tent_devs) >= self.window else None
        self._current = {"ema": tent_ema, "dev_pct": p}
        # _ema and _devs NOT mutated

    # -- fallback (unit tests) ----------------------------------------------

    def compute(self, bars: list[Bar]) -> dict[str, float | None]:
        closes = [b.close for b in bars]
        emas = _ema_running(closes, self.ema_period)
        devs = [
            abs(c - e) / e * 100.0
            for c, e in zip(closes, emas)
            if e is not None
        ]
        if len(devs) < self.window:
            return {"ema": emas[-1], "dev_pct": None}
        return {"ema": emas[-1], "dev_pct": self._pct(devs[-self.window:])}

    # -- helpers ------------------------------------------------------------

    def _value(self) -> dict[str, float | None]:
        if self._ema is None or len(self._devs) < self.window:
            return {"ema": self._ema, "dev_pct": None}
        return {"ema": self._ema, "dev_pct": self._pct(list(self._devs))}

    def _pct(self, values: list[float]) -> float:
        """Nearest-rank percentile (no numpy dependency)."""
        s = sorted(values)
        idx = min(int(math.ceil(len(s) * self.percentile / 100.0)) - 1, len(s) - 1)
        return s[max(idx, 0)]


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class EMAMeanReversionFX(Strategy):
    """Mean-reversion on a single FX pair using a statistically-derived entry
    distance.  Instantiated once per pair inside PortfolioBacktest."""

    def setup(self) -> None:
        percentile = self.param(
            Parameter("percentile", 95.0, bounds=(50.0, 99.0),
                      description="Percentile of the 50-bar EMA-deviation distribution used as entry distance")
        )
        self._stop_mult = self.param(
            Parameter("stop_mult", 2.0, bounds=(1.0, 20.0),
                      description="Stop-loss distance as a multiple of the entry distance (entry ± stop_mult × entry_dist)")
        )
        self._adx_threshold = self.param(
            Parameter("adx_threshold", 25.0, bounds=(15.0, 40.0),
                      description="Skip new entries when ADX(14) exceeds this value (trending regime filter)")
        )
        self.edp = self.use(EMADevPercentile(D1, ema_period=50, window=50, percentile=percentile))
        self.adx = self.use(ADX(D1, period=14))

    def on_start(self) -> None:
        self._buy_order: Order | None = None
        self._sell_order: Order | None = None

    def on_bar(self, bar) -> None:
        v = self.indicator_value(self.edp)
        ema = v["ema"]
        dev_pct = v["dev_pct"]
        if ema is None or dev_pct is None:
            return

        adx_val = self.indicator_value(self.adx)
        trending = adx_val["adx"] is not None and adx_val["adx"] > self._adx_threshold

        entry_dist = ema * dev_pct / 100.0

        # Trail TP on open trades as EMA drifts (no SL).
        for trade in self.venue.open_trades():
            self.modify(trade, take_profit=ema)

        # Only fade the side that has extended: if close > EMA place a sell
        # limit above; if close < EMA place a buy limit below.  This ensures
        # at most one pending order exists at a time, which plays well with
        # high leverage allocations that consume most available margin.
        above_ema = bar.close >= ema

        # --- Long side (only when price is below EMA) ---
        bo = self._buy_order
        if bo is not None and bo.status == OrderStatus.WORKING:
            self.venue.cancel(bo)
            self._buy_order = None
            bo = None
        if bo is not None and bo.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._buy_order = None
            bo = None

        if bo is None and not above_ema and not trending:
            limit_price = ema - entry_dist
            stop_price = limit_price - self._stop_mult * entry_dist
            if bar.close > limit_price:
                size = self._risk_size(limit_price, stop_price)
                if size > 0:
                    self._buy_order = self.buy(
                        size,
                        type=OrderType.LIMIT,
                        limit=limit_price,
                        take_profit=ema,
                        stop_loss=stop_price,
                        tag="buy",
                    )

        # --- Short side (only when price is above EMA) ---
        so = self._sell_order
        if so is not None and so.status == OrderStatus.WORKING:
            self.venue.cancel(so)
            self._sell_order = None
            so = None
        if so is not None and so.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._sell_order = None
            so = None

        if so is None and above_ema and not trending:
            limit_price = ema + entry_dist
            stop_price = limit_price + self._stop_mult * entry_dist
            if bar.close < limit_price:
                size = self._risk_size(limit_price, stop_price)
                if size > 0:
                    self._sell_order = self.sell(
                        size,
                        type=OrderType.LIMIT,
                        limit=limit_price,
                        take_profit=ema,
                        stop_loss=stop_price,
                        tag="sell",
                    )

    def on_trade_closed(self, trade: Trade) -> None:
        if trade.side == Side.BUY:
            self._buy_order = None
        else:
            self._sell_order = None

    def _risk_size(self, entry: float, exit_: float) -> float:
        """Size so that risk from entry to exit matches the configured sizer."""
        from hermes import RiskPercent
        equity = self.venue.equity()
        per_unit_risk = abs(entry - exit_) * self.instrument.contract_size()
        if per_unit_risk <= 0:
            return 0.0
        sizer = self.sizer
        risk_frac = sizer.pct_of_equity if isinstance(sizer, RiskPercent) else 0.01
        return self.instrument.to_native_units(equity * risk_frac / per_unit_risk)


# ---------------------------------------------------------------------------
# Discovery factory
# ---------------------------------------------------------------------------

_DEFAULT_START = datetime(2016, 1, 1, tzinfo=UTC)


def _make_source(use_yfinance: bool, leverage: float = 30.0):
    return YFinanceSource() if use_yfinance else PepperstoneSource(leverage=leverage)


def build_backtest(**overrides) -> Backtest:
    use_yf = overrides.get("use_yfinance", True)
    leverage = float(overrides.get("leverage", 30.0))
    source = _make_source(use_yf, leverage)
    pair = overrides.get("symbol", "EURUSD")
    ticker = _YFINANCE_SYMBOLS.get(str(pair), str(pair) + "=X") if use_yf else str(pair)
    source_name = "yfinance" if use_yf else "pepperstone"
    starting_cash = float(overrides.get("starting_cash", 10_000.0))
    start = overrides.get("start", _DEFAULT_START)
    kw = dict(
        strategy=EMAMeanReversionFX(),
        source=source,
        symbol=Symbol(ticker, source_name),
        timeframes=[D1],
        starting_cash=starting_cash,
        start=start,
    )
    if "end" in overrides:
        kw["end"] = overrides["end"]
    if "cost_model" in overrides:
        kw["cost_model"] = overrides["cost_model"]
    return Backtest(**kw)


def build_portfolio(**overrides) -> PortfolioBacktest:
    use_yf = overrides.get("use_yfinance", True)
    leverage = float(overrides.get("leverage", 30.0))
    source = _make_source(use_yf, leverage)
    source_name = "yfinance" if use_yf else "pepperstone"
    starting_cash = float(overrides.get("starting_cash", 10_000.0))
    start = overrides.get("start", _DEFAULT_START)
    leg_kw = dict(timeframes=[D1], starting_cash=starting_cash, start=start)
    if "end" in overrides:
        leg_kw["end"] = overrides["end"]
    if "cost_model" in overrides:
        leg_kw["cost_model"] = overrides["cost_model"]
    legs = [
        Backtest(
            strategy=EMAMeanReversionFX(),
            source=source,
            symbol=Symbol(
                _YFINANCE_SYMBOLS[pair] if use_yf else pair,
                source_name,
            ),
            **leg_kw,
        )
        for pair in PAIRS
    ]
    return PortfolioBacktest(legs=legs, starting_cash=starting_cash)


if __name__ == "__main__":
    port = build_portfolio()
    pr = port.run()
    m = pr.result.metrics
    print(
        f"trades={m.num_trades}  return={m.total_return:.2%}  "
        f"sharpe={m.sharpe:.2f}  max_dd={m.max_drawdown:.2%}  "
        f"win_rate={m.win_rate:.1%}"
    )
    print("\nPer-pair breakdown:")
    for row in sorted(pr.summary_rows(), key=lambda r: r["net_pnl"] or 0, reverse=True):
        win = f"{row['win_%']:.1f}%" if row["win_%"] is not None else "n/a"
        print(f"  {row['symbol']:<10} trades={row['trades']:>4}  "
              f"win={win:>6}  pnl={row['net_pnl']:>10,.2f}")
