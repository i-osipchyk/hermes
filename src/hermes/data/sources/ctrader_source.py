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

Credentials default from the environment (``CTRADER_CLIENT_ID``, ``CTRADER_CLIENT_SECRET``,
``CTRADER_ACCESS_TOKEN``, ``CTRADER_ACCOUNT_ID``) so ``CTraderSource()`` / ``PepperstoneSource()``
work with no arguments once those are exported.

The transport is the Spotware Open API (Protobuf over TLS, via the ``ctrader-open-api``
Twisted client). ``_CTraderConnection`` bridges that async, callback-driven client behind
a plain blocking call so ``history()`` can stay a normal synchronous method; it requires
credentials and live network, so it is not exercised in tests. The pure
decode/normalise/paginate/resample logic *is* tested.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, time
from queue import Queue
from zoneinfo import ZoneInfo

from ...core import Bar, Cfd, Instrument, SessionCalendar, Symbol, Timeframe
from ..aggregation import MultiTimeframeView
from ..cache import BarCache
from ..source import DataSource

_H1 = Timeframe.parse("1h")
_PRICE_SCALE = 1e5  # cTrader trendbar prices are integers scaled by 10^5
# Hermes Timeframe -> cTrader trendbar period name (only <= 1h is fetched natively).
_PERIOD = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1"}
_MAX_TRENDBARS_PER_REQUEST = 1000  # Spotware Open API page size cap

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


def next_page_start(
    page: list[dict], from_ms: int, to_ms: int, period_seconds: int
) -> int | None:
    """Given one page of raw trendbar dicts (as returned by the Open API), return the
    ``fromTimestamp`` (ms) to request next, or ``None`` once ``to_ms`` is covered.

    Pure so the pagination logic is tested without a live connection; the network
    loop (:meth:`CTraderSource._request_trendbars`) just calls this in a while-loop.
    """
    if not page:
        return None
    last_ts_ms = max(tb["utcTimestampInMinutes"] for tb in page) * 60_000
    next_start = last_ts_ms + period_seconds * 1000
    if next_start >= to_ms or len(page) < _MAX_TRENDBARS_PER_REQUEST:
        return None
    return next_start


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    return int(raw) if raw else None


