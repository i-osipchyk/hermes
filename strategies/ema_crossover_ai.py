"""EMA Close Crossover strategy with AI Advisor confirmation gate (daily, long-only).

Identical edge to ``ema_crossover.py`` — ride the trend while the 8-period EMA stays
above the 32-period EMA — but every entry must pass an AI fundamental filter before
the order is submitted.  The AI gate can only veto; it cannot invent or resize trades.

Entry:  Golden cross (8 EMA crosses above 32 EMA) on bar N close → candidate order
        sent to the AI Advisor with PIT fundamentals, 10-K excerpts, and recent news →
        approved: order sent; vetoed: order cancelled.  Fills at bar N+1 open.

Exit:   Death cross (8 EMA crosses below 32 EMA) on bar M → market close, no AI gate.
        Fills at bar M+1 open.

AI goal: filter out golden crosses in companies whose fundamentals do not support a
         sustained uptrend — deteriorating margins, balance sheet stress, or material
         negative news that the price action hasn't priced in yet.

Sizing: Defaults to ``EquityFraction(0.95)`` when no sizer is set on the Backtest.

# Parameters: 3  (≤ 3 recommended; more reduces parameter_adjusted_sharpe)
"""

from __future__ import annotations

from hermes import (
    AIAdvisor,
    ClaudeProvider,
    EDGARFilingEnricher,
    EMA,
    Backtest,
    EquityFraction,
    Parameter,
    PolygonNewsEnricher,
    Strategy,
    Symbol,
    Timeframe,
    YFinanceFundamentalsEnricher,
)
from hermes.data import YFinanceSource

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1D")

# Parameters: 3  (≤ 3 recommended; more reduces parameter_adjusted_sharpe)
# TODO: replace this placeholder with a real task description for the model.
_AI_PROMPT = """\
Strategy: EMA Crossover (8 EMA / 32 EMA, daily bars, long-only)
Signal: The 8-period EMA just crossed above the 32-period EMA (golden cross), \
generating a candidate long entry.

Your task: assess whether this ticker has the fundamental and business quality \
to sustain an uptrend from this point forward. The technical signal is already \
confirmed — your job is to filter out false positives where the crossover is \
unlikely to lead to a lasting trend.

APPROVE if ALL of the following hold:
  - The business is profitable or on a credible path to profitability \
(positive ROE, healthy margins, or a clear revenue growth story from the filing).
  - The balance sheet is not under existential stress. NOTE: a current ratio \
below 1 is normal and expected for large-cap companies that park cash in \
long-term investments (e.g. Apple, Microsoft) — do NOT treat this alone as a \
stress signal. Evaluate debt load in the context of earnings power and sector norms.
  - There is no immediate fundamental headwind visible in the 10-K or recent news \
(e.g. loss of major customer, regulatory action, covenant breach, going-concern language).
  - Recent news is neutral-to-positive or unrelated to structural business deterioration.

VETO if ANY of the following is true:
  - The company is deeply unprofitable with deteriorating margins and no clear \
path to profitability.
  - The balance sheet shows genuine solvency risk: debt that cannot be serviced \
from operating cash flow, covenant breach language, or a going-concern note in \
the filing. A current ratio below 1 alone is NOT sufficient to veto — look at \
the full picture.
  - The 10-K or recent news reveals a material negative catalyst (earnings \
collapse, major lawsuit, product recall, executive fraud, industry disruption \
that directly threatens this business model).
  - The crossover is occurring in a company whose fundamentals do not justify \
holding through a trend — i.e. this looks like a technical bounce in a \
fundamentally deteriorating name.

Be decisive. If fundamentals are missing or unavailable, default to APPROVE \
and note the data gap in your reason — do not veto on uncertainty alone.\
"""


