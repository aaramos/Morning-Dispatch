from __future__ import annotations

import pytest

from backend.app.core.config import reset_settings_cache
from backend.app.services import mcp_status, model_catalog


@pytest.fixture(autouse=True)
def _reset_process_caches() -> None:
    """Clear process-level TTL caches around every test.

    get_settings() caches the built Settings for a few seconds; tests
    monkeypatch MORNING_DISPATCH_* env vars and expect the next call to
    observe them. Status probes use similar short-lived memos, so keep all of
    them from leaking between tests.
    """
    reset_settings_cache()
    mcp_status.reset_status_cache()
    model_catalog.reset_catalog_cache()
    yield
    reset_settings_cache()
    mcp_status.reset_status_cache()
    model_catalog.reset_catalog_cache()