class _CTraderConnection:
    """One authenticated cTrader session per (client_id, access_token, account_id, host),
    reused across requests and ``CTraderSource`` instances — the TLS handshake plus
    app/account auth handshake is expensive, and the Open API expects a long-lived
    connection rather than one per request.

    This is the sole networked piece of :class:`CTraderSource`: it bridges the
    ``ctrader-open-api`` Twisted client (an async, callback/Deferred-driven library)
    behind plain blocking calls, running the reactor on a dedicated background thread
    so ``history()`` can stay a normal synchronous method. Requires live credentials +
    network, so it is not exercised in tests — see module docstring.
    """

    _instances: dict[tuple, _CTraderConnection] = {}
    _registry_lock = threading.Lock()
    _reactor = None
    _reactor_thread: threading.Thread | None = None
    _reactor_lock = threading.Lock()

    def __init__(
        self, client_id: str, client_secret: str, access_token: str, account_id: int, host: str
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._account_id = account_id
        self._host = host
        self._client = None
        self._symbol_ids: dict[str, int] = {}
        self._setup_lock = threading.Lock()

    @classmethod
    def get(
        cls, client_id: str, client_secret: str, access_token: str, account_id: int, host: str
    ) -> _CTraderConnection:
        key = (client_id, access_token, account_id, host)
        with cls._registry_lock:
            conn = cls._instances.get(key)
            if conn is None:
                conn = cls(client_id, client_secret, access_token, account_id, host)
                cls._instances[key] = conn
        return conn

    @classmethod
    def _get_reactor(cls):
        with cls._reactor_lock:
            if cls._reactor_thread is None:
                from twisted.internet import reactor

                cls._reactor = reactor
                cls._reactor_thread = threading.Thread(
                    target=reactor.run, kwargs={"installSignalHandlers": False}, daemon=True
                )
                cls._reactor_thread.start()
        return cls._reactor

    def _call(self, make_deferred, timeout: float = 30):
        """Run ``make_deferred()`` on the reactor thread and block for its result.
        ``make_deferred`` must perform the actual Twisted call when invoked — that's
        what makes it safe to hand to ``callFromThread``."""
        reactor = self._get_reactor()
        result: Queue = Queue(maxsize=1)

        def _start():
            d = make_deferred()
            d.addCallback(lambda r: result.put(("ok", r)))
            d.addErrback(lambda f: result.put(("err", f.value)))

        reactor.callFromThread(_start)
        status, value = result.get(timeout=timeout)
        if status == "err":
            raise RuntimeError(f"cTrader request failed: {value}") from value
        return value

    def _ensure_authed(self) -> None:
        with self._setup_lock:
            if self._client is not None:
                return
            from ctrader_open_api import Client, EndPoints, TcpProtocol

            host = (
                EndPoints.PROTOBUF_LIVE_HOST
                if self._host == "live"
                else EndPoints.PROTOBUF_DEMO_HOST
            )
            client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
            connected = threading.Event()
            client.setConnectedCallback(lambda _c: connected.set())
            reactor = self._get_reactor()
            reactor.callFromThread(client.startService)
            if not connected.wait(timeout=15):
                raise TimeoutError("cTrader: TLS connection to the Open API timed out")
            self._client = client
            self._call(
                lambda: client.send(
                    "ProtoOAApplicationAuthReq",
                    clientId=self._client_id,
                    clientSecret=self._client_secret,
                )
            )
            self._call(
                lambda: client.send(
                    "ProtoOAAccountAuthReq",
                    ctidTraderAccountId=self._account_id,
                    accessToken=self._access_token,
                )
            )

    def symbol_id(self, ticker: str) -> int:
        self._ensure_authed()
        if ticker not in self._symbol_ids:
            res = self._call(
                lambda: self._client.send(
                    "ProtoOASymbolsListReq", ctidTraderAccountId=self._account_id
                )
            )
            self._symbol_ids.update({s.symbolName: s.symbolId for s in res.symbol})
            if ticker not in self._symbol_ids:
                raise ValueError(f"cTrader: unknown symbol {ticker!r} on account {self._account_id}")
        return self._symbol_ids[ticker]

    def trendbars(self, symbol_id: int, period: str, from_ms: int, to_ms: int, count: int) -> list:
        self._ensure_authed()
        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod

        res = self._call(
            lambda: self._client.send(
                "ProtoOAGetTrendbarsReq",
                ctidTraderAccountId=self._account_id,
                symbolId=symbol_id,
                period=ProtoOATrendbarPeriod.Value(period),
                fromTimestamp=from_ms,
                toTimestamp=to_ms,
                count=count,
            )
        )
        return list(res.trendbar)


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
        # Explicit args win; otherwise fall back to the environment so
        # CTraderSource()/PepperstoneSource() work with no arguments.
        self.client_id = client_id or os.environ.get("CTRADER_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("CTRADER_CLIENT_SECRET")
        self.access_token = access_token or os.environ.get("CTRADER_ACCESS_TOKEN")
        self.account_id = account_id if account_id is not None else _int_env("CTRADER_ACCOUNT_ID")
        self.host = host
        self.cache = cache or BarCache()
        self.session = session
        self.bar_utc_offset_minutes = bar_utc_offset_minutes
        self.tick_size = tick_size
        self.lot_size = lot_size
        self.leverage = leverage

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
        raw = self._request_trendbars(
            instrument.symbol.ticker, period, timeframe.seconds, start, end
        )
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

    def _request_trendbars(
        self, ticker: str, period: str, period_seconds: int, start: datetime, end: datetime
    ) -> list[dict]:
        """Call the Spotware Open API for trendbars, paginating until ``end`` is
        covered. Returns dicts with the raw ProtoOATrendbar fields. Requires
        credentials + live network (install with ``pip install 'hermes[pepperstone]'``);
        not exercised in tests — see module docstring."""
        conn = self._connection()
        symbol_id = conn.symbol_id(ticker)
        from_ms, to_ms = _ms(start), _ms(end)
        out: list[dict] = []
        while from_ms is not None:
            page = [
                {
                    "low": tb.low,
                    "deltaOpen": tb.deltaOpen,
                    "deltaHigh": tb.deltaHigh,
                    "deltaClose": tb.deltaClose,
                    "volume": tb.volume,
                    "utcTimestampInMinutes": tb.utcTimestampInMinutes,
                }
                for tb in conn.trendbars(symbol_id, period, from_ms, to_ms, _MAX_TRENDBARS_PER_REQUEST)
            ]
            out.extend(page)
            from_ms = next_page_start(page, from_ms, to_ms, period_seconds)
        return out

    def _connection(self) -> _CTraderConnection:
        if not all((self.client_id, self.client_secret, self.access_token, self.account_id)):
            raise RuntimeError(
                "CTraderSource requires client_id, client_secret, access_token, account_id "
                "(pass explicitly, or set CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET / "
                "CTRADER_ACCESS_TOKEN / CTRADER_ACCOUNT_ID)."
            )
        try:
            import ctrader_open_api  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "CTraderSource needs 'ctrader-open-api'. pip install 'hermes[pepperstone]'."
            ) from e
        return _CTraderConnection.get(
            self.client_id, self.client_secret, self.access_token, self.account_id, self.host
        )


# Pepperstone is accessed through cTrader — expose under the broker name too.
class PepperstoneSource(CTraderSource):
    name = "pepperstone"
