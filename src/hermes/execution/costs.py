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
from datetime import date, datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

from ..core import AssetClass, Instrument, PriceBasis
from .order import Liquidity, Side


class CommissionModel(ABC):
    # True when the fee is a fraction of notional and so comes out in the Instrument's
    # quote currency. Fixed per-lot / per-share fees are already in account currency.
    notional_based: ClassVar[bool] = False

    @abstractmethod
    def commission(
        self, instrument: Instrument, price: float, size: float, liquidity: Liquidity
    ) -> float: ...


@dataclass(slots=True)
class PercentCommission(CommissionModel):
    notional_based: ClassVar[bool] = True
    """Flat percentage of notional, regardless of maker/taker."""

    rate: float = 0.001

    def commission(self, instrument, price, size, liquidity=Liquidity.TAKER) -> float:
        return abs(price * size * instrument.contract_size()) * self.rate


@dataclass(slots=True)
class MakerTakerCommission(CommissionModel):
    notional_based: ClassVar[bool] = True
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


@dataclass(frozen=True, slots=True)
class SpreadWindow:
    """A local-time window ``[start, end)`` in which the base spread is multiplied.
    ``weekdays`` (Mon=0..Sun=6) restricts it; ``None`` means every day."""

    multiplier: float
    start: time
    end: time
    weekdays: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class SpreadSchedule:
    """Spread multiplier by time of week. Bars carry only the bid, so the spread can't be
    measured from data; this is the assumption that stands in for it. Windows are read in
    ``timezone`` (so a New York 17:00 rollover tracks DST); where windows overlap the widest
    wins; outside every window the multiplier is 1."""

    timezone: ZoneInfo
    windows: tuple[SpreadWindow, ...] = ()

    @classmethod
    def cfd_default(cls) -> SpreadSchedule:
        """FX/CFD liquidity gaps: x10 for the hour after the Sunday open and x5 around the
        17:00 New York daily rollover. Rough, round numbers (spreads widen 5-20x in
        practice); post-release spikes are not scheduled -- add a window for those."""
        return cls(ZoneInfo("America/New_York"), (
            SpreadWindow(10.0, time(17, 0), time(18, 0), weekdays=(6,)),
            SpreadWindow(5.0, time(16, 55), time(17, 5), weekdays=(0, 1, 2, 3, 4)),
        ))

    @classmethod
    def us_equity_open(cls, minutes: int = 5, multiplier: float = 3.0) -> SpreadSchedule:
        """Wider quotes in the first ``minutes`` after the 09:30 New York open."""
        end = datetime(2000, 1, 1, 9, 30) + timedelta(minutes=minutes)
        return cls(ZoneInfo("America/New_York"), (
            SpreadWindow(multiplier, time(9, 30), end.time(), weekdays=(0, 1, 2, 3, 4)),
        ))

    def multiplier(self, ts: datetime) -> float:
        local = ts.astimezone(self.timezone)
        best = 1.0
        for w in self.windows:
            if w.weekdays is not None and local.weekday() not in w.weekdays:
                continue
            if w.start <= local.time() < w.end:
                best = max(best, w.multiplier)
        return best


@dataclass(slots=True)
class SpreadModel:
    """Applies bid/ask at fill for **taker** fills. ``points`` is the full spread in
    price units. A maker fill rests at its own price and does not cross the spread.
    An optional ``schedule`` scales ``points`` by the fill's timestamp."""

    points: float = 0.0
    schedule: SpreadSchedule | None = None

    def adjust_fill(
        self, instrument, side: Side, price: float, liquidity=Liquidity.TAKER,
        ts: datetime | None = None,
    ) -> float:
        points = self.points
        if self.schedule is not None and ts is not None:
            points *= self.schedule.multiplier(ts)
        if points <= 0 or liquidity is Liquidity.MAKER:
            return price
        if instrument.price_basis is PriceBasis.BID:
            # Bars are bid; ask = bid + spread. Buys lift the ask, sells hit the bid.
            return price + points if side is Side.BUY else price
        # LAST/MID basis: split the spread half each side around mid.
        half = points / 2
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


