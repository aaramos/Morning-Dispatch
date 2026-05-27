from __future__ import annotations

import asyncio
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.services import mcp_status


def settings(tmp_path: Path) -> Settings:
    return Settings(
        home_dir=tmp_path,
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        database_path=tmp_path / "data" / "db.sqlite3",
        gmail_client_secret_path=tmp_path / "secrets" / "gmail" / "client.json",
        gmail_credentials_path=tmp_path / "secrets" / "gmail" / "credentials.json",
        gmail_oauth_state_path=tmp_path / "secrets" / "gmail" / "state.json",
        model_settings_path=tmp_path / "data" / "model-settings.json",
        model_base_url="http://omlx.local/v1",
        model_api_key="test-key",
    )


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url: str, **kwargs):
        if url.endswith("/mcp/servers"):
            return FakeResponse(
                {
                    "servers": [
                        {"name": "fetch", "state": "connected", "transport": "stdio", "tools_count": 1},
                        {
                            "name": "gmail",
                            "state": "connected",
                            "transport": "stdio",
                            "tools_count": 4,
                            "error": "BRAVE_API_KEY=" + "BSA" + "secretthing1234567890",
                        },
                    ]
                }
            )
        return FakeResponse(
            {
                "tools": [
                    {"name": "fetch__fetch"},
                    {"name": "gmail__gmail_search"},
                    {"name": "gmail__gmail_get_message"},
                    {"name": "gmail__gmail_extract_links"},
                    {"name": "gmail__gmail_fetch_newsletters"},
                ]
            }
        )


class FailingAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *_args, **_kwargs):
        raise OSError("offline")


def test_mcp_status_reports_gmail_fetch_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_status.httpx, "AsyncClient", FakeAsyncClient)

    payload = asyncio.run(mcp_status.status(settings(tmp_path)))

    assert payload["available"] is True
    assert payload["server_count"] == 2
    assert payload["tool_count"] == 5
    assert payload["gmail"]["connected"] is True
    assert payload["gmail"]["tools_count"] == 4
    assert payload["gmail"]["fetch_tool_present"] is True
    assert "BSAsecretthing" not in str(payload["servers"])


def test_mcp_status_degrades_when_omlx_unreachable(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_status.httpx, "AsyncClient", FailingAsyncClient)

    payload = asyncio.run(mcp_status.status(settings(tmp_path)))

    assert payload["available"] is False
    assert payload["gmail"]["connected"] is False
    assert "Could not reach" in payload["error"]
