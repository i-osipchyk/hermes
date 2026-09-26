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

_UNSET = object()
"""Sentinel to distinguish 'not provided' from ``None`` (clear the level)."""


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
        self, trade: Trade, *, stop_loss=_UNSET, take_profit=_UNSET
    ) -> None:
        """Mutate a live Trade's protective levels (move-to-breakeven, trailing).

        Pass ``None`` to clear a level; omit the argument to leave it unchanged."""

    @abstractmethod
    def close_trade(self, trade: Trade) -> None:
        """Close a specific open Trade at market."""

    @abstractmethod
    def open_trades(self) -> list[Trade]: ...

    @abstractmethod
    def position(self) -> Position: ...

    @abstractmethod
    def force_close_all(self, ts) -> None:
        """Close all open positions at the given timestamp."""

    @abstractmethod
    def unrealised_pnl(self, price: float | None = None) -> float:
        """Total unrealised P&L across open trades."""

    @abstractmethod
    def equity(self) -> float:
        """Current account equity (cash + unrealised P&L)."""
