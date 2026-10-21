import os

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_integration_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make` exports `.env`, so a developer's local `RISK_*` values would reach the tests. A test that builds the
    integration settings sees only what it sets itself."""
    for name in [n for n in os.environ if n.startswith("RISK_")]:
        monkeypatch.delenv(name)
