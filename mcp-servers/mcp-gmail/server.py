from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from mcp.server.fastmcp import FastMCP

from backend.agents.digestor import gmail as gmail_direct


mcp = FastMCP(
    "Morning Dispatch Gmail",
    instructions=(
        "Read-only Gmail tools for Morning Dispatch. Use only approved newsletter "
        "senders and avoid exposing credentials or secret file contents."
    ),
)


@mcp.tool()
async def gmail_fetch_newsletters(
    digest_id: str,
    sender_allowlist: list[str],
    lookback_hours: int,
    db_path: str,
) -> dict[str, Any]:
    """Fetch approved Gmail newsletters and links for a Morning Dispatch digest."""
    payloads = await gmail_direct.fetch_newsletters(
        digest_id=digest_id,
        sender_allowlist=sender_allowlist,
        lookback_hours=lookback_hours,
        db_path=db_path,
    )
    return {"payloads": [asdict(payload) for payload in payloads]}


@mcp.tool()
def gmail_search(query: str, max_results: int = 20) -> dict[str, Any]:
    """Search Gmail messages using a Gmail search query."""
    max_results = max(1, min(max_results, 50))
    service = gmail_direct.get_gmail_service()
    response = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    return {
        "query": query,
        "messages": response.get("messages", []),
        "result_size_estimate": response.get("resultSizeEstimate", 0),
    }


@mcp.tool()
def gmail_get_message(message_id: str) -> dict[str, Any]:
    """Fetch one Gmail message and return readable text plus extracted links."""
    service = gmail_direct.get_gmail_service()
    message = gmail_direct._get_message(service, message_id)
    payload = message.get("payload", {})
    links = gmail_direct.extract_link_items_from_html(payload)
    return {
        "id": message_id,
        "subject": gmail_direct.header_value(payload, "Subject"),
        "published_at": gmail_direct.message_published_at(message),
        "plain_text": gmail_direct.extract_plain_text(payload),
        "links": [asdict(link) for link in links],
    }


@mcp.tool()
def gmail_extract_links(message_id: str | None = None, html_body: str | None = None) -> dict[str, Any]:
    """Extract useful article links from a Gmail message or supplied HTML body."""
    if message_id:
        service = gmail_direct.get_gmail_service()
        message = gmail_direct._get_message(service, message_id)
        payload = message.get("payload", {})
    elif html_body:
        payload = {
            "mimeType": "text/html",
            "body": {"data": _encode_gmail_body(html_body)},
        }
    else:
        raise ValueError("Provide message_id or html_body.")

    links = gmail_direct.extract_link_items_from_html(payload)
    return {"links": [asdict(link) for link in links]}


def _encode_gmail_body(value: str) -> str:
    import base64

    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


if __name__ == "__main__":
    mcp.run(transport="stdio")
