"""EMA Close Crossover strategy with AI news filter (daily, long-only).

Identical edge to ``ema_crossover.py`` — ride the trend while the fast EMA stays
above the slow EMA — but every entry must pass an AI news filter before the order
is submitted.  The AI gate can only veto; it cannot invent or resize trades.

EMA periods are configurable via ``short_ema`` (default 8) and ``long_ema`` (default 32)
parameters; the AI prompt is generated dynamically to reflect the active periods.

Entry:  Golden cross (fast EMA crosses above slow EMA) on bar N close → candidate order
        sent to the AI Advisor with recent news → approved: order sent; vetoed: order
        cancelled.  Fills at bar N+1 open.

Exit:   Death cross (fast EMA crosses below slow EMA) on bar M → market close, no AI gate.
        Fills at bar M+1 open.

AI goal: given that price is already in an uptrend confirmed by the EMA crossover, decide
         whether recent news gives the trend a reasonable chance to persist — or contains
         a catalyst that is likely to break it.

Sizing: Defaults to ``EquityFraction(0.95)`` when no sizer is set on the Backtest.

# Parameters: 3  (≤ 3 recommended; more reduces parameter_adjusted_sharpe)
"""

from __future__ import annotations

from hermes import (
    AIAdvisor,
    ClaudeProvider,
    EMA,
    Backtest,
    EquityFraction,
    Parameter,
    PolygonNewsEnricher,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.data import YFinanceSource

GENERATED_BY = "hermes-strategy"

D1 = Timeframe.parse("1D")

# Parameters: 3  (≤ 3 recommended; more reduces parameter_adjusted_sharpe)
def _ai_prompt(short_len: int, long_len: int) -> str:
    return f"""\
Strategy: EMA Crossover ({short_len} EMA / {long_len} EMA, daily bars, long-only)
Signal: The {short_len}-period EMA just crossed above the {long_len}-period EMA (golden cross).
The price is in a confirmed uptrend. The technical entry is already locked in.

Your task: read the recent news and decide whether an identifiable positive catalyst \
is driving this trend — or whether the news reveals a reason the trend is unlikely to persist.

DEFAULT TO VETO. Only approve when the news contains a clear catalyst from the APPROVE \
list below. If the news is ambiguous, mixed, or doesn't fit any APPROVE category, VETO.

────────────────────────────────────────────────────────
APPROVE — news driven by one of these catalysts:
  • Sector / peer momentum: the broader sector or key peers are in a confirmed uptrend,
    with identifiable reasons (demand surge, policy tailwind, commodity move, etc.).
  • Specific macro tailwind: a named government policy, spending programme, rate decision,
    or macro event that directly benefits this stock — not generic "market is up" language.
  • Recovery with catalyst: stock rebounding from a prior selloff and there is a specific
    identifiable reason for the recovery (new management, resolved overhang, guidance raise).
  • Regulatory win: FDA approval, contract award from a government body, favourable ruling.
  • Earnings beat with raised guidance: reported results above consensus AND guidance raised;
    not just an "expected beat" or analyst estimate revision.

VETO — news driven by one of these catalysts (even if framed positively):
  • M&A / deals / activism: any mention of an acquisition, merger, asset sale, activist
    investor stake, or takeover rumour — these introduce binary event risk that breaks trends.
  • Analyst opinion only: the reason to approve is solely an analyst upgrade, price target
    raise, or inclusion in a stock list, with no underlying business catalyst.
  • Product / contract announcement: a new product launch, partnership, or contract win
    announced in the news — these are typically already priced in or fail to sustain momentum.
  • Dividend / income framing: the stock is highlighted as a dividend payer or income vehicle
    with no growth catalyst — income framing signals low expected momentum.
  • Absence of bad news: the only positive signal is that no negative news was found.
    "No catalyst either way" is not a reason to enter a momentum trade.
  • No news available: VETO at confidence 0.35.

────────────────────────────────────────────────────────
CONFIDENCE CALIBRATION:
  0.0–0.2  Strong veto: clear negative catalyst or hard-veto news type above.
  0.2–0.4  Clear veto: news fits a veto category, impact on trend is certain.
  0.4–0.5  Weak veto: news is ambiguous or does not fit any approve category.
  0.5–0.6  Borderline approval: weak match to an approve category; some doubt remains.
  0.6–0.75 Moderate approval: clear match to an approve category.
  0.75–1.0 Strong approval: strong, specific catalyst from the approve list above.

Hard rules:
  - VETO confidence must be ≤ 0.5.
  - No news available → VETO at 0.35.
  - When in doubt, veto — false negatives (missing a trade) cost less than false positives.
  - State the specific headline or event and the category it matched.\
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

    def on_bar(self, bar) -> None:
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
    Requires ``POLYGON_API_KEY`` for news headlines (PolygonNewsEnricher).

    In backtest mode all decisions are served from the deterministic content-
    addressed cache, so subsequent re-runs are free and reproducible.
    News API responses are cached to ``.cache/pit/``.
    """
    symbol = overrides.pop("symbol", Symbol("SPY", "yfinance"))
    starting_cash = overrides.pop("starting_cash", 100_000)
    advisor = overrides.pop(
        "advisor",
        AIAdvisor(
            ClaudeProvider(),
            system_prompt=(
                "You are a news-based trend filter for a momentum trading strategy. "
                "A technical entry signal (EMA golden cross on daily bars) has already fired — "
                "the stock is in an uptrend. Your only job is to read the recent news and decide "
                "whether the trend has a reasonable chance to persist. "
                "APPROVE if news is neutral or positive. "
                "VETO if news contains an explicit negative catalyst likely to break the trend. "
                "Reply with a JSON object: {\"approved\": true|false, \"confidence\": 0.0-1.0, "
                "\"reason\": \"one sentence citing the specific headline or event that drove the decision\"}. "
                "Vetos must have confidence ≤ 0.5. No news available is a veto at 0.35."
            ),
            enrichers=[
                PolygonNewsEnricher(days_back=30), # requires POLYGON_API_KEY
            ],
            min_confidence=0.0,
        ),
    )
    return Backtest(
        strategy=EmaCrossoverAI(),
        source=YFinanceSource(),
        symbol=symbol,
        timeframes=[D1],
        starting_cash=starting_cash,
        advisor=advisor,
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
