from __future__ import annotations

import asyncio
import base64
import sqlite3
from pathlib import Path
from typing import Any

import pytest

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
        self.list_kwargs: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> FakeRequest:
        self.list_kwargs.append(dict(kwargs))
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


@pytest.fixture(autouse=True)
def default_gmail_boundary(monkeypatch):
    monkeypatch.setattr(gmail, "_after_timestamp", lambda *_args, **_kwargs: 0)


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
    <a href="https://example.com/a">Apple unveils new AI features</a>
    <a href="https://example.com/b">Running large models on Mac Studio</a>
    <a href="https://example.com/c">MLX framework deep dive</a>
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
    assert [payload.metadata["link_text"] for payload in link_payloads] == [
        "Apple unveils new AI features",
        "Running large models on Mac Studio",
        "MLX framework deep dive",
    ]


def test_utility_links_are_filtered(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    html = """
    <a href="https://example.com/article">Useful article</a>
    <a href="https://example.com/unsubscribe">Unsubscribe</a>
    <a href="https://example.com/advertise">Advertise</a>
    <a href="https://example.com/tracker.gif">Pixel</a>
    <a href="https://link.mail.beehiiv.com/ss/c/u001.example">Trending AI Tools</a>
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


def test_boilerplate_nav_and_social_links_filtered(monkeypatch, tmp_path):
    # The bulk of newsletter "links" are chrome: empty-text image links, "View Online",
    # "Shop", and social icons. They must be dropped so they don't flood screening and
    # cause the whole Gmail lane to be discarded.
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    html = """
    <a href="https://example.com/real-article">Apple expands on-device AI in macOS</a>
    <a href="https://example.com/hero-image"><img src="x.png"/></a>
    <a href="https://example.com/online">View Online</a>
    <a href="https://example.com/shop">Shop</a>
    <a href="https://twitter.com/someone">Follow us</a>
    <a href="https://www.linkedin.com/company/x">LinkedIn</a>
    <a href="https://example.com/click">Click here</a>
    """
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService({"msg-1": gmail_message("msg-1", plain_text="Links inside", html=html)}),
    )

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))
    link_payloads = [payload for payload in payloads if payload.source_type == "gmail_link"]
    assert [p.original_url for p in link_payloads] == ["https://example.com/real-article"]


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
    html = '<a href="https://example.com/a">Apple AI feature deep dive</a>'
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


def test_discover_newsletter_candidates_groups_by_sender(monkeypatch):
    service = FakeService(
        {
            "msg-1": gmail_message(
                "msg-1",
                plain_text="AI infrastructure newsletter with links and unsubscribe details.",
                subject="AI Weekly Newsletter",
                sender="AI Weekly <ai@example.com>",
                internal_date="1779984000000",
            ),
            "msg-2": gmail_message(
                "msg-2",
                plain_text="The latest AI agents digest.",
                subject="AI Weekly Digest",
                sender="AI Weekly <ai@example.com>",
                internal_date="1779897600000",
            ),
            "msg-3": gmail_message(
                "msg-3",
                plain_text="Personal note about coffee.",
                subject="Coffee",
                sender="friend@example.com",
                internal_date="1779811200000",
            ),
        }
    )
    monkeypatch.setattr(gmail, "get_gmail_service", lambda: service)

    candidates = asyncio.run(
        gmail.discover_newsletter_candidates(
            query_text="AI related newsletters received in last 7 days",
            lookback_hours=168,
            limit=4,
        )
    )

    assert len(candidates) == 1
    assert candidates[0].sender == "ai@example.com"
    assert candidates[0].sender_name == "AI Weekly"
    assert candidates[0].message_count == 2
    assert "newer_than:7d" in service.fake_messages.queries[0]
    assert "maxResults" in service.fake_messages.list_kwargs[0]


def test_invoice_is_not_filtered(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")
    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService({"msg-1": gmail_message("msg-1", plain_text="Let's talk about the invoice software startup in AI space.")}),
    )

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    assert len(payloads) == 1
    assert payloads[0].raw_text == "Let's talk about the invoice software startup in AI space."


def test_newsletter_body_split_by_headers(monkeypatch, tmp_path):
    db_path = init_db(tmp_path / "dispatch.sqlite3")

    html = """
    <h2>First Headline</h2>
    <p>This is the first article text. It is quite substantial and tells an interesting story about AI models and agentic workflows. We need enough characters to exceed 450. To achieve this, we will write a very long sentence repeating keywords and adding details about artificial intelligence, model training, fine-tuning, large language models, agentic frameworks, the model context protocol, and client server integrations. More content is added to ensure we have a robust block of text that represents a full, deep newsletter article with enough length to meet the requirement.</p>
    <h2>Second Headline</h2>
    <p>This is the second article text. It is also substantial enough to exceed 450 characters. We write more sentences to ensure it passes the length check. OpenAI released something new and GPT models are improving. Agentic tools are widely discussed. In addition to that, we will expand this paragraph with generic details about newsletter parsing, web scraping, link resolution, and developer operations. Let's make sure it has sufficient size to be parsed as a standalone article candidate in our pipeline test.</p>
    """

    plain_text = (
        "Introduction text. This intro is not long enough to be its own section.\n\n"
        "First Headline\n"
        "This is the first article text. It is quite substantial and tells an interesting story about AI models and agentic workflows. We need enough characters to exceed 450. To achieve this, we will write a very long sentence repeating keywords and adding details about artificial intelligence, model training, fine-tuning, large language models, agentic frameworks, the model context protocol, and client server integrations. More content is added to ensure we have a robust block of text that represents a full, deep newsletter article with enough length to meet the requirement.\n\n"
        "Second Headline\n"
        "This is the second article text. It is also substantial enough to exceed 450 characters. We write more sentences to ensure it passes the length check. OpenAI released something new and GPT models are improving. Agentic tools are widely discussed. In addition to that, we will expand this paragraph with generic details about newsletter parsing, web scraping, link resolution, and developer operations. Let's make sure it has sufficient size to be parsed as a standalone article candidate in our pipeline test."
    )

    monkeypatch.setattr(
        gmail,
        "get_gmail_service",
        lambda: FakeService({"msg-1": gmail_message("msg-1", plain_text=plain_text, html=html)}),
    )

    payloads = asyncio.run(gmail.fetch_newsletters("digest-1", ["news@example.com"], 24, db_path))

    gmail_payloads = [p for p in payloads if p.source_type == "gmail"]
    # Should split into 2 sections since both are substantial, and the intro was too short (<450)
    assert len(gmail_payloads) == 2
    assert gmail_payloads[0].metadata["section_title"] == "First Headline"
    assert "First Headline" in gmail_payloads[0].raw_text
    assert "section-0" in gmail_payloads[0].original_url
    assert gmail_payloads[1].metadata["section_title"] == "Second Headline"
    assert "Second Headline" in gmail_payloads[1].raw_text
    assert "section-1" in gmail_payloads[1].original_url
