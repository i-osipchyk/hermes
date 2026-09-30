"""Instrument: the tradable thing plus its metadata.

A polymorphic hierarchy (``Stock`` / ``CryptoPair`` / ``Cfd``) that absorbs every
source-specific difference so Strategies can stay **fully polymorphic** and never
branch on the concrete subtype (see ADR-0001 / CONTEXT.md). Strategy code should
only ever touch the shared interface below.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from .symbol import Symbol


class AssetClass(Enum):
    STOCK = "stock"
    CRYPTO = "crypto"
    CRYPTO_PERP = "crypto_perp"
    CFD = "cfd"


class PriceBasis(Enum):
    """What a Bar's prices represent — determines how spread is applied at fill."""

    LAST = "last"   # crypto, stocks
    MID = "mid"
    BID = "bid"     # Pepperstone / MT5 bars are bid


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    """Per-Instrument trading hours + timezone.

    Defines higher-Timeframe bucket boundaries (anchored to *this* tz) and when the
    Instrument is tradeable. A ``None`` ``open_time`` means 24/7 (crypto). Holidays
    beyond the weekday mask can be layered later via ``calendar_code`` +
    ``exchange-calendars``; the weekday mask alone is enough for the aggregation
    mechanics and is fully offline-testable.

    Note: DST transitions are handled approximately (wall-clock arithmetic); crypto
    (UTC) is unaffected.
    """

    timezone: ZoneInfo
    open_time: time | None = None
    close_time: time | None = None
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon-Fri; crypto overrides to all
    calendar_code: str | None = None
    # Time-of-day (in this tz) at which the trading "day" rolls over, for
    # higher-Timeframe bucket anchoring. ``None`` = local midnight (stocks/crypto).
    # Forex/CFD roll at 17:00 New York, which is why cTrader 4h/1d bars look shifted
    # versus a midnight-anchored view — set this to align them (see ADR-0006).
    day_anchor: time | None = None

    @property
    def is_24_7(self) -> bool:
        return self.open_time is None or self.close_time is None

    def to_local(self, moment_utc: datetime) -> datetime:
        return moment_utc.astimezone(self.timezone)

    @property
    def is_overnight(self) -> bool:
        """True when the session wraps past local midnight (open_time > close_time),
        e.g. a 17:00 -> 16:00 futures/CFD day."""
        return not self.is_24_7 and self.open_time > self.close_time

    def is_open(self, moment_utc: datetime) -> bool:
        """Whether the market is open at ``moment_utc`` (tz-aware UTC)."""
        local = self.to_local(moment_utc)
        if local.weekday() not in self.weekdays:
            return False
        if self.is_24_7:
            return True
        naive = local.time()
        if self.is_overnight:
            # Two disjoint windows in local wall-clock terms: [open, midnight)
            # and [midnight, close). Comparing open <= t < close is empty here.
            return naive >= self.open_time or naive < self.close_time
        return self.open_time <= naive < self.close_time

    def bars_per_year(self, timeframe) -> float:
        """How many bars of ``timeframe`` this session actually produces per year.

        Annualising a Sharpe ratio needs the number of *observations* per year, not
        the number of ``timeframe`` intervals in a calendar year.  A 5-minute bar on
        a 6.5-hour, 5-day-a-week equity session yields ~20k bars/year, not the
        ~105k that 365.25 x 86400 / 300 implies -- using the latter overstates every
        annualised statistic by ~2.3x.

        The result is capped at the continuous-trading rate, which is what a 24/7
        instrument (crypto) legitimately hits.
        """
        trading_days = 365.25 * len(self.weekdays) / 7.0
        if self.is_24_7:
            seconds_per_day = 86_400.0
        else:
            open_s = self.open_time.hour * 3600 + self.open_time.minute * 60
            close_s = self.close_time.hour * 3600 + self.close_time.minute * 60
            seconds_per_day = float((close_s - open_s) % 86_400) or 86_400.0
        # A bar longer than one session still yields at least one bar per trading
        # day; the cap then corrects timeframes that span several days (e.g. 1w).
        per_day = max(1.0, seconds_per_day / timeframe.seconds)
        continuous = 365.25 * 86_400.0 / timeframe.seconds
        return min(trading_days * per_day, continuous)

    def session_open_utc(self, moment_utc: datetime) -> datetime | None:
        """UTC datetime of the session OPEN for the trading day containing
        ``moment_utc``. ``None`` when 24/7."""
        if self.is_24_7:
            return None
        local = self.to_local(moment_utc)
        open_local = local.replace(
            hour=self.open_time.hour, minute=self.open_time.minute, second=0, microsecond=0
        )
        if self.is_overnight and local.time() < self.close_time:
            # Still inside the session that began yesterday evening.
            open_local -= timedelta(days=1)
        return open_local.astimezone(UTC)

    def session_close_utc(self, moment_utc: datetime) -> datetime | None:
        """UTC datetime of the session CLOSE for the trading day containing
        ``moment_utc``. ``None`` when 24/7."""
        if self.is_24_7:
            return None
        local = self.to_local(moment_utc)
        close_local = local.replace(
            hour=self.close_time.hour, minute=self.close_time.minute, second=0, microsecond=0
        )
        if self.is_overnight and local.time() >= self.close_time:
            # This session runs on into tomorrow.
            close_local += timedelta(days=1)
        return close_local.astimezone(UTC)


