from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery.language_support import trusted_language_options
from backend.agents.discovery.types import Candidate, CostProfile, SourceAdapterContext, TopicProfile
from backend.agents.discovery.web_search import search_web
from backend.app.core.config import get_settings
from backend.app.services import model_routing

logger = logging.getLogger(__name__)

MAX_FOREIGN_LANGUAGES = 3
DEFAULT_RESULTS_PER_LANGUAGE = 6


class ForeignMediaSourceAdapter:
    name = "foreign_media"
    cost_profile = CostProfile(label="medium", timeout_seconds=40.0)
    good_for = ("foreign_signal", "native_language_sources", "public_web")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        plan = await foreign_language_plan_for_profile(profile)
        if not plan:
            return []

        per_language_limit = max(
            3,
            min(DEFAULT_RESULTS_PER_LANGUAGE, max(1, context.candidate_limit) // max(1, len(plan))),
        )
        results = await asyncio.gather(
            *(
                search_web(str(item["native_query"]), limit=per_language_limit, language=str(item["code"]))
                for item in plan
                if str(item.get("native_query") or "").strip()
            ),
            return_exceptions=True,
        )

        candidates: list[Candidate] = []
        for item, result in zip([entry for entry in plan if str(entry.get("native_query") or "").strip()], results, strict=False):
            if isinstance(result, BaseException):
                logger.info("Foreign media search failed for %s: %s", item.get("code"), result)
                continue
            language_code = str(item["code"])
            language_name = str(item["name"])
            for rank, hit in enumerate(result[:per_language_limit], start=1):
                if not hit.url:
                    continue
                score = round(max(0.0, min(0.98, float(hit.score or 0.55) + max(0.0, 0.05 - rank * 0.005))), 3)
                candidates.append(
                    Candidate(
                        adapter=self.name,
                        payload=NormalizedPayload(
                            source_type="foreign_web",
                            source_name=hit.title or hit.url,
                            raw_text=hit.snippet,
                            original_url=hit.url,
                            metadata={
                                "link_quality_score": score,
                                "search_query": item["native_query"],
                                "search_provider": hit.provider,
                                "source_language": language_code,
                                "source_language_name": language_name,
                                "language_reason": item.get("reason") or item.get("rationale") or "",
                                "native_entity_terms": list(item.get("native_entity_terms") or []),
                                "needs_translation": True,
                                "original_search_title": hit.title,
                                "original_search_summary": hit.snippet,
                            },
                        ),
                        score=score,
                        reason=f"Native-language {language_name} web result.",
                    )
                )
        return candidates

    async def fetch(self, candidate: Candidate) -> NormalizedPayload:
        return candidate.payload


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


def _sanitize_plan(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    languages = {item["code"]: item for item in trusted_language_options()}
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("language") or "").strip().lower()
        if code not in languages or code in seen:
            continue
        native_query = " ".join(str(item.get("native_query") or "").split()).strip()
        if not native_query:
            continue
        language = languages[code]
        cleaned.append(
            {
                "code": code,
                "name": language["name"],
                "native_query": native_query[:340],
                "native_entity_terms": _string_list(item.get("native_entity_terms"), limit=8),
                "reason": str(item.get("reason") or item.get("rationale") or "").strip()[:220],
            }
        )
        seen.add(code)
    return tuple(cleaned)


def _derive_language_seeds(profile: TopicProfile) -> list[dict[str, Any]]:
    text = _profile_text(profile)
    languages = {item["code"]: item for item in trusted_language_options()}
    selected: dict[str, dict[str, Any]] = {}

    for code, reason in _explicit_language_requests(text):
        if code in languages:
            selected.setdefault(code, {**languages[code], "reason": reason, "native_entity_terms": []})

    for hint in ENTITY_LANGUAGE_HINTS:
        if not _contains_entity(text, hint["aliases"]):
            continue
        code = hint["code"]
        if code not in languages:
            continue
        entry = selected.setdefault(
            code,
            {
                **languages[code],
                "reason": f"because you're tracking {hint['label']}",
                "native_entity_terms": [],
            },
        )
        entry["native_entity_terms"] = _merge_strings(entry.get("native_entity_terms"), hint.get("native_terms"))

    for code, reason in _topic_language_hints(text):
        if code in languages:
            selected.setdefault(code, {**languages[code], "reason": reason, "native_entity_terms": []})

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
            *profile.subtopics,
            *profile.keywords,
            *profile.search_queries,
            *[query for queries in profile.source_queries.values() for query in queries],
            *profile.exclusions,
        ]
    ).casefold()


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
