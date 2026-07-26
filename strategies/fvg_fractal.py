"""FVG + Fractal structural strategy.

Setup: a 1-hour Fair Value Gap identifies a trade zone; 15-minute Williams fractals
supply the Stop Loss (last swing low/high confirmed at or before the first FVG bar
closes) and Take Profit (highest/lowest swing high/low confirmed during FVG formation).

Entry: a limit order placed at the price that yields exactly 2 R:R.  If the 2RR level
falls above the FVG top (bullish) / below the FVG bottom (bearish) the entry moves to
the FVG boundary — which gives better than 2RR.  If the 2RR level would require an
entry outside the "unfavorable" side of the FVG the setup is skipped.

Renewal: when price blows through the Take Profit before the entry fills, the order is
cancelled and the highest/lowest fractal formed after the FVG confirmation becomes the
new TP (up to two renewals).  The setup is invalidated the moment price touches the
near FVG boundary (tested) without filling the entry.

Trades both bullish FVGs (long) and bearish FVGs (short) independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from hermes import (
    Backtest,
    FairValueGap,
    Fractals,
    Order,
    OrderType,
    RiskPercent,
    Strategy,
    Symbol,
    Timeframe,
)
from hermes.data import BinanceFuturesSource

GENERATED_BY = "hermes-strategy"

M15 = Timeframe.parse("15m")
H1 = Timeframe.parse("1h")

_MAX_RENEWALS = 2


@dataclass
class _FVGRecord:
    """State machine for one live FVG setup."""

    bullish: bool
    top: float              # c3.low (bullish) / c1.low (bearish)
    bottom: float           # c1.high (bullish) / c3.high (bearish)
    c1_end_ts: datetime     # c1 close time — fractal SL lookback boundary
    confirmed_ts: datetime  # c3 close time — FVG confirmed
    skip_ts: datetime       # bar timestamp on which FVG was detected; skip tested check

    sl: float
    tp: float
    entry: float

    order: Order | None = None
    renewal_count: int = 0
    awaiting_renewal: bool = False
    done: bool = False


class FvgFractalStrategy(Strategy):

    def setup(self) -> None:
        # Registered so the engine computes the correct Lead-in length.
        self._fvg_ind = self.use(FairValueGap(H1))
        self._frac_ind = self.use(Fractals(M15))

        # Confirmed 15m fractals: (candidate_bar_timestamp, price)
        self._highs: list[tuple[datetime, float]] = []
        self._lows: list[tuple[datetime, float]] = []
        self._recorded_ts: set[datetime] = set()  # dedup guard

        # Active FVG setups and a dedup guard for detection
        self._fvgs: list[_FVGRecord] = []
        self._seen_c3_ts: set[datetime] = set()

    def on_start(self) -> None:
        """Pre-populate fractal history from lead-in bars."""
        bars = self.data(M15).closed()
        for i in range(2, len(bars)):
            self._check_fractal(bars[i - 2], bars[i - 1], bars[i])

    def on_bar(self, bar) -> None:
        self._record_current_fractal()
        self._detect_fvgs(bar)
        self._manage_states(bar)

    # ── fractal tracking ──────────────────────────────────────────────────────

    def _record_current_fractal(self) -> None:
        bars = self.data(M15).closed()
        if len(bars) < 3:
            return
        self._check_fractal(bars[-3], bars[-2], bars[-1])

    def _check_fractal(self, prev, cand, nxt) -> None:
        ts = cand.timestamp
        if ts in self._recorded_ts:
            return
        self._recorded_ts.add(ts)
        if prev.high < cand.high and nxt.high <= cand.high:
            self._highs.append((ts, cand.high))
        if prev.low > cand.low and nxt.low >= cand.low:
            self._lows.append((ts, cand.low))

    # ── FVG detection ─────────────────────────────────────────────────────────

    def _detect_fvgs(self, bar) -> None:
        h1 = self.data(H1)
        # h1.forming is None only at the last 15m sub-bar of an H1 period
        # (the engine calls _finalize inside push() before on_bar fires).
        if h1.forming is not None:
            return
        closed = h1.closed()
        if len(closed) < 3:
            return
        c1, _c2, c3 = closed[-3], closed[-2], closed[-1]
        if c3.timestamp in self._seen_c3_ts:
            return
        self._seen_c3_ts.add(c3.timestamp)

        c1_end_ts = c1.timestamp + timedelta(seconds=H1.seconds)
        c3_end_ts = c3.timestamp + timedelta(seconds=H1.seconds)

        if c1.high < c3.low:  # bullish gap
            self._try_create(
                bullish=True,
                top=c3.low, bottom=c1.high,
                c1_end_ts=c1_end_ts,
                fvg_start_ts=c1.timestamp, fvg_end_ts=c3_end_ts,
                skip_ts=bar.timestamp,
            )
        elif c1.low > c3.high:  # bearish gap (mutually exclusive)
            self._try_create(
                bullish=False,
                top=c1.low, bottom=c3.high,
                c1_end_ts=c1_end_ts,
                fvg_start_ts=c1.timestamp, fvg_end_ts=c3_end_ts,
                skip_ts=bar.timestamp,
            )

    def _try_create(
        self, *, bullish, top, bottom, c1_end_ts, fvg_start_ts, fvg_end_ts, skip_ts
    ) -> None:
        if bullish:
            sl = self._last_low_before(c1_end_ts)
            tp = self._highest_high_in(fvg_start_ts, fvg_end_ts)
        else:
            sl = self._last_high_before(c1_end_ts)
            tp = self._lowest_low_in(fvg_start_ts, fvg_end_ts)

        if sl is None or tp is None:
            return

        entry = self._long_entry(sl, tp, top, bottom) if bullish else \
                self._short_entry(sl, tp, top, bottom)
        if entry is None:
            return

        rec = _FVGRecord(
            bullish=bullish, top=top, bottom=bottom,
            c1_end_ts=c1_end_ts, confirmed_ts=fvg_end_ts, skip_ts=skip_ts,
            sl=sl, tp=tp, entry=entry,
        )
        self._place_order(rec)
        self._fvgs.append(rec)

    # ── entry computation ─────────────────────────────────────────────────────

    def _long_entry(self, sl, tp, top, bottom) -> float | None:
        """Entry for bullish long: place at 2RR level, or FVG top if 2RR is above it."""
        e = (tp + 2 * sl) / 3
        if e > top:
            return top      # FVG top gives better than 2RR — use it
        if e > bottom:
            return e        # 2RR level sits inside the FVG
        return None         # 2RR level is below the FVG — unachievable inside it

    def _short_entry(self, sl, tp, top, bottom) -> float | None:
        """Entry for bearish short: place at 2RR level, or FVG top if 2RR is below bottom."""
        e = (tp + 2 * sl) / 3
        if e < bottom:
            return top      # FVG top gives better than 2RR — use it
        if e < top:
            return e        # 2RR level sits inside the FVG
        return None         # 2RR level is above the FVG — unachievable inside it

    # ── order placement ───────────────────────────────────────────────────────

    def _place_order(self, rec: _FVGRecord) -> None:
        if rec.bullish:
            rec.order = self.buy(
                RiskPercent(0.01),
                type=OrderType.LIMIT,
                limit=rec.entry,
                stop_loss=rec.sl,
                take_profit=rec.tp,
                tag="fvg_long",
            )
        else:
            rec.order = self.sell(
                RiskPercent(0.01),
                type=OrderType.LIMIT,
                limit=rec.entry,
                stop_loss=rec.sl,
                take_profit=rec.tp,
                tag="fvg_short",
            )

    # ── state management ──────────────────────────────────────────────────────

    def _manage_states(self, bar) -> None:
        for rec in list(self._fvgs):
            if rec.done:
                self._fvgs.remove(rec)
                continue

            # Filled → trade is open and managed by its SL/TP automatically
            if rec.order is not None and rec.order.filled_at is not None:
                rec.done = True
                continue

            if rec.awaiting_renewal:
                if self._is_tested(rec, bar):
                    rec.done = True
                else:
                    self._try_renew(rec)
                continue

            # Active state: pending limit order
            if self._is_tested(rec, bar):
                if rec.order is not None:
                    self.venue.cancel(rec.order)
                rec.done = True
                continue

            if self._tp_broken(rec, bar):
                if rec.order is not None:
                    self.venue.cancel(rec.order)
                    rec.order = None
                if rec.renewal_count < _MAX_RENEWALS:
                    rec.awaiting_renewal = True
                else:
                    rec.done = True

    def _is_tested(self, rec: _FVGRecord, bar) -> bool:
        if bar.timestamp == rec.skip_ts:
            return False  # detection bar — bar is part of c3, not a retest
        return bar.low <= rec.top if rec.bullish else bar.high >= rec.bottom

    def _tp_broken(self, rec: _FVGRecord, bar) -> bool:
        return bar.high > rec.tp if rec.bullish else bar.low < rec.tp

    def _try_renew(self, rec: _FVGRecord) -> None:
        """Find a new TP above/below the old one and re-place the order."""
        if rec.bullish:
            new_tp = self._highest_high_after(rec.tp, rec.confirmed_ts)
            if new_tp is None:
                return
            new_entry = self._long_entry(rec.sl, new_tp, rec.top, rec.bottom)
        else:
            new_tp = self._lowest_low_after(rec.tp, rec.confirmed_ts)
            if new_tp is None:
                return
            new_entry = self._short_entry(rec.sl, new_tp, rec.top, rec.bottom)

        if new_entry is None:
            rec.done = True
            return

        rec.tp = new_tp
        rec.entry = new_entry
        rec.renewal_count += 1
        rec.awaiting_renewal = False
        self._place_order(rec)

    # ── fractal lookup helpers ─────────────────────────────────────────────────

    def _last_low_before(self, ts: datetime) -> float | None:
        result = result_ts = None
        for t, p in self._lows:
            if t <= ts and (result_ts is None or t > result_ts):
                result, result_ts = p, t
        return result

    def _last_high_before(self, ts: datetime) -> float | None:
        result = result_ts = None
        for t, p in self._highs:
            if t <= ts and (result_ts is None or t > result_ts):
                result, result_ts = p, t
        return result

    def _highest_high_in(self, from_ts: datetime, to_ts: datetime) -> float | None:
        highs = [p for t, p in self._highs if from_ts <= t < to_ts]
        return max(highs) if highs else None

    def _lowest_low_in(self, from_ts: datetime, to_ts: datetime) -> float | None:
        lows = [p for t, p in self._lows if from_ts <= t < to_ts]
        return min(lows) if lows else None

    def _highest_high_after(self, above: float, from_ts: datetime) -> float | None:
        highs = [p for t, p in self._highs if t >= from_ts and p > above]
        return max(highs) if highs else None

    def _lowest_low_after(self, below: float, from_ts: datetime) -> float | None:
        lows = [p for t, p in self._lows if t >= from_ts and p < below]
        return min(lows) if lows else None


def build_backtest(**overrides) -> Backtest:
    # start/end omitted -> Backtest defaults to year-to-date (Jan 1 -> today).
    symbol = overrides.pop("symbol", Symbol("BTCUSDT", "binance-futures"))
    starting_cash = overrides.pop("starting_cash", 1_000_000.0)
    return Backtest(
        strategy=FvgFractalStrategy(),
        source=BinanceFuturesSource(),
        symbol=symbol,
        timeframes=[M15, H1],
        starting_cash=starting_cash,
        **overrides,
    )


if __name__ == "__main__":
    result = build_backtest().run()
    m = result.metrics
    print(
        f"trades={m.num_trades}  return={m.total_return:.2%}  "
        f"sharpe={m.sharpe:.2f}  max_dd={m.max_drawdown:.2%}  "
        f"win_rate={m.win_rate:.2%}"
    )
