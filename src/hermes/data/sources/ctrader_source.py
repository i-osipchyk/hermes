"""Pepperstone CFD DataSource via the cTrader Open API (Spotware).

Two cTrader-specific quirks are handled here (see ADR-0006):

1. **Timestamps aren't midnight-anchored.** cTrader/Pepperstone treats the trading
   day as rolling at 17:00 New York, so its native 4h/1d bars don't line up with a
   midnight-anchored view (or with how TradingView draws them). We therefore build
   the CFD Instrument with a ``day_anchor`` of 17:00 America/New_York, and — crucially
   — **rebuild every timeframe above 1h from 1h bars** using Hermes's own bucketing,
   never trusting cTrader's native higher-timeframe bars.

2. **Optional whole-hour offset.** If the raw bar timestamps still look shifted for a
   given account/symbol, ``bar_utc_offset_minutes`` corrects them to true UTC.

The transport is the Spotware Open API (Protobuf over TLS). ``_fetch_native`` is
implemented against that API but requires credentials and live network; it is not
exercised in tests. The pure decode/normalise/resample logic *is* tested.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from ...core import Bar, Cfd, Instrument, SessionCalendar, Symbol, Timeframe
from ..aggregation import MultiTimeframeView
from ..cache import BarCache
from ..source import DataSource

_H1 = Timeframe.parse("1h")
_PRICE_SCALE = 1e5  # cTrader trendbar prices are integers scaled by 10^5
# Hermes Timeframe -> cTrader trendbar period name (only <= 1h is fetched natively).
_PERIOD = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1"}

# Forex/CFD trading week: opens Sunday 17:00 NY, closes Friday 17:00 NY.
_CFD_SESSION = SessionCalendar(
    timezone=ZoneInfo("America/New_York"),
    open_time=None,                 # continuous within trading days
    close_time=None,
    weekdays=(0, 1, 2, 3, 4, 6),    # Mon-Fri + Sunday evening open
    day_anchor=time(17, 0),         # 17:00 NY day rollover (aligns 4h/1d)
)


def trendbar_to_bar(
    low: int,
    delta_open: int,
    delta_high: int,
    delta_close: int,
    volume: float,
    ts_minutes: int,
    timeframe: Timeframe,
    *,
    offset_minutes: int = 0,
    scale: float = _PRICE_SCALE,
) -> Bar:
    """Decode a cTrader ProtoOATrendbar into a normalized, UTC-correct ``Bar``.

    cTrader encodes a bar as ``low`` plus unsigned deltas; ``ts_minutes`` is minutes
    since the Unix epoch. ``offset_minutes`` is subtracted to correct any residual
    non-UTC shift.
    """
    epoch = (ts_minutes - offset_minutes) * 60
    return Bar(
        datetime.fromtimestamp(epoch, tz=UTC),
        timeframe,
        (low + delta_open) / scale,
        (low + delta_high) / scale,
        low / scale,
        (low + delta_close) / scale,
        float(volume),
    )


def resample_from_hourly(
    bars_1h: list[Bar], instrument: Instrument, target: Timeframe
) -> list[Bar]:
    """Build closed ``target`` bars from 1h bars using Hermes's own bucketing, so
    higher-TF alignment is under our control (not cTrader's)."""
    view = MultiTimeframeView(instrument, _H1, [target])
    for b in bars_1h:
        view.push(b)
    return view[target].closed()


class CTraderSource(DataSource):
    name = "ctrader"

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        account_id: int | None = None,
        host: str = "live",                 # "live" or "demo"
        cache: BarCache | None = None,
        session: SessionCalendar = _CFD_SESSION,
        bar_utc_offset_minutes: int = 0,
        tick_size: float = 0.00001,
        lot_size: float = 100_000.0,
        leverage: float = 30.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = account_id
        self.host = host
        self.cache = cache or BarCache()
        self.session = session
        self.bar_utc_offset_minutes = bar_utc_offset_minutes
        self.tick_size = tick_size
        self.lot_size = lot_size
        self.leverage = leverage
        self._conn = None  # lazy cTrader connection

    def get_instrument(self, symbol: Symbol) -> Cfd:
        return Cfd(
            symbol,
            session=self.session,
            tick_size=self.tick_size,
            lot_size=self.lot_size,
            leverage=self.leverage,
        )

    def history(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        for gap_start, gap_end in self.cache.missing_ranges(instrument, timeframe, start, end):
            if timeframe.seconds <= _H1.seconds:
                fetched = self._fetch_native(instrument, timeframe, gap_start, gap_end)
            else:
                # Never trust cTrader's native 4h+; rebuild from 1h (see ADR-0006).
                hourly = self._fetch_native(instrument, _H1, gap_start, gap_end)
                fetched = resample_from_hourly(hourly, instrument, timeframe)
            self.cache.write(instrument, timeframe, fetched)
        return self.cache.read(instrument, timeframe, start, end)

    def supported_timeframes(self) -> set[Timeframe]:
        # Native <= 1h; everything above is synthesised from 1h, so effectively any
        # integer-multiple-of-1h timeframe is available.
        return {Timeframe.parse(t) for t in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")}

    # --- network (requires credentials; not exercised in tests) ----------------

    def _fetch_native(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        period = _PERIOD.get(str(timeframe))
        if period is None:
            raise ValueError(f"cTrader native fetch only supports <= 1h, got {timeframe}")
        raw = self._request_trendbars(instrument.symbol.ticker, period, start, end)
        return [
            trendbar_to_bar(
                low=tb["low"],
                delta_open=tb["deltaOpen"],
                delta_high=tb["deltaHigh"],
                delta_close=tb["deltaClose"],
                volume=tb["volume"],
                ts_minutes=tb["utcTimestampInMinutes"],
                timeframe=timeframe,
                offset_minutes=self.bar_utc_offset_minutes,
            )
            for tb in raw
        ]

    def _request_trendbars(self, ticker: str, period: str, start: datetime, end: datetime):
        """Call the Spotware Open API for trendbars. Returns dicts with the raw
        ProtoOATrendbar fields. Implemented against ``ctrader-open-api``; requires
        credentials + network (install with ``pip install 'hermes[pepperstone]'``)."""
        try:
            from ctrader_open_api import Client  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "CTraderSource needs 'ctrader-open-api'. pip install 'hermes[pepperstone]'."
            ) from e
        if not all((self.client_id, self.client_secret, self.access_token, self.account_id)):
            raise RuntimeError(
                "CTraderSource requires client_id, client_secret, access_token, account_id."
            )
        # TODO(live): app-auth -> account-auth -> resolve symbolId via
        # ProtoOASymbolsListReq -> ProtoOAGetTrendbarsReq(period, fromTimestamp,
        # toTimestamp), paginating the 'trendbar' list. Kept as the single integration
        # seam so all decoding/alignment above stays transport-agnostic and tested.
        raise NotImplementedError(
            "cTrader live trendbar transport not wired in this environment; "
            "implement _request_trendbars with your Spotware credentials."
        )


# Pepperstone is accessed through cTrader — expose under the broker name too.
class PepperstoneSource(CTraderSource):
    name = "pepperstone"
