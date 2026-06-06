from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from backend.app.core.config import Settings

MODEL_CAPACITY_STATUS = "model_capacity"

# Process-wide pool of httpx clients so model calls reuse keep-alive connections
# instead of paying connection/TLS setup on every request. Keyed by endpoint +
# timeout and bound to the event loop that created the client (a different loop —
# e.g. a fresh asyncio.run in tests — transparently gets its own client). Closed
# at app shutdown via aclose_shared_model_clients().
_SHARED_HTTP_CLIENTS: dict[tuple[str, float], tuple[Any, httpx.AsyncClient]] = {}


def _shared_http_client(config: "ModelClientConfig") -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    key = (config.base_url, float(config.timeout_seconds))
    existing = _SHARED_HTTP_CLIENTS.get(key)
    if existing is not None:
        bound_loop, client = existing
        if bound_loop is loop and not client.is_closed:
            return client
    client = httpx.AsyncClient(
        timeout=config.timeout_seconds,
        limits=httpx.Limits(
            max_keepalive_connections=max(4, config.concurrency),
            max_connections=max(8, config.concurrency * 2),
        ),
    )
    _SHARED_HTTP_CLIENTS[key] = (loop, client)
    return client


@asynccontextmanager
async def _leased_http_client(config: "ModelClientConfig") -> AsyncIterator[httpx.AsyncClient]:
    # Yields a pooled client WITHOUT closing it on exit, so connections persist
    # across calls. Lifecycle is owned by the process-wide pool.
    yield _shared_http_client(config)


async def aclose_shared_model_clients() -> None:
    clients = list(_SHARED_HTTP_CLIENTS.values())
    _SHARED_HTTP_CLIENTS.clear()
    for _loop, client in clients:
        if hasattr(client, "is_closed") and not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - best-effort shutdown
                pass
