from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.app.core.config import Settings
from backend.app.core.secret_redaction import looks_like_secret

_MCP_CONFIG_PATH = Path.home() / ".lmstudio" / "mcp.json"


def status(settings: Settings) -> dict[str, Any]:
    items = [
        _secret_item(
            "gmail_client",
            "Gmail OAuth client",
            settings.gmail_client_secret_path,
            configured=settings.gmail_client_secret_path.exists(),
        ),
        _secret_item(
            "gmail_token",
            "Gmail token",
            settings.gmail_credentials_path,
            configured=settings.gmail_credentials_path.exists(),
        ),
        _secret_item(
            "podcastindex_key",
            "Podcast Index key",
            settings.secrets_dir / "podcastindex" / "api_key",
            configured=bool(settings.podcastindex_api_key),
        ),
        _secret_item(
            "podcastindex_secret",
            "Podcast Index secret",
            settings.secrets_dir / "podcastindex" / "api_secret",
            configured=bool(settings.podcastindex_api_secret),
        ),
        _secret_item(
            "youtube_key",
            "YouTube API key",
            settings.secrets_dir / "youtube" / "api_key",
            configured=bool(settings.youtube_api_key),
        ),
        _secret_item(
            "fred_key",
            "FRED API key",
            settings.secrets_dir / "fred" / "api_key",
            configured=bool(settings.fred_api_key),
        ),
        _secret_item(
            "brave_key",
            "Brave Search key",
            settings.secrets_dir / "brave" / "api_key",
            configured=bool(settings.web_search_brave_api_key),
        ),
        _secret_item(
            "tavily_key",
            "Tavily key",
            settings.secrets_dir / "tavily" / "api_key",
            configured=bool(settings.web_search_tavily_api_key),
        ),
        _secret_item(
            "serpapi_key",
            "SerpAPI key",
            settings.secrets_dir / "serpapi" / "api_key",
            configured=bool(settings.web_search_serpapi_api_key),
        ),
        _secret_item(
            "serper_key",
            "Serper key",
            settings.secrets_dir / "serper" / "api_key",
            configured=bool(settings.web_search_serper_api_key),
        ),
        _secret_item(
            "model_key",
            "Local model API key",
            settings.secrets_dir / "model" / "api_key",
            configured=bool(settings.model_api_key),
        ),
    ]
    directory_permissions = _permission_summary(settings.secrets_dir)
    external_plaintext = _mcp_plaintext_findings(_MCP_CONFIG_PATH)
    warning_count = sum(1 for item in items if item["status"] == "warning")
    warning_count += 1 if directory_permissions["status"] == "warning" else 0
    warning_count += len(external_plaintext)
    missing_count = sum(1 for item in items if item["status"] == "missing")
    return {
        "secrets_dir": str(settings.secrets_dir),
        "directory_permissions": directory_permissions,
        "items": items,
        "external_plaintext": external_plaintext,
        "summary": {
            "configured_count": sum(1 for item in items if item["configured"]),
            "missing_count": missing_count,
            "warning_count": warning_count,
        },
    }


def _secret_item(identifier: str, label: str, path: Path, *, configured: bool) -> dict[str, Any]:
    exists = path.exists()
    permissions = _permission_summary(path) if exists else {"status": "missing", "mode": None}
    if exists:
        storage = "secret file"
        message = "Stored in the app secrets folder."
    elif configured:
        storage = "environment or shared config"
        message = "Configured outside the app secrets folder."
    else:
        storage = "not configured"
        message = "Missing."
    status_value = permissions["status"] if exists else ("warning" if configured else "missing")
    return {
        "id": identifier,
        "label": label,
        "configured": configured,
        "status": status_value,
        "storage": storage,
        "path": str(path),
        "permissions": permissions,
        "message": message,
    }


def _virtual_item(identifier: str, label: str, *, configured: bool, storage: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "label": label,
        "configured": configured,
        "status": "ok" if configured else "missing",
        "storage": storage if configured else "not configured",
        "path": None,
        "permissions": {"status": "not_applicable", "mode": None},
        "message": "Configured without exposing a value." if configured else "Missing.",
    }


def _permission_summary(path: Path) -> dict[str, Any]:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return {"status": "missing", "mode": None}
    allowed = 0o700 if path.is_dir() else 0o600
    return {
        "status": "ok" if mode & 0o077 == 0 else "warning",
        "mode": oct(mode),
        "expected": oct(allowed),
    }


def _mcp_plaintext_findings(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict):
        return []
    findings: list[dict[str, str]] = []
    for server_name, server in servers.items():
        if not isinstance(server, dict):
            continue
        for location in ("env", "headers"):
            values = server.get(location)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if not isinstance(value, str):
                    continue
                if _is_plain_secret(str(key), value):
                    findings.append(
                        {
                            "server": str(server_name),
                            "location": location,
                            "key": str(key),
                            "path": str(path),
                        }
                    )
    return findings


def _is_plain_secret(key: str, value: str) -> bool:
    if "${" in value or value.startswith("$"):
        return False
    lowered_key = key.lower()
    if lowered_key.endswith("_dir") or lowered_key.endswith("_path") or value.startswith(("/", "~")):
        return False
    if any(token in lowered_key for token in ("key", "secret", "token", "authorization", "password")):
        return len(value.strip()) >= 8
    return looks_like_secret(value)
