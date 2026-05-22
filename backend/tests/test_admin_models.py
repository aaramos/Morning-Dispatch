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
    return runtime


async def fake_available_models(_settings):
    return [
        {"id": "env-default-model", "owned_by": "omlx", "created": None},
        {"id": "Gemma - MTP - 4Bit", "owned_by": "omlx", "created": None},
    ]


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


def test_admin_rejects_model_not_reported_by_omlx(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_api.model_catalog, "fetch_available_models", fake_available_models)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/admin/model/selection", json={"model_name": "not-installed"})

    assert response.status_code == 400
