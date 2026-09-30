"""EMA Close Crossover strategy with AI news filter (daily, long-only).

Identical edge to ``ema_crossover.py`` — ride the trend while the fast EMA stays
above the slow EMA — but every entry must pass an AI news filter before the order
is submitted.  The AI gate can only veto; it cannot invent or resize trades.

EMA periods are configurable via ``short_ema`` (default 8) and ``long_ema`` (default 32)
parameters; the AI prompt is generated dynamically to reflect the active periods.

Entry:  Golden cross (fast EMA crosses above slow EMA) on bar N close → candidate order.
        If no news was found for the ticker in the enrichment window, the order is
        cancelled immediately (no AI call) — no news is treated as a default skip, not
        left to the model to veto. Otherwise the order is sent to the AI Advisor with
        recent news → approved: order sent; vetoed: order cancelled. Fills at bar N+1 open.

Exit:   Death cross (fast EMA crosses below slow EMA) on bar M → market close, no AI gate.
        Fills at bar M+1 open.

AI goal: given that price is already in an uptrend confirmed by the EMA crossover, decide
         whether recent news gives the trend a reasonable chance to persist — or contains
         a catalyst that is likely to break it. When the news itself is genuinely neutral
         (not negative, just uninformative), the AI falls back to a point-in-time
         fundamentals snapshot as a tiebreaker rather than auto-vetoing — see the
         "AMBIGUOUS / NEUTRAL NEWS" section of the prompt. Fundamentals are a helper for
         that narrow case only; they never override a genuine news-driven veto or approval.

Sizing: Defaults to ``EquityFraction(0.95)`` when no sizer is set on the Backtest.

# Parameters: 3  (≤ 3 recommended; more reduces parameter_adjusted_sharpe)
"""

from __future__ import annotations

