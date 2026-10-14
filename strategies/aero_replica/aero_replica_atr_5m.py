# Parameters: 0
#
# aero_replica_atr.py on 5m base bars: AeroReplicaAtr (TP 0.33 x ATR).
# Finer bars place stop fills, the 3h recovery stop and the target closer to the EA's
# tick-level behaviour than 15m does.

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "hermes_strategy_aero_replica_atr", Path(__file__).with_name("aero_replica_atr.py")
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

GENERATED_BY = "hermes-strategy"


def build_backtest(**overrides):
    overrides.setdefault("base", "5m")
    return _base.build_backtest(**overrides)


if __name__ == "__main__":
    build_backtest().run()
