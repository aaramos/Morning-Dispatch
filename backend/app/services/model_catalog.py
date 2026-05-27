from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from backend.app.core.config import Settings, ensure_runtime_dirs


class ModelCatalogError(RuntimeError):
    pass


async def catalog_status(settings: Settings) -> dict[str, Any]:
    cloud_catalog = await _ollama_cloud_catalog_status(settings)
    try:
        models = await fetch_available_models(settings)
    except ModelCatalogError as exc:
        return {
            "available": False,
            "models": [],
            "error": str(exc),
            "selected_model": settings.librarian_model,
            "base_url": settings.model_base_url,
            "providers": {
                "local": {
                    "available": False,
                    "models": [],
                    "error": str(exc),
                    "base_url": settings.model_base_url,
                },
                "ollama_cloud": cloud_catalog,
            },
        }

    return {
        "available": True,
        "models": models,
        "error": None,
        "selected_model": settings.librarian_model,
        "base_url": settings.model_base_url,
        "providers": {
            "local": {
                "available": True,
                "models": models,
                "error": None,
                "base_url": settings.model_base_url,
            },
            "ollama_cloud": cloud_catalog,
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
            response = await client.get(url, headers=headers)
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


def save_selected_model(settings: Settings, model_name: str) -> None:
    ensure_runtime_dirs(settings)
    payload = _read_model_settings(settings.model_settings_path)
    payload["librarian_model"] = model_name
    payload["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    payload["updated_by"] = "admin"
    _write_json(settings.model_settings_path, payload)


def selected_model_source(settings: Settings) -> str:
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


async def _ollama_cloud_catalog_status(settings: Settings) -> dict[str, Any]:
    if not settings.ollama_api_key:
        return {
            "available": False,
            "configured": False,
            "models": [],
            "error": "Ollama Cloud key is not configured.",
            "base_url": settings.ollama_base_url,
        }
    try:
        models = await fetch_ollama_cloud_models(settings)
    except ModelCatalogError as exc:
        return {
            "available": False,
            "configured": bool(settings.ollama_api_key),
            "models": [],
            "error": str(exc),
            "base_url": settings.ollama_base_url,
        }
    return {
        "available": True,
        "configured": bool(settings.ollama_api_key),
        "models": models,
        "error": None,
        "base_url": settings.ollama_base_url,
    }


async def fetch_ollama_cloud_models(settings: Settings) -> list[dict[str, Any]]:
    if not settings.ollama_base_url:
        raise ModelCatalogError("Ollama Cloud base URL is not configured.")
    url = f"{settings.ollama_base_url.rstrip('/')}/tags"
    headers = {}
    if settings.ollama_api_key:
        headers["Authorization"] = f"Bearer {settings.ollama_api_key}"
    try:
        async with httpx.AsyncClient(timeout=min(settings.model_timeout_seconds, 8.0)) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise ModelCatalogError("Ollama Cloud rejected the configured API key.") from exc
        raise ModelCatalogError(f"Ollama Cloud catalog returned HTTP {exc.response.status_code}.") from exc
    except Exception as exc:
        raise ModelCatalogError("Could not reach the Ollama Cloud model catalog.") from exc
    models = _parse_ollama_tag_list(payload)
    if not models:
        raise ModelCatalogError("Ollama Cloud did not report any available models.")
    return models


def _parse_ollama_tag_list(payload: Any) -> list[dict[str, Any]]:
    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return []
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_models:
        model_id = None
        if isinstance(item, dict):
            raw_value = item.get("name") or item.get("model")
            model_id = raw_value if isinstance(raw_value, str) else None
        elif isinstance(item, str):
            model_id = item
        if not model_id:
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            {
                "id": model_id,
                "owned_by": "ollama_cloud",
                "created": item.get("modified_at") if isinstance(item, dict) else None,
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
