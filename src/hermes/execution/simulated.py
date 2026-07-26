"""SimulatedVenue: the backtest ExecutionVenue (ADR-0004).

Owns every simulation concern so the rest of the framework stays honest:
  * market orders fill at the NEXT base bar's open; resting limit/stop entries fill
    when a bar's range touches their price (gaps fill at the open, worse);
  * SL/TP monitored each base step on trades open from a PRIOR bar; on a single-bar
    SL+TP clash, resolve via the Fill-resolution Timeframe (bar magnifier) if given,
    else assume Stop first;
  * Cost Model applied (spread + slippage embedded in fill price; commission +
    financing as explicit cash);
  * margin checked against the Account (reject over-margin orders; no liquidation).

The engine calls :meth:`on_base_bar` for every new base bar BEFORE the strategy
sees it, then the strategy places orders that fill on subsequent bars.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..core import Bar, Instrument
from .account import Account
from .costs import CostModel
from .order import Order, OrderStatus, OrderType, Side
from .trade import Position, Trade
from .venue import ExecutionVenue


class SimulatedVenue(ExecutionVenue):
    def __init__(
        self,
        instrument: Instrument,
        account: Account,
        cost_model: CostModel,
        *,
        magnifier_bars: Callable[[Bar], list[Bar] | None] | None = None,
    ) -> None:
        self.instrument = instrument
        self.account = account
        self.cost_model = cost_model
        self._magnifier = magnifier_bars

        self._working: list[Order] = []        # market + resting limit/stop entries
        self._pending_closes: list[Trade] = []  # exits decided last bar
        self._open: list[Trade] = []
        self._closed: list[Trade] = []
        self._last_bar: Bar | None = None
        self._last_day: object | None = None

    # --- engine hook -----------------------------------------------------------

    def on_base_bar(self, bar: Bar) -> None:
        # 1. Financing on a new trading day (positions carried overnight).
        day = self.instrument.session.to_local(bar.timestamp).date()
        if self._last_day is not None and day != self._last_day and self._open:
            self._accrue_financing()
        self._last_day = day

        # 2. Exits decided on the previous bar fill at this bar's open.
        for trade in list(self._pending_closes):
            self._close_trade(trade, bar.open, "signal")
        self._pending_closes.clear()

        # 3. SL/TP on trades that were already open at the start of this bar.
        for trade in list(self._open):
            hit = self._check_exit(trade, bar)
            if hit is not None:
                raw_price, reason = hit
                self._close_trade(trade, raw_price, reason)

        # 4. Working orders: market entries fill at open; limit/stop when touched.
        for order in list(self._working):
            fill = self._try_fill_entry(order, bar)
            if fill is not None:
                self._open_trade(order, fill, bar.timestamp)
                self._working.remove(order)

        self._last_bar = bar

    # --- ExecutionVenue interface ---------------------------------------------

    def submit(self, order: Order) -> Order:
        order.created_at = self._last_bar.timestamp if self._last_bar else None
        ref_price = order.limit_price or order.stop_price or (
            self._last_bar.close if self._last_bar else 0.0
        )
        if not self._can_afford(order.side, order.size, ref_price):
            order.status = OrderStatus.REJECTED
            return order
        order.status = OrderStatus.WORKING
        self._working.append(order)
        return order

    def cancel(self, order: Order) -> None:
        if order in self._working:
            self._working.remove(order)
            order.status = OrderStatus.CANCELLED

    def modify_trade(self, trade, *, stop_loss=None, take_profit=None) -> None:
        if stop_loss is not None:
            trade.stop_loss = stop_loss
        if take_profit is not None:
            trade.take_profit = take_profit

    def close_trade(self, trade: Trade) -> None:
        if trade.is_open and trade not in self._pending_closes:
            self._pending_closes.append(trade)

    def open_trades(self) -> list[Trade]:
        return list(self._open)

    def position(self) -> Position:
        pos = Position(self.instrument)
        pos.open_trades = list(self._open)
        return pos

    # --- valuation -------------------------------------------------------------

    def unrealised_pnl(self, price: float | None = None) -> float:
        price = price if price is not None else (self._last_bar.close if self._last_bar else 0.0)
        cs = self.instrument.contract_size()
        total = 0.0
        for t in self._open:
            direction = 1 if t.side is Side.BUY else -1
            total += (price - t.entry_price) * direction * t.size * cs
        return total

    def equity(self) -> float:
        return self.account.equity(self.unrealised_pnl())

    def used_margin(self) -> float:
        return sum(
            Account.margin_required(self.instrument, t.entry_price, t.size) for t in self._open
        )

    # --- internals -------------------------------------------------------------

    def _can_afford(self, side: Side, size: float, price: float) -> bool:
        if side is Side.SELL and not self.instrument.can_short and not self._open:
            # Selling with no long position on a non-shortable instrument.
            return False
        req = Account.margin_required(self.instrument, price, size)
        return req <= self.account.free_margin(self.used_margin(), self.unrealised_pnl()) + 1e-9

    def _try_fill_entry(self, order: Order, bar: Bar) -> float | None:
        """Return the RAW fill price if this bar fills the order, else None."""
        if order.type is OrderType.MARKET:
            return bar.open
        if order.type is OrderType.LIMIT:
            lp = order.limit_price
            if order.side is Side.BUY and bar.low <= lp:
                return min(lp, bar.open)
            if order.side is Side.SELL and bar.high >= lp:
                return max(lp, bar.open)
        if order.type is OrderType.STOP:
            sp = order.stop_price
            if order.side is Side.BUY and bar.high >= sp:
                return max(sp, bar.open)   # gap up fills worse
            if order.side is Side.SELL and bar.low <= sp:
                return min(sp, bar.open)   # gap down fills worse
        return None

    def _open_trade(self, order: Order, raw_price: float, ts: datetime) -> None:
        fill_price = self.cost_model.fill_price(self.instrument, order.side, raw_price)
        commission = self.cost_model.commission.commission(
            self.instrument, fill_price, order.size
        )
        self.account.debit(commission)
        trade = Trade(
            instrument=self.instrument,
            side=order.side,
            size=order.size,
            entry_price=fill_price,
            entry_time=ts,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            costs=commission,
        )
        order.status = OrderStatus.FILLED
        order.filled_at = ts
        order.fill_price = fill_price
        self._open.append(trade)

    def _check_exit(self, trade: Trade, bar: Bar) -> tuple[float, str] | None:
        long = trade.side is Side.BUY
        sl, tp = trade.stop_loss, trade.take_profit
        if long:
            sl_hit = sl is not None and bar.low <= sl
            tp_hit = tp is not None and bar.high >= tp
        else:
            sl_hit = sl is not None and bar.high >= sl
            tp_hit = tp is not None and bar.low <= tp

        if sl_hit and tp_hit:
            reason = self._resolve_clash(trade, bar)
        elif sl_hit:
            reason = "stop_loss"
        elif tp_hit:
            reason = "take_profit"
        else:
            return None

        level = sl if reason == "stop_loss" else tp
        # Gap handling: a gap beyond the level fills at the open.
        if reason == "stop_loss":
            raw = min(level, bar.open) if long else max(level, bar.open)
        else:
            raw = max(level, bar.open) if long else min(level, bar.open)
        return raw, reason

    def _resolve_clash(self, trade: Trade, bar: Bar) -> str:
        """Both SL and TP touched this bar. Use the magnifier if available, else
        assume Stop first (conservative)."""
        subbars = self._magnifier(bar) if self._magnifier else None
        if subbars:
            long = trade.side is Side.BUY
            for sb in subbars:
                if long:
                    if sb.low <= trade.stop_loss:
                        return "stop_loss"
                    if sb.high >= trade.take_profit:
                        return "take_profit"
                else:
                    if sb.high >= trade.stop_loss:
                        return "stop_loss"
                    if sb.low <= trade.take_profit:
                        return "take_profit"
        return "stop_loss"

    def _close_trade(self, trade: Trade, raw_price: float, reason: str) -> None:
        if not trade.is_open:
            return
        exit_side = Side.SELL if trade.side is Side.BUY else Side.BUY
        exit_price = self.cost_model.fill_price(self.instrument, exit_side, raw_price)
        cs = self.instrument.contract_size()
        direction = 1 if trade.side is Side.BUY else -1
        gross = (exit_price - trade.entry_price) * direction * trade.size * cs
        exit_commission = self.cost_model.commission.commission(
            self.instrument, exit_price, trade.size
        )
        self.account.credit(gross - exit_commission)

        trade.exit_price = exit_price
        trade.exit_time = self._last_bar.timestamp if self._last_bar else trade.entry_time
        trade.exit_reason = reason
        trade.gross_pnl = gross
        trade.costs = (trade.costs or 0.0) + exit_commission
        self._open.remove(trade)
        self._closed.append(trade)

    def _accrue_financing(self) -> None:
        price = self._last_bar.close if self._last_bar else 0.0
        for t in self._open:
            notional = Account.notional(self.instrument, price, t.size)
            fin = self.cost_model.financing.overnight_charge(self.instrument, notional, days=1)
            if fin:
                self.account.debit(fin)
                t.costs = (t.costs or 0.0) + fin

    @property
    def closed_trades(self) -> list[Trade]:
        return self._closed