class Instrument(ABC):
    """Abstract base for anything tradable.

    Concrete subclasses carry the messy per-asset-class facts. NOTE: the *count*
    of instruments a Strategy trades is out of scope here — v1 is single-instrument
    (ADR-0003) but nothing in this interface assumes it.
    """

    def __init__(
        self,
        symbol: Symbol,
        *,
        quote_currency: str,
        tick_size: float,
        session: SessionCalendar,
        price_basis: PriceBasis,
    ) -> None:
        self.symbol = symbol
        self.quote_currency = quote_currency
        self.tick_size = tick_size
        self.session = session
        self.price_basis = price_basis

    # --- shared interface a Strategy may rely on -------------------------------

    @property
    @abstractmethod
    def asset_class(self) -> AssetClass: ...

    @property
    @abstractmethod
    def can_short(self) -> bool:
        """Crypto spot: False. CFD: True. Stock: account-dependent."""

    @abstractmethod
    def contract_size(self) -> float:
        """Units of the underlying per 1 size-unit (shares=1, FX lot=100_000, ...)."""

    @abstractmethod
    def to_native_units(self, size: float) -> float:
        """Convert a Sizer's resolved size into the venue's native units/lots."""

    @property
    def leverage(self) -> float:
        """Maximum leverage for margin instruments (default 1× = unleveraged)."""
        return 1.0


@dataclass(eq=False)
class Stock(Instrument):
    """An equity (yfinance / Tiingo).

    Prices are fully adjusted (splits AND dividends), so total return is already
    embedded in the series; dividends are NOT credited separately as cash.
    """

    leverage: float = 1.0

    def __init__(
        self,
        symbol: Symbol,
        *,
        session: SessionCalendar,
        tick_size: float = 0.01,
        shortable: bool = False,
        leverage: float = 1.0,
    ):
        super().__init__(
            symbol,
            quote_currency="USD",
            tick_size=tick_size,
            session=session,
            price_basis=PriceBasis.LAST,
        )
        self._shortable = shortable
        self.leverage = leverage

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.STOCK

    @property
    def can_short(self) -> bool:
        return self._shortable

    def contract_size(self) -> float:
        return 1.0

    def to_native_units(self, size: float) -> float:
        return math.floor(size)  # stocks: whole shares only, round down


@dataclass(eq=False)
class CryptoPair(Instrument):
    """A crypto spot pair (Binance). 24/7, real volume, no shorting on spot."""

    base_asset: str = ""

    def __init__(self, symbol: Symbol, *, base_asset: str, quote_currency: str, tick_size: float):
        super().__init__(
            symbol,
            quote_currency=quote_currency,
            tick_size=tick_size,
            session=SessionCalendar(
                timezone=ZoneInfo("UTC"), weekdays=(0, 1, 2, 3, 4, 5, 6)
            ),  # 24/7
            price_basis=PriceBasis.LAST,
        )
        self.base_asset = base_asset

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.CRYPTO

    @property
    def can_short(self) -> bool:
        return False  # spot only in v1

    def contract_size(self) -> float:
        return 1.0

    def to_native_units(self, size: float) -> float:
        return size


@dataclass(eq=False)
class CryptoPerpetual(Instrument):
    """A crypto perpetual future (Binance USD-M). Like a spot pair — 24/7, real volume,
    last-price — but **leveraged** and **shortable**. Funding (paid between longs/shorts)
    is not auto-modelled; set a `FinancingModel` on the Cost Model if you want to charge
    carry."""

    base_asset: str = ""
    leverage: float = 10.0

    def __init__(
        self,
        symbol: Symbol,
        *,
        base_asset: str,
        quote_currency: str,
        tick_size: float,
        leverage: float = 10.0,
    ):
        super().__init__(
            symbol,
            quote_currency=quote_currency,
            tick_size=tick_size,
            session=SessionCalendar(timezone=ZoneInfo("UTC"), weekdays=(0, 1, 2, 3, 4, 5, 6)),
            price_basis=PriceBasis.LAST,
        )
        self.base_asset = base_asset
        self.leverage = leverage

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.CRYPTO_PERP

    @property
    def can_short(self) -> bool:
        return True

    def contract_size(self) -> float:
        return 1.0

    def to_native_units(self, size: float) -> float:
        return size


@dataclass(eq=False)
class Cfd(Instrument):
    """A contract for difference (Pepperstone / MT5). Leveraged, bid-based bars,
    sized in lots, near-24/5, dividend-adjusted for share CFDs.

    ``tick_size`` (the minimum PRICE increment) and ``volume_step`` (the minimum
    ORDER SIZE increment, in lots) are independent broker facts. Rounding size to
    ``tick_size`` conflated them: NAS100 prices to 0.1 but trades in 0.01 lots, so
    every order was quantised 10x coarser than the broker requires — and any size
    below 0.1 lots floored to zero and was rejected outright.
    """

    lot_size: float = 100_000.0
    leverage: float = 30.0
    volume_step: float = 0.01

    def __init__(
        self,
        symbol: Symbol,
        *,
        session: SessionCalendar,
        tick_size: float,
        lot_size: float,
        leverage: float,
        volume_step: float = 0.01,
    ):
        super().__init__(
            symbol,
            quote_currency="USD",
            tick_size=tick_size,
            session=session,
            price_basis=PriceBasis.BID,
        )
        self.lot_size = lot_size
        self.leverage = leverage
        self.volume_step = volume_step

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.CFD

    @property
    def can_short(self) -> bool:
        return True

    def contract_size(self) -> float:
        return self.lot_size

    def to_native_units(self, size: float) -> float:
        # CFD lots: round down to the broker's volume step (e.g. 0.01 lots).
        if self.volume_step > 0:
            # Re-round to kill float dust like 0.30000000000000004.
            steps = math.floor(round(size / self.volume_step, 9))
            return round(steps * self.volume_step, 9)
        return size
