from __future__ import annotations

import json
import logging
import os
import sys
import base64
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.agents.digestor import gmail as gmail_direct
from backend.agents.digestor.base import NormalizedPayload, pii_filter, utc_now
from backend.app.core.config import get_settings
from backend.db.queries import upsert_watermark

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[3]
SERVER_PATH = PROJECT_DIR / "mcp-servers" / "mcp-gmail" / "server.py"
GMAIL_FETCH_TOOL = "gmail__gmail_fetch_newsletters"
GOOGLE_GMAIL_MCP_ENDPOINT = "https://gmailmcp.googleapis.com/mcp/v1"


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

    if _remote_mcp_enabled():
        try:
            structured = await _call_google_remote_fetch(arguments)
            return _payloads_from_structured_content(structured)
        except Exception as exc:
            logger.warning("Google-hosted Gmail MCP fetch failed; falling back to local MCP: %s", exc)

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


async def _call_google_remote_fetch(arguments: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    project_id = settings.google_cloud_project_id
    if not project_id:
        raise RuntimeError("Google Cloud project ID is not configured for hosted Gmail MCP")

    token = _google_remote_mcp_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-goog-user-project": project_id,
    }
    timeout = max(90.0, settings.model_timeout_seconds)
    payloads: list[NormalizedPayload] = []
    seen_urls: set[str] = set()
    seen_message_ids: set[str] = set()

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for sender in arguments["sender_allowlist"]:
            sender_email = str(sender).strip()
            if not sender_email:
                continue
            latest_id: str | None = None
            latest_at: str | None = None
            source_key = f"gmail:{sender_email}"
            after_timestamp = gmail_direct._after_timestamp(
                str(arguments["db_path"]),
                str(arguments["digest_id"]),
                source_key,
                int(arguments["lookback_hours"]),
            )
            search_result = await _call_google_remote_tool(
                client,
                "search_threads",
                {"query": gmail_direct.build_query(sender_email, after_timestamp), "pageSize": 50},
            )

            for thread in search_result.get("threads", []):
                if not isinstance(thread, dict):
                    continue
                thread_id = str(thread.get("id") or "")
                if not thread_id:
                    continue
                thread_result = await _call_google_remote_tool(
                    client,
                    "get_thread",
                    {"threadId": thread_id, "messageFormat": "FULL_CONTENT"},
                )
                for message in thread_result.get("messages", []):
                    if not isinstance(message, dict):
                        continue
                    message_payloads, message_at, message_id = _payloads_from_remote_message(
                        message,
                        sender_email=sender_email,
                        thread_id=thread_id,
                        seen_urls=seen_urls,
                        seen_message_ids=seen_message_ids,
                    )
                    payloads.extend(message_payloads)
                    if message_id:
                        latest_at, latest_id = gmail_direct._newer_message_marker(
                            latest_at=latest_at,
                            latest_id=latest_id,
                            message_at=message_at,
                            message_id=message_id,
                        )

            upsert_watermark(
                str(arguments["db_path"]),
                str(arguments["digest_id"]),
                source_key,
                latest_at or utc_now(),
                latest_id,
            )

    return {"payloads": [asdict(payload) for payload in payloads]}


async def _call_google_remote_tool(
    client: httpx.AsyncClient,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        GOOGLE_GMAIL_MCP_ENDPOINT,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    result = data.get("result")
    if not isinstance(result, dict):
        return {}
    if result.get("isError"):
        message = _remote_error_message(result)
        raise RuntimeError(message or "Google-hosted Gmail MCP tool returned an error")
    structured = result.get("structuredContent") or result.get("structured_content")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list) and content:
        text = content[0].get("text") if isinstance(content[0], dict) else None
        if isinstance(text, str) and text.strip():
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {"text": text}
    return {}


def _remote_error_message(result: dict[str, Any]) -> str | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


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


def _payloads_from_remote_message(
    message: dict[str, Any],
    *,
    sender_email: str,
    thread_id: str,
    seen_urls: set[str],
    seen_message_ids: set[str],
) -> tuple[list[NormalizedPayload], str | None, str | None]:
    message_id = str(message.get("id") or "")
    if not message_id:
        return [], None, None

    remote_sender = str(message.get("sender") or sender_email)
    parsed_sender = parseaddr(remote_sender)[1] or remote_sender
    if parsed_sender.lower() != sender_email.lower():
        return [], None, message_id

    subject = str(message.get("subject") or "")
    published_at = _remote_message_published_at(message.get("date"))
    plain_text = gmail_direct._clean_text(str(message.get("plaintextBody") or ""))
    html_body = str(message.get("htmlBody") or "")
    payloads: list[NormalizedPayload] = []

    if plain_text and message_id not in seen_message_ids:
        body_payload = NormalizedPayload(
            source_type="gmail",
            source_name=sender_email,
            raw_text=plain_text,
            original_url=None,
            published_at=published_at,
            metadata={
                "gmail_message_id": message_id,
                "gmail_thread_id": thread_id,
                "sender_email": sender_email,
                "subject": subject,
                "gmail_mcp": "google_hosted",
            },
        )
        if pii_filter(body_payload):
            payloads.append(body_payload)
            seen_message_ids.add(message_id)

    for link in _remote_html_links(html_body):
        if link.url in seen_urls:
            continue
        link_payload = NormalizedPayload(
            source_type="gmail_link",
            source_name=sender_email,
            raw_text=link.context,
            original_url=link.url,
            published_at=published_at,
            metadata={
                "gmail_message_id": message_id,
                "gmail_thread_id": thread_id,
                "parent_subject": subject,
                "subject": subject,
                "sender_email": sender_email,
                "link_text": link.text,
                "gmail_mcp": "google_hosted",
            },
        )
        if pii_filter(link_payload):
            payloads.append(link_payload)
            seen_urls.add(link.url)

    return payloads, published_at, message_id


def _remote_html_links(html_body: str) -> list[gmail_direct.ExtractedLink]:
    if not html_body.strip():
        return []
    encoded = base64.urlsafe_b64encode(html_body.encode("utf-8")).decode("ascii").rstrip("=")
    payload = {"mimeType": "text/html", "body": {"data": encoded}}
    return gmail_direct.extract_link_items_from_html(payload)


def _remote_message_published_at(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return datetime.fromisoformat(text).replace(tzinfo=UTC).isoformat(timespec="seconds")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        return None


def _remote_mcp_enabled() -> bool:
    settings = get_settings()
    return bool(
        settings.gmail_remote_mcp_enabled
        and settings.google_cloud_project_id
        and settings.gmail_credentials_path.exists()
    )


def _google_remote_mcp_token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    settings = get_settings()
    creds = Credentials.from_authorized_user_file(
        str(settings.gmail_credentials_path),
        gmail_direct.required_scopes(hosted_mcp_enabled=True),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        settings.gmail_credentials_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid or not creds.token:
        raise RuntimeError("Gmail credentials are not valid for hosted Gmail MCP")
    return str(creds.token)


def _mcp_environment() -> dict[str, str]:
    env: dict[str, str] = {
        "PYTHONPATH": str(PROJECT_DIR),
    }
    for key in (
        "MORNING_DISPATCH_GOOGLE_CLOUD_PROJECT_ID",
        "MORNING_DISPATCH_GMAIL_REMOTE_MCP_ENABLED",
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
