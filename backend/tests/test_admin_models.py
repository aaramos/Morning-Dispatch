from __future__ import annotations

import json

from fastapi.testclient import TestClient

import backend.app.api.admin as admin_api
from backend.app.main import create_app


def configure_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("MORNING_DISPATCH_LIBRARIAN_MODEL", "env-default-model")
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))
    return runtime


async def fake_available_models(_settings):
    return [
        {"id": "env-default-model", "owned_by": "omlx", "created": None},
        {"id": "Gemma - MTP - 4Bit", "owned_by": "omlx", "created": None},
    ]


def test_admin_can_save_local_model_api_key(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("MORNING_DISPATCH_MODEL_API_KEY", raising=False)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/admin/model/api-key", json={"api_key": "sk-local-xyz"})

    assert response.status_code == 200
    providers = {provider["id"]: provider for provider in response.json()["providers"]}
    assert providers["local"]["configured"] is True
    key_path = runtime / "secrets" / "model" / "api_key"
    assert key_path.read_text(encoding="utf-8") == "sk-local-xyz"


def test_admin_rejects_model_api_key_with_spaces(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/admin/model/api-key", json={"api_key": "bad key with spaces"})

    assert response.status_code == 400


def test_admin_brief_settings_defaults_round_trip(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        initial = client.get("/api/admin/brief-settings")
        updated = client.put(
            "/api/admin/brief-settings/defaults",
            json={
                "lookback_hours": 168,
                "content_limits": {
                    "total_items": 80,
                    "target_items": 18,
                    "lead_items": 4,
                    "quality_floor": "strong",
                    "per_source": {"web_search": 35, "youtube": 8},
                },
            },
    )

    assert initial.status_code == 200
    assert initial.json()["defaults"]["lookback_hours"] == 168
    assert initial.json()["defaults"]["content_limits"]["total_items"] == 600
    assert initial.json()["defaults"]["content_limits"]["target_items"] == 30
    assert initial.json()["defaults"]["content_limits"]["lead_items"] == 6
    assert initial.json()["defaults"]["content_limits"]["per_source"]["gmail"] == 24
    assert initial.json()["pipeline_limits"]["article_fetches"] == 1000
    assert any(group["group"] == "AI review caps" for group in initial.json()["system_limits"])
    assert updated.status_code == 200
    defaults = updated.json()["defaults"]
    assert defaults["lookback_hours"] == 168
    assert defaults["content_limits"]["total_items"] == 80
    assert defaults["content_limits"]["target_items"] == 18
    assert defaults["content_limits"]["lead_items"] == 4
    assert defaults["content_limits"]["quality_floor"] == "strong"
    assert defaults["content_limits"]["per_source"]["web_search"] == 35
    assert defaults["content_limits"]["per_source"]["youtube"] == 8
    payload = json.loads((runtime / "data" / "brief-settings.json").read_text(encoding="utf-8"))
    assert payload["brief_defaults"]["content_limits"]["total_items"] == 80

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        pipeline_updated = client.put(
            "/api/admin/brief-settings/pipeline-limits",
            json={
                "article_fetches": 120,
                "article_fetch_concurrency": 6,
                "model_refinement_items": 60,
                "source_audit_candidates": 12,
                "editorial_candidates": 80,
                "critic_articles": 25,
                "critic_newsletter_records": 8,
            },
        )

    assert pipeline_updated.status_code == 200
    limits = pipeline_updated.json()["pipeline_limits"]
    assert limits["article_fetches"] == 120
    assert limits["article_fetch_concurrency"] == 6
    assert limits["model_refinement_items"] == 60
    assert limits["source_audit_candidates"] == 12
    assert limits["editorial_candidates"] == 80
    assert limits["critic_articles"] == 25
    assert limits["critic_newsletter_records"] == 8
    payload = json.loads((runtime / "data" / "brief-settings.json").read_text(encoding="utf-8"))
    assert payload["pipeline_limits"]["source_audit_candidates"] == 12


def test_admin_status_includes_omlx_model_catalog(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_api.model_catalog, "fetch_available_models", fake_available_models)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/admin/status")

    assert response.status_code == 200
    catalog = response.json()["model"]["catalog"]
    assert catalog["available"] is True
    assert [model["id"] for model in catalog["models"]] == ["env-default-model", "Gemma - MTP - 4Bit"]
    assert response.json()["model"]["model"] == "env-default-model"
    assert list(catalog["providers"].keys()) == ["local"]
    assert [provider["id"] for provider in response.json()["model"]["routing"]["providers"]] == ["local"]


def test_admin_can_persist_selected_omlx_model(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_api.model_catalog, "fetch_available_models", fake_available_models)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/admin/model/selection", json={"model_name": "Gemma - MTP - 4Bit"})
        status = client.get("/api/admin/status")

    assert response.status_code == 200
    assert response.json()["model"] == "Gemma - MTP - 4Bit"
    assert status.json()["model"]["model"] == "Gemma - MTP - 4Bit"
    assert status.json()["model"]["selection_source"] == "admin"
    settings_payload = json.loads((runtime / "data" / "model-settings.json").read_text(encoding="utf-8"))
    assert settings_payload["librarian_model"] == "Gemma - MTP - 4Bit"


def test_admin_can_restore_model_defaults(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_api.model_catalog, "fetch_available_models", fake_available_models)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        client.post("/api/admin/model/selection", json={"model_name": "Gemma - MTP - 4Bit"})
        response = client.post("/api/admin/model/defaults/restore")

    assert response.status_code == 200
    payload = json.loads((runtime / "data" / "model-settings.json").read_text(encoding="utf-8"))
    assert payload["librarian_model"] == "Gemma4-MTP-26B-8Bit"
    assert "ollama_cloud_model" not in payload


def test_admin_rejects_model_not_reported_by_omlx(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_api.model_catalog, "fetch_available_models", fake_available_models)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/admin/model/selection", json={"model_name": "not-installed"})

    assert response.status_code == 400
