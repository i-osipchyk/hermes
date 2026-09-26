"""Sizers: interchangeable ways to express order size (CONTEXT.md).

All Sizers resolve to the Instrument's native units/lots before the Order is
placed. Forms: risk-in-cash, risk-in-%-equity (given entry->stop distance),
notional/position size in cash, and explicit native units/lots.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core import Instrument


@dataclass(frozen=True, slots=True)
class SizingContext:
    """Everything a Sizer needs to resolve size at order time."""

    instrument: Instrument
    price: float                 # intended entry reference price
    equity: float                # current account equity
    stop_price: float | None = None  # required for risk-based sizing


class Sizer(ABC):
    @abstractmethod
    def resolve(self, ctx: SizingContext) -> float:
        """Return size in the Instrument's native units/lots."""


@dataclass(frozen=True, slots=True)
class Units(Sizer):
    size: float

    def resolve(self, ctx: SizingContext) -> float:
        return ctx.instrument.to_native_units(self.size)


@dataclass(frozen=True, slots=True)
class NotionalCash(Sizer):
    cash: float

    def resolve(self, ctx: SizingContext) -> float:
        per_unit = ctx.price * ctx.instrument.contract_size()
        if per_unit <= 0:
            return 0.0
        return ctx.instrument.to_native_units(self.cash / per_unit)


def _risk_units(ctx: SizingContext, risk_cash: float) -> float:
    if ctx.stop_price is None:
        raise ValueError("Risk-based sizing requires a stop_price")
    per_unit_risk = abs(ctx.price - ctx.stop_price) * ctx.instrument.contract_size()
    if per_unit_risk <= 0:
        return 0.0
    return ctx.instrument.to_native_units(risk_cash / per_unit_risk)


@dataclass(frozen=True, slots=True)
class RiskCash(Sizer):
    risk_cash: float

    def resolve(self, ctx: SizingContext) -> float:
        return _risk_units(ctx, self.risk_cash)


@dataclass(frozen=True, slots=True)
class RiskPercent(Sizer):
    pct_of_equity: float  # e.g. 0.01 for 1%

    def resolve(self, ctx: SizingContext) -> float:
        return _risk_units(ctx, ctx.equity * self.pct_of_equity)


@dataclass(frozen=True, slots=True)
class EquityFraction(Sizer):
    """Invest a fixed fraction of current equity per trade (no stop required).

    e.g. ``EquityFraction(0.95)`` invests 95% of current equity.
    Unlike ``RiskPercent``, this does not require a stop_loss.
    """

    fraction: float  # e.g. 0.95 for 95%

    def resolve(self, ctx: SizingContext) -> float:
        notional = ctx.equity * self.fraction
        per_unit = ctx.price * ctx.instrument.contract_size()
        if per_unit <= 0:
            return 0.0
        return ctx.instrument.to_native_units(notional / per_unit)


@dataclass(frozen=True, slots=True)
class LeveragedFraction(Sizer):
    """Allocate a fraction of *leveraged* buying power per trade.

    notional = equity × leverage × fraction
    size     = notional / (price × contract_size)

    With ``fraction=1.0`` and leverage=30 on a $10k account the full $300k
    buying power is deployed — margin used equals 100% of equity.
    Leverage is read from the instrument (``instrument.leverage``); falls back
    to 1.0 for unleveraged instruments.
    """

    fraction: float  # e.g. 1.0 for 100% of buying power

    def resolve(self, ctx: SizingContext) -> float:
        leverage = getattr(ctx.instrument, "leverage", 1.0) or 1.0
        notional = ctx.equity * leverage * self.fraction
        per_unit = ctx.price * ctx.instrument.contract_size()
        if per_unit <= 0:
            return 0.0
        return ctx.instrument.to_native_units(notional / per_unit)
