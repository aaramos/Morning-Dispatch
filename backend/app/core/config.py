from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIBRARIAN_MODEL = "Gemma-4 MTP 6Bit"
DEFAULT_LIBRARIAN_MODEL_MAX_ITEMS = 120
DEFAULT_MODEL_TIMEOUT_SECONDS = 90.0
DEFAULT_SCHEDULER_DAILY_RUN_TIME = "05:00"
DEFAULT_SCHEDULER_TIMEZONE = "America/Los_Angeles"


@dataclass(frozen=True)
class Settings:
    home_dir: Path
    data_dir: Path
    secrets_dir: Path
    database_path: Path
    gmail_client_secret_path: Path
    gmail_credentials_path: Path
    gmail_oauth_state_path: Path
    model_settings_path: Path
    public_base_url: str | None = None
    environment: str = "development"
    model_base_url: str | None = None
    model_api_key: str | None = None
    librarian_model: str | None = DEFAULT_LIBRARIAN_MODEL
    librarian_use_model: bool = False
    librarian_model_max_items: int = DEFAULT_LIBRARIAN_MODEL_MAX_ITEMS
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    model_concurrency: int = 1
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 300
    scheduler_daily_run_time: str = DEFAULT_SCHEDULER_DAILY_RUN_TIME
    scheduler_timezone: str = DEFAULT_SCHEDULER_TIMEZONE


def _path_from_env(name: str, default: Path) -> Path:
    raw_value = os.environ.get(name)
    if not raw_value:
        return default
    return Path(raw_value).expanduser()


def _float_from_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def _int_from_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _model_enabled(raw_value: str | None, *, model: str | None, api_key: str | None) -> bool:
    value = (raw_value or "auto").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return bool(model and api_key)


def _bool_from_env(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _librarian_model_from_runtime(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("librarian_model")
    if not isinstance(model, str):
        return None
    model = model.strip()
    return model or None


def get_settings() -> Settings:
    home_dir = _path_from_env("MORNING_DISPATCH_HOME", Path.home() / ".morning-dispatch")
    data_dir = _path_from_env("MORNING_DISPATCH_DATA_DIR", home_dir / "data")
    secrets_dir = _path_from_env("MORNING_DISPATCH_SECRETS_DIR", home_dir / "secrets")
    database_path = _path_from_env(
        "MORNING_DISPATCH_DB_PATH",
        data_dir / "db" / "morning_dispatch.sqlite3",
    )
    gmail_client_secret_path = _path_from_env(
        "MORNING_DISPATCH_GMAIL_CLIENT_SECRET_PATH",
        secrets_dir / "gmail" / "gmail_client_secret.json",
    )
    gmail_credentials_path = _path_from_env(
        "MORNING_DISPATCH_GMAIL_CREDENTIALS_PATH",
        secrets_dir / "gmail" / "gmail_credentials.json",
    )
    gmail_oauth_state_path = _path_from_env(
        "MORNING_DISPATCH_GMAIL_OAUTH_STATE_PATH",
        secrets_dir / "gmail" / "oauth_state.json",
    )
    model_settings_path = _path_from_env(
        "MORNING_DISPATCH_MODEL_SETTINGS_PATH",
        data_dir / "model-settings.json",
    )
    model_api_key = (
        os.environ.get("MORNING_DISPATCH_MODEL_API_KEY")
        or os.environ.get("OMLX_API_KEY")
        or os.environ.get("LM_API_KEY")
    )
    librarian_model = (
        _librarian_model_from_runtime(model_settings_path)
        or os.environ.get("MORNING_DISPATCH_LIBRARIAN_MODEL", DEFAULT_LIBRARIAN_MODEL)
    )
    return Settings(
        home_dir=home_dir,
        data_dir=data_dir,
        secrets_dir=secrets_dir,
        database_path=database_path,
        gmail_client_secret_path=gmail_client_secret_path,
        gmail_credentials_path=gmail_credentials_path,
        gmail_oauth_state_path=gmail_oauth_state_path,
        model_settings_path=model_settings_path,
        public_base_url=os.environ.get("MORNING_DISPATCH_PUBLIC_BASE_URL"),
        environment=os.environ.get("MORNING_DISPATCH_ENV", "development"),
        model_base_url=os.environ.get("MORNING_DISPATCH_MODEL_BASE_URL", "http://127.0.0.1:1234/v1"),
        model_api_key=model_api_key,
        librarian_model=librarian_model,
        librarian_use_model=_model_enabled(
            os.environ.get("MORNING_DISPATCH_LIBRARIAN_USE_MODEL"),
            model=librarian_model,
            api_key=model_api_key,
        ),
        librarian_model_max_items=max(
            0,
            _int_from_env("MORNING_DISPATCH_LIBRARIAN_MODEL_MAX_ITEMS", DEFAULT_LIBRARIAN_MODEL_MAX_ITEMS),
        ),
        model_timeout_seconds=_float_from_env("MORNING_DISPATCH_MODEL_TIMEOUT_SECONDS", DEFAULT_MODEL_TIMEOUT_SECONDS),
        model_concurrency=max(1, _int_from_env("MORNING_DISPATCH_MODEL_CONCURRENCY", 1)),
        scheduler_enabled=_bool_from_env("MORNING_DISPATCH_SCHEDULER_ENABLED", False),
        scheduler_interval_seconds=max(30, _int_from_env("MORNING_DISPATCH_SCHEDULER_INTERVAL_SECONDS", 300)),
        scheduler_daily_run_time=os.environ.get(
            "MORNING_DISPATCH_SCHEDULER_DAILY_RUN_TIME",
            DEFAULT_SCHEDULER_DAILY_RUN_TIME,
        ),
        scheduler_timezone=os.environ.get("MORNING_DISPATCH_SCHEDULER_TIMEZONE", DEFAULT_SCHEDULER_TIMEZONE),
    )


def ensure_runtime_dirs(settings: Settings) -> None:
    for directory in (
        settings.data_dir,
        settings.data_dir / "db",
        settings.data_dir / "article-cache",
        settings.data_dir / "digest-output",
        settings.data_dir / "podcast-audio",
        settings.secrets_dir,
        settings.secrets_dir / "gmail",
        settings.secrets_dir / "reddit",
        settings.secrets_dir / "podcastindex",
        settings.secrets_dir / "tavily",
    ):
        directory.mkdir(parents=True, exist_ok=True)