_RETRYABLE_STATUSES = {"model_error", "timeout"}
_RETRYABLE_ERROR_FRAGMENTS = (
    "peer closed connection",
    "incomplete chunked read",
    "connection reset",
    "server disconnected",
    "remote protocol",
)


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
    provider: str = "local"
    api_mode: str = "openai"
    route_name: str | None = None


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
    def from_settings(
        cls,
        settings: Settings,
        *,
        model: str | None = None,
        route_name: str | None = None,
    ) -> ModelClient | None:
        model_name = (model or settings.librarian_model or "").strip()
        if not settings.librarian_use_model or not settings.model_base_url or not model_name:
            return None
        return cls(
            ModelClientConfig(
                base_url=settings.model_base_url,
                model=model_name,
                api_key=settings.model_api_key,
                timeout_seconds=settings.model_timeout_seconds,
                concurrency=settings.model_concurrency,
                provider="local",
                api_mode="openai",
                route_name=route_name,
            )
        )

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
        on_token: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        response, parsed = await self.complete_json_with_metrics(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            on_token=on_token,
        )
        return parsed

    async def complete_json_with_metrics(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
        on_token: Callable[[str], None] | None = None,
    ) -> tuple[ModelResponse, dict[str, Any]]:
        response = await self.complete_response(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            on_token=on_token,
        )
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

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        response = await self.complete_response(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            on_token=on_token,
        )
        return response.content

    async def complete_response(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
        on_token: Callable[[str], None] | None = None,
        json_mode: bool = True,
    ) -> ModelResponse:
        attempts = _retry_attempts(self.config)
        for attempt in range(attempts):
            try:
                return await self._complete_response_once(
                    system=system,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    on_token=on_token,
                    json_mode=json_mode,
                )
            except ModelClientError as exc:
                if attempt >= attempts - 1 or not _should_retry(exc):
                    raise
                await asyncio.sleep(_retry_delay_seconds(attempt))
        raise ModelClientError("Model request failed after retry attempts", status="model_error")

    async def _complete_response_once(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        on_token: Callable[[str], None] | None,
        json_mode: bool = True,
    ) -> ModelResponse:
        if self.config.api_mode == "ollama":
            if on_token is None:
                return await self._complete_response_ollama_nonstream(system=system, prompt=prompt, max_tokens=max_tokens)
            return await self._complete_response_ollama_stream(
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
                on_token=on_token,
            )
        if on_token is None:
            return await self._complete_response_nonstream(system=system, prompt=prompt, max_tokens=max_tokens, json_mode=json_mode)
        return await self._complete_response_stream(system=system, prompt=prompt, max_tokens=max_tokens, on_token=on_token, json_mode=json_mode)

    async def _complete_response_ollama_nonstream(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
    ) -> ModelResponse:
        url = f"{self.config.base_url.rstrip('/')}/chat"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_predict": max_tokens},
        }

        submitted_at = perf_counter()
        queue_wait_ms = 0
        prompt_token_estimate = estimate_tokens(system, prompt)
        async with self._semaphore:
            queue_wait_ms = _elapsed_ms(submitted_at)
            try:
                async with _leased_http_client(self.config) as client:
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
            except Exception as exc:  # pragma: no cover - network timing dependent.
                raise ModelClientError(
                    str(exc),
                    status="model_error",
                    queue_wait_ms=queue_wait_ms,
                    total_ms=_elapsed_ms(submitted_at),
                    prompt_tokens=prompt_token_estimate,
                ) from exc

        content = _ollama_message_content(data)
        if not content:
            raise ModelClientError(
                "Model response content was empty",
                status="empty_output",
                queue_wait_ms=queue_wait_ms,
                total_ms=_elapsed_ms(submitted_at),
                prompt_tokens=prompt_token_estimate,
            )
        total_ms = _elapsed_ms(submitted_at)
        completion_tokens = _ollama_int(data, "eval_count") or estimate_tokens(content)
        prompt_tokens = _ollama_int(data, "prompt_eval_count") or prompt_token_estimate
        generation_ms = _duration_ms(data.get("eval_duration")) or max(1, total_ms - queue_wait_ms)
        return ModelResponse(
            content=content.strip(),
            queue_wait_ms=queue_wait_ms,
            ttft_ms=None,
            generation_ms=generation_ms,
            total_ms=total_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tokens_per_sec=_token_rate(completion_tokens, generation_ms),
        )

    async def _complete_response_ollama_stream(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
        on_token: Callable[[str], None],
    ) -> ModelResponse:
        url = f"{self.config.base_url.rstrip('/')}/chat"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "format": "json",
            "options": {"temperature": 0, "num_predict": max_tokens},
        }

        submitted_at = perf_counter()
        queue_wait_ms = 0
        prompt_token_estimate = estimate_tokens(system, prompt)
        chunks: list[str] = []
        final_data: dict[str, Any] | None = None
        first_token_ms: int | None = None
        async with self._semaphore:
            queue_wait_ms = _elapsed_ms(submitted_at)
            try:
                async with _leased_http_client(self.config) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            raw = line.strip()
                            if not raw:
                                continue
                            try:
                                frame = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            final_data = frame
                            content = _ollama_message_content(frame)
                            if content:
                                if first_token_ms is None:
                                    first_token_ms = _elapsed_ms(submitted_at)
                                chunks.append(content)
                                on_token(content)
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
            except Exception as exc:  # pragma: no cover - network timing dependent.
                raise ModelClientError(
                    str(exc),
                    status="model_error",
                    queue_wait_ms=queue_wait_ms,
                    total_ms=_elapsed_ms(submitted_at),
                    prompt_tokens=prompt_token_estimate,
                ) from exc

        content = "".join(chunks).strip()
        if not content:
            raise ModelClientError(
                "Model response was empty",
                status="empty_output",
                queue_wait_ms=queue_wait_ms,
                total_ms=_elapsed_ms(submitted_at),
                prompt_tokens=prompt_token_estimate,
            )
        total_ms = _elapsed_ms(submitted_at)
        completion_tokens = _ollama_int(final_data, "eval_count") or estimate_tokens(content)
        prompt_tokens = _ollama_int(final_data, "prompt_eval_count") or prompt_token_estimate
        generation_ms = _duration_ms((final_data or {}).get("eval_duration")) or max(
            1,
            total_ms - (first_token_ms or queue_wait_ms),
        )
        return ModelResponse(
            content=content,
            queue_wait_ms=queue_wait_ms,
            ttft_ms=first_token_ms,
            generation_ms=generation_ms,
            total_ms=total_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tokens_per_sec=_token_rate(completion_tokens, generation_ms),
        )

    async def _complete_response_nonstream(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
        json_mode: bool = True,
    ) -> ModelResponse:
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
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        submitted_at = perf_counter()
        queue_wait_ms = 0
        prompt_token_estimate = estimate_tokens(system, prompt)
        async with self._semaphore:
            queue_wait_ms = _elapsed_ms(submitted_at)
            try:
                async with _leased_http_client(self.config) as client:
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
        prompt_tokens = _usage_int(usage, "prompt_tokens")
        completion_tokens = _usage_int(usage, "completion_tokens")
        prompt_tokens = prompt_tokens if prompt_tokens is not None else prompt_token_estimate
        completion_tokens = completion_tokens if completion_tokens is not None else estimate_tokens(str(content))
        generation_ms = max(1, total_ms - queue_wait_ms)
        return ModelResponse(
            content=content.strip(),
            queue_wait_ms=queue_wait_ms,
            ttft_ms=None,
            generation_ms=generation_ms,
            total_ms=total_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tokens_per_sec=_token_rate(completion_tokens, generation_ms),
        )

    async def _complete_response_stream(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 900,
        on_token: Callable[[str], None],
        json_mode: bool = True,
    ) -> ModelResponse:
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
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        submitted_at = perf_counter()
        queue_wait_ms = 0
        prompt_token_estimate = estimate_tokens(system, prompt)
        chunks: list[str] = []
        final_data: dict[str, Any] | None = None
        usage: dict[str, Any] | None = None
        first_token_ms: int | None = None

        async with self._semaphore:
            queue_wait_ms = _elapsed_ms(submitted_at)
            try:
                async with _leased_http_client(self.config) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line.removeprefix("data:").strip()
                            if raw == "[DONE]":
                                continue
                            try:
                                frame = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            final_data = frame
                            frame_usage = frame.get("usage")
                            if isinstance(frame_usage, dict):
                                usage = frame_usage
                            choices = frame.get("choices")
                            if not isinstance(choices, list):
                                continue
                            for choice in choices:
                                if not isinstance(choice, dict):
                                    continue
                                delta = choice.get("delta")
                                if not isinstance(delta, dict):
                                    continue
                                content = delta.get("content")
                                if not isinstance(content, str) or not content:
                                    continue
                                if first_token_ms is None:
                                    first_token_ms = _elapsed_ms(submitted_at)
                                chunks.append(content)
                                on_token(content)
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
            except Exception as exc:  # pragma: no cover - network timing dependent.
                raise ModelClientError(
                    str(exc),
                    status="model_error",
                    queue_wait_ms=queue_wait_ms,
                    total_ms=_elapsed_ms(submitted_at),
                    prompt_tokens=prompt_token_estimate,
                ) from exc

        content = "".join(chunks).strip()
        if not content:
            raise ModelClientError(
                "Model response was empty",
                status="empty_output",
                queue_wait_ms=queue_wait_ms,
                total_ms=_elapsed_ms(submitted_at),
                prompt_tokens=prompt_token_estimate,
            )
        total_ms = _elapsed_ms(submitted_at)
        prompt_tokens = _usage_int(usage, "prompt_tokens")
        completion_tokens = _usage_int(usage, "completion_tokens")
        prompt_tokens = prompt_tokens if prompt_tokens is not None else prompt_token_estimate
        completion_tokens = completion_tokens if completion_tokens is not None else estimate_tokens(content)
        generation_ms = max(1, total_ms - (first_token_ms or queue_wait_ms))
        return ModelResponse(
            content=content,
            queue_wait_ms=queue_wait_ms,
            ttft_ms=first_token_ms,
            generation_ms=generation_ms,
            total_ms=total_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tokens_per_sec=_token_rate(completion_tokens, generation_ms),
        )


