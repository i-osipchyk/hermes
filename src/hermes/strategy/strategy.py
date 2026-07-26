"""Strategy: the primary user-facing API (ADR-0001).

Authors subclass ``Strategy``, declare Indicators + Parameters in :meth:`setup`,
and write logic in :meth:`on_bar`. The *same* subclass runs unchanged in backtest
and live — it only ever touches the injected DataSource/ExecutionVenue interfaces,
never their concrete types. Order helpers below emit imperative Orders; sizing is
expressed via a Sizer and resolved to native units before submission.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core import Bar, Instrument, Timeframe
from ..execution import Order, OrderType, Side, Trade
from ..indicators import Indicator
from .parameter import Parameter
from .sizing import Sizer, SizingContext


class Strategy(ABC):
    """Base class for user strategies.

    The engine wires ``instrument``, the multi-timeframe view, the ExecutionVenue,
    the Account, and (optionally) the AI Advisor before the run starts. During the
    Lead-in the engine feeds bars to indicators but suppresses ``on_bar`` until
    every declared Indicator is warm; :meth:`on_start` fires when trading begins.
    """

    # Populated by the engine at wiring time.
    instrument: Instrument
    base_timeframe: Timeframe
    venue: object          # ExecutionVenue
    advisor: object | None  # AIAdvisor or None

    def __init__(self) -> None:
        self._indicators: list[Indicator] = []
        self._params: dict[str, object] = {}
        self._param_specs: dict[str, Parameter] = {}
        self._view = None           # MultiTimeframeView, set by the engine
        self._current_bar: Bar | None = None
        self.venue = None
        self.advisor = None

    # --- data access (inside on_bar) ------------------------------------------

    def data(self, timeframe: Timeframe):
        """The :class:`TimeframeSeries` for ``timeframe`` (closed bars + Forming
        Bar). Read ``.current()``, ``.closed()``, ``.forming`` from it."""
        return self._view[timeframe]

    def indicator_value(self, indicator: Indicator) -> dict[str, float | None]:
        """Compute an Indicator over its Timeframe's visible series (incl. Forming
        Bar) as of now."""
        return indicator.compute(self._view[indicator.timeframe].bars_for_compute())

    @property
    def price(self) -> float:
        """Current Base bar close."""
        return self._current_bar.close

    @property
    def registered_indicators(self) -> list[Indicator]:
        return self._indicators

    # --- declaration (called once) --------------------------------------------

    @abstractmethod
    def setup(self) -> None:
        """Declare Parameters and Indicators, subscribe Timeframes. Called once
        before any data flows."""

    def param(self, spec: Parameter):
        """Declare a tunable Parameter and return its current value (an override seeded
        by the engine if present, else the default)."""
        self._param_specs[spec.name] = spec
        self._params.setdefault(spec.name, spec.default)
        return self._params[spec.name]

    def declared_parameters(self) -> list[Parameter]:
        """The Parameter specs this strategy declared in :meth:`setup` — what a UI
        renders as editable controls. Requires ``setup()`` to have run."""
        return list(self._param_specs.values())

    def use(self, indicator: Indicator) -> Indicator:
        """Register an Indicator so the engine updates it each Base step and sizes
        the Lead-in from its lookback."""
        self._indicators.append(indicator)
        return indicator

    # --- lifecycle hooks (override as needed) ---------------------------------

    def on_start(self) -> None:
        """Called once when warmup completes and trading begins."""

    @abstractmethod
    def on_bar(self, bar: Bar) -> None:
        """Called on each Base bar (after indicators update). Primary logic goes
        here; read higher-Timeframe series/Forming Bars and place orders."""

    def on_trade_closed(self, trade: Trade) -> None:
        """Called when any Trade closes (stop/target/manual)."""

    def on_stop(self) -> None:
        """Called once at the end of the run."""

    # --- order helpers (imperative API) ---------------------------------------

    def buy(
        self,
        size: Sizer | float,
        *,
        type: OrderType = OrderType.MARKET,
        limit: float | None = None,
        stop: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        tag: str | None = None,
    ) -> Order:
        return self._order(Side.BUY, size, type, limit, stop, stop_loss, take_profit, tag)

    def sell(
        self,
        size: Sizer | float,
        *,
        type: OrderType = OrderType.MARKET,
        limit: float | None = None,
        stop: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        tag: str | None = None,
    ) -> Order:
        return self._order(Side.SELL, size, type, limit, stop, stop_loss, take_profit, tag)

    def close(self, trade: Trade) -> None:
        """Close a specific open Trade at market."""
        self.venue.close_trade(trade)

    def modify(self, trade: Trade, *, stop_loss=None, take_profit=None) -> None:
        self.venue.modify_trade(trade, stop_loss=stop_loss, take_profit=take_profit)

    def _order(self, side, size, type, limit, stop, stop_loss, take_profit, tag) -> Order:
        if isinstance(size, Sizer):
            ctx = SizingContext(
                instrument=self.instrument,
                price=self.price,
                equity=self.venue.equity(),
                stop_price=stop_loss,
            )
            resolved = size.resolve(ctx)
        else:
            resolved = self.instrument.to_native_units(float(size))
        order = Order(
            instrument=self.instrument,
            side=side,
            size=abs(resolved),
            type=type,
            limit_price=limit,
            stop_price=stop,
            stop_loss=stop_loss,
            take_profit=take_profit,
            tag=tag,
        )
        return self.venue.submit(order)

    # --- optional AI confirmation gate (ADR-0005) -----------------------------

    def confirm_with_ai(self, order: Order, prompt: str) -> bool:
        """Ask the AI Advisor to approve/veto an already-formed candidate order.

        Returns True (approve / no advisor configured) or False (veto). The Advisor
        assembles look-ahead-safe context from the current view; in backtest the
        decision is served from the deterministic cache.
        """
        if getattr(self, "advisor", None) is None:
            return True
        decision = self.advisor.evaluate(self, order, prompt)
        return decision.approved
