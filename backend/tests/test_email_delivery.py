from __future__ import annotations

from types import SimpleNamespace

from backend.app.services import email_delivery


def test_delivery_capability_requires_valid_gmail_credentials(monkeypatch, tmp_path) -> None:
    credentials_path = tmp_path / "gmail_credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    settings = SimpleNamespace(gmail_credentials_path=credentials_path)

    monkeypatch.setattr(email_delivery, "gmail_token_scopes", lambda _settings: {email_delivery.SEND_SCOPE})
    monkeypatch.setattr(
        email_delivery,
        "gmail_credentials_health",
        lambda _settings: {
            "configured": True,
            "valid": False,
            "requires_reconnect": True,
            "reason": "Reconnect Gmail in Admin Sources. Google says the saved token has expired or was revoked.",
        },
    )

    capability = email_delivery.delivery_capability(settings)

    assert capability["gmail_send_ready"] is False
    assert capability["requires_gmail_reconnect"] is True
    assert capability["gmail_send_reason"] == (
        "Reconnect Gmail in Admin Sources. Google says the saved token has expired or was revoked."
    )
    assert capability["token_scopes"] == [email_delivery.SEND_SCOPE]


def test_delivery_capability_requires_send_scope(monkeypatch, tmp_path) -> None:
    credentials_path = tmp_path / "gmail_credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    settings = SimpleNamespace(gmail_credentials_path=credentials_path)

    monkeypatch.setattr(email_delivery, "gmail_token_scopes", lambda _settings: {"https://www.googleapis.com/auth/gmail.readonly"})
    monkeypatch.setattr(
        email_delivery,
        "gmail_credentials_health",
        lambda _settings: {
            "configured": True,
            "valid": True,
            "requires_reconnect": False,
            "reason": None,
        },
    )

    capability = email_delivery.delivery_capability(settings)

    assert capability["gmail_send_ready"] is False
    assert capability["requires_gmail_reconnect"] is True
    assert capability["gmail_send_reason"] == "Reconnect Gmail in Admin Sources to grant send permission."


def test_delivery_error_normalizes_revoked_gmail_token() -> None:
    error = email_delivery._delivery_error(RuntimeError("invalid_grant: Token has been expired or revoked."))

    assert error == "Reconnect Gmail in Admin Sources. Google says the saved token has expired or was revoked."
