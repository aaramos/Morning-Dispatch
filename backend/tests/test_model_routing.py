from __future__ import annotations

import json
import pytest

import httpx
from httpx._content import IteratorByteStream

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.model import client as model_client_module
from backend.app.core.config import get_settings
from backend.app.services import model_routing


def configure_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_DB_PATH", str(runtime / "data" / "db" / "morning_dispatch.sqlite3"))
    monkeypatch.setenv("MORNING_DISPATCH_MODEL_API_KEY", "local-key")
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_MODEL", "local-default")
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))
    return runtime


def test_model_routes_default_to_local(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    settings = get_settings()

    status = model_routing.routes_status(settings)

    assert status["routes"]["editorial"]["provider"] == "local"
    assert status["routes"]["editorial"]["effective_model"] == "local-default"
    assert status["defaults"]["ollama_cloud"] == "Gemma4-MTP-26B-BF16"


def test_model_routes_can_be_saved(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)
    settings = get_settings()

    model_routing.save_routes(
        settings,
        {"editorial": {"provider": "ollama_cloud", "model": "gpt-oss:120b", "allow_private_cloud": False}},
    )

    payload = json.loads((runtime / "data" / "model-settings.json").read_text(encoding="utf-8"))
    assert payload["model_routes"]["editorial"]["provider"] == "ollama_cloud"
    assert payload["model_routes"]["editorial"]["model"] == "gpt-oss:120b"


def test_cloud_route_uses_openai_api_for_public_sources(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_OLLAMA_API_KEY", "ollama-key")
    monkeypatch.setenv("MORNING_DISPATCH_OLLAMA_MODEL", "cloud-default")
    settings = get_settings()
    monkeypatch.setenv("MORNING_DISPATCH_OLLAMA_BASE_URL", "https://api.ollama.com")
    model_routing.save_routes(
        settings,
        {"editorial": {"provider": "ollama_cloud", "model": "gpt-oss:120b", "allow_private_cloud": False}},
    )
    settings = get_settings()
    public_item = NormalizedPayload(source_type="youtube_video", raw_text="public video")

    resolution = model_routing.client_for_agent("editorial", settings=settings, items=[public_item])

    assert resolution.client is not None
    assert resolution.client.config.provider == "ollama_cloud"
    assert resolution.client.config.api_mode == "openai"
    assert resolution.client.config.base_url == "https://ollama.com/v1"
    assert resolution.client.config.model == "gpt-oss:120b"


def test_cloud_route_uses_cloud_default_when_route_has_no_model(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_OLLAMA_API_KEY", "ollama-key")
    monkeypatch.setenv("MORNING_DISPATCH_OLLAMA_MODEL", "cloud-default")
    settings = get_settings()
    model_routing.save_routes(
        settings,
        {"editorial": {"provider": "ollama_cloud", "model": None, "allow_private_cloud": False}},
    )
    settings = get_settings()

    resolution = model_routing.client_for_agent("editorial", settings=settings, items=[])

    assert resolution.client is not None
    assert resolution.client.config.model == "cloud-default"


def test_cloud_route_rejects_invalid_key(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    settings = get_settings()
    with pytest.raises(ValueError, match="doesn't look like an Ollama Cloud token"):
        model_routing.save_ollama_api_key(settings, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...")


def test_cloud_url_normalization():
    assert model_routing._normalize_ollama_base_url("https://ollama.com/api") == "https://ollama.com/v1"
    assert model_routing._normalize_ollama_base_url("https://api.ollama.com") == "https://ollama.com/v1"
    assert model_routing._normalize_ollama_base_url("https://api.ollama.com/v1/models") == "https://ollama.com/v1"


def test_private_source_forces_cloud_route_to_local(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MORNING_DISPATCH_OLLAMA_API_KEY", "ollama-key")
    settings = get_settings()
    model_routing.save_routes(
        settings,
        {"critic": {"provider": "ollama_cloud", "model": "gpt-oss:120b", "allow_private_cloud": False}},
    )
    settings = get_settings()
    private_item = NormalizedPayload(source_type="gmail_link", raw_text="private newsletter")

    resolution = model_routing.client_for_agent("critic", settings=settings, items=[private_item])

    assert resolution.privacy_forced_local is True
    assert resolution.client is not None
    assert resolution.client.config.provider == "local"
    assert resolution.client.config.model == "local-default"


def test_streaming_http_error_detail_does_not_mask_model_fallback():
    response = httpx.Response(
        401,
        request=httpx.Request("POST", "https://api.ollama.com/v1/chat/completions"),
        stream=IteratorByteStream(iter([b'{"error":"invalid api key"}'])),
    )

    detail = model_client_module._response_detail(response)

    assert detail == "Unauthorized"
