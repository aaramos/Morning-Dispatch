from __future__ import annotations

import json
import logging
import os
import asyncio
from datetime import timedelta
from pathlib import Path
import re
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[3]
REDDIT_MCP_SERVER_RAW = os.environ.get("MORNING_DISPATCH_REDDIT_MCP_SERVER", "").strip()
REDDIT_MCP_SERVER = Path(REDDIT_MCP_SERVER_RAW) if REDDIT_MCP_SERVER_RAW else None
REDDIT_MCP_COMMAND = os.environ.get("MORNING_DISPATCH_REDDIT_MCP_COMMAND", "/opt/homebrew/bin/node")


async def browse_subreddit(
    subreddit: str,
    *,
    sort: str = "top",
    time: str = "week",
    limit: int = 15,
) -> list[dict[str, Any]]:
    payload = await _call_first_reddit_tool(
        (
            "fetch_reddit_hot_threads",
            {
                "subreddit": subreddit,
                "limit": max(1, min(limit, 100)),
            },
        ),
        (
            "browse_subreddit",
            {
                "subreddit": subreddit,
                "sort": sort,
                "time": time,
                "limit": max(1, min(limit, 100)),
                "include_nsfw": False,
                "include_subreddit_info": False,
            },
        ),
        (
            "get_top_posts",
            {
                "subreddit": subreddit,
                "time_filter": time,
                "limit": max(1, min(limit, 100)),
            },
        ),
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
    base_arguments: dict[str, Any] = {
        "query": query,
        "sort": sort,
        "time_filter": time,
        "limit": max(1, min(limit, 100)),
        "type": "link",
    }
    if not subreddits:
        try:
            payload = await _call_first_reddit_tool(
                ("search_reddit", base_arguments),
                ("search_reddit", _jordan_search_arguments(base_arguments)),
            )
            return _extract_posts(payload, "results")
        except Exception:
            payload = await _call_reddit_tool(
                "fetch_reddit_hot_threads",
                {"subreddit": "all", "limit": max(1, min(limit, 100))},
            )
            return _extract_posts(payload, "posts")

    try:
        payload = await _call_reddit_tool("search_reddit", {**base_arguments, "subreddits": subreddits})
        posts = _extract_posts(payload, "results")
        if posts:
            return posts[: max(1, min(limit, 100))]
    except Exception:
        pass

    batches = await asyncio.gather(
        *(
            _call_first_reddit_tool(
                (
                    "fetch_reddit_hot_threads",
                    {"subreddit": subreddit, "limit": max(1, min(limit, 100))},
                ),
                (
                    "search_reddit",
                    _jordan_search_arguments({**base_arguments, "subreddit": subreddit}),
                ),
            )
            for subreddit in subreddits
        ),
        return_exceptions=True,
    )
    posts: list[dict[str, Any]] = []
    errors: list[str] = []
    for subreddit, batch in zip(subreddits, batches, strict=True):
        if isinstance(batch, Exception):
            errors.append(f"r/{subreddit}: {batch}")
            continue
        posts.extend(_extract_posts(batch, "results"))
    if not posts and errors:
        raise RuntimeError("; ".join(errors[:3]))
    return posts[: max(1, min(limit, 100))]


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
    return await _call_first_reddit_tool(
        ("fetch_reddit_post_content", {"post_id": post_id or "", "comment_limit": comment_limit, "comment_depth": 2}),
        ("get_post_details", arguments),
        ("get_reddit_post", arguments),
    )


async def _call_first_reddit_tool(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    for tool_name, arguments in calls:
        try:
            return await _call_reddit_tool(tool_name, arguments)
        except Exception as exc:
            errors.append(f"{tool_name}: {exc}")
    raise RuntimeError("; ".join(errors))


def _jordan_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    converted = dict(arguments)
    if "time" in converted:
        converted["time_filter"] = converted.pop("time")
    converted.pop("subreddits", None)
    return converted


async def _call_reddit_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    try:
        return await _call_omlx_tool(f"reddit__{tool_name}", arguments)
    except Exception as exc:
        errors.append(f"oMLX: {exc}")
        logger.info("Reddit MCP through oMLX failed; falling back to stdio MCP: %s", exc)
    try:
        return await _call_stdio_tool(tool_name, arguments)
    except Exception as exc:
        errors.append(f"stdio: {exc}")
        logger.warning("Reddit MCP call failed for %s: %s", tool_name, exc)
        raise RuntimeError("Reddit MCP call failed: " + "; ".join(errors)) from exc


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
        message = str(data.get("error_message") or "").strip()
        if not message:
            message = _payload_text(_coerce_payload(data.get("content")))
        raise RuntimeError(message or "oMLX MCP tool returned an error")
    return _coerce_payload(data.get("content"))


async def _call_stdio_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if REDDIT_MCP_SERVER is None:
        raise RuntimeError("MORNING_DISPATCH_REDDIT_MCP_SERVER is not configured")
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
        raw_posts = payload.get("posts") if isinstance(payload, dict) else None
    if isinstance(raw_posts, list):
        return [post for post in raw_posts if isinstance(post, dict)]
    text = _payload_text(payload)
    return _parse_markdown_posts(text)


def _coerce_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        if isinstance(content.get("content"), list):
            return {"text": _content_list_text(content.get("content"))}
        return content
    if isinstance(content, list):
        return {"text": _content_list_text(content)}
    if isinstance(content, str) and content.strip():
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"text": content}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _payload_text(payload: dict[str, Any]) -> str:
    text = payload.get("text") if isinstance(payload, dict) else ""
    return str(text or "").strip()


def _content_list_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif hasattr(item, "text"):
            parts.append(str(getattr(item, "text") or ""))
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _parse_markdown_posts(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    key_value_posts = _parse_key_value_posts(text)
    if key_value_posts:
        return key_value_posts
    header_subreddit = ""
    header_match = re.search(r"(?im)^#\s+Top Posts from r/([A-Za-z0-9_]{2,40})", text)
    if header_match:
        header_subreddit = header_match.group(1)
    chunks = re.split(r"(?m)^###\s+\d+\.\s+", text)
    posts: list[dict[str, Any]] = []
    for chunk in chunks[1:]:
        lines = [line.rstrip() for line in chunk.strip().splitlines()]
        if not lines:
            continue
        title = lines[0].strip()
        post: dict[str, Any] = {"title": title, "content": ""}
        body_lines: list[str] = []
        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("- Author:"):
                post["author"] = stripped.split(":", 1)[1].strip().removeprefix("u/")
            elif stripped.startswith("- Subreddit:"):
                post["subreddit"] = stripped.split(":", 1)[1].strip().removeprefix("r/")
            elif stripped.startswith("- Score:"):
                post["score"] = _first_int(stripped)
            elif stripped.startswith("- Comments:"):
                post["num_comments"] = _first_int(stripped)
            elif stripped.startswith("- Link:"):
                url = stripped.split(":", 1)[1].strip()
                post["permalink"] = url
                post["url"] = url
                post["id"] = _post_id_from_url(url)
            elif not stripped.startswith("- Posted:") and not stripped.startswith("- Time"):
                body_lines.append(stripped)
        post.setdefault("subreddit", header_subreddit)
        post.setdefault("author", "unknown")
        post.setdefault("score", 0)
        post.setdefault("num_comments", 0)
        post.setdefault("id", _post_id_from_url(str(post.get("permalink") or "")) or title[:80])
        if body_lines:
            post["content"] = "\n".join(body_lines)
        posts.append(post)
    return posts


def _parse_key_value_posts(text: str) -> list[dict[str, Any]]:
    blocks = [block.strip() for block in re.split(r"(?m)^---\s*$", text) if block.strip()]
    posts: list[dict[str, Any]] = []
    for block in blocks:
        post: dict[str, Any] = {"content": ""}
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "title":
                post["title"] = value
            elif key == "score":
                post["score"] = _first_int(value)
            elif key == "comments":
                post["num_comments"] = _first_int(value)
            elif key == "author":
                post["author"] = value.removeprefix("u/")
            elif key == "content":
                post["content"] = value
            elif key == "link":
                post["permalink"] = value
                post["url"] = value
                post["id"] = _post_id_from_url(value)
                post["subreddit"] = _subreddit_from_url(value)
        if post.get("title"):
            post.setdefault("author", "unknown")
            post.setdefault("score", 0)
            post.setdefault("num_comments", 0)
            post.setdefault("id", _post_id_from_url(str(post.get("permalink") or "")) or str(post["title"])[:80])
            post.setdefault("subreddit", _subreddit_from_url(str(post.get("permalink") or "")))
            posts.append(post)
    return posts


def _first_int(value: str) -> int:
    match = re.search(r"[-+]?\d[\d,]*", value)
    if not match:
        return 0
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return 0


def _post_id_from_url(url: str) -> str:
    match = re.search(r"/comments/([^/\s]+)/", url)
    return match.group(1) if match else ""


def _subreddit_from_url(url: str) -> str:
    match = re.search(r"/r/([^/\s]+)/", url, re.IGNORECASE)
    return match.group(1) if match else ""


def _mcp_environment() -> dict[str, str]:
    settings = get_settings()
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
        "NODE_ENV": "production",
    }
    secret_values = {
        "REDDIT_CLIENT_ID": settings.reddit_client_id,
        "REDDIT_CLIENT_SECRET": settings.reddit_client_secret,
        "REDDIT_USERNAME": settings.reddit_username,
        "REDDIT_PASSWORD": settings.reddit_password,
        "REDDIT_USER_AGENT": settings.reddit_user_agent,
    }
    for key, value in secret_values.items():
        if value:
            env[key] = value
    if settings.reddit_client_id and settings.reddit_client_secret:
        env["REDDIT_AUTH_MODE"] = "authenticated"
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
