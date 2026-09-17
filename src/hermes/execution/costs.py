"""Cost Model: commission + spread + slippage + financing (CONTEXT.md).

Pluggable per Instrument with asset-class defaults. Costs are **liquidity-aware**
(ADR-0009): a resting limit order (limit entry, take-profit) fills as a **maker** — it
pays the maker fee (often a rebate) and takes **no adverse slippage or spread**, because
it fills at its own price. A market/stop order is a **taker** — it crosses the book, so
it pays the taker fee plus slippage/spread. Ignoring this makes limit-order strategies
look far worse than a live account.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core import AssetClass, Instrument, PriceBasis
from .order import Liquidity, Side


class CommissionModel(ABC):
    @abstractmethod
    def commission(
        self, instrument: Instrument, price: float, size: float, liquidity: Liquidity
    ) -> float: ...


@dataclass(slots=True)
class PercentCommission(CommissionModel):
    """Flat percentage of notional, regardless of maker/taker."""

    rate: float = 0.001

    def commission(self, instrument, price, size, liquidity=Liquidity.TAKER) -> float:
        return abs(price * size * instrument.contract_size()) * self.rate


@dataclass(slots=True)
class MakerTakerCommission(CommissionModel):
    """Separate maker/taker rates of notional. ``maker_rate`` may be **negative** (a
    rebate — you get paid to provide liquidity)."""

    maker_rate: float = 0.0002
    taker_rate: float = 0.0005

    def commission(self, instrument, price, size, liquidity=Liquidity.TAKER) -> float:
        rate = self.maker_rate if liquidity is Liquidity.MAKER else self.taker_rate
        return abs(price * size * instrument.contract_size()) * rate


@dataclass(slots=True)
class PerShareCommission(CommissionModel):
    per_share: float = 0.0
    minimum: float = 0.0

    def commission(self, instrument, price, size, liquidity=Liquidity.TAKER) -> float:
        return max(self.minimum, abs(size) * self.per_share) if size else 0.0


@dataclass(slots=True)
class PerLotCommission(CommissionModel):
    per_lot: float = 0.0

    def commission(self, instrument, price, size, liquidity=Liquidity.TAKER) -> float:
        return abs(size) * self.per_lot


@dataclass(slots=True)
class SpreadModel:
    """Applies bid/ask at fill for **taker** fills. ``points`` is the full spread in
    price units. A maker fill rests at its own price and does not cross the spread."""

    points: float = 0.0

    def adjust_fill(self, instrument, side: Side, price: float, liquidity=Liquidity.TAKER) -> float:
        if self.points <= 0 or liquidity is Liquidity.MAKER:
            return price
        if instrument.price_basis is PriceBasis.BID:
            # Bars are bid; ask = bid + spread. Buys lift the ask, sells hit the bid.
            return price + self.points if side is Side.BUY else price
        # LAST/MID basis: split the spread half each side around mid.
        half = self.points / 2
        return price + half if side is Side.BUY else price - half


@dataclass(slots=True)
class SlippageModel:
    """Adverse slippage on **taker** fills only. A resting limit/take-profit fills at
    its price (or better), so a maker fill takes no slippage."""

    ticks: float = 0.0        # fixed ticks of adverse slippage
    percent: float = 0.0      # or a fraction of price

    def adjust_fill(self, instrument, side: Side, price: float, liquidity=Liquidity.TAKER) -> float:
        if liquidity is Liquidity.MAKER:
            return price
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

    def fill_price(
        self, instrument: Instrument, side: Side, raw_price: float, liquidity=Liquidity.TAKER
    ) -> float:
        """Apply spread then slippage to a raw fill price (both no-ops for a maker)."""
        priced = self.spread.adjust_fill(instrument, side, raw_price, liquidity)
        return self.slippage.adjust_fill(instrument, side, priced, liquidity)

    def scaled(self, factor: float) -> CostModel:
        """Return a new CostModel with all cost components scaled by factor."""
        comm = self.commission
        if isinstance(comm, PercentCommission):
            new_comm = PercentCommission(rate=comm.rate * factor)
        elif isinstance(comm, MakerTakerCommission):
            new_comm = MakerTakerCommission(
                maker_rate=comm.maker_rate * factor,
                taker_rate=comm.taker_rate * factor,
            )
        elif isinstance(comm, PerShareCommission):
            new_comm = PerShareCommission(
                per_share=comm.per_share * factor,
                minimum=comm.minimum * factor,
            )
        elif isinstance(comm, PerLotCommission):
            new_comm = PerLotCommission(per_lot=comm.per_lot * factor)
        else:
            new_comm = comm

        return CostModel(
            commission=new_comm,
            spread=SpreadModel(points=self.spread.points * factor),
            slippage=SlippageModel(
                ticks=self.slippage.ticks * factor,
                percent=self.slippage.percent * factor,
            ),
            financing=FinancingModel(annual_rate=self.financing.annual_rate * factor),
        )

    @classmethod
    def etoro_stock(cls) -> CostModel:
        """eToro fees for real (unleveraged) stocks and ETFs backed by Yahoo Finance data.

        eToro charges:
        - $0 commission for real stock/ETF trades.
        - Market bid-ask spread (the main cost). Yahoo Finance uses LAST prices so the
          spread is split half each side: a 1-tick spread ($0.01 for most US stocks)
          costs ~$0.005 per share on entry and ~$0.005 on exit.
        - No overnight financing for unleveraged, real-asset positions.

        Slippage is left at zero because the spread already captures fill friction.
        Scale the spread via ``CostModel.scaled()`` for illiquid or wide-spread names.
        """
        return cls(
            commission=PerShareCommission(per_share=0.0),
            spread=SpreadModel(points=0.01),   # 1-tick bid-ask spread ($0.01 for US equities)
            slippage=SlippageModel(ticks=0.0),
            financing=FinancingModel(annual_rate=0.0),
        )

    @classmethod
    def default_for(cls, instrument: Instrument) -> CostModel:
        """Sensible per-asset-class defaults (all overridable)."""
        ac = instrument.asset_class
        if ac is AssetClass.CRYPTO:
            # Binance spot VIP0: maker = taker = 0.10%.
            return cls(
                commission=MakerTakerCommission(maker_rate=0.001, taker_rate=0.001),
                spread=SpreadModel(0.0),
                slippage=SlippageModel(percent=0.0002),
                financing=FinancingModel(0.0),  # spot: no carry
            )
        if ac is AssetClass.CRYPTO_PERP:
            # Binance USD-M VIP0: maker 0.02%, taker 0.05% (set maker negative for a rebate).
            return cls(
                commission=MakerTakerCommission(maker_rate=0.0002, taker_rate=0.0005),
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
