"""Trade and Position (ADR-0003).

A Trade is one open exposure with its own mutable Stop Loss / Take Profit. A
Strategy may hold several concurrent Trades on its one Instrument (hedging-style);
each closes independently. The Position is the derived net aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..core import Instrument
from .order import Side


@dataclass(slots=True)
class Trade:
    instrument: Instrument
    side: Side
    size: float                       # native units/lots, always > 0
    entry_price: float
    entry_time: datetime
    stop_loss: float | None = None    # mutable after entry (move-to-breakeven, trailing)
    take_profit: float | None = None  # mutable after entry
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: str | None = None    # "stop_loss" | "take_profit" | "signal" | ...
    # Accounting captured at close for the blotter:
    gross_pnl: float | None = None
    costs: float | None = None        # commission + spread + slippage + financing
    ai_decision: object | None = None  # the Advisor Decision, if consulted

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def net_pnl(self) -> float | None:
        if self.gross_pnl is None:
            return None
        return self.gross_pnl - (self.costs or 0.0)


class Position:
    """Derived net exposure across all open Trades on an Instrument.

    Not the primary unit — the Trade is. This aggregates size and blended entry for
    reporting and margin.
    """

    def __init__(self, instrument: Instrument) -> None:
        self.instrument = instrument
        self.open_trades: list[Trade] = []

    @property
    def net_size(self) -> float:
        return sum(t.size if t.side is Side.BUY else -t.size for t in self.open_trades)

    @property
    def is_flat(self) -> bool:
        return self.net_size == 0
