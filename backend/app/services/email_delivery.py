from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from backend.agents.digestor.gmail import SCOPES
from backend.app.core.config import Settings, get_settings
from backend.app.db import database

logger = logging.getLogger(__name__)

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def delivery_capability(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    token_scopes = gmail_token_scopes(settings)
    return {
        "gmail_send_ready": SEND_SCOPE in token_scopes,
        "requires_gmail_reconnect": settings.gmail_credentials_path.exists() and SEND_SCOPE not in token_scopes,
        "token_scopes": sorted(token_scopes),
    }


def gmail_token_scopes(settings: Settings | None = None) -> set[str]:
    return _token_scopes(settings or get_settings())


async def deliver_scheduled_digest(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    digest_id = str(run.get("digest_id") or "")
    settings = database.get_delivery_settings(digest_id)
    if not settings.get("enabled") or not settings.get("recipient_email"):
        return None
    return send_latest_digest(digest_id, recipient_email=str(settings["recipient_email"]))


def send_latest_digest(digest_id: str, *, recipient_email: str | None = None) -> dict[str, Any]:
    digest = database.get_digest(digest_id)
    if digest is None:
        return {"status": "not_found", "error": "Digest not found"}
    delivery_settings = database.get_delivery_settings(digest_id)
    recipient = (recipient_email or delivery_settings.get("recipient_email") or "").strip()
    if not recipient:
        database.record_delivery_result(
            digest_id=digest_id,
            status="skipped",
            error="No delivery email configured.",
        )
        return {"status": "skipped", "error": "No delivery email configured."}

    issue = database.get_latest_issue(digest_id)
    if issue is None:
        database.record_delivery_result(
            digest_id=digest_id,
            status="failed",
            error="No completed brief is available to send.",
        )
        return {"status": "failed", "error": "No completed brief is available to send."}

    try:
        service = _gmail_service()
        subject = str(issue.get("title") or digest.get("name") or "Morning Dispatch")
        html = _email_html(database.clean_issue_html_for_display(str(issue.get("html_content") or "")))
        plain_text = _html_to_text(html)
        raw_message = _build_raw_message(
            recipient=recipient,
            subject=subject,
            html=html,
            plain_text=plain_text,
        )
        response = service.users().messages().send(userId="me", body={"raw": raw_message}).execute()
        delivered_at = database.utc_now()
        updated = database.record_delivery_result(
            digest_id=digest_id,
            status="sent",
            delivered_at=delivered_at,
            error=None,
        )
        return {
            "status": "sent",
            "message_id": response.get("id"),
            "recipient_email": recipient,
            "delivered_at": delivered_at,
            "settings": updated,
        }
    except Exception as exc:  # pragma: no cover - Gmail API failures vary by account state.
        logger.warning("Digest email delivery failed for %s: %s", digest_id, exc)
        database.record_delivery_result(
            digest_id=digest_id,
            status="failed",
            error=_delivery_error(exc),
        )
        return {"status": "failed", "recipient_email": recipient, "error": _delivery_error(exc)}


def send_exploration_brief(
    exploration_id: str,
    *,
    recipient_email: str | None = None,
) -> dict[str, Any]:
    exploration = database.get_exploration(exploration_id)
    if exploration is None or exploration.get("deleted_at"):
        return {"status": "not_found", "error": "Exploration not found"}
    recipient = (recipient_email or _default_recipient_email()).strip()
    if not recipient:
        return {"status": "skipped", "error": "No delivery email configured."}
    brief_ref = str(exploration.get("brief_ref") or "")
    if not brief_ref:
        return {"status": "failed", "error": "No completed brief is available to send."}

    try:
        html = Path(brief_ref).read_text(encoding="utf-8")
        topic = database.get_topic_profile(str(exploration.get("topic_id") or "")) or {}
        profile = topic.get("profile") if isinstance(topic.get("profile"), dict) else {}
        subject = _exploration_subject(profile)
        service = _gmail_service()
        email_html = _email_html(database.clean_issue_html_for_display(html))
        plain_text = _html_to_text(email_html)
        raw_message = _build_raw_message(
            recipient=recipient,
            subject=subject,
            html=email_html,
            plain_text=plain_text,
        )
        response = service.users().messages().send(userId="me", body={"raw": raw_message}).execute()
        database.mark_exploration_emailed(exploration_id)
        return {
            "status": "sent",
            "message_id": response.get("id"),
            "recipient_email": recipient,
            "delivered_at": database.utc_now(),
        }
    except OSError:
        return {"status": "failed", "recipient_email": recipient, "error": "Exploration brief file was not found."}
    except Exception as exc:  # pragma: no cover - Gmail API failures vary by account state.
        logger.warning("Exploration email delivery failed for %s: %s", exploration_id, exc)
        return {"status": "failed", "recipient_email": recipient, "error": _delivery_error(exc)}


def _gmail_service() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    settings = get_settings()
    creds = Credentials.from_authorized_user_file(str(settings.gmail_credentials_path), SCOPES)
    if not creds.has_scopes([SEND_SCOPE]):
        raise RuntimeError("Gmail send permission is missing. Reconnect Gmail from Admin.")
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        settings.gmail_credentials_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def _build_raw_message(*, recipient: str, subject: str, html: str, plain_text: str) -> str:
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(plain_text or "Morning Dispatch brief is attached below.")
    message.add_alternative(html, subtype="html")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    return soup.get_text("\n", strip=True)


def _email_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script"]):
        element.decompose()
    for element in soup.select(".feedback-controls"):
        element.decompose()
    return str(soup)


def _default_recipient_email() -> str:
    enabled_settings = database.enabled_delivery_settings()
    if enabled_settings:
        return str(enabled_settings[0].get("recipient_email") or "")
    for digest in database.list_digests():
        delivery_settings = database.get_delivery_settings(str(digest.get("id") or ""))
        recipient = str(delivery_settings.get("recipient_email") or "").strip()
        if recipient:
            return recipient
    return ""


def _exploration_subject(profile: dict[str, Any]) -> str:
    scope = str(profile.get("scope") or profile.get("statement") or "").strip()
    if scope:
        return f"Morning Dispatch Explore: {scope[:100]}"
    return "Morning Dispatch Explore"


def _token_scopes(settings: Settings) -> set[str]:
    try:
        import json

        payload = json.loads(settings.gmail_credentials_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    raw_scopes = payload.get("scopes") or payload.get("scope") or []
    if isinstance(raw_scopes, str):
        return {scope for scope in raw_scopes.split() if scope}
    if isinstance(raw_scopes, list):
        return {str(scope) for scope in raw_scopes if scope}
    return set()


def _delivery_error(exc: Exception) -> str:
    detail = str(exc)
    if "insufficient" in detail.lower() or "permission" in detail.lower():
        return "Gmail send permission is missing. Reconnect Gmail from Admin."
    return detail[:500]
