from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.digestor import gmail_mcp_client
from backend.agents.digestor.gmail_mcp_client import _payloads_from_structured_content
from backend.app.db.schema import SCHEMA_SQL
from backend.db.queries import get_watermark


PROJECT_DIR = Path(__file__).resolve().parents[2]
SERVER_PATH = PROJECT_DIR / "mcp-servers" / "mcp-gmail" / "server.py"


def init_db(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
    return str(path)


def test_gmail_mcp_lists_tools_and_extracts_links():
    async def run_check():
        server = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_PATH)],
            cwd=str(PROJECT_DIR),
            env={"PYTHONPATH": str(PROJECT_DIR)},
        )
        async with stdio_client(server) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool_names = {tool.name for tool in tools.tools}
                result = await session.call_tool(
                    "gmail_extract_links",
                    {
                        "html_body": """
                        <a href="https://example.com/article">Useful article</a>
                        <a href="https://example.com/unsubscribe">Unsubscribe</a>
                        """,
                    },
                )
        return tool_names, result.structuredContent

    tool_names, structured_content = asyncio.run(run_check())

    assert {"gmail_search", "gmail_get_message", "gmail_extract_links", "gmail_fetch_newsletters"} <= tool_names
    assert structured_content == {
        "links": [
            {
                "url": "https://example.com/article",
                "text": "Useful article",
                "context": "Useful article",
            }
        ]
    }


def test_gmail_mcp_client_hydrates_payloads():
    payloads = _payloads_from_structured_content(
        {
            "payloads": [
                {
                    "id": "payload-1",
                    "source_type": "gmail",
                    "source_name": "news@example.com",
                    "raw_text": "Useful model update",
                    "original_url": None,
                    "published_at": "2026-05-22T12:00:00+00:00",
                    "fetched_at": "2026-05-22T12:01:00+00:00",
                    "metadata": {"gmail_message_id": "msg-1"},
                }
            ]
        }
    )

    assert payloads == [
        NormalizedPayload(
            id="payload-1",
            source_type="gmail",
            source_name="news@example.com",
            raw_text="Useful model update",
            original_url=None,
            published_at="2026-05-22T12:00:00+00:00",
            fetched_at="2026-05-22T12:01:00+00:00",
            metadata={"gmail_message_id": "msg-1"},
        )
    ]


def test_gmail_mcp_client_prefers_omlx(monkeypatch):
    async def fake_omlx_tool(tool_name, arguments):
        assert tool_name == "gmail__gmail_fetch_newsletters"
        assert arguments["digest_id"] == "digest-1"
        return {
            "payloads": [
                {
                    "id": "payload-omlx",
                    "source_type": "gmail",
                    "source_name": "news@example.com",
                    "raw_text": "Fetched through oMLX MCP",
                    "original_url": None,
                    "published_at": None,
                    "fetched_at": "2026-05-22T12:01:00+00:00",
                    "metadata": {"gmail_message_id": "msg-omlx"},
                }
            ]
        }

    async def fail_stdio(_arguments):
        raise AssertionError("stdio fallback should not be used when oMLX MCP succeeds")

    monkeypatch.setattr(gmail_mcp_client, "_call_omlx_tool", fake_omlx_tool)
    monkeypatch.setattr(gmail_mcp_client, "_call_stdio_fetch_tool", fail_stdio)

    payloads = asyncio.run(
        gmail_mcp_client.fetch_newsletters("digest-1", ["news@example.com"], 24, "/tmp/test.sqlite3")
    )

    assert len(payloads) == 1
    assert payloads[0].id == "payload-omlx"
    assert payloads[0].raw_text == "Fetched through oMLX MCP"


def test_gmail_mcp_client_prefers_google_hosted_mcp(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    monkeypatch.setenv("MORNING_DISPATCH_GOOGLE_CLOUD_PROJECT_ID", "digestor-496920")
    monkeypatch.setenv("MORNING_DISPATCH_GMAIL_REMOTE_MCP_ENABLED", "true")
    monkeypatch.setattr(gmail_mcp_client, "_remote_mcp_enabled", lambda: True)
    monkeypatch.setattr(gmail_mcp_client, "_google_remote_mcp_token", lambda: "access-token")

    async def fake_remote_tool(_client, tool_name, arguments):
        if tool_name == "search_threads":
            assert arguments["query"].startswith("from:news@example.com after:")
            return {"threads": [{"id": "thread-1"}]}
        if tool_name == "get_thread":
            assert arguments == {"threadId": "thread-1", "messageFormat": "FULL_CONTENT"}
            return {
                "messages": [
                    {
                        "id": "msg-remote",
                        "sender": "news@example.com",
                        "subject": "Hosted MCP",
                        "date": "2026-05-22",
                        "plaintextBody": "Remote Gmail MCP body",
                        "htmlBody": '<a href="https://example.com/article">Article</a>',
                    }
                ]
            }
        raise AssertionError(f"unexpected tool: {tool_name}")

    async def fail_omlx(_tool_name, _arguments):
        raise AssertionError("local MCP fallback should not be used when Google-hosted MCP succeeds")

    monkeypatch.setattr(gmail_mcp_client, "_call_google_remote_tool", fake_remote_tool)
    monkeypatch.setattr(gmail_mcp_client, "_call_omlx_tool", fail_omlx)

    payloads = asyncio.run(
        gmail_mcp_client.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path)
    )

    assert [payload.source_type for payload in payloads] == ["gmail", "gmail_link"]
    assert payloads[0].raw_text == "Remote Gmail MCP body"
    assert payloads[1].original_url == "https://example.com/article"
    assert payloads[0].metadata["gmail_mcp"] == "google_hosted"
    assert get_watermark(db_path, "digest-1", "gmail:news@example.com") == {
        "last_fetched": "2026-05-22T00:00:00+00:00",
        "last_id": "msg-remote",
    }


def test_gmail_mcp_client_falls_back_to_stdio(monkeypatch):
    async def fail_omlx(_tool_name, _arguments):
        raise RuntimeError("oMLX MCP unavailable")

    async def fake_stdio(arguments):
        assert arguments["sender_allowlist"] == ["news@example.com"]
        return {
            "payloads": [
                {
                    "id": "payload-stdio",
                    "source_type": "gmail",
                    "source_name": "news@example.com",
                    "raw_text": "Fetched through stdio fallback",
                    "original_url": None,
                    "published_at": None,
                    "fetched_at": "2026-05-22T12:01:00+00:00",
                    "metadata": {"gmail_message_id": "msg-stdio"},
                }
            ]
        }

    monkeypatch.setattr(gmail_mcp_client, "_call_omlx_tool", fail_omlx)
    monkeypatch.setattr(gmail_mcp_client, "_call_stdio_fetch_tool", fake_stdio)

    payloads = asyncio.run(
        gmail_mcp_client.fetch_newsletters("digest-1", ["news@example.com"], 24, "/tmp/test.sqlite3")
    )

    assert len(payloads) == 1
    assert payloads[0].id == "payload-stdio"
    assert payloads[0].raw_text == "Fetched through stdio fallback"
