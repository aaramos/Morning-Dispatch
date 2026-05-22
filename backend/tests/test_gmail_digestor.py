from __future__ import annotations

import asyncio
import base64
import sqlite3
from pathlib import Path
from typing import Any

from backend.agents.digestor import gmail
from backend.app.db.schema import SCHEMA_SQL
from backend.db.queries import get_watermark


class FakeRequest:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None):
        self.result = result or {}
        self.error = error

    def execute(self) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.result


class FakeMessages:
    def __init__(self, messages: dict[str, dict[str, Any]], list_error: Exception | None = None):
        self.messages = messages
        self.list_error = list_error
        self.queries: list[str] = []

    def list(self, **kwargs: Any) -> FakeRequest:
        self.queries.append(str(kwargs["q"]))
        if self.list_error:
            return FakeRequest(error=self.list_error)
        return FakeRequest({"messages": [{"id": message_id} for message_id in self.messages]})

    def get(self, **kwargs: Any) -> FakeRequest:
        return FakeRequest(self.messages[str(kwargs["id"])])


class FakeUsers:
    def __init__(self, messages: FakeMessages):
        self._messages = messages

    def messages(self) -> FakeMessages:
        return self._messages


class FakeService:
    def __init__(self, messages: dict[str, dict[str, Any]], list_error: Exception | None = None):
        self.fake_messages = FakeMessages(messages, list_error=list_error)

    def users(self) -> FakeUsers:
        return FakeUsers(self.fake_messages)


class FakeApiError(Exception):
    def __init__(self, status: int):
        super().__init__(f"status {status}")
        self.status_code = status


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def gmail_message(
    message_id: str,
    *,
    plain_text: str = "",
    html: str = "",
    subject: str = "AI Newsletter",
    sender: str = "news@example.com",
    internal_date: str = "1767225600000",
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if plain_text:
        parts.append({"mimeType": "text/plain", "body": {"data": encoded(plain_text)}})
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": encoded(html)}})
    return {
        "id": message_id,
        "internalDate": internal_date,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "parts": parts,
        },
    }


def init_db(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
    return str(path)


def test_fetch_returns_empty_on_auth_failure(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")

    def raise_auth_failure() -> None:
        raise FakeApiError(401)

    monkeypatch.setattr(gmail, "get_gmail_service", raise_auth_failure)

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    assert payloads == []


def test_newsletter_body_becomes_payload(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService({"msg-1": gmail_message("msg-1", plain_text="Useful model update")}),
    )

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    assert len(payloads) == 1
    assert payloads[0].source_type == "gmail"
    assert payloads[0].raw_text == "Useful model update"
    assert payloads[0].metadata["gmail_message_id"] == "msg-1"


def test_links_extracted_as_separate_payloads(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    html = """
    <a href="https://example.com/a">A</a>
    <a href="https://example.com/b">B</a>
    <a href="https://example.com/c">C</a>
    """
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService({"msg-1": gmail_message("msg-1", plain_text="Links inside", html=html)}),
    )

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    link_payloads = [payload for payload in payloads if payload.source_type == "gmail_link"]
    assert len(payloads) == 4
    assert [payload.original_url for payload in link_payloads] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert [payload.metadata["link_text"] for payload in link_payloads] == ["A", "B", "C"]


def test_utility_links_are_filtered(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    html = """
    <a href="https://example.com/article">Useful article</a>
    <a href="https://example.com/unsubscribe">Unsubscribe</a>
    <a href="https://example.com/advertise">Advertise</a>
    <a href="https://example.com/tracker.gif">Pixel</a>
    """
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService({"msg-1": gmail_message("msg-1", plain_text="Links inside", html=html)}),
    )

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    link_payloads = [payload for payload in payloads if payload.source_type == "gmail_link"]
    assert len(link_payloads) == 1
    assert link_payloads[0].original_url == "https://example.com/article"


def test_pii_filtered_payload_dropped(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService({"msg-1": gmail_message("msg-1", plain_text="Your password reset code")}),
    )

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    assert payloads == []


def test_watermark_upserted_after_fetch(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService({"msg-1": gmail_message("msg-1", plain_text="Useful model update")}),
    )

    asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    watermark = get_watermark(db_path, "digest-1", "gmail:news@example.com")
    assert watermark == {"last_fetched": "2026-01-01T00:00:00+00:00", "last_id": "msg-1"}


def test_watermark_uses_newest_message_when_gmail_returns_newest_first(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService(
            {
                "msg-new": gmail_message(
                    "msg-new",
                    plain_text="New model update",
                    internal_date="1767398400000",
                ),
                "msg-old": gmail_message(
                    "msg-old",
                    plain_text="Old model update",
                    internal_date="1767225600000",
                ),
            }
        ),
    )

    asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    watermark = get_watermark(db_path, "digest-1", "gmail:news@example.com")
    assert watermark == {"last_fetched": "2026-01-03T00:00:00+00:00", "last_id": "msg-new"}


def test_deduplication_by_url(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    html = '<a href="https://example.com/a">A</a>'
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService(
            {
                "msg-1": gmail_message("msg-1", plain_text="First", html=html),
                "msg-2": gmail_message("msg-2", plain_text="Second", html=html),
            }
        ),
    )

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    link_payloads = [payload for payload in payloads if payload.source_type == "gmail_link"]
    assert len(link_payloads) == 1
    assert link_payloads[0].original_url == "https://example.com/a"
