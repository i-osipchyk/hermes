"""EMA Close Crossover strategy with AI Advisor confirmation gate (daily, long-only).

Identical edge to ``ema_crossover.py`` — ride the trend while the fast EMA stays
above the slow EMA — but every entry must pass an AI fundamental filter before
the order is submitted.  The AI gate can only veto; it cannot invent or resize trades.

EMA periods are configurable via ``short_ema`` (default 8) and ``long_ema`` (default 32)
parameters; the AI prompt is generated dynamically to reflect the active periods.

Entry:  Golden cross (fast EMA crosses above slow EMA) on bar N close → candidate order
        sent to the AI Advisor with PIT fundamentals, 10-K excerpts, and recent news →
        approved: order sent; vetoed: order cancelled.  Fills at bar N+1 open.

Exit:   Death cross (fast EMA crosses below slow EMA) on bar M → market close, no AI gate.
        Fills at bar M+1 open.

AI goal: filter out golden crosses where the fundamental TRAJECTORY is deteriorating —
         compressing margins, decelerating revenue, rising leverage, or a recent negative
         catalyst — regardless of whether absolute metrics look healthy.

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
def _ai_prompt(short_len: int, long_len: int) -> str:
    return f"""\
Strategy: EMA Crossover ({short_len} EMA / {long_len} EMA, daily bars, long-only)
Signal: The {short_len}-period EMA just crossed above the {long_len}-period EMA (golden cross), \
generating a candidate long entry.

Your task: decide whether this crossover has fundamental backing strong enough to \
justify entering. The default answer is VETO. APPROVE only if you find at least TWO \
of the five signals below — each verified against a specific number or named event \
from the data provided. You may not infer, estimate, or extrapolate signals that \
are not directly stated in the fundamentals snapshot, 10-K excerpts, or news items.

THE FIVE VALID SIGNALS — each must be verified, not inferred:

  S1. MARGIN EXPANSION (last reported year vs the year before):
      Gross margin OR operating margin improved by at least 100bps year-over-year.
      You must state both the current and prior-year margin figures, sourced from \
the filing. If only one year's figure is available, this signal does not count.

  S2. REVENUE RE-ACCELERATION (last two reported periods):
      The YoY revenue growth rate in the most recent period is HIGHER than the \
growth rate in the preceding period — i.e., growth is speeding up, not just positive.
      You must state both growth rates with the source periods. Positive-but-decelerating \
growth does NOT qualify.

  S3. EARNINGS BEAT AND GUIDANCE RAISE — both in the same quarter:
      The most recent earnings release beat consensus EPS AND management raised \
forward guidance in the same announcement. Both conditions must be explicitly \
stated in the news window. A beat without a raise, or a raise without a beat, \
does not qualify.

  S4. LEVERAGE REDUCTION (year-over-year):
      Debt/Equity ratio fell by at least 10% year-over-year, OR interest coverage \
ratio improved by at least 0.5x. You must cite both the current and prior figures. \
If only one figure is available, this signal does not count.

  S5. NAMED REVENUE CATALYST (in the 30-day news window):
      A specific, named event that directly adds measurable near-term revenue: \
a signed contract with a stated dollar value, a regulatory approval for a product \
already in market, or a completed acquisition with revenue already consolidated. \
The dollar impact or approval must be explicitly stated in the news. The following \
do NOT qualify: analyst upgrades, price target raises, strategic intent, market \
size projections, memoranda of understanding, letters of intent, pipeline language, \
or acquisitions where revenue is not yet consolidated.

VETO if you cannot confirm two signals from the above list using the data provided.

AUTOMATIC VETO regardless of signals found:
  - Revenue declined year-over-year (negative growth rate in the most recent period).
  - Gross or operating margin compressed more than 150bps year-over-year.
  - The most recent earnings release was a miss AND guidance was cut.
  - Going-concern, covenant breach, or solvency warning in the 10-K.
  - Named negative event in the news: lost contract, regulatory enforcement action, \
product recall, material fraud allegation, or CFO/CEO departure under adverse \
circumstances — each explicitly stated, not inferred.

If the fundamentals snapshot is absent or the filing excerpts are missing, \
VETO at confidence 0.35.

CONFIDENCE CALIBRATION — use the full 0–1 range:
  0.0–0.2  Definitive veto: automatic veto trigger fired AND trajectory actively \
deteriorating across multiple axes.
  0.2–0.4  Clear veto: fewer than two confirmed signals AND at least one deteriorating \
axis or named negative catalyst.
  0.4–0.5  Weak veto: fewer than two confirmed signals, but no active deterioration. \
Trajectory is flat or ambiguous.
  0.5–0.6  Borderline approval: exactly two signals confirmed, but both are weak or \
offset by conflicting evidence.
  0.6–0.75 Moderate approval: two signals clearly confirmed, no conflicting evidence.
  0.75–0.9 Strong approval: three or more signals clearly confirmed.
  0.9–1.0  Very high conviction: four or more signals confirmed, trajectory \
unambiguously accelerating. Reserve for rare standout cases.

Hard rules:
  - VETO confidence must be ≤ 0.5.
  - Missing data → VETO at 0.35.
  - You must state which signals you counted and why, with the specific figures.
  - If you cannot produce the source figure, the signal does not count.\
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
        self._short_len = short_len
        self._long_len = long_len
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
            if not self.confirm_with_ai(order, _ai_prompt(self._short_len, self._long_len)):
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
            if not self.confirm_with_ai(order, _ai_prompt(self._short_len, self._long_len)):
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
                "You are a fundamental momentum analyst and trading risk filter. "
                "Your default answer is VETO. You APPROVE only when you can verify at least "
                "two of five defined signals using specific numbers or named events from the "
                "data provided — no inference, no extrapolation, no invented figures. "
                "Analyst opinions, market projections, and strategic intent do not count. "
                "You receive a technical entry signal (EMA golden cross on daily bars) "
                "together with point-in-time financial metrics, 10-K filing excerpts, "
                "and recent news. "
                "Reply with a JSON object: {\"approved\": true|false, \"confidence\": 0.0-1.0, "
                "\"reason\": \"one sentence citing the specific signals that drove the decision\"}. "
                "Vetos must have confidence ≤ 0.5. Missing data is a veto at 0.35. "
                "Approvals require at least two concrete positive momentum signals."
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
