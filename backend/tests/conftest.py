from __future__ import annotations

import pytest

from backend.app.core.config import reset_settings_cache


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Clear the settings TTL cache around every test.

    get_settings() caches the built Settings for a few seconds; tests
    monkeypatch MORNING_DISPATCH_* env vars and expect the next call to
    observe them, so the cache must never leak across tests.
    """
    reset_settings_cache()
    yield
    reset_settings_cache()
