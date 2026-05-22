from __future__ import annotations

import json
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.agents.digestor.base import NormalizedPayload
from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[3]
SERVER_PATH = PROJECT_DIR / "mcp-servers" / "mcp-gmail" / "server.py"
GMAIL_FETCH_TOOL = "gmail__gmail_fetch_newsletters"


async def fetch_newsletters(
    digest_id: str,
    sender_allowlist: list[str],
    lookback_hours: int,
    db_path: str,
) -> list[NormalizedPayload]:
    arguments = {
        "digest_id": digest_id,
        "sender_allowlist": sender_allowlist,
        "lookback_hours": lookback_hours,
        "db_path": db_path,
    }

    try:
        structured = await _call_omlx_fetch_tool(arguments)
    except Exception as exc:
        logger.warning("Gmail MCP fetch through oMLX failed; falling back to stdio MCP: %s", exc)
        try:
            structured = await _call_stdio_fetch_tool(arguments)
        except Exception as fallback_exc:
            logger.warning("Gmail stdio MCP fetch failed: %s", fallback_exc)
            return []

    return _payloads_from_structured_content(structured)


async def _call_omlx_fetch_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _call_omlx_tool(GMAIL_FETCH_TOOL, arguments)


async def _call_omlx_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.model_base_url:
        raise RuntimeError("oMLX model base URL is not configured")
    if not settings.model_api_key:
        raise RuntimeError("oMLX API key is not configured")

    url = f"{settings.model_base_url.rstrip('/')}/mcp/execute"
    headers = {
        "Authorization": f"Bearer {settings.model_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"tool_name": tool_name, "arguments": arguments}
    async with httpx.AsyncClient(timeout=max(90.0, settings.model_timeout_seconds)) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
    data = response.json()
    if data.get("is_error"):
        raise RuntimeError(str(data.get("error_message") or "oMLX MCP tool returned an error"))
    content = data.get("content")
    if isinstance(content, dict):
        return content
    if isinstance(content, str) and content.strip():
        return json.loads(content)
    return {}


async def _call_stdio_fetch_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        cwd=str(PROJECT_DIR),
        env=_mcp_environment(),
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=90)) as session:
            await session.initialize()
            result = await session.call_tool(
                "gmail_fetch_newsletters",
                arguments,
                read_timeout_seconds=timedelta(seconds=90),
            )
    if result.isError:
        message = result.content[0].text if result.content else "Gmail MCP tool returned an error"
        raise RuntimeError(message)
    if result.structuredContent:
        return dict(result.structuredContent)
    if result.content and getattr(result.content[0], "text", None):
        return json.loads(result.content[0].text)
    return {}


def _payloads_from_structured_content(payload: dict[str, Any]) -> list[NormalizedPayload]:
    raw_payloads = payload.get("payloads", [])
    if not isinstance(raw_payloads, list):
        return []

    hydrated: list[NormalizedPayload] = []
    for raw_payload in raw_payloads:
        if isinstance(raw_payload, dict):
            hydrated.append(NormalizedPayload(**raw_payload))
    return hydrated


def _mcp_environment() -> dict[str, str]:
    env: dict[str, str] = {
        "PYTHONPATH": str(PROJECT_DIR),
    }
    for key in (
        "MORNING_DISPATCH_HOME",
        "MORNING_DISPATCH_DATA_DIR",
        "MORNING_DISPATCH_SECRETS_DIR",
        "MORNING_DISPATCH_GMAIL_CLIENT_SECRET_PATH",
        "MORNING_DISPATCH_GMAIL_CREDENTIALS_PATH",
        "MORNING_DISPATCH_GMAIL_OAUTH_STATE_PATH",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env
