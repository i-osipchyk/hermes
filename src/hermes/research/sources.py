"""Registry of DataSources selectable in the web UI, so a strategy can run against a
different provider without editing its file.

cTrader/Pepperstone credentials are resolved by the source itself: the shared token pair
from AWS SSM, ``CTRADER_ACCOUNT_ID`` from the environment (see ``ctrader_source``). The
source builds fine without them; a fetch errors clearly until they're available.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from ..data import (
    BinanceFuturesSource,
    BinanceSource,
    DataSource,
    PepperstoneSource,
    YFinanceSource,
)


def _ctrader_from_env() -> PepperstoneSource:
    # Credentials are left to the source: passing the env token here would make a
    # stale .env copy win over the shared one in SSM.
    return PepperstoneSource(host=os.environ.get("CTRADER_HOST", "live"))


_FACTORIES: dict[str, Callable[[], DataSource]] = {
    BinanceSource.name: BinanceSource,                 # "binance" (spot)
    BinanceFuturesSource.name: BinanceFuturesSource,   # "binance-futures"
    YFinanceSource.name: YFinanceSource,               # "yfinance"
    PepperstoneSource.name: _ctrader_from_env,         # "pepperstone" (cTrader)
}

# Sources that need credentials / an unfinished transport — flagged in the UI.
NEEDS_SETUP = {PepperstoneSource.name}

# Sources whose instruments carry a leverage multiplier (CFDs, perps, margin stocks).
LEVERAGED_SOURCES = {PepperstoneSource.name, BinanceFuturesSource.name, YFinanceSource.name}


def source_names() -> list[str]:
    return list(_FACTORIES)


def build_source(name: str, *, leverage: float | None = None) -> DataSource:
    if name not in _FACTORIES:
        raise ValueError(f"Unknown source '{name}'. Known: {', '.join(_FACTORIES)}")
    source = _FACTORIES[name]()
    if leverage is not None and hasattr(source, "leverage"):
        source.leverage = leverage
    return source