class EmaCrossoverAI(Strategy):
    def setup(self) -> None:
        short_len = self.param(
            Parameter(
                "short_ema", 8, bounds=(2, 50),
                description="Period of the fast EMA",
            )
        )
        long_len = self.param(
            Parameter(
                "long_ema", 32, bounds=(10, 200),
                description="Period of the slow EMA",
            )
        )
        self._min_confidence = self.param(
            Parameter(
                "min_confidence", 0.0, bounds=(0.0, 1.0),
                description="Minimum AI confidence to approve a trade (0 = pass all approvals)",
            )
        )
        self.short_ema = self.use(EMA(D1, short_len))
        self.long_ema = self.use(EMA(D1, long_len))
        self._prev_short: float | None = None
        self._prev_long: float | None = None

    def on_start(self) -> None:
        if self.advisor is not None:
            self.advisor.min_confidence = self._min_confidence
        short = self.indicator_value(self.short_ema)["value"]
        long  = self.indicator_value(self.long_ema)["value"]
        if None in (short, long):
            return
        self._prev_short = short
        self._prev_long  = long
        # If already in a golden-cross state at window start, enter — AI-gated.
        if short > long and self.venue.position().is_flat:
            order = self.buy(self.sizer or EquityFraction(0.95), tag="ema_x_long_initial")
            if not self.confirm_with_ai(order, _AI_PROMPT):
                self.venue.cancel(order)

    def on_bar(self, bar) -> None:  # noqa: ARG002
        short = self.indicator_value(self.short_ema)["value"]
        long  = self.indicator_value(self.long_ema)["value"]

        prev_short = self._prev_short
        prev_long  = self._prev_long

        self._prev_short = short
        self._prev_long  = long

        if None in (short, long, prev_short, prev_long):
            return

        golden_cross = prev_short <= prev_long and short > long
        death_cross  = prev_short >= prev_long and short < long

        position = self.venue.position()

        if golden_cross and position.is_flat:
            order = self.buy(self.sizer or EquityFraction(0.95), tag="ema_x_long")
            if not self.confirm_with_ai(order, _AI_PROMPT):
                self.venue.cancel(order)

        elif death_cross and not position.is_flat:
            for trade in self.venue.open_trades():
                self.close(trade)


def build_backtest(**overrides) -> Backtest:
    """Factory used by hermes-backtest and the web UI.

    Requires ``ANTHROPIC_API_KEY`` in the environment for live AI calls.
    Optional env vars for PIT enrichers:
      - ``POLYGON_API_KEY`` — enables news headlines (PolygonNewsEnricher)
      - no key needed for EDGAR or yfinance fundamentals

    In backtest mode all decisions are served from the deterministic content-
    addressed cache, so subsequent re-runs are free and reproducible.
    The PIT enrichers cache their API responses to ``.cache/pit/``.
    """
    symbol = overrides.pop("symbol", Symbol("SPY", "yfinance"))
    starting_cash = overrides.pop("starting_cash", 100_000)
    return Backtest(
        strategy=EmaCrossoverAI(),
        source=YFinanceSource(),
        symbol=symbol,
        timeframes=[D1],
        starting_cash=starting_cash,
        advisor=AIAdvisor(
            ClaudeProvider(),
            system_prompt=(
                "You are a fundamental analyst and trading risk filter. "
                "You receive a technical entry signal (EMA golden cross on daily bars) "
                "together with point-in-time financial metrics, 10-K filing excerpts, "
                "and recent news. Your sole job is to approve or veto the trade based "
                "on whether the company's fundamentals support a sustained uptrend. "
                "Reply with a JSON object: {\"approved\": true|false, \"confidence\": 0-1, "
                "\"reason\": \"one sentence\"}."
            ),
            enrichers=[
                YFinanceFundamentalsEnricher(),   # PIT P/E, P/B, ROE, D/E — no API key
                EDGARFilingEnricher(),             # PIT 10-K Items 1, 1A, 7 — no API key
                PolygonNewsEnricher(days_back=30), # requires POLYGON_API_KEY
            ],
            min_confidence=0.0,
        ),
        **overrides,
    )


if __name__ == "__main__":
    result = build_backtest().run()
    m = result.metrics
    print(
        f"trades={m.num_trades}  return={m.total_return:.2%}  "
        f"sharpe={m.sharpe:.2f}  adj_sharpe={m.parameter_adjusted_sharpe:.2f}  "
        f"max_dd={m.max_drawdown:.2%}  win_rate={m.win_rate:.2%}"
    )
