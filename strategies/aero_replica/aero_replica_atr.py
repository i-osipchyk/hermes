# Parameters: 0
#
# aero_replica.py with the take-profit scaled to volatility instead of a fixed $12/oz.
# In the EA's own window (2026) $12 was ~0.33 x ATR(4h,20) (logged ATR median 37.3, which
# reads ~7% above Hermes' Wilder ATR on Pepperstone bars).  A fixed $12 is a 2.4-ATR target
# when gold was $1,300 (2018), a different trade; this keeps the 2026 geometry everywhere.

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_aero_replica", Path(__file__).with_name("aero_replica.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"

TP_ATR = 0.33


class AeroReplicaAtr(_base.AeroReplica):
    def _tp(self, atr: float) -> float:
        return TP_ATR * atr


def build_backtest(**overrides):
    overrides.setdefault("strategy", AeroReplicaAtr())
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
