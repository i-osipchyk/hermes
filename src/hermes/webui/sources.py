"""Registry of DataSources selectable in the web UI, so a strategy can run against a
different provider without editing its file.

cTrader/Pepperstone credentials are read from the environment (``CTRADER_*``); the
source builds fine without them, but a fetch will error clearly until they're set (and
the live cTrader transport is wired).
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
    account_id = os.environ.get("CTRADER_ACCOUNT_ID")
    return PepperstoneSource(
        client_id=os.environ.get("CTRADER_CLIENT_ID"),
        client_secret=os.environ.get("CTRADER_CLIENT_SECRET"),
        access_token=os.environ.get("CTRADER_ACCESS_TOKEN"),
        account_id=int(account_id) if account_id else None,
        host=os.environ.get("CTRADER_HOST", "live"),
    )


_FACTORIES: dict[str, Callable[[], DataSource]] = {
    BinanceSource.name: BinanceSource,                 # "binance" (spot)
    BinanceFuturesSource.name: BinanceFuturesSource,   # "binance-futures"
    YFinanceSource.name: YFinanceSource,               # "yfinance"
    PepperstoneSource.name: _ctrader_from_env,         # "pepperstone" (cTrader)
}

# Sources that need credentials / an unfinished transport — flagged in the UI.
NEEDS_SETUP = {PepperstoneSource.name}


def source_names() -> list[str]:
    return list(_FACTORIES)


def build_source(name: str) -> DataSource:
    if name not in _FACTORIES:
        raise ValueError(f"Unknown source '{name}'. Known: {', '.join(_FACTORIES)}")
    return _FACTORIES[name]()
