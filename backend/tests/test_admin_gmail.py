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
    monkeypatch.setenv("MORNING_DISPATCH_PUBLIC_BASE_URL", "https://ultras-mac-studio-2.tail4aeef0.ts.net:8000")
    return runtime


def client_secret_json() -> str:
    return json.dumps(
        {
            "web": {
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "client-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [
                    "https://ultras-mac-studio-2.tail4aeef0.ts.net:8000/api/admin/gmail/oauth/callback"
                ],
            }
        }
    )


def test_gmail_admin_status_and_client_secret_upload(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        status = client.get("/api/admin/gmail/status")
        assert status.status_code == 200
        assert status.json()["configured"] is False
        assert status.json()["connected"] is False

        saved = client.post(
            "/api/admin/gmail/client-secret",
            json={"client_secret_json": client_secret_json()},
        )
        assert saved.status_code == 200
        assert (runtime / "secrets" / "gmail" / "gmail_client_secret.json").exists()

        updated_status = client.get("/api/admin/gmail/status")
        assert updated_status.status_code == 200
        assert updated_status.json()["configured"] is True
        assert updated_status.json()["oauth_redirect_ready"] is True
        assert updated_status.json()["redirect_uri"].startswith("https://ultras-mac-studio-2.tail4aeef0.ts.net")


def test_gmail_admin_rejects_invalid_client_secret(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/api/admin/gmail/client-secret",
            json={"client_secret_json": '{"not": "a google oauth client"}'},
        )

    assert response.status_code == 400


def test_gmail_admin_oauth_start_returns_authorization_url(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        client.post("/api/admin/gmail/client-secret", json={"client_secret_json": client_secret_json()})
        response = client.post("/api/admin/gmail/oauth/start")

    assert response.status_code == 200
    assert response.json()["authorization_url"].startswith("https://accounts.google.com/o/oauth2")


def test_gmail_admin_blocks_raw_tailscale_ip_oauth_redirect(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_PUBLIC_BASE_URL", "http://100.113.204.75:8001")

    with TestClient(create_app(), client=("100.113.204.75", 50000)) as client:
        client.post("/api/admin/gmail/client-secret", json={"client_secret_json": client_secret_json()})
        status = client.get("/api/admin/gmail/status")
        response = client.post("/api/admin/gmail/oauth/start")

    assert status.json()["oauth_redirect_ready"] is False
    assert response.status_code == 400


def test_gmail_admin_can_complete_from_pasted_redirect_url(monkeypatch, tmp_path):
    runtime = configure_runtime(monkeypatch, tmp_path)

    class FakeCredentials:
        def to_json(self):
            return json.dumps({"token": "access-token", "refresh_token": "refresh-token"})

    class FakeFlow:
        credentials = FakeCredentials()
        code_verifier = "saved-code-verifier"

        def authorization_url(self, **_kwargs):
            return ("https://accounts.google.com/o/oauth2/auth?state=known-state", "known-state")

        def fetch_token(self, authorization_response):
            assert "code=returned-code" in authorization_response

    monkeypatch.setattr(admin_api, "_oauth_flow", lambda *_args, **_kwargs: FakeFlow())

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        client.post("/api/admin/gmail/client-secret", json={"client_secret_json": client_secret_json()})
        start = client.post("/api/admin/gmail/oauth/start")
        complete = client.post(
            "/api/admin/gmail/oauth/complete",
            json={
                "callback_url": (
                    "https://ultras-mac-studio-2.tail4aeef0.ts.net:8000"
                    "/api/admin/gmail/oauth/callback?state=known-state&code=returned-code"
                )
            },
        )

    assert start.status_code == 200
    assert complete.status_code == 200
    assert (runtime / "secrets" / "gmail" / "gmail_credentials.json").exists()
    assert not (runtime / "secrets" / "gmail" / "oauth_state.json").exists()
