"""No test may reach a real phone or dead-man's-switch service, even when the developer's .env configures one."""
import pytest


@pytest.fixture(autouse=True)
def _no_external_alert_channels(monkeypatch):
    for name in ("NTFY_URL", "NTFY_TOKEN", "HEALTHCHECK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("trader.config.load_dotenv", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("trader.supervisor.load_dotenv", lambda *a, **k: None, raising=False)
