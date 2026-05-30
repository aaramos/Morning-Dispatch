from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIBRARIAN_MODEL = "Gemma4-MTP-26B-8Bit"
DEFAULT_OLLAMA_CLOUD_MODEL = "Gemma4-MTP-26B-BF16"
DEFAULT_LIBRARIAN_MODEL_MAX_ITEMS = 150
DEFAULT_MODEL_TIMEOUT_SECONDS = 90.0
DEFAULT_SCHEDULER_DAILY_RUN_TIME = "05:00"
DEFAULT_SCHEDULER_TIMEZONE = "America/Los_Angeles"
MODEL_ROUTE_AGENTS = ("refinement", "librarian", "source_audit", "editorial", "critic")
MODEL_ROUTE_PROVIDERS = ("local", "ollama_cloud")
DEFAULT_MODEL_ROUTES: dict[str, dict[str, object]] = {
    agent: {"provider": "local", "model": None, "allow_private_cloud": False}
    for agent in MODEL_ROUTE_AGENTS
}


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
    brief_settings_path: Path
    google_cloud_project_id: str | None = None
    gmail_remote_mcp_enabled: bool = False
    public_base_url: str | None = None
    environment: str = "development"
    podcastindex_api_key: str | None = None
    podcastindex_api_secret: str | None = None
    podcast_transcribe_command: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_username: str | None = None
    reddit_password: str | None = None
    reddit_user_agent: str | None = None
    model_base_url: str | None = None
    model_api_key: str | None = None
    ollama_api_key: str | None = None
    ollama_base_url: str = "https://ollama.com/v1"
    librarian_model: str | None = DEFAULT_LIBRARIAN_MODEL
    ollama_cloud_model: str | None = DEFAULT_OLLAMA_CLOUD_MODEL
    librarian_use_model: bool = False
    librarian_model_max_items: int = DEFAULT_LIBRARIAN_MODEL_MAX_ITEMS
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    model_concurrency: int = 1
    # Seconds to reuse a previously fetched article body (keyed by canonical URL).
    # 0 disables the cache. Re-extraction always runs on the cached HTML, so
    # extraction/date improvements are never masked. Kept short so it cannot
    # resurrect content outside a multi-day recency window.
    article_fetch_cache_ttl_seconds: int = 0
    model_routes: dict[str, dict[str, object]] = field(default_factory=lambda: dict(DEFAULT_MODEL_ROUTES))
    web_search_provider: str = "auto"
    web_search_tavily_api_key: str | None = None
    web_search_brave_api_key: str | None = None
    web_search_serpapi_api_key: str | None = None
    youtube_api_key: str | None = None
    youtube_max_results: int = 40
    youtube_duration_filter: str = "medium"
    collections_root: Path | None = None
    collections_max_results: int = 50
    collections_max_file_bytes: int = 1_000_000
    markets_mode: str = "simple"
    markets_max_core_companies: int = 10
    markets_max_related_companies: int = 10
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 300
    scheduler_daily_run_time: str = DEFAULT_SCHEDULER_DAILY_RUN_TIME
    scheduler_timezone: str = DEFAULT_SCHEDULER_TIMEZONE


def _path_from_env(name: str, default: Path) -> Path:
    raw_value = os.environ.get(name)
    if not raw_value:
        return default
    return Path(raw_value).expanduser()


def _secret_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _env_file_value(path: Path | None, names: tuple[str, ...]) -> str | None:
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    wanted = {name.upper() for name in names}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        if key.strip().upper() not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value or None
    return None


def _shared_search_env_path() -> Path | None:
    raw_value = os.environ.get("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH")
    if raw_value is not None and not raw_value.strip():
        return None
    if raw_value:
        return Path(raw_value).expanduser()
    return Path.home() / ".hermes" / ".env"


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
    # Auto: enable when a model name is configured. Local servers (LM Studio,
    # Ollama) do not require an API key, so we don't gate on api_key here.
    return bool(model)


