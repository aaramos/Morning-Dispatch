from __future__ import annotations

from typing import Any

from backend.agents.model.client import ModelClient, ModelResponse, estimate_tokens
from backend.app.db import database


def record_model_response_metric(
    *,
    run_id: str | None,
    article_id: str,
    mode: str,
    model_client: ModelClient | Any,
    response: ModelResponse,
    system_prompt: str,
    prompt: str,
    status: str = "success",
    schema_valid: bool = True,
    fallback_triggered: bool = False,
    error_detail: str | None = None,
) -> None:
    model_name = _model_name(model_client)
    database.record_inference_metric(
        {
            "run_id": run_id,
            "article_id": article_id,
            "model": model_name,
            "model_tag": _model_tag(model_name),
            "quantization": _quantization(model_name),
            "backend": _backend_name(model_client),
            "route_name": _route_name(model_client),
            "mode": mode,
            "queue_wait_ms": response.queue_wait_ms,
            "ttft_ms": response.ttft_ms,
            "generation_ms": response.generation_ms,
            "total_ms": response.total_ms,
            "prompt_tokens": response.prompt_tokens if response.prompt_tokens is not None else estimate_tokens(system_prompt, prompt),
            "completion_tokens": response.completion_tokens,
            "tokens_per_sec": response.tokens_per_sec,
            "schema_valid": schema_valid,
            "fallback_triggered": fallback_triggered or bool(getattr(model_client, "fallback_triggered", False)),
            "status": status,
            "error_detail": error_detail[:600] if error_detail else None,
        }
    )


def record_model_error_metric(
    *,
    run_id: str | None,
    article_id: str,
    mode: str,
    model_client: ModelClient | Any,
    system_prompt: str,
    prompt: str,
    status: str,
    error_detail: str | None = None,
    total_ms: int | None = None,
    queue_wait_ms: int | None = None,
    ttft_ms: int | None = None,
    generation_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    tokens_per_sec: float | None = None,
) -> None:
    model_name = _model_name(model_client)
    database.record_inference_metric(
        {
            "run_id": run_id,
            "article_id": article_id,
            "model": model_name,
            "model_tag": _model_tag(model_name),
            "quantization": _quantization(model_name),
            "backend": _backend_name(model_client),
            "route_name": _route_name(model_client),
            "mode": mode,
            "queue_wait_ms": queue_wait_ms,
            "ttft_ms": ttft_ms,
            "generation_ms": generation_ms,
            "total_ms": total_ms if total_ms is not None else 0,
            "prompt_tokens": prompt_tokens if prompt_tokens is not None else estimate_tokens(system_prompt, prompt),
            "completion_tokens": completion_tokens,
            "tokens_per_sec": tokens_per_sec,
            "schema_valid": False,
            "fallback_triggered": True,
            "status": status,
            "error_detail": error_detail[:600] if error_detail else None,
        }
    )


def _model_name(model_client: Any) -> str:
    config = getattr(model_client, "config", None)
    return str(getattr(config, "model", None) or "unknown")


def _backend_name(model_client: Any) -> str:
    config = getattr(model_client, "config", None)
    provider = str(getattr(config, "provider", "") or "")
    if provider:
        return provider
    base_url = str(getattr(config, "base_url", "") or "")
    if "localhost" in base_url or "127.0.0.1" in base_url:
        return "local"
    return "remote" if base_url else "unknown"


def _route_name(model_client: Any) -> str | None:
    config = getattr(model_client, "config", None)
    route_name = getattr(config, "route_name", None)
    if isinstance(route_name, str):
        route_name = route_name.strip()
    return route_name or None


def _model_tag(model_name: str) -> str | None:
    lowered = model_name.lower()
    if "gemma" in lowered:
        return "gemma"
    if "llama" in lowered:
        return "llama"
    if "qwen" in lowered:
        return "qwen"
    if "mistral" in lowered or "mixtral" in lowered:
        return "mistral"
    return None


def _quantization(model_name: str) -> str | None:
    lowered = model_name.lower()
    for marker in ("bf16", "fp16", "fp8", "q8", "q6", "q5", "q4", "int8", "int4"):
        if marker in lowered:
            return marker.upper()
    return None
