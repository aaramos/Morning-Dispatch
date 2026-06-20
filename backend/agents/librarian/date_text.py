"""Locale-aware and relative date parsing shared across discovery and librarian.

This is a dependency-free leaf module so it can be imported from web search
(discovery), article extraction (librarian), and the exploration service without
risking import cycles. It augments the ASCII/CJK date handling those callers
already do with non-English Latin month names and English relative phrasing
(e.g. "19 ago 2025", "10 months ago") that providers like Serper emit for
foreign-language and organic results.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

__all__ = [
    "month_from_token",
    "parse_relative_date",
    "normalize_date_string",
    "parse_iso_datetime",
    "parse_rfc2822_datetime",
    "parse_datetime",
    "date_from_url",
]


def _fold(token: str) -> str:
    """Casefold and strip accents/punctuation so locale tokens compare uniformly."""
    decomposed = unicodedata.normalize("NFKD", str(token or ""))
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return ascii_only.strip(". ").lower()


# Month names and common abbreviations across the languages the foreign-media
# lane searches. Keys are accent-folded; several abbreviations are shared across
# languages (e.g. "mar", "ago"), which is fine because they map to one month.
_LOCALE_MONTHS: dict[str, int] = {}


def _register(*tokens: str, month: int) -> None:
    for token in tokens:
        _LOCALE_MONTHS[_fold(token)] = month


# English (full + abbrev)
_register("january", "jan", month=1)
_register("february", "feb", month=2)
_register("march", "mar", month=3)
_register("april", "apr", month=4)
_register("may", month=5)
_register("june", "jun", month=6)
_register("july", "jul", month=7)
_register("august", "aug", month=8)
_register("september", "sep", "sept", month=9)
_register("october", "oct", month=10)
_register("november", "nov", month=11)
_register("december", "dec", month=12)
# Spanish
_register("enero", "ene", month=1)
_register("febrero", month=2)
_register("marzo", month=3)
_register("abril", "abr", month=4)
_register("mayo", month=5)
_register("junio", month=6)
_register("julio", month=7)
_register("agosto", "ago", month=8)
_register("septiembre", "setiembre", "set", month=9)
_register("octubre", month=10)
_register("noviembre", month=11)
_register("diciembre", "dic", month=12)
# Portuguese
_register("janeiro", month=1)
_register("fevereiro", "fev", month=2)
_register("marco", month=3)
_register("maio", "mai", month=5)
_register("junho", month=6)
_register("julho", month=7)
_register("setembro", month=9)
_register("outubro", "out", month=10)
_register("novembro", month=11)
_register("dezembro", "dez", month=12)
# French
_register("janvier", "janv", month=1)
_register("fevrier", "fevr", month=2)
_register("mars", month=3)
_register("avril", "avr", month=4)
_register("juin", month=6)
_register("juillet", "juil", month=7)
_register("aout", month=8)
_register("septembre", month=9)
_register("octobre", month=10)
_register("novembre", month=11)
_register("decembre", month=12)
# German
_register("januar", month=1)
_register("februar", month=2)
_register("maerz", "marz", month=3)
_register("april", month=4)
_register("juni", month=6)
_register("juli", month=7)
_register("oktober", "okt", month=10)
_register("dezember", month=12)
# Italian
_register("gennaio", "gen", month=1)
_register("febbraio", month=2)
_register("aprile", month=4)
_register("maggio", "mag", month=5)
_register("giugno", "giu", month=6)
_register("luglio", "lug", month=7)
_register("settembre", "sett", month=9)
_register("ottobre", "ott", month=10)
_register("dicembre", month=12)


def month_from_token(token: str) -> int | None:
    """Return the 1-12 month for a month name/abbreviation in any supported locale."""
    return _LOCALE_MONTHS.get(_fold(token))


# Relative-date units across the locales providers actually emit (en + the
# major foreign-media languages). Keys are accent-folded/lowercased to match
# `_fold` output; sub-day units resolve to "today" (0 days).
_RELATIVE_UNIT_DAYS = {
    # second / minute / hour -> same day
    "second": 0, "seconds": 0, "segundo": 0, "segundos": 0, "seconde": 0, "secondes": 0,
    "sekunde": 0, "sekunden": 0, "secondo": 0, "secondi": 0,
    "minute": 0, "minutes": 0, "minuto": 0, "minutos": 0, "minuten": 0, "minuti": 0, "minuut": 0,
    "hour": 0, "hours": 0, "hora": 0, "horas": 0, "heure": 0, "heures": 0,
    "stunde": 0, "stunden": 0, "ora": 0, "ore": 0, "uur": 0, "uren": 0,
    # day
    "day": 1, "days": 1, "dia": 1, "dias": 1, "jour": 1, "jours": 1, "tag": 1, "tage": 1,
    "tagen": 1, "giorno": 1, "giorni": 1, "dag": 1, "dagen": 1, "gun": 1,
    # week
    "week": 7, "weeks": 7, "semana": 7, "semanas": 7, "semaine": 7, "semaines": 7,
    "woche": 7, "wochen": 7, "settimana": 7, "settimane": 7, "weken": 7, "hafta": 7,
    # month
    "month": 30, "months": 30, "mes": 30, "meses": 30, "mois": 30, "monat": 30, "monate": 30,
    "monaten": 30, "mese": 30, "mesi": 30, "maand": 30, "maanden": 30, "ay": 30,
    # year
    "year": 365, "years": 365, "ano": 365, "anos": 365, "an": 365, "ans": 365, "annee": 365,
    "annees": 365, "jahr": 365, "jahre": 365, "jahren": 365, "anno": 365, "anni": 365,
    "jaar": 365, "jaren": 365, "yil": 365,
}
# Unit captured as a generic word, then folded and looked up above (keeps the
# regex accent-agnostic). Two marker styles: prefix ("hace/há/il y a/vor 3 días")
# and suffix ("3 days ago", "3 giorni fa", "3 dagen geleden", "3 dias atrás").
_REL_UNIT_WORD = r"([^\W\d_]{2,12})"
_REL_PREFIX_RE = re.compile(
    rf"\b(?:hace|h[aá]|il\s+y\s+a|vor)\s+(\d{{1,3}})\s+{_REL_UNIT_WORD}\b",
    re.IGNORECASE,
)
_REL_SUFFIX_RE = re.compile(
    rf"\b(\d{{1,3}})\s+{_REL_UNIT_WORD}\s+(?:ago|fa|geleden|atr[aá]s|önce)\b",
    re.IGNORECASE,
)


# CJK relative dates: NUMBER + unit char(s) + 前 (ja/zh "ago") or 전 (ko "ago"),
# e.g. "4 日前" (ja, 4 days), "3일 전" (ko), "4天前" (zh). Multi-char units are
# listed before their single-char prefixes so the alternation matches greedily.
_REL_CJK_UNIT_DAYS = {
    "時間": 0, "小时": 0, "小時": 0, "시간": 0, "時": 0, "分": 0, "분": 0, "秒": 0, "초": 0,
    "日": 1, "天": 1, "일": 1,
    "週間": 7, "星期": 7, "週": 7, "周": 7, "주": 7,
    "か月": 30, "ヶ月": 30, "カ月": 30, "个月": 30, "個月": 30, "개월": 30, "月": 30,
    "年": 365, "년": 365,
}
_REL_CJK_RE = re.compile(
    r"(\d{1,3})\s*"
    r"(時間|小时|小時|시간|週間|星期|か月|ヶ月|カ月|个月|個月|개월|日|天|일|週|周|주|月|年|년|分|분|時|秒|초)"
    r"\s*[前전]"
)


def parse_relative_date(text: str, *, now: datetime | None = None) -> date | None:
    """Resolve relative phrasing to an absolute date across supported locales.

    Handles English ("10 months ago") plus the foreign-media languages providers
    emit — e.g. "hace 3 días" (es), "há 2 dias"/"3 dias atrás" (pt), "il y a 2
    jours" (fr), "vor 3 Tagen" (de), "3 giorni fa" (it), "3 dagen geleden" (nl),
    and CJK forms "4 日前" (ja), "3일 전" (ko), "4天前" (zh).
    """
    s = str(text or "")
    reference = (now or datetime.now(UTC)).date()

    cjk = _REL_CJK_RE.search(s)
    if cjk:
        days_per_unit = _REL_CJK_UNIT_DAYS.get(cjk.group(2))
        if days_per_unit is not None:
            return reference - timedelta(days=int(cjk.group(1)) * days_per_unit)

    match = _REL_SUFFIX_RE.search(s) or _REL_PREFIX_RE.search(s)
    if not match:
        return None
    days_per_unit = _RELATIVE_UNIT_DAYS.get(_fold(match.group(2)))
    if days_per_unit is None:
        return None
    return reference - timedelta(days=int(match.group(1)) * days_per_unit)


# Token allows Unicode letters so accented month names are captured, then folded.
_MONTH_WORD = r"([^\W\d_]{3,12})"
# Optional Romance-language connectors ("3 de marzo de 2025", "3 di marzo").
_CONN = r"(?:de\s+|del\s+|di\s+|d['’]\s*)?"
_DAY_FIRST_RE = re.compile(rf"\b(\d{{1,2}})\s+{_CONN}{_MONTH_WORD}\.?\s+{_CONN}(20\d{{2}})\b")
_MONTH_FIRST_RE = re.compile(rf"\b{_MONTH_WORD}\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})\b")
_ISO_DT_RE = re.compile(
    r"\b20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_SLASH_RE = re.compile(r"\b(20\d{2})/(\d{1,2})/(\d{1,2})\b")
_DOTTED_RE = re.compile(r"\b(20\d{2})\.(\d{1,2})\.(\d{1,2})\.?")
_CJK_RE = re.compile(r"(20\d{2})\s*[年년]\s*(\d{1,2})\s*[月월]\s*(\d{1,2})\s*[日일]?")


def _iso_or_none(year: int | str, month: int | str, day: int | str) -> str | None:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (ValueError, TypeError):
        return None


def normalize_date_string(value: str, *, allow_relative: bool = True, now: datetime | None = None) -> str | None:
    """Best-effort parse of a provider/byline date string to ISO `YYYY-MM-DD`.

    Handles ISO passthrough, locale numeric forms (slash/dotted/CJK), non-English
    Latin month names, and English relative phrasing. Returns None when nothing
    recognizable is found so callers can fall back to other signals.

    Set ``allow_relative=False`` when scanning free body text, where phrases like
    "posted 2 days ago" in page chrome would otherwise be mistaken for the
    article's publish date.
    """
    text = str(value or "").strip()
    if not text:
        return None
    iso_dt = _ISO_DT_RE.search(text)
    if iso_dt:
        return iso_dt.group(0)
    iso_date = _ISO_DATE_RE.search(text)
    if iso_date:
        return _iso_or_none(iso_date.group(1), iso_date.group(2), iso_date.group(3))
    if allow_relative:
        relative = parse_relative_date(text, now=now)
        if relative:
            return relative.isoformat()
    day_first = _DAY_FIRST_RE.search(text)
    if day_first:
        month = month_from_token(day_first.group(2))
        if month is not None:
            return _iso_or_none(day_first.group(3), month, day_first.group(1))
    month_first = _MONTH_FIRST_RE.search(text)
    if month_first:
        month = month_from_token(month_first.group(1))
        if month is not None:
            return _iso_or_none(month_first.group(3), month, month_first.group(2))
    slash = _SLASH_RE.search(text)
    if slash:
        return _iso_or_none(slash.group(1), slash.group(2), slash.group(3))
    cjk = _CJK_RE.search(text)
    if cjk:
        return _iso_or_none(cjk.group(1), cjk.group(2), cjk.group(3))
    dotted = _DOTTED_RE.search(text)
    if dotted:
        return _iso_or_none(dotted.group(1), dotted.group(2), dotted.group(3))
    return None


def _to_utc(parsed: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC (naive values are assumed UTC)."""
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_iso_datetime(value: object) -> datetime | None:
    """Parse an ISO 8601 date/datetime string to a timezone-aware UTC datetime.

    Accepts the trailing ``Z`` suffix that :func:`datetime.fromisoformat` rejects
    on older corpora, date-only strings (midnight UTC), and naive datetimes
    (assumed UTC). Returns None when the string is not strict ISO 8601.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _to_utc(parsed)


def parse_rfc2822_datetime(value: object) -> datetime | None:
    """Parse an RFC 2822 date string (email/RSS ``pubDate``) to aware UTC.

    Naive results (e.g. the ``-0000`` unknown-zone convention) are assumed UTC.
    Returns None when the string is not RFC 2822.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:  # pre-3.10 parsedate_to_datetime returns None on failure
        return None
    return _to_utc(parsed)


