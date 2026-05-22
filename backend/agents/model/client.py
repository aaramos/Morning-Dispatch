from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

from backend.app.core.config import Settings

MODEL_CAPACITY_STATUS = "model_capacity"


class ModelClientError(RuntimeError):
    """Raised when the local model backend cannot complete a request."""

    def __init__(
        self,
        message: str,
        *,
        status: str = "model_error",
        queue_wait_ms: int | None = None,
        ttft_ms: int | None = None,
        generation_ms: int | None = None,
        total_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        tokens_per_sec: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.queue_wait_ms = queue_wait_ms
        self.ttft_ms = ttft_ms
        self.generation_ms = generation_ms
        self.total_ms = total_ms
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.tokens_per_sec = tokens_per_sec


@dataclass(frozen=True)
class ModelClientConfig:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: float = 60.0
    concurrency: int = 1


@dataclass(frozen=True)
class ModelResponse:
    content: str
    queue_wait_ms: int
    ttft_ms: int | None
    generation_ms: int | None
    total_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    tokens_per_sec: float | None


class ModelClient:
    def __init__(self, config: ModelClientConfig):
        self.config = config
        self._semaphore = asyncio.Semaphore(max(1, config.concurrency))

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelClient | None:
        if not settings.librarian_use_model or not settings.model_base_url or not settings.librarian_model:
            return None
        return cls(
            ModelClientConfig(
                base_url=settings.model_base_url,
                model=settings.librarian_model,
                api_key=settings.model_api_key,
                timeout_seconds=settings.model_timeout_seconds,
                concurrency=settings.model_concurrency,
            )
        )

    async def complete_json(self, *, system: str, prompt: str, max_tokens: int = 900) -> dict[str, Any]:
        response, parsed = await self.complete_json_with_metrics(system=system, prompt=prompt, max_tokens=max_tokens)
        return parsed

    async def complete_json_with_metrics(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
    ) -> tuple[ModelResponse, dict[str, Any]]:
        response = await self.complete_response(system=system, prompt=prompt, max_tokens=max_tokens)
        try:
            parsed = _parse_json_object(response.content)
        except ModelClientError as exc:
            raise ModelClientError(
                str(exc),
                status=exc.status,
                queue_wait_ms=response.queue_wait_ms,
                ttft_ms=response.ttft_ms,
                generation_ms=response.generation_ms,
                total_ms=response.total_ms,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                tokens_per_sec=response.tokens_per_sec,
            ) from exc
        return response, parsed

    async def complete(self, *, system: str, prompt: str, max_tokens: int = 900) -> str:
        response = await self.complete_response(system=system, prompt=prompt, max_tokens=max_tokens)
        return response.content

    async def complete_response(self, *, system: str, prompt: str, max_tokens: int = 900) -> ModelResponse:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }

        submitted_at = perf_counter()
        queue_wait_ms = 0
        prompt_token_estimate = estimate_tokens(system, prompt)
        async with self._semaphore:
            queue_wait_ms = _elapsed_ms(submitted_at)
            try:
                async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                    response = await asyncio.wait_for(
                        client.post(url, headers=headers, json=payload),
                        timeout=self.config.timeout_seconds,
                    )
                    response.raise_for_status()
                    data = response.json()
            except asyncio.TimeoutError as exc:  # pragma: no cover - network timing dependent.
                raise ModelClientError(
                    str(exc) or "Model request timed out",
                    status="timeout",
                    queue_wait_ms=queue_wait_ms,
                    total_ms=_elapsed_ms(submitted_at),
                    prompt_tokens=prompt_token_estimate,
                ) from exc
            except httpx.HTTPStatusError as exc:
                status = _status_for_http(exc.response.status_code)
                raise ModelClientError(
                    _http_error_message(exc),
                    status=status,
                    queue_wait_ms=queue_wait_ms,
                    total_ms=_elapsed_ms(submitted_at),
                    prompt_tokens=prompt_token_estimate,
                ) from exc
            except Exception as exc:  # pragma: no cover - exercised through Librarian fallback tests.
                raise ModelClientError(
                    str(exc),
                    status="model_error",
                    queue_wait_ms=queue_wait_ms,
                    total_ms=_elapsed_ms(submitted_at),
                    prompt_tokens=prompt_token_estimate,
                ) from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelClientError(
                "Model response did not include chat content",
                status="empty_output",
                queue_wait_ms=queue_wait_ms,
                total_ms=_elapsed_ms(submitted_at),
                prompt_tokens=prompt_token_estimate,
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelClientError(
                "Model response content was empty",
                status="empty_output",
                queue_wait_ms=queue_wait_ms,
                total_ms=_elapsed_ms(submitted_at),
                prompt_tokens=prompt_token_estimate,
            )
        total_ms = _elapsed_ms(submitted_at)
        usage = data.get("usage") if isinstance(data, dict) else None
        prompt_tokens = _usage_int(usage, "prompt_tokens") or prompt_token_estimate
        completion_tokens = _usage_int(usage, "completion_tokens") or estimate_tokens(str(content))
        return ModelResponse(
            content=content.strip(),
            queue_wait_ms=queue_wait_ms,
            ttft_ms=None,
            generation_ms=None,
            total_ms=total_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tokens_per_sec=None,
        )


def _parse_json_object(value: str) -> dict[str, Any]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ModelClientError("Model response was not a JSON object", status="parse_error")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ModelClientError("Model response JSON could not be parsed", status="parse_error") from exc
    if not isinstance(parsed, dict):
        raise ModelClientError("Model response JSON was not an object", status="parse_error")
    return parsed


def estimate_tokens(*parts: str) -> int:
    text = " ".join(part for part in parts if part)
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


def _status_for_http(status_code: int) -> str:
    if status_code == 507:
        return MODEL_CAPACITY_STATUS
    if status_code == 429:
        return "rate_limited"
    return "http_error"


def _http_error_message(exc: httpx.HTTPStatusError) -> str:
    status_code = exc.response.status_code
    detail = _response_detail(exc.response)
    if status_code == 507:
        message = "oMLX reported insufficient model capacity"
        return f"{message}: {detail}" if detail else message
    return f"{exc}: {detail}" if detail else str(exc)


def _response_detail(response: httpx.Response) -> str:
    text = response.text.strip()
    if not text:
        return ""
    try:
        payload = response.json()
    except ValueError:
        return text[:500]
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error") or payload.get("message")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail")
        if detail:
            return str(detail)[:500]
    return text[:500]


def _usage_int(usage: object, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if not isinstance(value, int):
        return None
    return value
