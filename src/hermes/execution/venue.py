"""ExecutionVenue: the order-execution side of the parity seam (ADR-0001).

A Strategy emits Orders to an ExecutionVenue and receives fills back. The backtest
implementation (:class:`SimulatedVenue`) owns ALL fill/cost/margin/SL-TP
simulation; a live broker adapter (out of scope) implements the same interface, so
Strategy code is identical across backtest and live.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .order import Order
from .trade import Position, Trade


class ExecutionVenue(ABC):
    @abstractmethod
    def submit(self, order: Order) -> Order:
        """Accept an order (market fills next open; limit/stop rest as WORKING).
        Returns the order with updated status; rejects if margin insufficient."""

    @abstractmethod
    def cancel(self, order: Order) -> None:
        """Cancel a resting (WORKING) order."""

    @abstractmethod
    def modify_trade(
        self, trade: Trade, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> None:
        """Mutate a live Trade's protective levels (move-to-breakeven, trailing)."""

    @abstractmethod
    def close_trade(self, trade: Trade) -> None:
        """Close a specific open Trade at market."""

    @abstractmethod
    def open_trades(self) -> list[Trade]: ...

    @abstractmethod
    def position(self) -> Position: ...
