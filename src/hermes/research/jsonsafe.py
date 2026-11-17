"""Strict-JSON floats for the ledger and CLI (audit F10).

``json`` writes ``inf``/``nan`` as the bare tokens ``Infinity``/``NaN``, which Python reads
back but jq, JavaScript and most other parsers reject. A run with no losing trades has
``profit_factor = inf``, so it is routine. Non-finite floats are written as the strings
``"inf"``, ``"-inf"`` and ``"nan"`` and mapped back to floats on load.
"""

from __future__ import annotations

import math
from typing import Any

_TO_FLOAT = {"inf": math.inf, "-inf": -math.inf, "nan": math.nan}


def encode_nonfinite(obj: Any) -> Any:
    """Copy of ``obj`` with every non-finite float replaced by its string form."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return "nan"
        if math.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        return obj
    if isinstance(obj, dict):
        return {k: encode_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [encode_nonfinite(v) for v in obj]
    return obj


def decode_nonfinite(obj: Any) -> Any:
    """Inverse of :func:`encode_nonfinite`. Only the exact strings ``"inf"``, ``"-inf"``
    and ``"nan"`` are converted, so words like ``"info"`` or a ticker ``"INF"`` are safe."""
    if isinstance(obj, str):
        return _TO_FLOAT.get(obj, obj)
    if isinstance(obj, dict):
        return {k: decode_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decode_nonfinite(v) for v in obj]
    return obj
