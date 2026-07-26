"""Order: an instruction to transact (ADR-0004).

Market orders fill at the next Base bar's open. Limit/stop entries are resting
(GTC) orders that persist across bars until their price is touched or they are
cancelled. Fills use conservative OHLC-touch rules; a gap through a stop fills at
the open (worse than the stop price)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..core import Instrument


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    WORKING = "working"      # resting, awaiting a touch
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"    # e.g. exceeds available margin


class Liquidity(Enum):
    """Whether a fill provided or removed liquidity — drives maker/taker fees and
    whether slippage applies."""

    MAKER = "maker"   # a resting limit order (limit entry, take-profit): fills at its
                      # price, provides liquidity, no adverse slippage
    TAKER = "taker"   # a market/stop order that crosses the book: pays taker fee + slippage


@dataclass(slots=True)
class Order:
    instrument: Instrument
    side: Side
    size: float                      # resolved native units/lots (see Sizer)
    type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    # Protective levels to attach to the resulting Trade on fill:
    stop_loss: float | None = None
    take_profit: float | None = None
    status: OrderStatus = OrderStatus.WORKING
    created_at: datetime | None = None
    filled_at: datetime | None = None
    fill_price: float | None = None
    tag: str | None = None           # author label, surfaced in the blotter
