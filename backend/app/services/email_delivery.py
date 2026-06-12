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

    return _resolve_css_variables(str(soup))


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
    if "invalid_grant" in detail or "expired or revoked" in detail:
        return "Reconnect Gmail in Admin Sources. Google says the saved token has expired or was revoked."
    if "insufficient" in detail.lower() or "permission" in detail.lower():
        return "Gmail send permission is missing. Reconnect Gmail from Admin."
    return detail[:500]