from hermes import (
    EMA,
    AIAdvisor,
    Backtest,
    ClaudeProvider,
    EquityFraction,
    MassiveFundamentalsEnricher,
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

DEFAULT TO VETO. Unfiltered golden-cross entries lose money on average (IS win rate ~32%, \
mean return ~-0.4%) — approval must be earned, not assumed. Approve when the news contains \
a clear, specific catalyst from the APPROVE list below, and no VETO condition applies. If \
the news actively matches a VETO trigger, VETO — full stop, regardless of fundamentals. If \
the news is merely thin, generic, or uninformative (no clear catalyst either way), don't \
auto-veto — follow the AMBIGUOUS / NEUTRAL NEWS section below instead.

────────────────────────────────────────────────────────
VETO — news driven by one of these catalysts (even if framed positively). These are proven
false-positive patterns in the IS data; fundamentals do NOT excuse them — a binary-risk or
momentum-negative catalyst stays vetoed regardless of balance-sheet quality:
  • Analyst opinion only: the reason to approve is solely an analyst upgrade, price target
    raise, Zacks-style rank, or inclusion in a stock list, with no underlying business
    catalyst. This is the single most common false-positive pattern in the data — screen
    for it aggressively.
  • M&A / deals / activism: any mention of an acquisition, merger, asset sale, activist
    investor stake, or takeover rumour — these introduce binary event risk that breaks trends.
  • Dividend / income framing: the stock is highlighted as a dividend payer or income vehicle
    with no growth catalyst — income framing signals low expected momentum.
  • Product / contract announcement: a new product launch, partnership, or contract win
    announced in the news — these are typically already priced in or fail to sustain momentum.
  • IPO-adjacent noise: the stock is trading near/below its own IPO price, or the news is
    dominated by an unrelated peer's IPO, lockup expiration, or meme/speculative-stock
    coverage — this pattern correlates with weak forward returns, likely as a proxy for an
    already-volatile, speculative name.

(Trades with no news found at all never reach you — the strategy skips them before
calling the AI, so you will always have at least some article text to reason about.)

APPROVE — news driven by one of these catalysts. Treat all of these as moderate-strength
signals, not slam-dunks: none of them showed a statistically reliable edge on their own in
the IS data, so require a specific, named event — not just a topic-keyword match — and
weight confidence accordingly (see calibration below):
  • Earnings beat with raised guidance: reported results above consensus AND guidance raised.
    Note: in IS data this pattern alone trended slightly negative (possibly already priced
    in by the time the crossover fires) — require the guidance raise to be explicit and
    forward-looking, not just a beat, and cap confidence at "moderate" absent other support.
  • Regulatory win: FDA approval, contract award from a government body, favourable ruling.
  • Recovery with catalyst: stock rebounding from a prior selloff and there is a specific
    identifiable reason for the recovery (new management, resolved overhang, guidance raise).
  • Specific macro tailwind: a named government policy, spending programme, rate decision,
    or macro event that directly benefits this stock — not generic "market is up" language.
  • Sector / peer momentum: the broader sector or key peers are in a confirmed uptrend,
    with identifiable reasons (demand surge, policy tailwind, commodity move, etc.).

AMBIGUOUS / NEUTRAL NEWS — consult fundamentals instead of auto-vetoing:
News that is genuinely uninformative rather than actively negative — recycled price-action
recaps ("closed at $X, moving Y% from the previous session"), generic ETF/style-box
mentions, forward-looking earnings previews ("what's in the offing/cards") with no actual
result, or coverage that's simply thin/mixed with no dominant narrative — is NOT one of the
proven-bad VETO patterns above. It's just a case where the news gives you nothing to reason
about. When (and only when) you land here — no VETO trigger matched, no APPROVE catalyst
matched — look at the Fundamentals block appended below the news (point-in-time margins,
ROE, leverage, current ratio) as a tiebreaker:
  • Fundamentals show real strength (margin expansion, healthy/improving ROE, comfortable
    liabilities/equity and current ratio) → APPROVE at confidence 0.50–0.55, citing the
    specific metric that tipped it.
  • Fundamentals are weak, deteriorating, or the block is missing/empty → VETO at confidence
    0.35–0.45, noting that neither news nor fundamentals gave a reason to enter.
This path is a helper for the ambiguous middle only — it never applies once a VETO trigger
or an APPROVE catalyst is already matched from the news itself.

CONTEXT MODIFIERS — adjust confidence up or down within a category, do not use alone to flip
an approve/veto decision:
  • Light, focused coverage (a handful of specific, substantive headlines) is a mildly
    positive sign — it is NOT a reason to veto or to invoke "no news available." Sparse
    coverage outperformed heavy coverage in the IS data.
  • A heavy volume of news dominated by recaps, previews, and filler (see AMBIGUOUS / NEUTRAL
    NEWS above) is a mildly negative sign even when one relevant headline is buried in it —
    it suggests the move is already well-covered / potentially priced in.
  • An explicit new 52-week-high / all-time-high mention alongside a real catalyst modestly
    reinforces approval — it corroborates that the move is a genuine breakout, not noise.

────────────────────────────────────────────────────────
CONFIDENCE CALIBRATION:
  0.0–0.2  Strong veto: clear negative catalyst or hard-veto news type above.
  0.2–0.4  Clear veto: news fits a veto category, impact on trend is certain.
  0.35–0.45 Fundamentals-tiebreak veto: ambiguous/neutral news AND weak or missing
           fundamentals (see AMBIGUOUS / NEUTRAL NEWS).
  0.4–0.5  Weak veto: news is ambiguous or does not fit any approve category, no
           fundamentals data to fall back on either.
  0.50–0.55 Fundamentals-tiebreak approval: ambiguous/neutral news, but fundamentals show
           real strength (see AMBIGUOUS / NEUTRAL NEWS) — cap here, this is not a catalyst
           match and should never be scored as a "moderate" or "strong" approval below.
  0.55–0.6 Borderline approval: weak match to an approve category; some doubt remains.
  0.6–0.75 Moderate approval: clear match to an approve category, no reinforcing modifier.
  0.75–1.0 Strong approval: clear match to an approve category PLUS a positive context
           modifier (light/focused coverage, breakout confirmation) or unusually explicit,
           specific catalyst language.

Hard rules:
  - VETO confidence must be ≤ 0.5.
  - A VETO-list match is final — fundamentals never upgrade it to an approval.
  - Fundamentals are only ever consulted under AMBIGUOUS / NEUTRAL NEWS; ignore them
    entirely once a VETO trigger or an APPROVE catalyst is already matched from the news.
  - When in doubt and fundamentals don't help either, veto — false negatives (missing a
    trade) cost less than false positives.
  - State the specific headline or event (or, for the fundamentals-tiebreak path, the
    specific metric) and the category it matched.\
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
        self._news_enrichers: list[PolygonNewsEnricher] = []

    def on_start(self) -> None:
        if self.advisor is not None:
            self.advisor.min_confidence = self._min_confidence
            self._news_enrichers = [
                e for e in self.advisor.enrichers if isinstance(e, PolygonNewsEnricher)
            ]
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
            if not self._has_news(bar.timestamp.date()):
                self.venue.cancel(order)
            elif not self.confirm_with_ai(order, _ai_prompt(self._short_len, self._long_len)):
                self.venue.cancel(order)

        elif death_cross and not position.is_flat:
            for trade in self.venue.open_trades():
                self.close(trade)

    def _has_news(self, as_of) -> bool:
        """No news found for this ticker/window is a default skip — not left to the
        AI to veto (IS research: no-news trades gave the model nothing to reason
        about, and skipping them outright is cheaper than a guaranteed-veto AI call)."""
        if self.advisor is None or not self._news_enrichers:
            return True
        ticker = self.instrument.symbol.ticker
        return any(e.enrich(ticker, as_of) for e in self._news_enrichers)


def build_backtest(**overrides) -> Backtest:
    """Factory used by hermes-backtest and the web UI.

    Requires ``ANTHROPIC_API_KEY`` in the environment for live AI calls.
    Requires ``POLYGON_API_KEY`` for news headlines (PolygonNewsEnricher) and point-in-time
    fundamentals (MassiveFundamentalsEnricher) — the latter is only consulted by the prompt
    as a tiebreaker for ambiguous/neutral news, never as a primary signal.

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
                "If the news is genuinely ambiguous or uninformative (not negative, just thin), "
                "use the point-in-time fundamentals snapshot appended below the news as a "
                "tiebreaker instead of defaulting to veto — this is the ONLY case where "
                "fundamentals should influence the decision; a news-driven veto or approve "
                "always takes precedence over fundamentals. "
                "Reply with a JSON object: {\"approved\": true|false, \"confidence\": 0.0-1.0, "
                "\"reason\": \"one sentence citing the specific headline, event, or (for the "
                "fundamentals-tiebreak case) metric that drove the decision\"}. "
                "Vetos must have confidence ≤ 0.5."
            ),
            enrichers=[
                PolygonNewsEnricher(days_back=30),        # requires POLYGON_API_KEY
                MassiveFundamentalsEnricher(),            # requires POLYGON_API_KEY; helper only
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
