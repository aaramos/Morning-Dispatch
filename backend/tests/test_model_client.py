from __future__ import annotations

import asyncio

import pytest

from backend.agents.model import ModelClient, ModelClientConfig, ModelClientError, ModelResponse


def test_model_client_retries_transient_connection_drop(monkeypatch):
    client = ModelClient(
        ModelClientConfig(
            base_url="http://127.0.0.1:1234/v1",
            model="Gemma4-MTP-26B-BF16",
            provider="local",
        )
    )
    attempts = 0

    async def flaky_once(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelClientError(
                "peer closed connection without sending complete message body (incomplete chunked read)",
                status="model_error",
            )
        return ModelResponse(
            content='{"ok": true}',
            queue_wait_ms=0,
            ttft_ms=None,
            generation_ms=1,
            total_ms=1,
            prompt_tokens=1,
            completion_tokens=1,
            tokens_per_sec=1.0,
        )

    monkeypatch.setattr(client, "_complete_response_once", flaky_once)

    response = asyncio.run(client.complete_response(system="system", prompt="prompt", max_tokens=8))

    assert response.content == '{"ok": true}'
    assert attempts == 2


def test_model_client_does_not_retry_auth_error(monkeypatch):
    client = ModelClient(
        ModelClientConfig(
            base_url="http://127.0.0.1:1234/v1",
            model="local-model",
            provider="local",
        )
    )
    attempts = 0

    async def unauthorized(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise ModelClientError("401 Unauthorized", status="http_error")

    monkeypatch.setattr(client, "_complete_response_once", unauthorized)

    with pytest.raises(ModelClientError):
        asyncio.run(client.complete_response(system="system", prompt="prompt", max_tokens=8))
    assert attempts == 1
