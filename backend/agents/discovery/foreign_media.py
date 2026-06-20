from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery.language_support import trusted_language_options
from backend.agents.discovery.types import Candidate, CostProfile, SourceAdapterContext, TopicProfile
from backend.agents.discovery.web_search import _repair_text_encoding, lookback_to_days, search_web
from backend.app.core.config import get_settings
from backend.app.services import model_routing

logger = logging.getLogger(__name__)

MAX_FOREIGN_LANGUAGES = 10
DEFAULT_RESULTS_PER_LANGUAGE = 20
# How many of the refinement-written native queries to fan out per language, on
# top of the plan's own native_query.
_QUERIES_PER_LANGUAGE = 6
# Per-search hit cap; results are merged + deduped to DEFAULT_RESULTS_PER_LANGUAGE.
_PER_QUERY_LIMIT = 10
# Bounds concurrent provider calls so the fan-out stays within the 40s timeout.
_FOREIGN_SEARCH_CONCURRENCY = 4
# Preferred web-search provider for the foreign lane: a language-aware Google
# index (serpapi honors hl/lr and returns native-language results with real,
# fetchable URLs) — unlike Tavily's English-centric index. Falls back to the
# configured chain when serpapi is unavailable (no key / out of searches).
_FOREIGN_PREFERRED_PROVIDER = "serpapi"
# Country code TLDs that indicate genuinely-local coverage for a language, used
# only as a small positive score nudge (never an exclusion).
LANGUAGE_LOCAL_TLDS: dict[str, tuple[str, ...]] = {
    "es": (".mx", ".com.mx", ".ar", ".com.ar", ".co", ".com.co", ".cl", ".pe", ".com.pe", ".es"),
    "pt": (".br", ".com.br", ".pt"),
    "fr": (".fr", ".ca"),
    "de": (".de", ".at", ".ch"),
    "it": (".it",),
    "nl": (".nl", ".be"),
    "pl": (".pl",),
    "tr": (".tr", ".com.tr"),
}
NON_FOREIGN_MEDIA_LANGUAGE_CODES = {"en"}
SCRIPT_RE = {
    "ko": re.compile(r"[\uac00-\ud7af]"),
    "ja": re.compile(r"[\u3040-\u30ff\u3400-\u9fff]"),
    "zh": re.compile(r"[\u3400-\u9fff]"),
    "yue": re.compile(r"[\u3400-\u9fff]"),
}
REGION_LANGUAGE_SEEDS: dict[str, tuple[tuple[str, str], ...]] = {
    "east_asia": (
        ("zh", "Chinese-language coverage for China and Taiwan"),
        ("ja", "Japanese-language coverage for Japan"),
        ("ko", "Korean-language coverage for South Korea"),
    ),
    "asia": (
        ("zh", "Chinese-language coverage for China and Taiwan"),
        ("ja", "Japanese-language coverage for Japan"),
        ("ko", "Korean-language coverage for South Korea"),
        ("hi", "Hindi-language coverage for India"),
        ("id", "Indonesian-language coverage for Southeast Asia"),
        ("vi", "Vietnamese-language coverage for Vietnam"),
    ),
    "europe": (
        ("de", "German-language coverage for DACH markets"),
        ("fr", "French-language coverage for France"),
        ("nl", "Dutch-language coverage for the Netherlands"),
        ("it", "Italian-language coverage for Italy"),
        ("es", "Spanish-language coverage for Spain"),
        ("pl", "Polish-language coverage for Central Europe"),
    ),
    "latin_america": (
        ("es", "Spanish-language coverage for Latin America"),
        ("pt", "Portuguese-language coverage for Brazil"),
    ),
    "middle_east": (
        ("ar", "Arabic-language coverage for Middle East markets"),
        ("fa", "Persian-language coverage for Iran"),
        ("tr", "Turkish-language coverage for Turkey"),
    ),
    "africa": (
        ("ar", "Arabic-language coverage for North Africa"),
        ("sw", "Swahili-language coverage for East Africa"),
        ("yo", "Yoruba-language coverage for West Africa"),
        ("am", "Amharic-language coverage for Ethiopia"),
    ),
    "oceania": (
        ("mi", "Maori-language coverage for New Zealand and Pacific context"),
        ("fj", "Fijian-language coverage for Pacific regional context"),
    ),
}
REGION_ALIASES: dict[str, str] = {
    "east asia": "east_asia",
    "east asian": "east_asia",
    "china japan korea": "east_asia",
    "asia pacific": "asia",
    "apac": "asia",
    "asia": "asia",
    "europe": "europe",
    "eu": "europe",
    "latin america": "latin_america",
    "latam": "latin_america",
    "south america": "latin_america",
    "middle east": "middle_east",
    "mena": "middle_east",
    "africa": "africa",
    "oceania": "oceania",
    "pacific": "oceania",
}
FOREIGN_MEDIA_BLOCKED_DOMAINS = {
    "blog.maxthon.com",
    "finance.yahoo.com",
    "news.yahoo.com",
    "yahoo.com",
    "msn.com",
    "marketbeat.com",
    "marketgrowthreports.com",
    "instagram.com",
    "threads.net",
    "youtube.com",
    "youtu.be",
}
FOREIGN_MEDIA_PREFERRED_DOMAINS = {
    "ko": {
        "businesspost.co.kr",
        "chosunbiz.com",
        "ddaily.co.kr",
        "etnews.com",
        "hankyung.com",
        "mk.co.kr",
        "news.einfomax.co.kr",
        "press9.kr",
        "sedaily.com",
        "thelec.kr",
        "zdnet.co.kr",
    },
    "ja": {
        "bloomberg.co.jp",
        "itmedia.co.jp",
        "jiji.com",
        "nikkei.com",
        "toyokeizai.net",
        "xtech.nikkei.com",
    },
    "zh": {
        "cnyes.com",
        "ctee.com.tw",
        "digitimes.com.tw",
        "money.udn.com",
        "technews.tw",
        "udn.com",
    },
}
LOW_SIGNAL_TITLE_PHRASES = (
    "market size",
    "market share",
    "market report",
    "market growth",
    "industry analysis",
    "forecast",
)


