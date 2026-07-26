"""Trust the OS certificate store for Hermes's HTTPS fetches.

Corporate machines often route TLS through an intercepting proxy whose self-signed
root lives in the OS keychain — which the stdlib ``ssl`` module ignores by default,
so `urllib`/`requests` fail with CERTIFICATE_VERIFY_FAILED. ``truststore`` (the same
mechanism pip uses) makes ``ssl`` consult the OS store, fixing both the urllib path
(Binance) and the requests path (yfinance).

This is **additive** — it broadens trust to whatever the OS is already configured to
trust; it never disables verification. Opt out with ``HERMES_NO_TRUSTSTORE=1``.
"""

from __future__ import annotations

import os

_injected = False


def ensure_system_trust() -> None:
    """Make the stdlib SSL use the OS trust store (idempotent, best-effort)."""
    global _injected
    if _injected or os.environ.get("HERMES_NO_TRUSTSTORE"):
        return
    try:
        import truststore

        truststore.inject_into_ssl()
        _injected = True
    except Exception:
        # truststore missing/unsupported — fall back to the default context, which
        # still honours SSL_CERT_FILE / REQUESTS_CA_BUNDLE if the user sets them.
        pass
