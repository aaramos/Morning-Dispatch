from __future__ import annotations

import asyncio
from typing import Any

import httpx

from backend.app.core.config import Settings
from backend.app.core.secret_redaction import redact_secret_text

GMAIL_FETCH_TOOL = "gmail__gmail_fetch_newsletters"
REDDIT_BROWSE_TOOL = "reddit__browse_subreddit"
REDDIT_SEARCH_TOOL = "reddit__search_reddit"


async def status(settings: Settings) -> dict[str, Any]:
    if not settings.model_base_url:
        return _unavailable("oMLX base URL is not configured.")

    headers = {}
    if settings.model_api_key:
        headers["Authorization"] = f"Bearer {settings.model_api_key}"

    base_url = settings.model_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=min(settings.model_timeout_seconds, 8.0)) as client:
            servers_response, tools_response = await asyncio.gather(
                client.get(f"{base_url}/mcp/servers", headers=headers),
                client.get(f"{base_url}/mcp/tools", headers=headers),
            )
            servers_response.raise_for_status()
            tools_response.raise_for_status()
            server_payload = servers_response.json()
            tools_payload = tools_response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            return _unavailable("oMLX rejected the configured API key.")
        return _unavailable(f"oMLX MCP status returned HTTP {exc.response.status_code}.")
    except Exception:
        return _unavailable("Could not reach oMLX MCP status.")

    servers = _parse_servers(server_payload)
    tools = _parse_tools(tools_payload)
    gmail_server = next((server for server in servers if server["name"] == "gmail"), None)
    gmail_tool_count = sum(1 for tool in tools if tool.startswith("gmail__"))
    fetch_tool_present = GMAIL_FETCH_TOOL in tools
    gmail_connected = bool(
        gmail_server
        and gmail_server["state"] == "connected"
        and fetch_tool_present
    )
    reddit_server = next((server for server in servers if server["name"] == "reddit"), None)
    reddit_tool_count = sum(1 for tool in tools if tool.startswith("reddit__"))
    reddit_connected = bool(
        reddit_server
        and reddit_server["state"] == "connected"
        and REDDIT_BROWSE_TOOL in tools
        and REDDIT_SEARCH_TOOL in tools
    )

    return {
        "available": True,
        "error": None,
        "base_url": base_url,
        "server_count": len(servers),
        "tool_count": len(tools),
        "servers": servers,
        "gmail": {
            "connected": gmail_connected,
            "server_state": gmail_server["state"] if gmail_server else "missing",
            "tools_count": gmail_tool_count,
            "fetch_tool_present": fetch_tool_present,
            "error": gmail_server["error"] if gmail_server else "Gmail MCP server is not registered.",
        },
        "reddit": {
            "connected": reddit_connected,
            "server_state": reddit_server["state"] if reddit_server else "missing",
            "tools_count": reddit_tool_count,
            "browse_tool_present": REDDIT_BROWSE_TOOL in tools,
            "search_tool_present": REDDIT_SEARCH_TOOL in tools,
            "error": reddit_server["error"] if reddit_server else "Reddit MCP server is not registered.",
        },
    }


def _parse_servers(payload: Any) -> list[dict[str, Any]]:
    raw_servers = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(raw_servers, list):
        return []

    servers: list[dict[str, Any]] = []
    for server in raw_servers:
        if not isinstance(server, dict):
            continue
        name = str(server.get("name") or "").strip()
        if not name:
            continue
        servers.append(
            {
                "name": name,
                "state": str(server.get("state") or "unknown"),
                "transport": str(server.get("transport") or "unknown"),
                "tools_count": int(server.get("tools_count") or 0),
                "error": redact_secret_text(str(server.get("error"))) if server.get("error") else None,
            }
        )
    return servers


def _parse_tools(payload: Any) -> set[str]:
    raw_tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(raw_tools, list):
        return set()

    tools: set[str] = set()
    for tool in raw_tools:
        if isinstance(tool, str):
            name = tool
        elif isinstance(tool, dict):
            name = str(tool.get("name") or "")
        else:
            name = ""
        name = name.strip()
        if name:
            tools.add(name)
    return tools


def _unavailable(error: str) -> dict[str, Any]:
    return {
        "available": False,
        "error": error,
        "base_url": None,
        "server_count": 0,
        "tool_count": 0,
        "servers": [],
        "gmail": {
            "connected": False,
            "server_state": "unavailable",
            "tools_count": 0,
            "fetch_tool_present": False,
            "error": error,
        },
        "reddit": {
            "connected": False,
            "server_state": "unavailable",
            "tools_count": 0,
            "browse_tool_present": False,
            "search_tool_present": False,
            "error": error,
        },
    }
