from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from backend.agents.digestor.base import NormalizedPayload, pii_filter, utc_now
from backend.db.queries import get_watermark, upsert_watermark

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]
HOSTED_GMAIL_MCP_SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/cloud-platform",
]
CREDENTIALS_PATH = "/secrets/gmail_credentials.json"

logger = logging.getLogger(__name__)


def required_scopes(*, hosted_mcp_enabled: bool = False) -> list[str]:
    if not hosted_mcp_enabled:
        return list(SCOPES)
    return [*SCOPES, *HOSTED_GMAIL_MCP_SCOPES]


@dataclass(frozen=True)
class ExtractedLink:
    url: str
    text: str
    context: str = ""


@dataclass(frozen=True)
class NewsletterCandidate:
    sender: str
    sender_name: str
    subject: str
    message_count: int = 1
    latest_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "sender_name": self.sender_name,
            "subject": self.subject,
            "message_count": self.message_count,
            "latest_at": self.latest_at,
        }


async def discover_newsletter_candidates(
    *,
    query_text: str,
    lookback_hours: int,
    limit: int = 12,
) -> list[NewsletterCandidate]:
    try:
        service = get_gmail_service()
    except Exception as exc:
        logger.warning("Gmail authentication failed during newsletter discovery: %s", exc)
        return []

    query = build_discovery_query(query_text=query_text, lookback_hours=lookback_hours)
    try:
        messages = _list_messages(service, query, limit=max(limit * 3, limit))
    except Exception as exc:
        logger.warning("Gmail newsletter discovery failed: %s", exc)
        return []

    candidates: dict[str, NewsletterCandidate] = {}
    for message_ref in messages:
        message_id = message_ref.get("id")
        if not message_id:
            continue
        try:
            message = _get_message(service, str(message_id))
        except Exception as exc:
            logger.warning("Skipping Gmail discovery message %s: %s", message_id, exc)
            continue
        payload = message.get("payload", {})
        sender_name, sender = sender_from_header(header_value(payload, "From"))
        if not sender:
            continue
        subject = header_value(payload, "Subject") or "(no subject)"
        if not _looks_like_newsletter(sender, subject, payload):
            continue
        published_at = message_published_at(message)
        current = candidates.get(sender)
        if current is None:
            candidates[sender] = NewsletterCandidate(
                sender=sender,
                sender_name=sender_name,
                subject=subject,
                latest_at=published_at,
            )
        else:
            candidates[sender] = NewsletterCandidate(
                sender=sender,
                sender_name=current.sender_name or sender_name,
                subject=current.subject,
                message_count=current.message_count + 1,
                latest_at=_latest_iso(current.latest_at, published_at),
            )

    return sorted(candidates.values(), key=lambda item: (item.message_count, item.latest_at or ""), reverse=True)[:limit]


async def fetch_newsletters(
    digest_id: str,
    sender_allowlist: list[str],
    lookback_hours: int,
    db_path: str,
) -> list[NormalizedPayload]:
    try:
        service = get_gmail_service()
    except Exception as exc:
        logger.warning("Gmail authentication failed: %s", exc)
        return []

    collected: list[NormalizedPayload] = []
    seen_urls: set[str] = set()
    seen_message_ids: set[str] = set()

    try:
        for sender in sender_allowlist:
            source_key = f"gmail:{sender}"
            after_timestamp = _after_timestamp(db_path, digest_id, source_key, lookback_hours)
            query = build_query(sender, after_timestamp)
            messages = _list_messages(service, query)
            latest_id: str | None = None
            latest_at: str | None = None

            for message_ref in messages:
                message_id = message_ref.get("id")
                if not message_id:
                    continue

                try:
                    message = _get_message(service, message_id)
                    payload = message.get("payload", {})
                    subject = header_value(payload, "Subject")
                    published_at = message_published_at(message)
                    latest_at, latest_id = _newer_message_marker(
                        latest_at=latest_at,
                        latest_id=latest_id,
                        message_at=published_at,
                        message_id=message_id,
                    )

                    plain_text = extract_plain_text(payload)
                    if plain_text and message_id not in seen_message_ids:
                        body_payload = NormalizedPayload(
                            source_type="gmail",
                            source_name=sender,
                            raw_text=plain_text,
                            original_url=None,
                            published_at=published_at,
                            metadata={
                                "gmail_message_id": message_id,
                                "sender_email": sender,
                                "subject": subject,
                            },
                        )
                        if pii_filter(body_payload):
                            collected.append(body_payload)
                            seen_message_ids.add(message_id)

                    for link in extract_link_items_from_html(payload):
                        if link.url in seen_urls:
                            continue
                        link_payload = NormalizedPayload(
                            source_type="gmail_link",
                            source_name=sender,
                            raw_text=link.context,
                            original_url=link.url,
                            published_at=published_at,
                            metadata={
                                "gmail_message_id": message_id,
                                "parent_subject": subject,
                                "subject": subject,
                                "sender_email": sender,
                                "link_text": link.text,
                            },
                        )
                        if pii_filter(link_payload):
                            collected.append(link_payload)
                            seen_urls.add(link.url)
                except Exception as exc:
                    logger.warning("Skipping Gmail message %s: %s", message_id, exc)

            upsert_watermark(db_path, digest_id, source_key, latest_at or utc_now(), latest_id)
    except Exception as exc:
        if _http_status(exc) in {401, 429}:
            logger.warning("Recoverable Gmail API error: %s", exc)
        else:
            logger.warning("Gmail fetch failed: %s", exc)
        return []

    return collected


