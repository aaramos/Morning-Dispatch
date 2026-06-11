from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.services import model_catalog


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
        brief_settings_path=tmp_path / "data" / "brief-settings.json",
        model_base_url="http://omlx.local/v1",
        model_api_key="test-key",
    )


def test_catalog_status_memoizes_within_ttl(monkeypatch, tmp_path):
    calls = []

    async def fake_fetch(_settings):
        calls.append(_settings)
        return [{"id": "model-a", "owned_by": "omlx", "created": None}]

    monkeypatch.setattr(model_catalog, "fetch_available_models", fake_fetch)

    first = asyncio.run(model_catalog.catalog_status(settings(tmp_path)))
    second = asyncio.run(model_catalog.catalog_status(settings(tmp_path)))

    assert len(calls) == 1
    assert first["available"] is True
    assert second == first

    model_catalog.reset_catalog_cache()
    asyncio.run(model_catalog.catalog_status(settings(tmp_path)))
    assert len(calls) == 2


def test_catalog_status_memoizes_errors(monkeypatch, tmp_path):
    calls = []

    async def failing_fetch(_settings):
        calls.append(_settings)
        raise model_catalog.ModelCatalogError("Could not reach the oMLX model catalog.")

    monkeypatch.setattr(model_catalog, "fetch_available_models", failing_fetch)

    first = asyncio.run(model_catalog.catalog_status(settings(tmp_path)))
    second = asyncio.run(model_catalog.catalog_status(settings(tmp_path)))

    assert len(calls) == 1
    assert first["available"] is False
    assert "Could not reach" in first["error"]
    assert second == first


def test_catalog_status_reflects_fresh_settings_despite_cache(monkeypatch, tmp_path):
    calls = []

    async def fake_fetch(_settings):
        calls.append(_settings)
        return [{"id": "model-a", "owned_by": "omlx", "created": None}]

    monkeypatch.setattr(model_catalog, "fetch_available_models", fake_fetch)

    base = settings(tmp_path)
    first = asyncio.run(model_catalog.catalog_status(base))
    updated = replace(base, librarian_model="model-a")
    second = asyncio.run(model_catalog.catalog_status(updated))

    assert len(calls) == 1
    assert first["selected_model"] != "model-a"
    assert second["selected_model"] == "model-a"
    assert second["models"] == first["models"]


def test_catalog_status_changed_base_url_misses_cache(monkeypatch, tmp_path):
    calls = []

    async def fake_fetch(_settings):
        calls.append(_settings)
        return [{"id": "model-a", "owned_by": "omlx", "created": None}]

    monkeypatch.setattr(model_catalog, "fetch_available_models", fake_fetch)

    base = settings(tmp_path)
    asyncio.run(model_catalog.catalog_status(base))
    asyncio.run(model_catalog.catalog_status(replace(base, model_base_url="http://other.local/v1")))

    assert len(calls) == 2
