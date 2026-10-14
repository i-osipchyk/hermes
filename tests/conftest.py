import pytest


@pytest.fixture(autouse=True)
def _no_llmkit_sinks(monkeypatch):
    """Tests never write to the shared ~/.llmkit spend log or send to Langfuse."""
    monkeypatch.setenv("LLMKIT_SINKS", "none")


@pytest.fixture(autouse=True)
def _no_ctrader_ssm(monkeypatch):
    """Tests never read the shared cTrader token pair from AWS SSM."""
    monkeypatch.setenv("HERMES_CTRADER_SHARED_PATH", "")