def _repair_json_string(s: str) -> str:
    import re
    # 0. Clean up escaped single quotes which are invalid in JSON double-quoted strings
    s = s.replace("\\'", "'")
    s = s.replace(r"\'", "'")

    # Safely convert single quotes enclosing keys/values to double quotes,
    # leaving single quotes inside text untouched (e.g. "It's good")
    s = re.sub(r"'\s*:", '":', s)
    s = re.sub(r":\s*'", ': "', s)
    s = re.sub(r",\s*'", ', "', s)
    s = re.sub(r"'\s*,", '",', s)
    s = re.sub(r"\[\s*'", '["', s)
    s = re.sub(r"'\s*\]", '"]', s)
    s = re.sub(r"\{\s*'", '{"', s)
    s = re.sub(r"'\s*\}", '"}', s)

    # 1. Missing comma between objects: } { -> },{
    s = re.sub(r'\}\s*\{', '},{', s)
    # 2. Missing comma between arrays: ] [ -> ],[
    s = re.sub(r'\]\s*\[', '],[', s)
    # 3. Missing comma between quote/value and object/quote
    s = re.sub(r'\}\s*"', '},"', s)
    s = re.sub(r'"\s*\{', '", {', s)
    # 4. Trailing commas before closing braces: , } -> } and , ] -> ]
    s = re.sub(r',\s*\}', '}', s)
    s = re.sub(r',\s*\]', ']', s)
    # 5. Missing quotes around keys: {id: 1 -> {"id": 1
    s = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', s)
    return s


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
        try:
            repaired = _repair_json_string(cleaned[start : end + 1])
            parsed = json.loads(repaired)
        except Exception:
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


