from __future__ import annotations

from backend.app.core.config import get_settings


def test_production_model_defaults(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.delenv("MORNING_DISPATCH_LIBRARIAN_MODEL", raising=False)
    monkeypatch.delenv("MORNING_DISPATCH_LIBRARIAN_MODEL_MAX_ITEMS", raising=False)
    monkeypatch.delenv("MORNING_DISPATCH_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))

    settings = get_settings()

    assert settings.librarian_model == "Gemma4-MTP-26B-BF16"
    assert settings.librarian_model_max_items == 150
    assert settings.youtube_max_results == 40
    assert settings.collections_max_results == 50
    assert settings.markets_max_core_companies == 10
    assert settings.markets_max_related_companies == 10
    assert settings.model_timeout_seconds == 90.0
    assert settings.scheduler_daily_run_time == "05:00"
    assert settings.scheduler_timezone == "America/Los_Angeles"


def test_web_search_reuses_shared_search_keys(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    shared_env = tmp_path / "hermes.env"
    shared_env.write_text(
        "\n".join(
            [
                "BRAVE_SEARCH_API_KEY=shared-brave-key",
                "TAVILY_API_KEY='shared-tavily-key'",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(shared_env))
    monkeypatch.delenv("MORNING_DISPATCH_BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("MORNING_DISPATCH_TAVILY_API_KEY", raising=False)

    settings = get_settings()

    assert settings.web_search_brave_api_key == "shared-brave-key"
    assert settings.web_search_tavily_api_key == "shared-tavily-key"


def test_web_search_saved_key_overrides_shared_search_key(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    secrets_dir = runtime / "secrets"
    shared_env = tmp_path / "hermes.env"
    shared_env.write_text("BRAVE_SEARCH_API_KEY=shared-brave-key\n", encoding="utf-8")
    (secrets_dir / "brave").mkdir(parents=True)
    (secrets_dir / "brave" / "api_key").write_text("saved-brave-key\n", encoding="utf-8")
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(shared_env))
    monkeypatch.delenv("MORNING_DISPATCH_BRAVE_API_KEY", raising=False)

    settings = get_settings()

    assert settings.web_search_brave_api_key == "saved-brave-key"
