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

    settings = get_settings()

    assert settings.librarian_model == "Gemma-4 MTP 6Bit"
    assert settings.librarian_model_max_items == 120
    assert settings.model_timeout_seconds == 90.0
    assert settings.scheduler_daily_run_time == "05:00"
    assert settings.scheduler_timezone == "America/Los_Angeles"
