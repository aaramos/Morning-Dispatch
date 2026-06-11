"""Unit tests for the shared date_text leaf parser and the modules migrated onto it.

Covers the M5 consolidation additions: English month names, ISO 8601 with/without
``Z``, RFC 2822, URL-embedded dates, and the datetime-returning helpers — plus
behavior-preservation checks for the migrated call sites in markets, gmail,
gmail_mcp_client, editor, and scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.agents.librarian.date_text import (
    date_from_url,
    normalize_date_string,
    parse_datetime,
    parse_iso_datetime,
    parse_rfc2822_datetime,
)


# --- English month names ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("June 10, 2026", "2026-06-10"),
        ("Jun 10, 2026", "2026-06-10"),
        ("10 June 2026", "2026-06-10"),
        ("10 Jun 2026", "2026-06-10"),
        ("Sept 3, 2025", "2025-09-03"),
        ("February 28, 2026", "2026-02-28"),
    ],
)
def test_normalize_date_string_english_months(raw: str, expected: str) -> None:
    assert normalize_date_string(raw) == expected


# --- ISO 8601 variants -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-06-10T12:00:00Z", datetime(2026, 6, 10, 12, tzinfo=UTC)),
        ("2026-06-10T12:00:00+00:00", datetime(2026, 6, 10, 12, tzinfo=UTC)),
        # Naive datetimes are assumed UTC.
        ("2026-06-10T12:00:00", datetime(2026, 6, 10, 12, tzinfo=UTC)),
        # Non-UTC offsets normalize to the same instant in UTC.
        ("2026-06-10T12:00:00+05:30", datetime(2026, 6, 10, 6, 30, tzinfo=UTC)),
        # Date-only strings become midnight UTC.
        ("2026-06-10", datetime(2026, 6, 10, tzinfo=UTC)),
        # Space-separated datetimes are valid ISO for fromisoformat.
        ("2026-06-10 12:00:00", datetime(2026, 6, 10, 12, tzinfo=UTC)),
    ],
)
def test_parse_iso_datetime_variants(raw: str, expected: datetime) -> None:
    parsed = parse_iso_datetime(raw)
    assert parsed == expected
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize("raw", ["", None, "not a date", "Tue, 09 Jun 2026 22:15:00 GMT"])
def test_parse_iso_datetime_rejects_non_iso(raw: object) -> None:
    assert parse_iso_datetime(raw) is None


# --- RFC 2822 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Tue, 09 Jun 2026 22:15:00 GMT", datetime(2026, 6, 9, 22, 15, tzinfo=UTC)),
        ("Tue, 09 Jun 2026 22:15:00 -0700", datetime(2026, 6, 10, 5, 15, tzinfo=UTC)),
        # The -0000 "unknown zone" convention yields a naive datetime: assume UTC.
        ("09 Jun 2026 22:15:00 -0000", datetime(2026, 6, 9, 22, 15, tzinfo=UTC)),
        ("Tue, 09 Jun 2026 22:15:00", datetime(2026, 6, 9, 22, 15, tzinfo=UTC)),
    ],
)
def test_parse_rfc2822_datetime(raw: str, expected: datetime) -> None:
    parsed = parse_rfc2822_datetime(raw)
    assert parsed == expected
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize("raw", ["", None, "garbage", "2026-06-10T12:00:00Z"])
def test_parse_rfc2822_datetime_rejects_non_rfc(raw: object) -> None:
    assert parse_rfc2822_datetime(raw) is None


# --- URL-embedded dates ----------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/2026/06/10/some-story", "2026-06-10"),
        ("https://example.com/2026/06/10", "2026-06-10"),
        ("https://example.com/2026/6/3/story", "2026-06-03"),
        ("https://example.com/news/2026-06-04-headline", "2026-06-04"),
        ("https://example.com/posts/2026-06-04", "2026-06-04"),
    ],
)
def test_date_from_url_extracts_full_dates(url: str, expected: str) -> None:
    assert date_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "https://example.com/story/12345",
        # Year/month-only paths are archive pages, not article dates.
        "https://example.com/2026/06/",
        # Digit runs that merely contain a plausible date must not match.
        "https://example.com/p/1024x768/photo",
        "https://example.com/id/202606105555",
        # Out-of-range month/day.
        "https://example.com/2026/13/10/story",
        "https://example.com/news/2026-06-32-headline",
    ],
)
def test_date_from_url_rejects_non_dates(url: object) -> None:
    assert date_from_url(url) is None


# --- parse_datetime superset -----------------------------------------------------


def test_parse_datetime_passthrough_and_epoch() -> None:
    aware = datetime(2026, 6, 10, 8, tzinfo=UTC)
    assert parse_datetime(aware) == aware
    naive = datetime(2026, 6, 10, 8)
    assert parse_datetime(naive) == aware
    assert parse_datetime(int(aware.timestamp())) == aware
    assert parse_datetime(aware.timestamp()) == aware


def test_parse_datetime_string_formats() -> None:
    assert parse_datetime("2026-06-10T12:00:00Z") == datetime(2026, 6, 10, 12, tzinfo=UTC)
    assert parse_datetime("Tue, 09 Jun 2026 22:15:00 GMT") == datetime(2026, 6, 9, 22, 15, tzinfo=UTC)
    # Locale text forms fall back through normalize_date_string (midnight UTC).
    assert parse_datetime("3 de marzo de 2025") == datetime(2025, 3, 3, tzinfo=UTC)
    assert parse_datetime("June 10, 2026") == datetime(2026, 6, 10, tzinfo=UTC)
    assert parse_datetime("Published 2026-01-27 by staff") == datetime(2026, 1, 27, tzinfo=UTC)
    assert parse_datetime("no date here") is None
    assert parse_datetime("") is None
    assert parse_datetime(None) is None
    assert parse_datetime(True) is None


def test_parse_datetime_relative_semantics() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    assert parse_datetime("posted 2 days ago", now=now) == datetime(2026, 6, 8, tzinfo=UTC)
    # Body-text scans must not turn page chrome into a publish date.
    assert parse_datetime("posted 2 days ago", allow_relative=False, now=now) is None


# --- migrated call sites keep their behavior --------------------------------------


def test_markets_published_at_parses_epoch_and_iso() -> None:
    from backend.agents.discovery.markets import _published_at

    instant = datetime(2026, 6, 10, 12, tzinfo=UTC)
    assert _published_at({"providerPublishTime": int(instant.timestamp())}) == instant
    assert _published_at({"pubDate": "2026-06-10T12:00:00Z"}) == instant
    assert _published_at({"published_at": "2026-06-10T12:00:00"}) == instant
    assert _published_at({"pubDate": "garbage"}) is None
    assert _published_at({}) is None


def test_gmail_message_published_at_rfc2822_header() -> None:
    from backend.agents.digestor.gmail import message_published_at

    message = {"payload": {"headers": [{"name": "Date", "value": "Tue, 09 Jun 2026 22:15:00 -0700"}]}}
    assert message_published_at(message) == "2026-06-10T05:15:00+00:00"
    assert message_published_at({"payload": {"headers": [{"name": "Date", "value": "junk"}]}}) is None
    assert message_published_at({"payload": {}}) is None


def test_gmail_timestamp_from_iso_still_raises_on_garbage() -> None:
    from backend.agents.digestor.gmail import _timestamp_from_iso

    assert _timestamp_from_iso("2026-06-10T12:00:00Z") == _timestamp_from_iso("2026-06-10T12:00:00")
    with pytest.raises(ValueError):
        _timestamp_from_iso("garbage")


def test_gmail_latest_iso_survives_bad_values() -> None:
    from backend.agents.digestor.gmail import _latest_iso

    assert _latest_iso("2026-06-10T12:00:00Z", "2026-06-09T12:00:00Z") == "2026-06-10T12:00:00Z"
    # Invalid candidates keep the existing watermark instead of raising.
    assert _latest_iso("not-a-date", "2026-06-09T12:00:00Z") == "not-a-date"


def test_gmail_mcp_remote_published_at_date_only_is_midnight_utc() -> None:
    from backend.agents.digestor.gmail_mcp_client import _remote_message_published_at

    assert _remote_message_published_at("2026-05-22") == "2026-05-22T00:00:00+00:00"
    assert _remote_message_published_at("2026-05-22T12:00:00+02:00") == "2026-05-22T10:00:00+00:00"
    assert _remote_message_published_at("nope") is None
    assert _remote_message_published_at(None) is None


def test_editor_recency_score_handles_bad_dates() -> None:
    from backend.agents.editor import _recency_score

    fresh = datetime.now(UTC).isoformat()
    assert _recency_score(fresh) == 1.0
    # Unparseable dates fall back to the undated penalty per weighting mode.
    assert _recency_score("garbage", "breaking") == 0.1
    assert _recency_score("garbage", "all_available") == 0.5
    assert _recency_score("garbage") == 0.3


def test_scheduler_latest_run_time_parses_iso_variants() -> None:
    from backend.app.services.scheduler import _latest_exploration_time, _latest_run_time

    expected = datetime(2026, 6, 10, 7, tzinfo=UTC)
    assert _latest_run_time({"completed_at": "2026-06-10T07:00:00Z"}) == expected
    assert _latest_run_time({"run_at": "2026-06-10T07:00:00"}) == expected
    assert _latest_run_time({"completed_at": "bad"}) is None
    assert _latest_run_time(None) is None
    assert _latest_exploration_time({"finished_at": "2026-06-10T09:00:00+02:00"}) == expected
    assert _latest_exploration_time({"started_at": "bad"}) is None
