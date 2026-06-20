from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.agents.model import ModelClient
from backend.app.core.config import (
    MODEL_ROUTE_AGENTS,
    Settings,
    ensure_runtime_dirs,
    get_settings,
)

logger = logging.getLogger(__name__)

PRIVATE_SOURCE_TYPES = {"gmail", "gmail_link", "collection_chunk"}


@dataclass(frozen=True)
class RouteResolution:
    client: Any | None
    route: dict[str, object]
    privacy_forced_local: bool = False
    fallback_configured: bool = False
    unavailable_reason: str | None = None


def routes_status(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    routes = normalized_routes(settings)
    return {
        "agents": [
            {"id": agent, "label": _agent_label(agent), "description": _agent_description(agent)}
            for agent in MODEL_ROUTE_AGENTS
        ],
        "providers": [
            {"id": "local", "label": "Local", "configured": bool(settings.model_api_key), "privacy": "private_ok"},
        ],
        "routes": {
            agent: {
                **route,
                "effective_model": effective_route_model(settings, agent, route),
                "label": _agent_label(agent),
            }
            for agent, route in routes.items()
        },
        "local": {
            "configured": bool(settings.model_api_key),
            "base_url": settings.model_base_url,
            "key_path": str(settings.secrets_dir / "model" / "api_key"),
            "default_model": settings.librarian_model,
        },
        "defaults": {
            "local": settings.librarian_model,
        },
    }


def normalized_routes(settings: Settings | None = None) -> dict[str, dict[str, object]]:
    settings = settings or get_settings()
    routes: dict[str, dict[str, object]] = {}
    for agent in MODEL_ROUTE_AGENTS:
        raw_route = settings.model_routes.get(agent) if isinstance(settings.model_routes, dict) else None
        route = raw_route if isinstance(raw_route, dict) else {}
        raw_model = route.get("model")
        model = raw_model.strip() if isinstance(raw_model, str) else None
        routes[agent] = {
            "provider": "local",
            "model": model or None,
        }
    return routes


def client_for_agent(
    agent: str,
    *,
    settings: Settings | None = None,
    items: list[Any] | tuple[Any, ...] | None = None,
    model_override: str | None = None,
) -> RouteResolution:
    settings = settings or get_settings()
    agent = agent if agent in MODEL_ROUTE_AGENTS else "librarian"
    route = normalized_routes(settings)[agent]
    # All agents run on the local model server. The `items` argument is retained for
    # call-site compatibility; with cloud routing removed there is no privacy decision.
    local = _local_client(settings, agent=agent, model_override=model_override or _route_model(route))
    return RouteResolution(
        client=local,
        route=route,
        unavailable_reason=None if local else "Local model is not configured.",
    )


def contains_private_source(items: list[Any] | tuple[Any, ...]) -> bool:
    for item in items:
        source_type = _source_type(item)
        if source_type in PRIVATE_SOURCE_TYPES:
            return True
    return False


def save_routes(settings: Settings, routes_payload: dict[str, Any]) -> dict[str, Any]:
    ensure_runtime_dirs(settings)
    existing = _read_model_settings(settings.model_settings_path)
    routes = normalized_routes(settings)
    for agent, raw_route in routes_payload.items():
        if agent not in MODEL_ROUTE_AGENTS or not isinstance(raw_route, dict):
            continue
        raw_model = raw_route.get("model")
        model = raw_model.strip() if isinstance(raw_model, str) else None
        routes[agent] = {
            "provider": "local",
            "model": model or None,
        }
    existing["model_routes"] = routes
    existing["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    existing["updated_by"] = "admin"
    _write_json(settings.model_settings_path, existing)
    return routes_status(get_settings())


def save_model_api_key(settings: Settings, api_key: str) -> dict[str, Any]:
    value = api_key.strip()
    if not value:
        raise ValueError("Model API key is required")
    if " " in value or "\n" in value:
        raise ValueError("The API key should be a single token with no spaces or line breaks.")
    path = settings.secrets_dir / "model" / "api_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError as exc:
        logger.warning("Could not restrict secret directory permissions for %s: %s", path.parent, exc)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError as exc:
        logger.warning("Could not restrict secret file permissions for %s: %s", path, exc)
    return {"configured": True, "path": str(path)}


def clear_model_api_key(settings: Settings) -> dict[str, Any]:
    path = settings.secrets_dir / "model" / "api_key"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove model API key at %s: %s", path, exc)
    return {"configured": False, "path": str(path)}


def effective_route_model(settings: Settings, agent: str, route: dict[str, object] | None = None) -> str | None:
    route = route or normalized_routes(settings).get(agent) or {}
    model = _route_model(route)
    if model:
        return model
    return settings.librarian_model


def _local_client(settings: Settings, *, agent: str, model_override: str | None = None) -> ModelClient | None:
    if model_override:
        client = _model_client_from_settings(settings, model=model_override, route_name=agent)
    else:
        client = _model_client_from_settings(settings, route_name=agent)
    if client is None:
        return None
    config = getattr(client, "config", None)
    if not getattr(config, "base_url", None) or not getattr(config, "model", None):
        return client
    return client


def _model_client_from_settings(
    settings: Settings,
    *,
    model: str | None = None,
    route_name: str | None = None,
) -> ModelClient | None:
    try:
        return ModelClient.from_settings(settings, model=model, route_name=route_name)
    except TypeError:
        if model:
            try:
                return ModelClient.from_settings(settings, model=model)
            except TypeError:
                pass
        return ModelClient.from_settings(settings)


def _source_type(item: Any) -> str:
    payload = getattr(item, "payload", None)
    if payload is not None:
        return str(getattr(payload, "source_type", "") or "")
    if isinstance(item, dict):
        payload = item.get("payload")
        if isinstance(payload, dict):
            return str(payload.get("source_type") or "")
        return str(item.get("source_type") or "")
    return str(getattr(item, "source_type", "") or "")


def _route_model(route: dict[str, object] | None) -> str | None:
    if not isinstance(route, dict):
        return None
    model = route.get("model")
    if isinstance(model, str):
        model = model.strip()
        return model or None
    return None


def _agent_label(agent: str) -> str:
    labels = {
        "refinement": "Refinement",
        "foreign_media": "Foreign Media",
        "librarian": "Librarian",
        "source_audit": "Source Audit",
        "editorial": "Editorial",
        "critic": "Critic",
    }
    return labels.get(agent, agent.replace("_", " ").title())


def _agent_description(agent: str) -> str:
    descriptions = {
        "refinement": "Turns the user's interest into a runnable search strategy.",
        "foreign_media": "Writes native-language search queries for foreign media discovery.",
        "librarian": "Cleans and enriches fetched items.",
        "source_audit": "Checks freshness, fit, source quality, and constraints.",
        "editorial": "Ranks the complete candidate set and picks the lead.",
        "critic": "Reviews the brief and cuts weak or off-scope items.",
    }
    return descriptions.get(agent, "")


def _read_model_settings(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
