from __future__ import annotations

import base64
import logging
import quopri
import re
from email.message import EmailMessage
from html import escape as _html_escape
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
    health = gmail_credentials_health(settings)
    has_send_scope = SEND_SCOPE in token_scopes
    gmail_valid = bool(health.get("valid"))
    reason = health.get("reason")
    if gmail_valid and not has_send_scope:
        reason = "Reconnect Gmail in Admin Sources to grant send permission."
    return {
        "gmail_send_ready": gmail_valid and has_send_scope,
        "requires_gmail_reconnect": bool(health.get("requires_reconnect"))
        or (settings.gmail_credentials_path.exists() and not has_send_scope),
        "gmail_send_reason": reason,
        "token_scopes": sorted(token_scopes),
    }


def gmail_token_scopes(settings: Settings | None = None) -> set[str]:
    return _token_scopes(settings or get_settings())


def gmail_credentials_health(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.gmail_client_secret_path.exists():
        return {
            "configured": False,
            "valid": False,
            "requires_reconnect": False,
            "reason": "Upload a Gmail OAuth client in Admin Sources, then connect Gmail.",
        }
    if not settings.gmail_credentials_path.exists():
        return {
            "configured": True,
            "valid": False,
            "requires_reconnect": True,
            "reason": "Finish the Gmail connection in Admin Sources.",
        }
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(settings.gmail_credentials_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            settings.gmail_credentials_path.write_text(creds.to_json(), encoding="utf-8")
        if creds.valid:
            return {
                "configured": True,
                "valid": True,
                "requires_reconnect": False,
                "reason": None,
            }
        return {
            "configured": True,
            "valid": False,
            "requires_reconnect": True,
            "reason": "Reconnect Gmail in Admin Sources. The saved Gmail token is no longer valid.",
        }
    except Exception as exc:
        detail = str(exc)
        if "invalid_grant" in detail or "expired or revoked" in detail:
            reason = "Reconnect Gmail in Admin Sources. Google says the saved token has expired or was revoked."
        else:
            reason = "Reconnect Gmail in Admin Sources. Gmail authentication failed."
        return {
            "configured": True,
            "valid": False,
            "requires_reconnect": True,
            "reason": reason,
        }


async def deliver_scheduled_digest(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    digest_id = str(run.get("digest_id") or "")
    settings = database.get_delivery_settings(digest_id)
    if not settings.get("enabled") or not settings.get("recipient_email"):
        return None
    if settings.get("last_delivery_status") == "failed":
        return {
            "status": "suppressed",
            "error": settings.get("last_error") or "Scheduled email delivery is paused after a failed send.",
        }
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
        subject = str(issue.get("title") or digest.get("name") or "Dispatch")
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


def retry_failed_delivery(topic_id: str) -> dict[str, Any]:
    """Re-attempt a previously failed scheduled/digest email delivery.

    Handles both failure shapes surfaced in the delivery alert: digest delivery
    settings and scheduled topic-profile delivery configs.
    """
    if database.get_digest(topic_id) is not None:
        # send_latest_digest records the new delivery result, clearing the
        # failed state on success.
        return send_latest_digest(topic_id)

    if database.get_topic_profile(topic_id) is None:
        return {"status": "not_found", "error": "No failed delivery found for this brief."}

    latest = database.get_latest_exploration(topic_id=topic_id, mode="scheduled")
    exploration_id = str(latest.get("exploration_id") or "") if latest else ""
    if not exploration_id:
        result: dict[str, Any] = {"status": "failed", "error": "No completed brief is available to send."}
    else:
        result = send_exploration_brief(exploration_id)
    database.record_topic_delivery_result(
        topic_id=topic_id,
        status=str(result.get("status") or "failed"),
        error=result.get("error"),
        delivered_at=result.get("delivered_at"),
    )
    return result


def clear_failed_delivery(topic_id: str) -> dict[str, Any]:
    """Dismiss a failed delivery state so the alert clears and delivery re-enables."""
    cleared = False
    if database.get_digest(topic_id) is not None:
        if database.get_delivery_settings(topic_id).get("last_delivery_status") == "failed":
            database.record_delivery_result(digest_id=topic_id, status="cleared", error=None)
            cleared = True
    if database.get_topic_profile(topic_id) is not None:
        database.update_topic_delivery_config(topic_id, {}, clear_failure=True)
        cleared = True
    if not cleared:
        return {"status": "not_found", "error": "No failed delivery found for this brief."}
    return {"status": "cleared", "topic_id": topic_id}


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


GMAIL_CLIP_BYTES = 102 * 1024


def _build_raw_message(*, recipient: str, subject: str, html: str, plain_text: str) -> str:
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(plain_text or "Dispatch brief is attached below.")
    message.add_alternative(html, subtype="html")
    _warn_if_clip_risk(message)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _warn_if_clip_risk(message: EmailMessage) -> None:
    """Log if the encoded HTML part risks Gmail's ~102 KB clip threshold."""
    try:
        for part in message.walk():
            if part.get_content_type() == "text/html":
                encoded = len(part.as_bytes())
                if encoded > GMAIL_CLIP_BYTES:
                    logger.warning(
                        "Email HTML part is ~%d KB (> Gmail's ~102 KB clip threshold); "
                        "the brief may be truncated in Gmail.",
                        encoded // 1024,
                    )
                return
    except Exception:  # pragma: no cover - guard logging must never break a send
        pass


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

    # --- Email presentation cleanup (email-only; the web view is untouched) ---
    # Remove reader-facing internals that are debug, not content: relevance
    # score badges, keyword chips, the story index number, and the telemetry
    # sidebar ("About this brief": AI tokens/calls/processing time/source mix).
    for sel in (".score", ".keywords", ".story-num", ".brief-sidebar", ".side-panel", ".provenance"):
        for element in soup.select(sel):
            element.decompose()
    # Drop per-card/media thumbnails to clear Gmail's clip budget (the few hero /
    # standings strip images are kept — glanceable and cheap on bytes).
    for sel in (".story-thumb", ".media-thumb", ".fallback-art"):
        for element in soup.select(sel):
            element.decompose()
    # Drop the heavy hidden modal bodies (full foreign translations + originals,
    # video/podcast transcripts) — 20-33 KB each, they blow Gmail's clip limit.
    # Each of those cards keeps its headline, summary, and a direct external
    # source link (rewritten just below). Newsletter modals are EXCLUDED: a
    # newsletter has no external link, so its body is the only content — those
    # are kept and collapsed by the modal->details conversion below.
    for sel in (
        ".foreign-modal",
        ".podcast-modal:not(.newsletter-modal)",
        ".youtube-modal:not(.newsletter-modal)",
        ".translation-original",
    ):
        for element in soup.select(sel):
            element.decompose()
    # Trim the internal "via <discovery title/query>" suffix from each meta line.
    for meta in soup.select(".meta"):
        text = meta.get_text(" ", strip=True)
        trimmed = re.split(r"\s*·\s*via\b", text, maxsplit=1)[0].strip(" ·")
        if trimmed and trimmed != text:
            meta.clear()
            meta.append(trimmed)

    # Convert YouTube modal links to direct links for email
    for a_tag in soup.find_all("a", attrs={"data-youtube-url": True}):
        yt_url = a_tag["data-youtube-url"]
        a_tag["href"] = yt_url
        a_tag["target"] = "_blank"
        a_tag["rel"] = "noreferrer"
        del a_tag["data-youtube-url"]
        if "data-youtube-modal-target" in a_tag.attrs:
            del a_tag["data-youtube-modal-target"]

    # Convert Podcast modal links to direct links for email
    for a_tag in soup.find_all("a", attrs={"data-podcast-url": True}):
        podcast_url = a_tag["data-podcast-url"]
        a_tag["href"] = podcast_url
        a_tag["target"] = "_blank"
        a_tag["rel"] = "noreferrer"
        del a_tag["data-podcast-url"]
        if "data-podcast-modal-target" in a_tag.attrs:
            del a_tag["data-podcast-modal-target"]

    # Convert foreign article modal links to direct original source links for email
    for a_tag in soup.find_all("a", attrs={"data-foreign-url": True}):
        foreign_url = a_tag["data-foreign-url"]
        a_tag["href"] = foreign_url
        a_tag["target"] = "_blank"
        a_tag["rel"] = "noreferrer"
        keys_to_del = [k for k in a_tag.attrs if k.startswith("data-foreign-")]
        for k in keys_to_del:
            del a_tag[k]

    # Transform modals into email-safe details/summary blocks instead of decomposing them
    for modal in soup.select(".podcast-modal, .youtube-modal"):
        modal_id = modal.get("id", "")
        classes = modal.get("class", [])
        if isinstance(classes, str):
            classes = [classes]

        if "foreign-modal" in classes or modal_id.startswith("foreign-"):
            # 1. Foreign article translation modal
            kicker = modal.select_one(".section-kicker")
            kicker_text = kicker.get_text(strip=True) if kicker else "Translation"

            title = modal.select_one("h3")
            title_text = title.get_text(strip=True) if title else ""

            ext_link = modal.select_one("a[data-external-source]")
            ext_url = ext_link.get("href", "") if ext_link else ""

            provenance = modal.select_one(".foreign-provenance")
            provenance_text = provenance.get_text(strip=True) if provenance else ""

            status = modal.select_one(".foreign-status")
            status_text = status.get_text(strip=True) if status else ""

            translated_body_div = modal.select_one(".foreign-view[data-foreign-view='translated'] .foreign-body")
            translated_body_html = ""
            if translated_body_div:
                translated_body_html = "".join(str(c) for c in translated_body_div.contents)

            original_body_div = modal.select_one(".foreign-view[data-foreign-view='original'] .foreign-body")
            original_body_html = ""
            if original_body_div:
                original_body_html = "".join(str(c) for c in original_body_div.contents)

            details_html = f"""
            <details id="{modal_id}" style="margin-top: 12px; margin-bottom: 12px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #f8fafc; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
              <summary style="cursor: pointer; padding: 12px 16px; font-weight: 600; color: #1e293b; background-color: #f1f5f9; border-bottom: 1px solid #e2e8f0; outline: none; list-style: none;">
                <span style="display: inline-flex; align-items: center;">
                  <span style="margin-right: 8px;">🌐</span> Read Translation &amp; Original Text
                </span>
              </summary>
              <div style="padding: 16px;">
                <div style="font-size: 12px; color: #64748b; margin-bottom: 4px;">{provenance_text or kicker_text}</div>
                <h4 style="margin: 0 0 12px 0; font-size: 16px; color: #0f172a; font-weight: 600;">{title_text}</h4>
                {f'<div style="margin-bottom: 16px;"><a href="{ext_url}" target="_blank" rel="noopener noreferrer" style="display: inline-block; font-size: 13px; color: #2563eb; text-decoration: none; font-weight: 500;">View original source &rarr;</a></div>' if ext_url else ''}
                {f'<p style="font-size: 12px; color: #64748b; margin-bottom: 12px; font-style: italic;">{status_text}</p>' if status_text else ''}
                <div style="background-color: #ffffff; padding: 16px; border: 1px solid #e2e8f0; border-radius: 6px; margin-bottom: 16px;">
                  <h5 style="margin: 0 0 8px 0; font-size: 12px; color: #475569; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;">English Translation</h5>
                  <div style="font-size: 14px; color: #1e293b; line-height: 1.6;">{translated_body_html}</div>
                </div>
                {f'<div style="background-color: #f1f5f9; padding: 16px; border: 1px solid #e2e8f0; border-radius: 6px;"><h5 style="margin: 0 0 8px 0; font-size: 12px; color: #475569; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;">Original Text</h5><div style="font-size: 14px; color: #334155; line-height: 1.6;">{original_body_html}</div></div>' if original_body_html else ''}
              </div>
            </details>
            """
            new_node = BeautifulSoup(details_html, "html.parser").find()
            modal.replace_with(new_node)

        elif "newsletter-modal" in classes or modal_id.startswith("newsletter-"):
            # 2. Newsletter modal
            kicker = modal.select_one(".section-kicker")
            kicker_text = kicker.get_text(strip=True) if kicker else "Newsletter"

            title = modal.select_one("h3")
            title_text = title.get_text(strip=True) if title else ""

            body_div = modal.select_one(".newsletter-body")
            body_html = ""
            if body_div:
                h4 = body_div.select_one("h4")
                if h4:
                    h4.decompose()
                body_html = "".join(str(c) for c in body_div.contents)

            details_html = f"""
            <details id="{modal_id}" style="margin-top: 12px; margin-bottom: 12px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #f8fafc; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
              <summary style="cursor: pointer; padding: 12px 16px; font-weight: 600; color: #1e293b; background-color: #f1f5f9; border-bottom: 1px solid #e2e8f0; outline: none; list-style: none;">
                <span style="display: inline-flex; align-items: center;">
                  <span style="margin-right: 8px;">✉️</span> Read Newsletter Content
                </span>
              </summary>
              <div style="padding: 16px;">
                <div style="font-size: 12px; color: #64748b; margin-bottom: 4px;">{kicker_text}</div>
                <h4 style="margin: 0 0 12px 0; font-size: 16px; color: #0f172a; font-weight: 600;">{title_text}</h4>
                <div style="font-size: 14px; color: #1e293b; line-height: 1.6; background-color: #ffffff; padding: 16px; border: 1px solid #e2e8f0; border-radius: 6px;">
                  {body_html}
                </div>
              </div>
            </details>
            """
            new_node = BeautifulSoup(details_html, "html.parser").find()
            modal.replace_with(new_node)

        elif "youtube-modal" in classes or modal_id.startswith("youtube-") or modal.select_one(".youtube-player"):
            # 3. YouTube video modal
            kicker = modal.select_one(".section-kicker")
            kicker_text = kicker.get_text(strip=True) if kicker else "YouTube Video"

            meta_div = modal.select_one(".meta")
            meta_text = meta_div.get_text(strip=True) if meta_div else kicker_text

            title = modal.select_one("h3")
            title_text = title.get_text(strip=True) if title else ""

            actions = modal.select_one(".podcast-actions")
            actions_html = ""
            if actions:
                for a_tag in actions.find_all("a"):
                    a_tag["style"] = "display: inline-block; font-size: 13px; color: #2563eb; text-decoration: none; font-weight: 500; margin-right: 12px;"
                actions_html = "".join(str(c) for c in actions.contents)

            transcript_section = modal.select_one(".youtube-transcript, .podcast-transcript")
            transcript_html = ""
            if transcript_section:
                h4 = transcript_section.select_one("h4")
                if h4:
                    h4.decompose()
                transcript_html = "".join(str(c) for c in transcript_section.contents)

            details_html = f"""
            <details id="{modal_id}" style="margin-top: 12px; margin-bottom: 12px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #f8fafc; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
              <summary style="cursor: pointer; padding: 12px 16px; font-weight: 600; color: #1e293b; background-color: #f1f5f9; border-bottom: 1px solid #e2e8f0; outline: none; list-style: none;">
                <span style="display: inline-flex; align-items: center;">
                  <span style="margin-right: 8px;">🎥</span> View Transcript &amp; Details
                </span>
              </summary>
              <div style="padding: 16px;">
                <div style="font-size: 12px; color: #64748b; margin-bottom: 4px;">{meta_text}</div>
                <h4 style="margin: 0 0 12px 0; font-size: 16px; color: #0f172a; font-weight: 600;">{title_text}</h4>
                {f'<div style="margin-bottom: 16px;">{actions_html}</div>' if actions_html else ''}
                {f'<div style="border-top: 1px solid #e2e8f0; padding-top: 16px;"><h5 style="margin: 0 0 8px 0; font-size: 13px; color: #475569; font-weight: 600;">Video Transcript</h5><div style="font-size: 13px; color: #475569; line-height: 1.6; max-height: 250px; overflow-y: auto; background: #ffffff; padding: 12px; border: 1px solid #e2e8f0; border-radius: 6px;">{transcript_html}</div></div>' if transcript_html else ''}
              </div>
            </details>
            """
            new_node = BeautifulSoup(details_html, "html.parser").find()
            modal.replace_with(new_node)

        else:
            # 4. Podcast episode modal
            art = modal.select_one(".podcast-art")
            if art:
                art["style"] = "width: 64px; height: 64px; border-radius: 6px; object-fit: cover;"
                if "class" in art.attrs:
                    del art["class"]
            art_html = str(art) if art else ""
            if not art:
                fallback_art = modal.select_one(".podcast-art.fallback")
                if fallback_art:
                    fallback_art["style"] = "width: 64px; height: 64px; border-radius: 6px; background-color: #e2e8f0; color: #475569; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 18px;"
                    art_html = str(fallback_art)

            meta_div = modal.select_one(".meta")
            meta_text = meta_div.get_text(strip=True) if meta_div else "Podcast Episode"

            title = modal.select_one("h3")
            title_text = title.get_text(strip=True) if title else ""

            actions = modal.select_one(".podcast-actions")
            actions_html = ""
            if actions:
                for a_tag in actions.find_all("a"):
                    a_tag["style"] = "display: inline-block; font-size: 13px; color: #2563eb; text-decoration: none; font-weight: 500; margin-right: 12px;"
                actions_html = "".join(str(c) for c in actions.contents)

            summary = modal.select_one(".podcast-summary")
            summary_html = ""
            if summary:
                h4 = summary.select_one("h4")
                if h4:
                    h4.decompose()
                summary_html = f'<div style="margin-bottom: 16px;"><h5 style="margin: 0 0 6px 0; font-size: 13px; color: #475569; font-weight: 600;">Summary</h5><div style="font-size: 14px; color: #334155; line-height: 1.5;">{"".join(str(c) for c in summary.contents)}</div></div>'

            audio = modal.select_one("audio")
            audio_html = ""
            if audio:
                audio["style"] = "width: 100%; margin-top: 8px; margin-bottom: 16px;"
                audio_html = str(audio)

            transcript_section = modal.select_one(".podcast-transcript")
            transcript_html = ""
            if transcript_section:
                h4 = transcript_section.select_one("h4")
                if h4:
                    h4.decompose()
                transcript_html = "".join(str(c) for c in transcript_section.contents)

            details_html = f"""
            <details id="{modal_id}" style="margin-top: 12px; margin-bottom: 12px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #f8fafc; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
              <summary style="cursor: pointer; padding: 12px 16px; font-weight: 600; color: #1e293b; background-color: #f1f5f9; border-bottom: 1px solid #e2e8f0; outline: none; list-style: none;">
                <span style="display: inline-flex; align-items: center;">
                  <span style="margin-right: 8px;">🎧</span> Listen &amp; View Details
                </span>
              </summary>
              <div style="padding: 16px;">
                <div style="display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; align-items: center;">
                  {art_html}
                  <div style="flex: 1; min-width: 200px;">
                    <div style="font-size: 12px; color: #64748b; margin-bottom: 4px;">{meta_text}</div>
                    <h4 style="margin: 0 0 8px 0; font-size: 16px; color: #0f172a; font-weight: 600;">{title_text}</h4>
                    {f'<div style="margin-top: 6px;">{actions_html}</div>' if actions_html else ''}
                  </div>
                </div>
                {audio_html}
                {summary_html}
                {f'<div style="border-top: 1px solid #e2e8f0; padding-top: 16px;"><h5 style="margin: 0 0 8px 0; font-size: 13px; color: #475569; font-weight: 600;">Transcript</h5><div style="font-size: 13px; color: #475569; line-height: 1.6; max-height: 250px; overflow-y: auto; background: #ffffff; padding: 12px; border: 1px solid #e2e8f0; border-radius: 6px;">{transcript_html}</div></div>' if transcript_html else ''}
              </div>
            </details>
            """
            new_node = BeautifulSoup(details_html, "html.parser").find()
            modal.replace_with(new_node)

    # Replace the web stylesheet(s) and web fonts with one lean, email-safe,
    # single-column, iPhone-first stylesheet.
    for tag in soup.find_all(["style", "link"]):
        tag.decompose()
    head = soup.find("head")
    if head is None:
        head = soup.new_tag("head")
        (soup.find("html") or soup).insert(0, head)
    if not head.find("meta", attrs={"name": "viewport"}):
        viewport = soup.new_tag("meta")
        viewport.attrs["name"] = "viewport"
        viewport.attrs["content"] = "width=device-width, initial-scale=1"
        head.append(viewport)
    style_tag = soup.new_tag("style")
    style_tag.string = _LEAN_EMAIL_CSS
    head.append(style_tag)

    # Drop inline styles left on original content so the lean stylesheet fully
    # controls presentation; the generated <details> blocks keep their own.
    for element in soup.select("[style]"):
        if element.name == "details" or element.find_parent("details"):
            continue
        del element["style"]

    # "Back to top" anchor + a tappable section index for navigating a long,
    # all-in-one brief on a phone.
    shell = soup.select_one(".brief-shell") or soup.body
    if shell is not None and not shell.get("id"):
        shell["id"] = "brief-top"
    _insert_section_index(soup)

    # Reader footer. This brief is a personal self-send, so identity only — no
    # bulk-mail unsubscribe machinery is required.
    _append_email_footer(soup)

    # Base colors inline so the brief survives a stripped <style> in strict clients.
    if soup.body is not None:
        soup.body["style"] = "margin:0;padding:0;background-color:#ffffff;color:#1a1a1a;"

    # Shorten summaries ONLY as much as needed to clear Gmail's clip limit; full
    # summaries whenever they fit. Every story, headline, and source link is kept.
    return _fit_email_size(soup)


# Stay safely under Gmail's ~102 KB clip threshold (encoded), leaving headroom.
GMAIL_CLIP_SAFE_BYTES = 96 * 1024


def _qp_len(html: str) -> int:
    """Approximate the quoted-printable-encoded byte length Gmail clips on."""
    return len(quopri.encodestring(html.encode("utf-8")))


def _clip_text(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    cut = text[:cap].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return (cut or text[:cap]) + "…"


def _fit_email_size(soup: BeautifulSoup) -> str:
    """Render to HTML, progressively trimming summaries until under the clip cap."""
    html_str = _resolve_css_variables(str(soup))
    if _qp_len(html_str) <= GMAIL_CLIP_SAFE_BYTES:
        return html_str
    summaries = [(el, el.get_text(" ", strip=True)) for el in soup.select(".story-summary, .lead-summary")]
    if not summaries:
        return html_str
    for cap in (400, 300, 220, 160, 120):
        for el, original in summaries:
            el.clear()
            el.append(_clip_text(original, cap))
        html_str = _resolve_css_variables(str(soup))
        if _qp_len(html_str) <= GMAIL_CLIP_SAFE_BYTES:
            break
    return html_str


_LEAN_EMAIL_CSS = """
body{margin:0;padding:0;background-color:#ffffff;color:#1a1a1a;-webkit-text-size-adjust:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}
.brief-shell{max-width:600px;margin:0 auto;padding:20px 16px 36px;}
.brief-masthead{display:block;border-bottom:2px solid #1a1a1a;padding-bottom:10px;margin-bottom:14px;}
.masthead-brand{font-family:Georgia,'Times New Roman',serif;font-size:22px;font-weight:bold;line-height:1.1;}
.masthead-meta,.dateline{font-size:12px;color:#6b6b66;text-align:left;margin-top:4px;}
.brief-header{display:block;margin:0 0 16px;}
h1{font-family:Georgia,'Times New Roman',serif;font-size:24px;line-height:1.2;margin:0 0 10px;font-weight:bold;}
h2{font-family:Georgia,'Times New Roman',serif;font-size:19px;line-height:1.25;margin:26px 0 6px;font-weight:bold;}
h3{font-family:Georgia,'Times New Roman',serif;font-size:17px;line-height:1.3;margin:0;font-weight:bold;}
.section-kicker{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#6b6b66;}
.brief-body{display:block;}
.story-column{display:block;width:100%;}
.brief-sidebar{display:none;}
.source-section,.media-section{margin-bottom:6px;}
.story-list{display:block;}
.story-row,.media-card,.low-conf-row{display:block;border-top:1px solid #eaeae5;padding:14px 0;}
.story-copy,.lead-content{display:block;width:100%;}
.story-meta{font-size:12px;color:#6b6b66;margin-bottom:5px;}
.source-type{display:inline-block;font-size:11px;font-weight:bold;color:#1e3a8a;text-transform:uppercase;letter-spacing:.04em;margin-right:6px;}
.meta{font-size:12px;color:#6b6b66;}
.story-title,.media-title{margin:2px 0 5px;}
.story-title a,.media-title a,h3 a{color:#1e3a8a;text-decoration:none;font-weight:bold;display:inline-block;padding:2px 0;}
.story-summary,.lead-summary{font-size:15px;line-height:1.5;color:#333330;margin:4px 0 0;}
.lead-block{display:block;border-top:1px solid #eaeae5;padding:14px 0;}
.lead-title{font-family:Georgia,'Times New Roman',serif;font-size:20px;line-height:1.25;font-weight:bold;}
.media-cta,.media-card a.media-cta{display:inline-block;margin-top:8px;font-size:13px;font-weight:bold;color:#1e3a8a;text-decoration:none;}
a{color:#1e3a8a;}
img{max-width:100%;height:auto;border-radius:6px;}
.img-strip{display:block;}
.strip-frame{display:block;margin:0 0 10px;}
.email-index{margin:0 0 18px;padding:14px 16px;background:#f5f5f0;border-radius:8px;}
.email-index .section-kicker{display:block;margin-bottom:9px;}
.email-index a{display:inline-block;font-size:13px;font-weight:bold;color:#1e3a8a;text-decoration:none;background:#ffffff;border:1px solid #e2e2dc;border-radius:999px;padding:8px 13px;margin:0 6px 8px 0;}
.sec-top{float:right;font-size:12px;font-weight:normal;color:#1e3a8a;text-decoration:none;}
.email-foot{margin-top:26px;padding-top:14px;border-top:1px solid #eaeae5;font-size:12px;color:#8a877f;line-height:1.6;}
.translation-badge{display:inline-block;font-size:11px;color:#0f6e56;margin-left:4px;}
@media (max-width:480px){.brief-shell{padding:16px 14px 32px;}h1{font-size:22px;}h2{font-size:18px;}}
@media (prefers-color-scheme:dark){body{background-color:#111111 !important;color:#ededed !important;}.story-summary,.lead-summary{color:#d6d6d6 !important;}.brief-masthead{border-bottom-color:#ededed !important;}.story-row,.media-card,.low-conf-row,.lead-block,.email-foot{border-top-color:#333333 !important;}.email-index{background:#1c1c1c !important;}.email-index a{background:#222222 !important;color:#9bb7e8 !important;border-color:#333333 !important;}.story-title a,.media-title a,h3 a,a,.source-type,.sec-top{color:#9bb7e8 !important;}}
""".strip()


def _insert_section_index(soup: BeautifulSoup) -> None:
    """Build a tappable jump-link index from the brief's section <h2> headers."""
    headings = [h for h in soup.find_all("h2") if h.get_text(strip=True)]
    if len(headings) < 2:
        return
    links: list[str] = []
    for h2 in headings:
        label = h2.get_text(" ", strip=True)
        section_id = h2.get("id")
        if not section_id:
            section_id = "sec-" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
            h2["id"] = section_id
        section = h2.find_parent(class_=re.compile(r"source-section|media-section")) or h2.parent
        count = len(section.select(".story-row, .media-card, .low-conf-row")) if section else 0
        suffix = f" {count}" if count else ""
        links.append(f'<a href="#{_html_escape(section_id, quote=True)}">{_html_escape(label)}{suffix}</a>')
        back = soup.new_tag("a", href="#brief-top")
        back["class"] = "sec-top"
        back.string = "↑ top"
        h2.append(back)
    nav_html = (
        '<div class="email-index"><span class="section-kicker">In this brief</span>'
        + "".join(links)
        + "</div>"
    )
    nav = BeautifulSoup(nav_html, "html.parser")
    anchor = soup.select_one(".brief-header") or soup.select_one(".brief-masthead")
    if anchor is not None:
        anchor.insert_after(nav)
        return
    target = soup.select_one(".story-column") or soup.body
    if target is not None:
        target.insert(0, nav)


def _append_email_footer(soup: BeautifulSoup) -> None:
    foot = BeautifulSoup(
        '<div class="email-foot">Morning Dispatch · your personal intelligence brief</div>',
        "html.parser",
    )
    shell = soup.select_one(".brief-shell") or soup.body
    if shell is not None:
        shell.append(foot)


CSS_VARIABLES = {
    "--paper": "#ffffff",
    "--paper-deep": "#fafaf9",
    "--ink": "#1a1a1a",
    "--muted": "#6b6b66",
    "--line": "#eaeae5",
    "--accent": "#1e3a8a",
    "--accent-dark": "#172554",
    "--sidebar": "#f5f5f0",
    "--shadow": "0 12px 40px rgba(0, 0, 0, .04)",
    "--display": "'Playfair Display', Georgia, serif",
    "--body": "'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "--mono": "'JetBrains Mono', monospace",
}


def _resolve_css_variables(html: str) -> str:
    import re
    for var_name, var_value in CSS_VARIABLES.items():
        pattern = re.compile(rf"var\(\s*{re.escape(var_name)}\s*\)", re.IGNORECASE)
        html = pattern.sub(var_value, html)
    return html


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
        return f"Dispatch Explore: {scope[:100]}"
    return "Dispatch Explore"


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
    if "invalid_grant" in detail or "expired or revoked" in detail:
        return "Reconnect Gmail in Admin Sources. Google says the saved token has expired or was revoked."
    if "insufficient" in detail.lower() or "permission" in detail.lower():
        return "Gmail send permission is missing. Reconnect Gmail from Admin."
    return detail[:500]
