"""Account: the capital pool behind a Strategy (CONTEXT.md).

P&L-based accounting (works uniformly across cash and leveraged instruments):
cash holds realised equity; open Trades contribute unrealised P&L to equity and
reserve margin = notional / leverage. Orders that exceed free margin are rejected;
there is no auto-liquidation in v1 — risk is managed via Stop Loss. Dividend cash
from held stock Trades is credited here on ex-dates.
"""

from __future__ import annotations

from ..core import Instrument


class Account:
    def __init__(self, starting_cash: float, currency: str = "USD") -> None:
        self.starting_cash = starting_cash
        self.currency = currency
        self.cash = starting_cash

    # --- margin (leverage-aware) ----------------------------------------------

    @staticmethod
    def notional(instrument: Instrument, price: float, size: float) -> float:
        return abs(price * size * instrument.contract_size())

    @staticmethod
    def margin_required(instrument: Instrument, price: float, size: float) -> float:
        leverage = getattr(instrument, "leverage", 1.0) or 1.0
        return Account.notional(instrument, price, size) / leverage

    def equity(self, unrealised_pnl: float = 0.0) -> float:
        return self.cash + unrealised_pnl

    def free_margin(self, used_margin: float, unrealised_pnl: float = 0.0) -> float:
        return self.equity(unrealised_pnl) - used_margin

    # --- cash movements --------------------------------------------------------

    def credit(self, amount: float) -> None:
        self.cash += amount

    def debit(self, amount: float) -> None:
        self.cash -= amount
