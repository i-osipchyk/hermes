import pytest


@pytest.fixture(autouse=True)
def _no_llmkit_sinks(monkeypatch):
    """Tests never write to the shared ~/.llmkit spend log or send to Langfuse."""
    monkeypatch.setenv("LLMKIT_SINKS", "none")


@pytest.fixture(autouse=True)
def _no_ctrader_ssm(monkeypatch):
    """Tests never read the shared cTrader token pair from AWS SSM."""
    monkeypatch.setenv("HERMES_CTRADER_SHARED_PATH", "")


@pytest.fixture(autouse=True)
def _isolated_holdouts(monkeypatch, tmp_path):
    """Tests never read or write the committed research/holdouts.json."""
    monkeypatch.setenv("HERMES_HOLDOUTS_PATH", str(tmp_path / "holdouts.json"))


@pytest.fixture(autouse=True)
def _isolated_ideas_dir(monkeypatch, tmp_path):
    """Tests never write reads logs into the committed research/ideas/."""
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(tmp_path / "ideas_default"))


@pytest.fixture(autouse=True)
def _no_data_probe_network(monkeypatch):
    """The driver's data check may fetch a week of bars to confirm an instrument; no test
    ever does. The stand-in answers "found" so the ordinary path is exercised."""
    monkeypatch.setattr("hermes.research.data_check.real_probe", lambda source, ticker, tf: 1)
