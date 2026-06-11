from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from backend.app.core.config import DEFAULT_LIBRARIAN_MODEL, Settings, ensure_runtime_dirs


class ModelCatalogError(RuntimeError):
    pass


_CATALOG_CACHE: dict[str, tuple[float, list[dict[str, Any]] | None, str | None]] = {}
_CATALOG_TTL_SECONDS = 20.0


def reset_catalog_cache() -> None:
    _CATALOG_CACHE.clear()


async def _cached_available_models(settings: Settings) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Fetch the model list, memoizing both success and failure for a short TTL.

    Keyed by the resolved base URL so a changed base URL misses the cache. Only the
    network result is cached; callers layer fresh settings (selected model, etc.)
    on top of it.
    """
    base_url = (settings.model_base_url or "").rstrip("/")
    cached = _CATALOG_CACHE.get(base_url)
    if cached is not None and time.monotonic() - cached[0] < _CATALOG_TTL_SECONDS:
        return cached[1], cached[2]

    try:
        models: list[dict[str, Any]] | None = await fetch_available_models(settings)
        error = None
    except ModelCatalogError as exc:
        models = None
        error = str(exc)
    _CATALOG_CACHE[base_url] = (time.monotonic(), models, error)
    return models, error


async def catalog_status(settings: Settings) -> dict[str, Any]:
    models, error = await _cached_available_models(settings)
    if models is None:
        return {
            "available": False,
            "models": [],
            "error": error,
            "selected_model": settings.librarian_model,
            "selected_local_model": settings.librarian_model,
            "base_url": settings.model_base_url,
            "providers": {
                "local": {
                    "available": False,
                    "models": [],
                    "error": error,
                    "base_url": settings.model_base_url,
                    "selected_model": settings.librarian_model,
                },
            },
        }

    return {
        "available": True,
        "models": models,
        "error": None,
        "selected_model": settings.librarian_model,
        "selected_local_model": settings.librarian_model,
        "base_url": settings.model_base_url,
        "providers": {
            "local": {
                "available": True,
                "models": models,
                "error": None,
                "base_url": settings.model_base_url,
                "selected_model": settings.librarian_model,
            },
        },
    }


async def fetch_available_models(settings: Settings) -> list[dict[str, Any]]:
    if not settings.model_base_url:
        raise ModelCatalogError("Model base URL is not configured.")

    url = f"{settings.model_base_url.rstrip('/')}/models"
    headers = {}
    if settings.model_api_key:
        headers["Authorization"] = f"Bearer {settings.model_api_key}"

    try:
        async with httpx.AsyncClient(timeout=min(settings.model_timeout_seconds, 8.0)) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise ModelCatalogError("oMLX rejected the configured API key.") from exc
        raise ModelCatalogError(f"oMLX model catalog returned HTTP {exc.response.status_code}.") from exc
    except Exception as exc:
        raise ModelCatalogError("Could not reach the oMLX model catalog.") from exc

    models = _parse_model_list(payload)
    if not models:
        raise ModelCatalogError("oMLX did not report any available models.")
    return models


def save_selected_model(settings: Settings, model_name: str, *, provider: str = "local") -> None:
    ensure_runtime_dirs(settings)
    payload = _read_model_settings(settings.model_settings_path)
    payload["librarian_model"] = model_name
    payload["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    payload["updated_by"] = "admin"
    _write_json(settings.model_settings_path, payload)


def restore_default_models(settings: Settings) -> None:
    ensure_runtime_dirs(settings)
    payload = _read_model_settings(settings.model_settings_path)
    payload["librarian_model"] = DEFAULT_LIBRARIAN_MODEL
    payload["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    payload["updated_by"] = "admin"
    _write_json(settings.model_settings_path, payload)


def selected_model_source(settings: Settings, *, provider: str = "local") -> str:
    payload = _read_model_settings(settings.model_settings_path)
    if payload.get("librarian_model") == settings.librarian_model:
        return "admin"
    return "environment"


def _parse_model_list(payload: Any) -> list[dict[str, Any]]:
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return []

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_models:
        model_id = _model_id(item)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            {
                "id": model_id,
                "owned_by": item.get("owned_by") if isinstance(item, dict) else None,
                "created": item.get("created") if isinstance(item, dict) else None,
            }
        )
    return sorted(models, key=lambda model: model["id"].lower())


def _model_id(item: Any) -> str | None:
    if isinstance(item, str):
        value = item
    elif isinstance(item, dict):
        raw_value = item.get("id")
        value = raw_value if isinstance(raw_value, str) else ""
    else:
        value = ""
    value = value.strip()
    return value or None


def _read_model_settings(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