def _token_rate(completion_tokens: int | None, generation_ms: int | None) -> float | None:
    if completion_tokens is None or generation_ms is None or generation_ms <= 0:
        return None
    return round(float(completion_tokens) / (float(generation_ms) / 1000), 2)


def _status_for_http(status_code: int) -> str:
    if status_code == 507:
        return MODEL_CAPACITY_STATUS
    if status_code == 429:
        return "rate_limited"
    return "http_error"


def _retry_attempts(config: ModelClientConfig) -> int:
    if config.provider == "local":
        return 3
    return 2


def _should_retry(exc: ModelClientError) -> bool:
    if exc.status not in _RETRYABLE_STATUSES:
        return False
    text = str(exc).lower()
    if exc.status == "timeout":
        return True
    return any(fragment in text for fragment in _RETRYABLE_ERROR_FRAGMENTS)


def _retry_delay_seconds(attempt: int) -> float:
    return min(1.5, 0.25 * (attempt + 1))


def _http_error_message(exc: httpx.HTTPStatusError) -> str:
    status_code = exc.response.status_code
    detail = _response_detail(exc.response)
    if status_code == 507:
        message = "oMLX reported insufficient model capacity"
        return f"{message}: {detail}" if detail else message
    return f"{exc}: {detail}" if detail else str(exc)


def _response_detail(response: httpx.Response) -> str:
    try:
        text = response.text.strip()
    except httpx.ResponseNotRead:
        return response.reason_phrase[:500]
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


def _ollama_message_content(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        return content if isinstance(content, str) else ""
    response = payload.get("response")
    return response if isinstance(response, str) else ""


def _ollama_int(payload: object, key: str) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if isinstance(value, int):
        return value
    return None


def _duration_ms(value: object) -> int | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return max(1, round(value / 1_000_000))
