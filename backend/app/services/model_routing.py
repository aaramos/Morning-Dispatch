from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from typing import Any

from backend.agents.model import ModelClient, ModelClientConfig, ModelClientError, ModelResponse
from backend.app.core.config import (
    MODEL_ROUTE_AGENTS,
    MODEL_ROUTE_PROVIDERS,
    Settings,
    ensure_runtime_dirs,
    get_settings,
)

logger = logging.getLogger(__name__)

PRIVATE_SOURCE_TYPES = {"gmail", "gmail_link", "collection_chunk"}
_OLLAMA_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/=\\-]{19,}$")


@dataclass(frozen=True)
class RouteResolution:
    client: Any | None
    route: dict[str, object]
    privacy_forced_local: bool = False
    fallback_configured: bool = False
    unavailable_reason: str | None = None


class RoutedModelClient:
    """Small wrapper that tries the configured route, then falls back locally."""

    def __init__(self, *, primary: ModelClient, fallback: ModelClient | None, route_name: str):
        self.primary = primary
        self.fallback = fallback
        self.route_name = route_name
        self._active = primary
        self.fallback_triggered = False

    @property
    def config(self) -> ModelClientConfig:
        return self._active.config

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        response, payload = await self.complete_json_with_metrics(**kwargs)
        return payload

    async def complete_json_with_metrics(self, **kwargs: Any) -> tuple[ModelResponse, dict[str, Any]]:
        try:
            response, payload = await self.primary.complete_json_with_metrics(**kwargs)
            self._active = self.primary
            self.fallback_triggered = False
            return response, payload
        except ModelClientError as exc:
            if self.fallback is None:
                raise
            logger.warning(
                "Model route %s failed on %s; falling back to %s: %s",
                self.route_name,
                self.primary.config.model,
                self.fallback.config.model,
                exc.status,
            )
            response, payload = await self.fallback.complete_json_with_metrics(**kwargs)
            self._active = self.fallback
            self.fallback_triggered = True
            return response, payload

    async def complete(self, **kwargs: Any) -> str:
        response = await self.complete_response(**kwargs)
        return response.content

    async def complete_response(self, **kwargs: Any) -> ModelResponse:
        try:
            response = await self.primary.complete_response(**kwargs)
            self._active = self.primary
            self.fallback_triggered = False
            return response
        except ModelClientError:
            if self.fallback is None:
                raise
            response = await self.fallback.complete_response(**kwargs)
            self._active = self.fallback
            self.fallback_triggered = True
            return response


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
            {
                "id": "ollama_cloud",
                "label": "Ollama Cloud",
                "configured": bool(settings.ollama_api_key),
                "privacy": "public_only_default",
            },
        ],
        "routes": {
            agent: {
                **route,
                "effective_model": effective_route_model(settings, agent, route),
                "label": _agent_label(agent),
            }
            for agent, route in routes.items()
        },
        "privacy": {
            "private_sources": sorted(PRIVATE_SOURCE_TYPES),
            "rule": "Gmail and Collections content stays local unless a later explicit override is added.",
        },
        "ollama_cloud": {
            "configured": bool(settings.ollama_api_key),
            "base_url": settings.ollama_base_url,
            "key_path": str(settings.secrets_dir / "ollama" / "api_key"),
            "default_model": settings.ollama_cloud_model,
        },
        "defaults": {
            "local": settings.librarian_model,
            "ollama_cloud": settings.ollama_cloud_model,
        },
    }