def get_gmail_service() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials_file = _credentials_path()
    creds = Credentials.from_authorized_user_file(str(credentials_file), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        try:
            credentials_file.write_text(creds.to_json(), encoding="utf-8")
        except OSError as exc:
            logger.warning("Gmail token refreshed but could not be persisted: %s", exc)
    return build("gmail", "v1", credentials=creds)


def _credentials_path() -> Path:
    configured_path = os.environ.get("MORNING_DISPATCH_GMAIL_CREDENTIALS_PATH")
    if configured_path:
        return Path(configured_path).expanduser()

    container_path = Path(CREDENTIALS_PATH)
    if container_path.exists():
        return container_path

    from backend.app.core.config import get_settings

    return get_settings().gmail_credentials_path


def build_query(sender: str, after_timestamp: int) -> str:
    return f"from:{sender} after:{after_timestamp}"


def build_discovery_query(*, query_text: str, lookback_hours: int) -> str:
    lookback_days = max(1, min(365, (max(1, lookback_hours) + 23) // 24))
    terms = _query_terms(query_text)
    topic = " OR ".join(terms[:8])
    newsletter_terms = "(newsletter OR digest OR roundup OR brief OR substack OR beehiiv)"
    if topic:
        return f"newer_than:{lookback_days}d ({topic}) {newsletter_terms}"
    return f"newer_than:{lookback_days}d {newsletter_terms}"


def extract_plain_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for part in _walk_mime_parts(payload):
        if part.get("mimeType") != "text/plain":
            continue
        decoded = _decode_body_data(part)
        if decoded:
            parts.append(decoded)
    return _clean_text("\n\n".join(parts))


def extract_links_from_html(payload: dict[str, Any]) -> list[str]:
    return [link.url for link in extract_link_items_from_html(payload)]


def extract_link_items_from_html(payload: dict[str, Any]) -> list[ExtractedLink]:
    links: list[ExtractedLink] = []
    seen: set[str] = set()
    for part in _walk_mime_parts(payload):
        if part.get("mimeType") != "text/html":
            continue
        decoded = _decode_body_data(part)
        if not decoded:
            continue
        soup = BeautifulSoup(decoded, "html.parser")
        for anchor in soup.find_all("a", href=True):
            url = str(anchor["href"]).strip()
            text = _clean_text(anchor.get_text(" ", strip=True))
            context = _link_context(anchor)
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            if not _keep_newsletter_link(url, text):
                continue
            if url in seen:
                continue
            links.append(ExtractedLink(url=url, text=text, context=context))
            seen.add(url)
    return links


def header_value(payload: dict[str, Any], name: str) -> str:
    for header in payload.get("headers", []):
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def sender_from_header(value: str) -> tuple[str, str]:
    name, address = parseaddr(value)
    return (_clean_text(name), address.strip().lower())


def message_published_at(message: dict[str, Any]) -> str | None:
    internal_date = message.get("internalDate")
    if internal_date:
        try:
            timestamp = int(internal_date) / 1000
            return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            pass

    raw_date = header_value(message.get("payload", {}), "Date")
    if not raw_date:
        return None
    try:
        parsed = parsedate_to_datetime(raw_date)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _after_timestamp(db_path: str, digest_id: str, source_key: str, lookback_hours: int) -> int:
    watermark = get_watermark(db_path, digest_id, source_key)
    if watermark and watermark.get("last_fetched"):
        return _timestamp_from_iso(str(watermark["last_fetched"]))
    boundary = datetime.now(UTC) - timedelta(hours=lookback_hours)
    return int(boundary.timestamp())


def _timestamp_from_iso(value: str) -> int:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _newer_message_marker(
    *,
    latest_at: str | None,
    latest_id: str | None,
    message_at: str | None,
    message_id: str,
) -> tuple[str | None, str | None]:
    if not message_at:
        return (latest_at, latest_id) if latest_at else (utc_now(), message_id)
    if not latest_at or _timestamp_from_iso(message_at) > _timestamp_from_iso(latest_at):
        return message_at, message_id
    return latest_at, latest_id


def _list_messages(service: Any, query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        kwargs = {"userId": "me", "q": query}
        if limit is not None:
            kwargs["maxResults"] = max(1, min(limit - len(messages), 100))
        if page_token:
            kwargs["pageToken"] = page_token
        request = service.users().messages().list(**kwargs)
        response = request.execute()
        messages.extend(response.get("messages", []))
        if limit is not None and len(messages) >= limit:
            return messages[:limit]
        page_token = response.get("nextPageToken")
        if not page_token:
            return messages


def _get_message(service: Any, message_id: str) -> dict[str, Any]:
    return service.users().messages().get(userId="me", id=message_id, format="full").execute()


def _walk_mime_parts(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in part.get("parts", []) or []:
        parts.extend(_walk_mime_parts(child))
    return parts


def _decode_body_data(part: dict[str, Any]) -> str:
    raw_data = part.get("body", {}).get("data")
    if not raw_data:
        return ""
    padded = raw_data + "=" * (-len(raw_data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return ""


def _clean_text(value: str) -> str:
    stripped = re.sub(r"[ \t]+", " ", value)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def _link_context(anchor: Any) -> str:
    parent = anchor.find_parent(["p", "li", "td", "div", "section", "article"]) or anchor.parent
    if parent is None or getattr(parent, "name", "") in {"[document]", "body", "html"}:
        return _clean_text(anchor.get_text(" ", strip=True))
    context = _clean_text(parent.get_text(" ", strip=True))
    if len(context) <= 700:
        return context
    return f"{context[:699].rstrip()}..."


def _keep_newsletter_link(url: str, text: str) -> bool:
    text_key = text.lower()
    utility_phrases = (
        "advertise",
        "archive",
        "community ai workflows",
        "email preferences",
        "follow on",
        "highlights: news, guides & events",
        "join the ai university",
        "manage preferences",
        "manage subscription",
        "privacy policy",
        "sign up",
        "subscribe",
        "terms of",
        "trending ai tools",
        "unsubscribe",
        "update preferences",
        "view in browser",
    )
    if any(phrase in text_key for phrase in utility_phrases):
        return False

    parsed = urlparse(url)
    path = parsed.path.lower()
    if path.endswith((".gif", ".jpg", ".jpeg", ".png", ".webp", ".svg")):
        return False
    if any(marker in url.lower() for marker in ("/unsubscribe", "unsubscribe=", "manage-preferences")):
        return False
    return True


def _query_terms(value: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9+.-]{1,}", value.lower())
    blocked = {
        "and",
        "are",
        "days",
        "emails",
        "from",
        "last",
        "newsletter",
        "newsletters",
        "received",
        "the",
        "this",
        "week",
        "with",
    }
    terms: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in blocked or word.isdigit() or word in seen:
            continue
        terms.append(word)
        seen.add(word)
    return terms


def _looks_like_newsletter(sender: str, subject: str, payload: dict[str, Any]) -> bool:
    haystack = " ".join([sender, subject, extract_plain_text(payload)[:900]]).lower()
    markers = (
        "newsletter",
        "digest",
        "roundup",
        "weekly",
        "daily",
        "brief",
        "substack",
        "beehiiv",
        "view in browser",
        "unsubscribe",
        "read more",
    )
    return any(marker in haystack for marker in markers)


def _latest_iso(left: str | None, right: str | None) -> str | None:
    if not left:
        return right
    if not right:
        return left
    try:
        return left if _timestamp_from_iso(left) >= _timestamp_from_iso(right) else right
    except (TypeError, ValueError):
        return left


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None
