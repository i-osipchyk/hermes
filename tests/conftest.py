import pytest


@pytest.fixture(autouse=True)
def _no_llmkit_sinks(monkeypatch):
    """Tests never write to the shared ~/.llmkit spend log or send to Langfuse."""
    monkeypatch.setenv("LLMKIT_SINKS", "none")