def _bool_from_env(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _model_settings_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _runtime_model_value(path: Path, key: str) -> str | None:
    payload = _model_settings_payload(path)
    if not payload:
        return None
    model = payload.get(key)
    if not isinstance(model, str):
        return None
    model = model.strip()
    return model or None


def _librarian_model_from_runtime(path: Path) -> str | None:
    return _runtime_model_value(path, "librarian_model")


def _ollama_cloud_model_from_runtime(path: Path) -> str | None:
    return _runtime_model_value(path, "ollama_cloud_model")


def _model_routes_from_runtime(path: Path) -> dict[str, dict[str, object]]:
    payload = _model_settings_payload(path)
    raw_routes = payload.get("model_routes") if isinstance(payload, dict) else None
    if not isinstance(raw_routes, dict):
        return {agent: dict(route) for agent, route in DEFAULT_MODEL_ROUTES.items()}
    routes: dict[str, dict[str, object]] = {}
    for agent in MODEL_ROUTE_AGENTS:
        raw_route = raw_routes.get(agent)
        route = raw_route if isinstance(raw_route, dict) else {}
        provider = str(route.get("provider") or "local").strip().lower()
        if provider not in MODEL_ROUTE_PROVIDERS:
            provider = "local"
        raw_model = route.get("model")
        model = raw_model.strip() if isinstance(raw_model, str) else None
        routes[agent] = {
            "provider": provider,
            "model": model or None,
            "allow_private_cloud": bool(route.get("allow_private_cloud")),
        }
    return routes


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
    brief_settings_path = _path_from_env(
        "MORNING_DISPATCH_BRIEF_SETTINGS_PATH",
        data_dir / "brief-settings.json",
    )
    model_api_key = (
        os.environ.get("MORNING_DISPATCH_MODEL_API_KEY")
        or os.environ.get("OMLX_API_KEY")
        or os.environ.get("LM_API_KEY")
    )
    ollama_api_key = os.environ.get("MORNING_DISPATCH_OLLAMA_API_KEY") or os.environ.get("OLLAMA_API_KEY") or _secret_text(
        secrets_dir / "ollama" / "api_key"
    )
    librarian_model = (
        _librarian_model_from_runtime(model_settings_path)
        or os.environ.get("MORNING_DISPATCH_LIBRARIAN_MODEL", DEFAULT_LIBRARIAN_MODEL)
    )
    ollama_cloud_model = (
        _ollama_cloud_model_from_runtime(model_settings_path)
        or os.environ.get("MORNING_DISPATCH_OLLAMA_MODEL", DEFAULT_OLLAMA_CLOUD_MODEL)
    )
    podcastindex_api_key = os.environ.get("MORNING_DISPATCH_PODCASTINDEX_API_KEY") or _secret_text(
        secrets_dir / "podcastindex" / "api_key"
    )
    podcastindex_api_secret = os.environ.get("MORNING_DISPATCH_PODCASTINDEX_API_SECRET") or _secret_text(
        secrets_dir / "podcastindex" / "api_secret"
    )
    reddit_client_id = os.environ.get("MORNING_DISPATCH_REDDIT_CLIENT_ID") or _secret_text(
        secrets_dir / "reddit" / "client_id"
    )
    reddit_client_secret = os.environ.get("MORNING_DISPATCH_REDDIT_CLIENT_SECRET") or _secret_text(
        secrets_dir / "reddit" / "client_secret"
    )
    reddit_username = os.environ.get("MORNING_DISPATCH_REDDIT_USERNAME") or _secret_text(
        secrets_dir / "reddit" / "username"
    )
    reddit_password = os.environ.get("MORNING_DISPATCH_REDDIT_PASSWORD") or _secret_text(
        secrets_dir / "reddit" / "password"
    )
    reddit_user_agent = os.environ.get("MORNING_DISPATCH_REDDIT_USER_AGENT") or _secret_text(
        secrets_dir / "reddit" / "user_agent"
    )
    web_search_provider = (os.environ.get("MORNING_DISPATCH_WEB_SEARCH_PROVIDER") or "auto").strip().lower()
    shared_search_env_path = _shared_search_env_path()
    web_search_tavily_api_key = (
        os.environ.get("MORNING_DISPATCH_TAVILY_API_KEY")
        or _secret_text(secrets_dir / "tavily" / "api_key")
        or _env_file_value(
            shared_search_env_path,
            (
                "TAVILY_API_KEY",
                "TAVILY_SEARCH_API_KEY",
                "TAVILY_MCP_API_KEY",
            ),
        )
    )
    web_search_brave_api_key = (
        os.environ.get("MORNING_DISPATCH_BRAVE_API_KEY")
        or _secret_text(secrets_dir / "brave" / "api_key")
        or _env_file_value(
            shared_search_env_path,
            (
                "BRAVE_SEARCH_API_KEY",
                "BRAVE_API_KEY",
            ),
        )
    )
    web_search_serpapi_api_key = (
        os.environ.get("MORNING_DISPATCH_SERPAPI_API_KEY")
        or _secret_text(secrets_dir / "serpapi" / "api_key")
        or _env_file_value(
            shared_search_env_path,
            (
                "SERPAPI_API_KEY",
                "SERP_API_KEY",
            ),
        )
    )
    youtube_api_key = os.environ.get("MORNING_DISPATCH_YOUTUBE_API_KEY") or _secret_text(
        secrets_dir / "youtube" / "api_key"
    )
    collections_root = _path_from_env(
        "MORNING_DISPATCH_COLLECTIONS_ROOT",
        Path.home() / "Documents" / "Collections",
    )
    return Settings(
        home_dir=home_dir,
        data_dir=data_dir,
        secrets_dir=secrets_dir,
        database_path=database_path,
        gmail_client_secret_path=gmail_client_secret_path,
        gmail_credentials_path=gmail_credentials_path,
        gmail_oauth_state_path=gmail_oauth_state_path,
        google_cloud_project_id=os.environ.get("MORNING_DISPATCH_GOOGLE_CLOUD_PROJECT_ID"),
        gmail_remote_mcp_enabled=_bool_from_env("MORNING_DISPATCH_GMAIL_REMOTE_MCP_ENABLED", False),
        model_settings_path=model_settings_path,
        brief_settings_path=brief_settings_path,
        public_base_url=os.environ.get("MORNING_DISPATCH_PUBLIC_BASE_URL"),
        environment=os.environ.get("MORNING_DISPATCH_ENV", "development"),
        podcastindex_api_key=podcastindex_api_key,
        podcastindex_api_secret=podcastindex_api_secret,
        podcast_transcribe_command=os.environ.get("MORNING_DISPATCH_PODCAST_TRANSCRIBE_COMMAND"),
        reddit_client_id=reddit_client_id,
        reddit_client_secret=reddit_client_secret,
        reddit_username=reddit_username,
        reddit_password=reddit_password,
        reddit_user_agent=reddit_user_agent,
        model_base_url=os.environ.get("MORNING_DISPATCH_MODEL_BASE_URL", "http://127.0.0.1:1234/v1"),
        model_api_key=model_api_key,
        ollama_api_key=ollama_api_key,
        ollama_base_url=os.environ.get("MORNING_DISPATCH_OLLAMA_BASE_URL", "https://ollama.com/v1").rstrip("/"),
        librarian_model=librarian_model,
        ollama_cloud_model=ollama_cloud_model,
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
        article_fetch_cache_ttl_seconds=max(0, _int_from_env("MORNING_DISPATCH_ARTICLE_FETCH_CACHE_TTL_SECONDS", 0)),
        model_routes=_model_routes_from_runtime(model_settings_path),
        web_search_provider=web_search_provider,
        web_search_tavily_api_key=web_search_tavily_api_key,
        web_search_brave_api_key=web_search_brave_api_key,
        web_search_serpapi_api_key=web_search_serpapi_api_key,
        youtube_api_key=youtube_api_key,
        youtube_max_results=max(1, min(_int_from_env("MORNING_DISPATCH_YOUTUBE_MAX_RESULTS", 40), 50)),
        youtube_duration_filter=os.environ.get("MORNING_DISPATCH_YOUTUBE_DURATION_FILTER", "medium"),
        collections_root=collections_root,
        collections_max_results=max(1, min(_int_from_env("MORNING_DISPATCH_COLLECTIONS_MAX_RESULTS", 50), 50)),
        collections_max_file_bytes=max(1_000, _int_from_env("MORNING_DISPATCH_COLLECTIONS_MAX_FILE_BYTES", 1_000_000)),
        markets_mode=os.environ.get("MORNING_DISPATCH_MARKETS_MODE", "simple").strip().lower() or "simple",
        markets_max_core_companies=max(1, min(_int_from_env("MORNING_DISPATCH_MARKETS_MAX_CORE_COMPANIES", 10), 10)),
        markets_max_related_companies=max(0, min(_int_from_env("MORNING_DISPATCH_MARKETS_MAX_RELATED_COMPANIES", 10), 10)),
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
        settings.data_dir / "foreign-article-cache",
        settings.data_dir / "digest-output",
        settings.data_dir / "podcast-audio",
        settings.data_dir / "podcast-transcripts",
        settings.secrets_dir,
        settings.secrets_dir / "gmail",
        settings.secrets_dir / "reddit",
        settings.secrets_dir / "podcastindex",
        settings.secrets_dir / "tavily",
        settings.secrets_dir / "brave",
        settings.secrets_dir / "serpapi",
        settings.secrets_dir / "youtube",
        settings.secrets_dir / "ollama",
    ):
        directory.mkdir(parents=True, exist_ok=True)
        if settings.secrets_dir in (directory, *directory.parents):
            try:
                directory.chmod(0o700)
            except OSError:
                pass
