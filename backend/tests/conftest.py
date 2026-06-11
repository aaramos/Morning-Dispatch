from __future__ import annotations

import pytest

from backend.app.services import mcp_status, model_catalog


@pytest.fixture(autouse=True)
def _reset_status_memos() -> None:
    """Keep the per-process status TTL memos from leaking between tests."""
    mcp_status.reset_status_cache()
    model_catalog.reset_catalog_cache()
