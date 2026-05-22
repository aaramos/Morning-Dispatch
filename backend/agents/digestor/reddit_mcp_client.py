from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[3]
REDDIT_MCP_SERVER = Path(os.environ.get("MORNING_DISPATCH_REDDIT_MCP_SERVER", "/Users/macstudio/Apps/reddit-mcp-buddy/dist/index.js"))
REDDIT_MCP_COMMAND = os.environ.get("MORNING_DISPATCH_REDDIT_MCP_COMMAND", "/opt/homebrew/bin/node")


async def browse_subreddit(
    subreddit: str,
    *,
    sort: str = "top",
    time: str = "week",
    limit: int = 15,
) -> list[dict[str, Any]]:
    payload = await _call_reddit_tool(
        "browse_subreddit",
        {
            "subreddit": subreddit,
            "sort": sort,
            "time": time,
            "limit": max(1, min(limit, 100)),
            "include_nsfw": False,
            "include_subreddit_info": False,
        },
    )
    return _extract_posts(payload, "posts")


async def search_reddit(
    query: str,
    *,
    subreddits: list[str] | None = None,
    sort: str = "relevance",
    time: str = "week",
    limit: int = 25,
) -> list[dict[str, Any]]:
    arguments: dict[str, Any] = {
        "query": query,
        "sort": sort,
        "time": time,
        "limit": max(1, min(limit, 100)),
    }
    if subreddits:
        arguments["subreddits"] = subreddits
    payload = await _call_reddit_tool(
        "search_reddit",
        arguments,
    )
    return _extract_posts(payload, "results")


async def get_post_details(
    *,
    post_id: str | None = None,
    subreddit: str | None = None,
    url: str | None = None,
    comment_limit: int = 20,
    max_top_comments: int = 5,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "comment_limit": max(1, min(comment_limit, 500)),
        "comment_sort": "best",
        "comment_depth": 2,
        "extract_links": False,
        "max_top_comments": max(1, min(max_top_comments, 20)),
    }
    if url:
        arguments["url"] = url
    else:
        arguments["post_id"] = post_id
        if subreddit:
            arguments["subreddit"] = subreddit
    return await _call_reddit_tool("get_post_details", arguments)


async def _call_reddit_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _call_omlx_tool(f"reddit__{tool_name}", arguments)
    except Exception as exc:
        logger.info("Reddit MCP through oMLX failed; falling back to stdio MCP: %s", exc)
    try:
        return await _call_stdio_tool(tool_name, arguments)
    except Exception as exc:
        logger.warning("Reddit MCP call failed for %s: %s", tool_name, exc)
        return {}


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
    async with httpx.AsyncClient(timeout=min(max(30.0, settings.model_timeout_seconds), 120.0)) as client:
        response = await client.post(url, headers=headers, json={"tool_name": tool_name, "arguments": arguments})
        response.raise_for_status()
    data = response.json()
    if data.get("is_error"):
        raise RuntimeError(str(data.get("error_message") or "oMLX MCP tool returned an error"))
    return _coerce_payload(data.get("content"))


async def _call_stdio_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not REDDIT_MCP_SERVER.exists():
        raise RuntimeError(f"Reddit MCP server is missing at {REDDIT_MCP_SERVER}")
    server = StdioServerParameters(
        command=REDDIT_MCP_COMMAND,
        args=[str(REDDIT_MCP_SERVER)],
        cwd=str(REDDIT_MCP_SERVER.parent.parent),
        env=_mcp_environment(),
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=45)) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments, read_timeout_seconds=timedelta(seconds=45))
    if result.isError:
        message = result.content[0].text if result.content else f"{tool_name} returned an error"
        raise RuntimeError(message)
    if result.structuredContent:
        return dict(result.structuredContent)
    if result.content and getattr(result.content[0], "text", None):
        return _coerce_payload(result.content[0].text)
    return {}


def _extract_posts(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw_posts = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(raw_posts, list):
        return []
    return [post for post in raw_posts if isinstance(post, dict)]


def _coerce_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str) and content.strip():
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _mcp_environment() -> dict[str, str]:
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
        "NODE_ENV": "production",
    }
    for key in (
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "MORNING_DISPATCH_HOME",
        "MORNING_DISPATCH_DATA_DIR",
        "MORNING_DISPATCH_SECRETS_DIR",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env
