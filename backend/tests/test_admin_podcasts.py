from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.core.config import get_settings
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
    monkeypatch.delenv("MORNING_DISPATCH_PODCASTINDEX_API_KEY", raising=False)
    monkeypatch.delenv("MORNING_DISPATCH_PODCASTINDEX_API_SECRET", raising=False)
    return runtime


def test_admin_can_save_podcast_index_credentials(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        before = client.get("/api/admin/status")
        assert before.status_code == 200
        assert before.json()["podcasts"]["aggregator_configured"] is False

        saved = client.post(
            "/api/admin/podcasts/credentials",
            json={"api_key": " podcast-key ", "api_secret": " podcast-secret "},
        )
        assert saved.status_code == 200
        assert saved.json()["aggregator_configured"] is True

        after = client.get("/api/admin/status")
        assert after.status_code == 200
        assert after.json()["podcasts"]["aggregator_configured"] is True

    assert (runtime / "secrets" / "podcastindex" / "api_key").read_text(encoding="utf-8") == "podcast-key\n"
    assert (runtime / "secrets" / "podcastindex" / "api_secret").read_text(encoding="utf-8") == "podcast-secret\n"
    settings = get_settings()
    assert settings.podcastindex_api_key == "podcast-key"
    assert settings.podcastindex_api_secret == "podcast-secret"