def normalized_routes(settings: Settings | None = None) -> dict[str, dict[str, object]]:
    settings = settings or get_settings()
    routes: dict[str, dict[str, object]] = {}
    for agent in MODEL_ROUTE_AGENTS:
        raw_route = settings.model_routes.get(agent) if isinstance(settings.model_routes, dict) else None
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
    has_private_content = contains_private_source(items or [])
    provider = str(route.get("provider") or "local")
    privacy_forced_local = bool(has_private_content and provider != "local" and not route.get("allow_private_cloud"))
    if privacy_forced_local:
        local = _local_client(settings, agent=agent, model_override=model_override)
        return RouteResolution(
            client=local,
            route={**route, "provider": "local", "privacy_reason": "private_source"},
            privacy_forced_local=True,
            fallback_configured=False,
            unavailable_reason=None if local else "Local model is not configured.",
        )

    if provider == "ollama_cloud":
        cloud = _ollama_client(settings, agent=agent, model_override=model_override or _route_model(route))
        local = _local_client(settings, agent=agent, model_override=model_override)
        if cloud is None:
            return RouteResolution(
                client=local,
                route=route,
                fallback_configured=False,
                unavailable_reason="Ollama Cloud is missing an API key or model.",
            )
        return RouteResolution(
            client=RoutedModelClient(primary=cloud, fallback=local, route_name=agent) if local else cloud,
            route=route,
            fallback_configured=bool(local),
        )

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
        provider = str(raw_route.get("provider") or routes[agent].get("provider") or "local").strip().lower()
        if provider not in MODEL_ROUTE_PROVIDERS:
            provider = "local"
        raw_model = raw_route.get("model")
        model = raw_model.strip() if isinstance(raw_model, str) else None
        routes[agent] = {
            "provider": provider,
            "model": model or None,
            "allow_private_cloud": bool(raw_route.get("allow_private_cloud")),
        }
    existing["model_routes"] = routes
    existing["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    existing["updated_by"] = "admin"
    _write_json(settings.model_settings_path, existing)
    return routes_status(get_settings())


def save_ollama_api_key(settings: Settings, api_key: str) -> dict[str, Any]:
    value = api_key.strip()
    if not value:
        raise ValueError("Ollama API key is required")
    if not _is_valid_ollama_api_key(value):
        raise ValueError(
            "That key doesn't look like an Ollama Cloud token. "
            "Please paste the token from your Ollama Cloud dashboard (single token string)."
        )
    path = settings.secrets_dir / "ollama" / "api_key"
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


def effective_route_model(settings: Settings, agent: str, route: dict[str, object] | None = None) -> str | None:
    route = route or normalized_routes(settings).get(agent) or {}
    model = _route_model(route)
    if model:
        return model
    if route.get("provider") == "ollama_cloud":
        return settings.ollama_cloud_model
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


def _ollama_client(settings: Settings, *, agent: str, model_override: str | None = None) -> ModelClient | None:
    model = (model_override or settings.ollama_cloud_model or "").strip()
    if not model or not settings.ollama_api_key:
        return None
    base_url = _normalize_ollama_base_url(settings.ollama_base_url)
    return ModelClient(
        ModelClientConfig(
            base_url=base_url,
            model=model,
            api_key=settings.ollama_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            concurrency=settings.model_concurrency,
            provider="ollama_cloud",
            api_mode="openai",
            route_name=agent,
        )
    )


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


def _normalize_ollama_base_url(raw_url: str) -> str:
    """Normalize an Ollama Cloud base URL to the OpenAI-compatible `/v1` style."""
    value = raw_url.strip()
    if not value:
        return value
    parsed = urlparse(value)
    netloc = parsed.netloc
    path = (parsed.path or "").rstrip("/") or "/"
    if parsed.hostname in {"api.ollama.com", "www.ollama.com"}:
        netloc = f"ollama.com:{parsed.port}" if parsed.port else "ollama.com"
    base_path = path
    if not base_path or base_path == "/":
        normalized_path = "/v1"
    elif base_path in {"/chat", "/chat/completions", "/models", "/v1/models", "/tags", "/v1/tags", "/api"}:
        normalized_path = "/v1"
    elif not base_path.endswith("/v1"):
        normalized_path = f"{base_path}/v1"
    else:
        normalized_path = base_path
    normalized = parsed._replace(netloc=netloc, path=normalized_path, params="", query="", fragment="")
    return urlunparse(normalized)


def _is_valid_ollama_api_key(value: str) -> bool:
    if " " in value:
        return False
    if value.lower().startswith("ssh-"):
        return False
    return bool(_OLLAMA_KEY_PATTERN.match(value))


def _agent_label(agent: str) -> str:
    labels = {
        "refinement": "Refinement",
        "librarian": "Librarian",
        "source_audit": "Source Audit",
        "editorial": "Editorial",
        "critic": "Critic",
    }
    return labels.get(agent, agent.replace("_", " ").title())


def _agent_description(agent: str) -> str:
    descriptions = {
        "refinement": "Turns the user's interest into a runnable search strategy.",
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
