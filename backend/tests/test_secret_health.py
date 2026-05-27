from __future__ import annotations

from pathlib import Path

from backend.app.core.config import Settings, ensure_runtime_dirs
from backend.app.core.secret_redaction import redact_secret_text
from backend.app.services import secret_health


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
        web_search_brave_api_key="brave-configured",
        youtube_api_key="youtube-configured",
        model_api_key="model-configured",
    )


def test_secret_health_reports_paths_without_values(tmp_path):
    app_settings = settings(tmp_path)
    ensure_runtime_dirs(app_settings)
    brave_path = app_settings.secrets_dir / "brave" / "api_key"
    youtube_path = app_settings.secrets_dir / "youtube" / "api_key"
    brave_path.write_text("BSA" + "verysecretvalue1234567890\n", encoding="utf-8")
    youtube_path.write_text("AIza" + "VerySecretValue1234567890\n", encoding="utf-8")
    brave_path.chmod(0o600)
    youtube_path.chmod(0o600)

    payload = secret_health.status(app_settings)

    assert payload["secrets_dir"] == str(app_settings.secrets_dir)
    assert payload["directory_permissions"]["status"] == "ok"
    rendered = str(payload)
    assert "BSAverysecretvalue" not in rendered
    assert "AIzaVerySecretValue" not in rendered
    assert {item["id"]: item["status"] for item in payload["items"]}["brave_key"] == "ok"
    assert {item["id"]: item["status"] for item in payload["items"]}["youtube_key"] == "ok"


def test_secret_health_flags_overly_open_secret_file(tmp_path):
    app_settings = settings(tmp_path)
    ensure_runtime_dirs(app_settings)
    path = app_settings.secrets_dir / "youtube" / "api_key"
    path.write_text("secret\n", encoding="utf-8")
    path.chmod(0o644)

    payload = secret_health.status(app_settings)

    youtube = next(item for item in payload["items"] if item["id"] == "youtube_key")
    assert youtube["status"] == "warning"
    assert youtube["permissions"]["mode"] == "0o644"


def test_secret_redaction_removes_common_key_shapes():
    tavily_like = "tv" + "ly-" + "dev-secretthing1234567890"
    brave_like = "BS" + "A" + "secretthing1234567890"
    raw = (
        "Authorization: Bearer "
        + tavily_like
        + " and BRAVE_API_KEY="
        + brave_like
    )

    redacted = redact_secret_text(raw)

    assert tavily_like[:20] not in redacted
    assert brave_like[:16] not in redacted
    assert "[redacted" in redacted