# Weight of the night that *starts* on each weekday (Mon..Sun). Spot FX settles T+2, so the
# Wednesday rollover books Wed, Thu and Fri-weekend value dates at once; Sat/Sun nights are
# never separately charged.
FX_NIGHT_WEIGHTS: tuple[float, ...] = (1.0, 1.0, 3.0, 1.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class RateChange:
    """A financing rate that takes effect on ``effective`` (the night starting that day)."""

    effective: date
    annual_rate: float
    short_rate: float | None = None  # None = same as annual_rate


@dataclass(slots=True)
class FinancingModel:
    """Overnight swap. ``annual_rate`` is the long rate (and the short rate too unless
    ``short_rate`` is given). A negative rate is carry paid *to* the position."""

    annual_rate: float = 0.0  # charged per day a position is held past a session
    short_rate: float | None = None
    # Per-weekday weight of the night starting that day; None = every calendar night is 1.
    night_weights: tuple[float, ...] | None = None

    # Dated rate changes; the latest one effective on or before a night replaces the base rate.
    schedule: tuple[RateChange, ...] = ()

    def scaled(self, factor: float) -> FinancingModel:
        def short(rate: float | None) -> float | None:
            return None if rate is None else rate * factor

        return FinancingModel(
            annual_rate=self.annual_rate * factor,
            short_rate=short(self.short_rate),
            night_weights=self.night_weights,
            schedule=tuple(
                RateChange(c.effective, c.annual_rate * factor, short(c.short_rate))
                for c in self.schedule
            ),
        )

    def _rate(self, side: Side, night: date | None = None) -> float:
        long_rate, short_rate = self.annual_rate, self.short_rate
        if night is not None:
            for change in sorted(self.schedule, key=lambda c: c.effective):
                if change.effective <= night:
                    long_rate, short_rate = change.annual_rate, change.short_rate
        return long_rate if side is Side.BUY or short_rate is None else short_rate

    def charge_nights(
        self, instrument: Instrument, notional: float, side: Side, first_night: date, nights: int
    ) -> float:
        """Signed cash cost of ``nights`` consecutive nights, the first starting on ``first_night``."""
        total = 0.0
        for i in range(nights):
            night = first_night + timedelta(days=i)
            weight = 1.0 if self.night_weights is None else self.night_weights[night.weekday()]
            total += abs(notional) * self._rate(side, night) / 365.0 * weight
        return total

    def overnight_charge(
        self, instrument: Instrument, notional: float, days: float = 1, side: Side = Side.BUY
    ) -> float:
        """Signed cash cost: positive is paid, negative is received."""
        return abs(notional) * self._rate(side) / 365.0 * days


@dataclass(slots=True)
class CostModel:
    commission: CommissionModel
    spread: SpreadModel
    slippage: SlippageModel
    financing: FinancingModel

    def fill_price(
        self, instrument: Instrument, side: Side, raw_price: float, liquidity=Liquidity.TAKER,
        ts: datetime | None = None,
    ) -> float:
        """Apply spread then slippage to a raw fill price (both no-ops for a maker).
        ``ts`` is the fill's time, which a spread schedule needs."""
        priced = self.spread.adjust_fill(instrument, side, raw_price, liquidity, ts)
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
            spread=SpreadModel(points=self.spread.points * factor, schedule=self.spread.schedule),
            slippage=SlippageModel(
                ticks=self.slippage.ticks * factor,
                percent=self.slippage.percent * factor,
            ),
            financing=self.financing.scaled(factor),
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
    def pepperstone_razor(cls, instrument: Instrument) -> CostModel:
        """Razor account: ~0.2 pip raw spread plus $7 per lot round trip ($3.50 a side).
        Calibrated for a 5-digit FX CFD (a pip = 10 ticks); set the spread yourself for
        indices. Slippage is zero: the quoted spread plus commission is the cost."""
        return cls(
            commission=PerLotCommission(per_lot=3.5),
            spread=SpreadModel(
                points=2 * instrument.tick_size, schedule=SpreadSchedule.cfd_default()
            ),
            slippage=SlippageModel(ticks=0.0),
            financing=FinancingModel(annual_rate=0.005),
        )

    @classmethod
    def pepperstone_standard(cls, instrument: Instrument) -> CostModel:
        """Standard account: ~1 pip spread, no commission. Same calibration as Razor."""
        return cls(
            commission=PerLotCommission(per_lot=0.0),
            spread=SpreadModel(
                points=10 * instrument.tick_size, schedule=SpreadSchedule.cfd_default()
            ),
            slippage=SlippageModel(ticks=0.0),
            financing=FinancingModel(annual_rate=0.005),
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
            # eToro: real (1x) stock/ETF positions carry no overnight fee, but a
            # leveraged position is a CFD-style margin loan charged daily at
            # (6.4% + benchmark) / 365 × notional -- the benchmark leg (e.g. SOFR)
            # isn't modelled here, so this is a floor; override via FinancingModel
            # for a specific rate. Weekend/holiday carry falls out of the actual
            # elapsed calendar days between sessions (see SimulatedVenue), not a
            # separate multiplier.
            leveraged = getattr(instrument, "leverage", 1.0) > 1.0
            return cls(
                commission=PerShareCommission(per_share=0.0),  # commission-free default
                spread=SpreadModel(0.0),
                slippage=SlippageModel(ticks=1.0),
                financing=FinancingModel(annual_rate=0.064 if leveraged else 0.0),
            )
        # CFD: spread is the cost; carry applies overnight.
        return cls(
            commission=PerLotCommission(0.0),
            spread=SpreadModel(
                points=2 * instrument.tick_size, schedule=SpreadSchedule.cfd_default()
            ),
            slippage=SlippageModel(ticks=1.0),
            financing=FinancingModel(annual_rate=0.005),
        )