def parse_datetime(
    value: object,
    *,
    allow_relative: bool = True,
    now: datetime | None = None,
) -> datetime | None:
    """Best-effort parse of any provider date value to a timezone-aware UTC datetime.

    Tries, in order: datetime passthrough, numeric epoch seconds, ISO 8601
    (with/without ``Z``), RFC 2822, then the locale/relative text forms handled
    by :func:`normalize_date_string` (resolved to midnight UTC when date-only).

    Set ``allow_relative=False`` when scanning free body text, where page chrome
    like "posted 2 days ago" must not become a publish date.
    """
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    parsed = parse_iso_datetime(value)
    if parsed is not None:
        return parsed
    parsed = parse_rfc2822_datetime(value)
    if parsed is not None:
        return parsed
    normalized = normalize_date_string(str(value or ""), allow_relative=allow_relative, now=now)
    if normalized is None:
        return None
    # The ISO-datetime passthrough can keep a comma decimal separator that
    # fromisoformat rejects; normalize it before the final conversion.
    return parse_iso_datetime(normalized.replace(",", "."))


# Full URL-embedded dates only: `/2026/06/10/` path segments and `2026-06-10`
# slug fragments. Year/month-only paths (`/2026/06/`) are archive pages, not
# article dates, so they are deliberately not matched.
_URL_SLASH_DATE_RE = re.compile(r"/(20\d{2})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:[/?#]|$)")
_URL_ISO_DATE_RE = re.compile(r"(?:^|[^\d])(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])(?:[^\d]|$)")


def date_from_url(url: object) -> str | None:
    """Extract an article date embedded in a URL path as ISO ``YYYY-MM-DD``.

    Recognizes `/2026/06/10/` style path segments and `2026-06-10` slug
    fragments. Returns None when no full year-month-day date is present.
    """
    text = str(url or "")
    if not text:
        return None
    match = _URL_SLASH_DATE_RE.search(text) or _URL_ISO_DATE_RE.search(text)
    if not match:
        return None
    return _iso_or_none(match.group(1), match.group(2), match.group(3))