class ForeignMediaSourceAdapter:
    name = "foreign_media"
    cost_profile = CostProfile(label="medium", timeout_seconds=40.0)
    good_for = ("foreign_signal", "native_language_sources", "public_web")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        plan = await foreign_language_plan_for_profile(profile)
        if not plan:
            return []

        language_queries = _language_query_plan(profile, plan)
        if not language_queries:
            return []

        days = lookback_to_days(context.lookback_hours)
        # Native travel/guide queries are evergreen; the news index returns almost
        # nothing for them. Use organic search (recency still bounded by the date
        # restrict + downstream window) unless the brief is explicitly breaking.
        vertical = "news" if profile.recency_weighting == "breaking" else "organic"
        semaphore = asyncio.Semaphore(_FOREIGN_SEARCH_CONCURRENCY)

        async def _run_search(query_text: str, language_code: str) -> Any:
            async with semaphore:
                try:
                    return await search_web(
                        query_text,
                        limit=_PER_QUERY_LIMIT,
                        language=language_code,
                        days=days,
                        vertical=vertical,
                        # Lead with the language-aware index (serper/Google honors
                        # hl=xx and returns native-language results) so foreign media
                        # isn't filled by Tavily's English-centric index; falls back
                        # to the configured chain when serper is unavailable.
                        prefer_provider=_FOREIGN_PREFERRED_PROVIDER,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one query's failure
                    # Note: Exception (not BaseException) so CancelledError still
                    # propagates and the runner's wait_for timeout can cancel us.
                    return exc

        candidates: list[Candidate] = []
        for entry, queries in language_queries:
            language_code = str(entry["code"])
            language_name = str(entry["name"])
            native_query = str(entry.get("native_query") or (queries[0] if queries else ""))

            search_results = await asyncio.gather(*(_run_search(q, language_code) for q in queries))

            # Merge every query's hits for this language and dedupe by URL before
            # applying the per-language candidate cap.
            merged_hits: list[Any] = []
            seen_urls: set[str] = set()
            failures = 0
            for result in search_results:
                if isinstance(result, BaseException):
                    failures += 1
                    logger.info("Foreign media search failed for %s: %s", language_code, result)
                    continue
                for hit in result:
                    url = str(getattr(hit, "url", "") or "").strip()
                    if not url:
                        continue
                    key = url.casefold()
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    merged_hits.append(hit)

            kept = 0
            excluded = 0
            for rank, hit in enumerate(merged_hits[:DEFAULT_RESULTS_PER_LANGUAGE], start=1):
                quality = _foreign_media_quality(hit, language_code)
                if quality["decision"] == "exclude":
                    excluded += 1
                    logger.info(
                        "Foreign media result excluded for %s: %s (%s)",
                        language_code,
                        hit.url,
                        quality["reason"],
                    )
                    continue
                score = round(
                    max(
                        0.0,
                        min(
                            0.98,
                            float(hit.score or 0.55)
                            + max(0.0, 0.05 - rank * 0.005)
                            + float(quality.get("score_adjustment") or 0.0),
                        ),
                    ),
                    3,
                )
                candidates.append(
                    Candidate(
                        adapter=self.name,
                        payload=NormalizedPayload(
                            source_type="foreign_web",
                            source_name=_repair_text_encoding(hit.title) or hit.url,
                            raw_text=_repair_text_encoding(hit.snippet),
                            original_url=hit.url,
                            published_at=hit.published_at,
                            metadata={
                                "link_quality_score": score,
                                "search_query": native_query,
                                "search_provider": hit.provider,
                                "source_language": language_code,
                                "source_language_name": language_name,
                                "language_reason": entry.get("reason") or entry.get("rationale") or "",
                                "native_entity_terms": list(entry.get("native_entity_terms") or []),
                                "needs_translation": True,
                                "original_search_title": _repair_text_encoding(hit.title),
                                "original_search_summary": _repair_text_encoding(hit.snippet),
                                "foreign_quality": quality,
                            },
                        ),
                        score=score,
                        reason=f"Native-language {language_name} web result. {quality['reason']}",
                    )
                )
                kept += 1

            logger.info(
                "Foreign media %s: %d queries -> %d hits, kept %d, excluded %d, failures %d",
                language_code,
                len(queries),
                len(merged_hits),
                kept,
                excluded,
                failures,
            )
        return candidates

    async def fetch(self, candidate: Candidate) -> NormalizedPayload:
        return candidate.payload


def _foreign_source_queries(profile: TopicProfile) -> list[str]:
    """Native queries the refinement agent wrote for the foreign-media lane."""
    raw = (profile.source_queries or {}).get("foreign_media") or ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for query in raw:
        value = " ".join(str(query or "").split()).strip()
        key = value.casefold()
        if value and key not in seen:
            cleaned.append(value)
            seen.add(key)
    return cleaned


def _language_query_plan(
    profile: TopicProfile,
    plan: tuple[dict[str, Any], ...],
) -> list[tuple[dict[str, Any], list[str]]]:
    """Build the per-language query fan-out: each language's plan native_query
    plus the refinement-written foreign source_queries, deduped and anchored to
    any must-have terms. The refinement queries are attached to the primary
    (first) language, since they are written in that language; additional
    languages still search with their own native_query.
    """
    from backend.agents.discovery.query_refiner import enforce_must_have_on_queries

    foreign_queries = _foreign_source_queries(profile)
    result: list[tuple[dict[str, Any], list[str]]] = []
    for index, entry in enumerate(plan):
        queries: list[str] = []
        seen: set[str] = set()

        def _add(value: str) -> None:
            text = " ".join(str(value or "").split()).strip()
            key = text.casefold()
            if text and key not in seen:
                queries.append(text)
                seen.add(key)

        _add(str(entry.get("native_query") or ""))
        if index == 0:
            for query in foreign_queries[:_QUERIES_PER_LANGUAGE]:
                _add(query)

        anchored = enforce_must_have_on_queries(profile, queries)
        if anchored:
            result.append((entry, anchored))
    return result


async def foreign_language_plan_for_profile(profile: TopicProfile) -> tuple[dict[str, Any], ...]:
    if not profile.source_selection.get("foreign_media", False):
        return ()

    existing = _sanitize_plan(profile.foreign_language_plan)
    if existing:
        return existing[:MAX_FOREIGN_LANGUAGES]

    selected = _derive_language_seeds(profile)
    if not selected:
        return ()

    settings = get_settings()
    client = model_routing.client_for_agent("refinement", settings=settings).client
    tasks = [_complete_plan_entry(profile, entry, client=client) for entry in selected[:MAX_FOREIGN_LANGUAGES]]
    entries = await asyncio.gather(*tasks)
    return tuple(entry for entry in entries if entry.get("native_query"))


def _code_to_name(code: str) -> str:
    """Return a human-readable language name for an ISO 639 code, falling back to the uppercased code."""
    for item in trusted_language_options():
        if item["code"] == code:
            return str(item["name"])
    return code.upper()


def _sanitize_plan(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("language") or "").strip().lower()
        if not re.match(r"^[a-z]{2,4}$", code) or code in seen or code in NON_FOREIGN_MEDIA_LANGUAGE_CODES:
            continue
        native_query = " ".join(str(item.get("native_query") or "").split()).strip()
        if not native_query:
            continue
        name = str(item.get("name") or "").strip() or _code_to_name(code)
        cleaned.append(
            {
                "code": code,
                "name": name,
                "native_query": native_query[:340],
                "native_entity_terms": _string_list(item.get("native_entity_terms"), limit=8),
                "reason": str(item.get("reason") or item.get("rationale") or "").strip()[:220],
            }
        )
        seen.add(code)
    return tuple(cleaned)


def _derive_language_seeds(profile: TopicProfile) -> list[dict[str, Any]]:
    text = _profile_text(profile)
    known_languages = {item["code"]: item for item in trusted_language_options()}
    selected: dict[str, dict[str, Any]] = {}

    for region in _normalized_regions(profile.foreign_regions):
        for code, reason in REGION_LANGUAGE_SEEDS.get(region, ()):
            if code in NON_FOREIGN_MEDIA_LANGUAGE_CODES:
                continue
            base = known_languages.get(code) or {"code": code, "name": _code_to_name(code)}
            selected.setdefault(code, {**base, "reason": reason, "native_entity_terms": []})

    for code, reason in _explicit_language_requests(text):
        if code not in NON_FOREIGN_MEDIA_LANGUAGE_CODES:
            base = known_languages.get(code) or {"code": code, "name": _code_to_name(code)}
            selected.setdefault(code, {**base, "reason": reason, "native_entity_terms": []})

    for hint in ENTITY_LANGUAGE_HINTS:
        if not _contains_entity(text, hint["aliases"]):
            continue
        code = hint["code"]
        base = known_languages.get(code) or {"code": code, "name": _code_to_name(code)}
        entry = selected.setdefault(
            code,
            {
                **base,
                "reason": f"because you're tracking {hint['label']}",
                "native_entity_terms": [],
            },
        )
        entry["native_entity_terms"] = _merge_strings(entry.get("native_entity_terms"), hint.get("native_terms"))

    for code, reason in _topic_language_hints(text):
        base = known_languages.get(code) or {"code": code, "name": _code_to_name(code)}
        selected.setdefault(code, {**base, "reason": reason, "native_entity_terms": []})

    return list(selected.values())[:MAX_FOREIGN_LANGUAGES]


async def _complete_plan_entry(profile: TopicProfile, entry: dict[str, Any], *, client: Any | None) -> dict[str, Any]:
    model_entry = await _native_query_with_model(profile, entry, client=client)
    if model_entry is not None:
        return model_entry
    return _fallback_plan_entry(profile, entry)


async def _native_query_with_model(profile: TopicProfile, entry: dict[str, Any], *, client: Any | None) -> dict[str, Any] | None:
    if client is None:
        return None
    prompt = json.dumps(
        {
            "task": "Generate one native-language web search query for Morning Dispatch foreign media discovery.",
            "language": {"code": entry["code"], "name": entry["name"]},
            "reason": entry.get("reason"),
            "native_entity_terms": entry.get("native_entity_terms", []),
            "profile": {
                "statement": profile.statement,
                "scope": profile.scope,
                "subtopics": list(profile.subtopics),
                "keywords": list(profile.keywords),
                "search_queries": list(profile.search_queries),
                "source_queries": {key: list(value) for key, value in profile.source_queries.items()},
                "exclusions": list(profile.exclusions),
            },
            "rules": [
                "Return an idiomatic query a native business or news search user would type.",
                "Prefer local company names, tickers, product names, and sector terms.",
                "Do not return a literal word-for-word translation if a local term is more natural.",
                "Keep the query under 180 characters.",
            ],
            "schema": {
                "native_query": "string",
                "native_entity_terms": ["string"],
                "rationale": "string",
            },
        },
        ensure_ascii=False,
    )
    try:
        payload = await client.complete_json(
            system="You generate native-language search queries for public foreign media discovery. Return strict JSON only.",
            prompt=prompt,
            max_tokens=360,
        )
    except Exception:
        logger.info("Foreign media native-query generation failed for %s", entry.get("code"), exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    native_query = " ".join(str(payload.get("native_query") or "").split()).strip()
    if len(native_query) < 4:
        return None
    return {
        "code": entry["code"],
        "name": entry["name"],
        "native_query": native_query[:340],
        "native_entity_terms": _merge_strings(entry.get("native_entity_terms"), payload.get("native_entity_terms")),
        "reason": str(payload.get("rationale") or entry.get("reason") or "").strip()[:220],
    }


def _fallback_plan_entry(profile: TopicProfile, entry: dict[str, Any]) -> dict[str, Any]:
    code = str(entry["code"])
    terms = _string_list(entry.get("native_entity_terms"), limit=5)
    fallback_terms = _fallback_language_terms(code)
    topic_terms = _compact_topic_terms(profile)
    query = " ".join([*terms, *fallback_terms, *topic_terms]).strip()
    if not query:
        query = profile.scope or profile.statement
    return {
        "code": code,
        "name": entry["name"],
        "native_query": query[:340],
        "native_entity_terms": terms,
        "reason": entry.get("reason") or f"{entry['name']} was selected for this topic.",
    }


def _profile_text(profile: TopicProfile) -> str:
    return " ".join(
        [
            profile.statement,
            profile.scope,
            *profile.foreign_regions,
            *profile.subtopics,
            *profile.keywords,
            *profile.search_queries,
            *[query for queries in profile.source_queries.values() for query in queries],
            *profile.exclusions,
        ]
    ).casefold()


def _normalized_regions(value: Any) -> list[str]:
    regions: list[str] = []
    seen: set[str] = set()
    for raw in _string_list(value, limit=12):
        key = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
        normalized = REGION_ALIASES.get(key, key.replace(" ", "_"))
        if normalized not in REGION_LANGUAGE_SEEDS or normalized in seen:
            continue
        regions.append(normalized)
        seen.add(normalized)
    return regions


def _explicit_language_requests(text: str) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []
    for name, code in _language_name_to_code().items():
        if re.search(rf"\b{re.escape(name)}\b", text):
            requests.append((code, f"because you asked to include {name.title()} sources"))
    return requests


def _topic_language_hints(text: str) -> list[tuple[str, str]]:
    hints: list[tuple[str, str]] = []
    for marker, code, reason in TOPIC_LANGUAGE_HINTS:
        if marker in text:
            hints.append((code, reason))
    return hints


def _contains_entity(text: str, aliases: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(alias.lower())}\b", text) for alias in aliases)


def _compact_topic_terms(profile: TopicProfile) -> list[str]:
    raw_terms = [
        *profile.search_queries,
        *profile.keywords,
        *profile.subtopics,
        profile.scope,
        profile.statement,
    ]
    terms: list[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        for part in re.findall(r"[A-Za-z0-9][A-Za-z0-9&.+-]{2,}", str(raw or "")):
            key = part.lower()
            if key in seen or key in QUERY_DROP_TERMS:
                continue
            terms.append(part)
            seen.add(key)
            if len(terms) >= 8:
                return terms
    return terms


def _fallback_language_terms(code: str) -> list[str]:
    return {
        "ar": ["أحدث الأخبار", "السوق", "التوقعات"],
        "bn": ["সর্বশেষ খবর", "বাজার", "দৃষ্টিভঙ্গি"],
        "ko": ["최신 뉴스", "실적", "전망"],
        "ja": ["最新ニュース", "業績", "見通し"],
        "zh": ["最新消息", "业绩", "展望"],
        "de": ["aktuelle Nachrichten", "Markt", "Ausblick"],
        "fa": ["آخرین اخبار", "بازار", "چشم‌انداز"],
        "fr": ["actualités", "marché", "perspectives"],
        "hi": ["ताज़ा खबर", "बाजार", "दृष्टिकोण"],
        "id": ["berita terbaru", "pasar", "prospek"],
        "it": ["ultime notizie", "mercato", "prospettive"],
        "nl": ["nieuws", "markt", "vooruitzichten"],
        "pl": ["najnowsze wiadomości", "rynek", "perspektywy"],
        "pt": ["notícias recentes", "mercado", "perspectivas"],
        "ru": ["последние новости", "рынок", "перспективы"],
        "es": ["noticias", "mercado", "perspectivas"],
        "ta": ["சமீபத்திய செய்திகள்", "சந்தை", "பார்வை"],
        "te": ["తాజా వార్తలు", "మార్కెట్", "అవలోకనం"],
        "th": ["ข่าวล่าสุด", "ตลาด", "แนวโน้ม"],
        "tr": ["son haberler", "piyasa", "görünüm"],
        "ur": ["تازہ خبریں", "مارکیٹ", "آؤٹ لک"],
        "vi": ["tin mới nhất", "thị trường", "triển vọng"],
    }.get(code, [])


def _foreign_media_quality(hit: Any, language_code: str) -> dict[str, Any]:
    url = str(getattr(hit, "url", "") or "")
    host = urlparse(url).netloc.lower().removeprefix("www.")
    title = str(getattr(hit, "title", "") or "")
    snippet = str(getattr(hit, "snippet", "") or "")
    combined = f"{title} {snippet}"
    title_lower = title.casefold()
    url_lower = url.casefold()

    if _host_matches(host, FOREIGN_MEDIA_BLOCKED_DOMAINS):
        return _quality_result("exclude", "blocked aggregator, social, video, or low-quality domain")
    if "/tag/" in url_lower or "/tags/" in url_lower or title_lower.startswith("tag "):
        return _quality_result("exclude", "tag/archive pages are not article coverage")
    if any(phrase in title_lower for phrase in LOW_SIGNAL_TITLE_PHRASES):
        return _quality_result("exclude", "generic market-report page")
    if _looks_like_english_result(language_code, combined):
        return _quality_result("exclude", "result does not look like native-language coverage")

    preferred_domains = FOREIGN_MEDIA_PREFERRED_DOMAINS.get(language_code, set())
    if _host_matches(host, preferred_domains):
        return _quality_result("include", "preferred local business/technology source", score_adjustment=0.08)
    if language_code in {"zh", "yue"} and host.endswith(".tw"):
        return _quality_result("include", "Taiwanese local-domain source", score_adjustment=0.06)
    if language_code == "ko" and host.endswith(".kr"):
        return _quality_result("include", "Korean local-domain source", score_adjustment=0.05)
    if language_code == "ja" and host.endswith(".jp"):
        return _quality_result("include", "Japanese local-domain source", score_adjustment=0.05)
    if _contains_expected_script(language_code, combined):
        return _quality_result("include", "native-script coverage")
    local_tlds = LANGUAGE_LOCAL_TLDS.get(language_code, ())
    if local_tlds and any(host == tld.lstrip(".") or host.endswith(tld) for tld in local_tlds):
        return _quality_result("include", "country-local domain", score_adjustment=0.05)
    return _quality_result("include", "accepted foreign-media result", score_adjustment=-0.04)


def _quality_result(decision: str, reason: str, *, score_adjustment: float = 0.0) -> dict[str, Any]:
    return {
        "decision": decision,
        "reason": reason,
        "score_adjustment": score_adjustment,
    }


def _host_matches(host: str, domains: set[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _contains_expected_script(language_code: str, text: str) -> bool:
    pattern = SCRIPT_RE.get(language_code)
    return bool(pattern and pattern.search(text))


def _looks_like_english_result(language_code: str, text: str) -> bool:
    if language_code not in SCRIPT_RE:
        return False
    native_count = len(SCRIPT_RE[language_code].findall(text))
    ascii_letters = len(re.findall(r"[A-Za-z]", text))
    return (native_count == 0 and ascii_letters > 25) or (ascii_letters > 80 and native_count < 3)


def _string_list(value: Any, *, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = " ".join(str(item or "").split()).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        cleaned.append(text[:120])
        seen.add(key)
        if len(cleaned) >= limit:
            break
    return cleaned


def _merge_strings(existing: Any, incoming: Any) -> list[str]:
    return _string_list([*_string_list(existing), *_string_list(incoming)])


LANGUAGE_NAME_ALIASES = {
    "korean": "ko",
    "korea": "ko",
    "japanese": "ja",
    "japan": "ja",
    "chinese": "zh",
    "china": "zh",
    "mandarin": "zh",
    "mandarin chinese": "zh",
    "cantonese": "yue",
    "german": "de",
    "germany": "de",
    "french": "fr",
    "france": "fr",
    "dutch": "nl",
    "netherlands": "nl",
    "spanish": "es",
    "spain": "es",
    "mexican": "es",
    "arabic": "ar",
    "standard arabic": "ar",
    "farsi": "fa",
    "persian": "fa",
    "russian": "ru",
    "hindi": "hi",
    "portuguese": "pt",
    "brazilian portuguese": "pt",
    "italian": "it",
    "indonesian": "id",
    "turkish": "tr",
    "vietnamese": "vi",
    "polish": "pl",
    "bengali": "bn",
    "telugu": "te",
    "tamil": "ta",
    "marathi": "mr",
    "thai": "th",
    "urdu": "ur",
    "swahili": "sw",
    "yoruba": "yo",
    "amharic": "am",
    "hausa": "ha",
    "zulu": "zu",
    "maori": "mi",
    "māori": "mi",
    "fijian": "fj",
    "samoan": "sm",
    "tongan": "to",
    "tahitian": "ty",
    "chamorro": "ch",
    "marshallese": "mh",
    "niuean": "niu",
    "tetum": "tet",
    "bislama": "bi",
    "tok pisin": "tpi",
    "palauan": "pau",
    "nauruan": "nau",
    "gilbertese": "gil",
    "rotuman": "rtm",
    "wallisian": "wls",
    "cook islands maori": "rar",
    "cook islands māori": "rar",
    "kapingamarangi": "kpg",
    "vaiaku": "tvl",
}


def _language_name_to_code() -> dict[str, str]:
    mapping = dict(LANGUAGE_NAME_ALIASES)
    for language in trusted_language_options():
        mapping.setdefault(str(language["name"]).casefold(), str(language["code"]))
    return mapping

ENTITY_LANGUAGE_HINTS = (
    {"label": "SK Hynix", "code": "ko", "aliases": ("sk hynix", "hynix"), "native_terms": ("SK하이닉스", "하이닉스")},
    {"label": "Samsung", "code": "ko", "aliases": ("samsung", "samsung electronics"), "native_terms": ("삼성전자", "삼성")},
    {"label": "Kioxia", "code": "ja", "aliases": ("kioxia",), "native_terms": ("キオクシア",)},
    {"label": "Sony", "code": "ja", "aliases": ("sony",), "native_terms": ("ソニー",)},
    {"label": "TSMC", "code": "zh", "aliases": ("tsmc", "taiwan semiconductor"), "native_terms": ("台積電", "台积电")},
    {"label": "ASML", "code": "nl", "aliases": ("asml",), "native_terms": ("ASML",)},
    {"label": "Infineon", "code": "de", "aliases": ("infineon",), "native_terms": ("Infineon",)},
    {"label": "SAP", "code": "de", "aliases": ("sap",), "native_terms": ("SAP",)},
    {"label": "LVMH", "code": "fr", "aliases": ("lvmh",), "native_terms": ("LVMH",)},
)

TOPIC_LANGUAGE_HINTS = (
    ("mexico city", "es", "because Spanish-language local reporting is useful for Mexico City"),
    ("mexico", "es", "because Spanish-language local reporting is useful for Mexico"),
    ("seoul", "ko", "because Korean-language reporting is useful for Seoul"),
    ("tokyo", "ja", "because Japanese-language reporting is useful for Tokyo"),
    ("taiwan", "zh", "because Chinese-language regional reporting is useful for Taiwan"),
    ("germany", "de", "because German-language reporting is useful for Germany"),
    ("france", "fr", "because French-language reporting is useful for France"),
    ("netherlands", "nl", "because Dutch-language reporting is useful for the Netherlands"),
)

QUERY_DROP_TERMS = {
    "about",
    "brief",
    "curious",
    "interest",
    "interested",
    "news",
    "track",
    "tracking",
    "update",
    "updates",
}
