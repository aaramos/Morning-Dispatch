from __future__ import annotations

import json

import httpx
from httpx._content import IteratorByteStream

from backend.agents.model import client as model_client_module
from backend.app.core.config import get_settings, reset_settings_cache
from backend.app.services import model_routing


def configure_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(runtime / "data" / "db" / "morning_dispatch.sqlite3"))
    monkeypatch.setenv("MORNING_DISPATCH_MODEL_API_KEY", "local-key")
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_MODEL", "local-default")
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_USE_MODEL", "true")
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))
    return runtime


def test_routes_are_local_only(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    settings = get_settings()

    status = model_routing.routes_status(settings)

    assert [provider["id"] for provider in status["providers"]] == ["local"]
    assert status["routes"]["editorial"]["provider"] == "local"
    assert status["routes"]["editorial"]["effective_model"] == "local-default"
    assert status["defaults"] == {"local": "local-default"}


def test_routes_can_set_per_agent_local_model(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)
    settings = get_settings()

    model_routing.save_routes(
        settings,
        {"editorial": {"provider": "ollama_cloud", "model": "Gemma4-Fast"}},
    )

    payload = json.loads((runtime / "data" / "model-settings.json").read_text(encoding="utf-8"))
    # Cloud was removed: any saved route is forced to local, but the model choice is kept.
    assert payload["model_routes"]["editorial"]["provider"] == "local"
    assert payload["model_routes"]["editorial"]["model"] == "Gemma4-Fast"

    # Drop the TTL-cached Settings so the saved routes are visible immediately.
    reset_settings_cache()
    resolution = model_routing.client_for_agent("editorial", settings=get_settings(), items=[])
    assert resolution.client is not None
    assert resolution.client.config.provider == "local"
    assert resolution.client.config.model == "Gemma4-Fast"


def test_client_for_agent_uses_local_model(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    settings = get_settings()

    resolution = model_routing.client_for_agent("critic", settings=settings, items=[])

    assert resolution.privacy_forced_local is False
    assert resolution.client is not None
    assert resolution.client.config.provider == "local"
    assert resolution.client.config.api_mode == "openai"
    assert resolution.client.config.model == "local-default"


def test_save_model_api_key_writes_secret(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("MORNING_DISPATCH_MODEL_API_KEY", raising=False)
    settings = get_settings()

    result = model_routing.save_model_api_key(settings, "sk-local-123")

    assert result["configured"] is True
    key_path = runtime / "secrets" / "model" / "api_key"
    assert key_path.read_text(encoding="utf-8") == "sk-local-123"
    # A freshly loaded Settings should pick the key up from disk.
    reset_settings_cache()
    assert get_settings().model_api_key == "sk-local-123"


def test_streaming_http_error_detail_does_not_mask_model_fallback():
    response = httpx.Response(
        401,
        request=httpx.Request("POST", "http://127.0.0.1:1234/v1/chat/completions"),
        stream=IteratorByteStream(iter([b'{"error":"invalid api key"}'])),
    )

    detail = model_client_module._response_detail(response)

    assert detail == "Unauthorized"
