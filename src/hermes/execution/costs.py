"""Cost Model: commission + spread + slippage + financing (CONTEXT.md).

Pluggable per Instrument with asset-class defaults. Spread is the dominant cost for
CFDs (buy@ask / sell@bid); financing is the carry for positions held past a
session (a configurable rate, since it isn't in candle data).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core import AssetClass, Instrument, PriceBasis
from .order import Side


class CommissionModel(ABC):
    @abstractmethod
    def commission(self, instrument: Instrument, price: float, size: float) -> float: ...


@dataclass(slots=True)
class PercentCommission(CommissionModel):
    rate: float = 0.001  # e.g. Binance taker 0.1%

    def commission(self, instrument: Instrument, price: float, size: float) -> float:
        notional = abs(price * size * instrument.contract_size())
        return notional * self.rate


@dataclass(slots=True)
class PerShareCommission(CommissionModel):
    per_share: float = 0.0
    minimum: float = 0.0

    def commission(self, instrument: Instrument, price: float, size: float) -> float:
        return max(self.minimum, abs(size) * self.per_share) if size else 0.0


@dataclass(slots=True)
class PerLotCommission(CommissionModel):
    per_lot: float = 0.0

    def commission(self, instrument: Instrument, price: float, size: float) -> float:
        return abs(size) * self.per_lot


@dataclass(slots=True)
class SpreadModel:
    """Applies bid/ask at fill. ``points`` is the full spread in price units."""

    points: float = 0.0

    def adjust_fill(self, instrument: Instrument, side: Side, price: float) -> float:
        if self.points <= 0:
            return price
        if instrument.price_basis is PriceBasis.BID:
            # Bars are bid; ask = bid + spread. Buys lift the ask, sells hit the bid.
            return price + self.points if side is Side.BUY else price
        # LAST/MID basis: split the spread half each side around mid.
        half = self.points / 2
        return price + half if side is Side.BUY else price - half


@dataclass(slots=True)
class SlippageModel:
    ticks: float = 0.0        # fixed ticks of adverse slippage
    percent: float = 0.0      # or a fraction of price

    def adjust_fill(self, instrument: Instrument, side: Side, price: float) -> float:
        slip = self.ticks * instrument.tick_size + self.percent * price
        return price + slip if side is Side.BUY else price - slip


@dataclass(slots=True)
class FinancingModel:
    annual_rate: float = 0.0  # charged per day a position is held past a session

    def overnight_charge(self, instrument: Instrument, notional: float, days: int = 1) -> float:
        return abs(notional) * self.annual_rate / 365.0 * days


@dataclass(slots=True)
class CostModel:
    commission: CommissionModel
    spread: SpreadModel
    slippage: SlippageModel
    financing: FinancingModel

    def fill_price(self, instrument: Instrument, side: Side, raw_price: float) -> float:
        """Apply spread then slippage to a raw fill price."""
        priced = self.spread.adjust_fill(instrument, side, raw_price)
        return self.slippage.adjust_fill(instrument, side, priced)

    @classmethod
    def default_for(cls, instrument: Instrument) -> CostModel:
        """Sensible per-asset-class defaults (all overridable)."""
        ac = instrument.asset_class
        if ac is AssetClass.CRYPTO:
            return cls(
                commission=PercentCommission(0.001),
                spread=SpreadModel(0.0),
                slippage=SlippageModel(percent=0.0002),
                financing=FinancingModel(0.0),  # spot: no carry
            )
        if ac is AssetClass.CRYPTO_PERP:
            return cls(
                commission=PercentCommission(0.0004),  # futures taker ~0.04%
                spread=SpreadModel(0.0),
                slippage=SlippageModel(percent=0.0002),
                financing=FinancingModel(0.0),  # funding not auto-modelled; set if needed
            )
        if ac is AssetClass.STOCK:
            return cls(
                commission=PerShareCommission(per_share=0.0),  # commission-free default
                spread=SpreadModel(0.0),
                slippage=SlippageModel(ticks=1.0),
                financing=FinancingModel(0.0),  # cash account
            )
        # CFD: spread is the cost; carry applies overnight.
        return cls(
            commission=PerLotCommission(0.0),
            spread=SpreadModel(points=2 * instrument.tick_size),
            slippage=SlippageModel(ticks=1.0),
            financing=FinancingModel(annual_rate=0.05),
        )
