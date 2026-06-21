"""Shared profile/patch/merge utilities and prose-free helpers for topic refinement.

Lowest layer of the refinement package split (M7): every other refinement
module imports from here. Code moved verbatim from refinement.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import UTC, datetime
from typing import Any

from backend.agents.discovery.language_support import trusted_language_options
from backend.agents.discovery.markets import normalize_market_query_tickers, resolve_tickers_from_text
from backend.agents.discovery.types import DEFAULT_EXPLORE_SOURCE_SELECTION, fold_text
from backend.agents.librarian.text_utils import keyword_set
from backend.app.db import database


logger = logging.getLogger(__name__)


MIN_REFINEMENT_TURNS = 2


MAX_REFINEMENT_TURNS = 10


AGENT_PENDING_FIELD = "refinement_agent"


GMAIL_RULES_FIELD = "gmail_rules"


GMAIL_SENDER_SELECTION_FIELD = "gmail_sender_selection"


PENDING_STRATEGY_FIELD = "strategy_refinement"


PENDING_STRATEGY_PROFILE_KEY = "pending_strategy_refinement"


STRATEGY_REVIEW_PROFILE_KEY = "strategy_review"


STRATEGY_REVIEW_RESOLVED_STATUSES = {"passed", "applied", "discarded", "suppressed", "unavailable"}


FIELD_ORDER = ("scope", "related_interests", "depth", "recency_weighting", "requested_sources", "exclusions", "must_have")


FIELD_HINT_TEXT_SOURCE = ("statement", "scope", "keywords", "subtopics")


DEPTH_PRACTITIONER_TOKENS = (
    "deep",
    "technical",
    "implementation",
    "architecture",
    "hands-on",
    "tutorial",
    "how to",
)


RECENCY_BREAKING_TOKENS = (
    "breaking",
    "latest",
    "just in",
    "current",
    "today",
    "week",
    "month",
)


QUESTIONS = {
    "scope": "What angle would make this most useful for you?",
    "related_interests": "Are there any nearby topics or constraints I should include?",
    "depth": "Do you want practical, get-things-done coverage, or a deeper expert-level read?",
    "recency_weighting": "How recent does the source material need to be? For example: last 24 hours, last 3 days, no more than a year old, or best available regardless of date.",
    "requested_sources": "Any specific podcast, YouTube channel, newsletter, site, company, or collection I should try to include?",
    "exclusions": "Anything I should avoid so the brief stays focused?",
    "must_have": "Is there a term every single item must mention, like a place, company, product, or model? Optional; I’ll also match common aliases and translations.",
    GMAIL_RULES_FIELD: "How do you want me to use Gmail for this brief? For example: AI-related newsletters received in the last 7 days.",
}


VALID_SOURCE_ADAPTERS = {"gmail", "podcasts", "web_search", "foreign_media", "youtube", "collections", "markets", "reddit", "google_news", "academic", "regulatory", "hacker_news"}


PODCAST_STRATEGY_FIELDS = (
    "direct_episode_queries",
    "related_episode_queries",
    "negative_constraints",
    "priority_terms",
)


def _visible_prose(full_text: str, *, final: bool = False) -> tuple[str, bool]:
    """Return the user-visible prose (text before the json fence) and whether the fence was seen."""
    idx = full_text.find("```")
    if idx != -1:
        return full_text[:idx], True
    visible = full_text
    if not final:
        # Hold back a trailing backtick run that may turn into a fence on the next token.
        stripped = visible.rstrip("`")
        if stripped != visible:
            visible = stripped
    return visible, False


def _parse_chat_payload(full_text: str) -> tuple[dict[str, Any], bool, str]:
    block = _extract_json_block(full_text)
    if not isinstance(block, dict):
        return {}, False, "continue"
    patch = block.get("profile_patch")
    intent = str(block.get("intent") or "").strip()
    return (
        patch if isinstance(patch, dict) else {},
        bool(block.get("ready_to_build")) or _normalize_refinement_intent(intent) == "build",
        intent,
    )


def _normalize_refinement_intent(value: Any) -> str:
    intent = str(value or "").strip().casefold()
    if intent in {"build", "confirm_changes", "discard_changes"}:
        return intent
    return "continue"


def _extract_json_block(text: str) -> dict[str, Any] | None:
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _pending_strategy_refinement(profile: dict[str, Any]) -> dict[str, Any]:
    pending = profile.get(PENDING_STRATEGY_PROFILE_KEY)
    return dict(pending) if isinstance(pending, dict) else {}


def _prune_unselected_source_fields(profile: dict[str, Any]) -> dict[str, Any]:
    selection = _source_selection_dict(profile.get("source_selection"))
    selected_sources = {source for source, enabled in selection.items() if enabled}
    if not selected_sources:
        return {
            **profile,
            "source_queries": {},
            "requested_sources": [],
            "source_selection": selection,
        }
    return {
        **profile,
        "source_queries": {
            source: queries
            for source, queries in _clean_source_queries(profile.get("source_queries")).items()
            if source in selected_sources
        },
        "requested_sources": [
            source
            for source in _normalize_requested_sources(profile.get("requested_sources"))
            if str(source.get("adapter") or "") in selected_sources
        ],
        "source_selection": selection,
    }


def _stable_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable_jsonable(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_stable_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _models(payload: Any) -> dict[str, str | None]:
    models: dict[str, str | None] = {}
    if not isinstance(payload, dict):
        return models
    for key in ("refinement", "brief"):
        if key not in payload:
            continue
        raw_model = payload.get(key)
        if raw_model is None:
            models[key] = None
            continue
        if not isinstance(raw_model, str):
            continue
        cleaned = raw_model.strip()
        models[key] = cleaned or None
    return models


# Must-haves are a hard content gate and must be set explicitly by the user, never
# inferred by the agent from the topic. We only honor an agent-proposed must-have
# term when the user's own words express a filtering requirement on results. This
# is deliberately conservative — a missed signal just means the user sets the term
# in the Confirmation panel instead, whereas a false positive silently gates a brief.
_MUST_HAVE_INTENT_RE = re.compile(
    r"(?:"
    r"must (?:mention|include|contain|reference|name)"
    r"|(?:every|each|all) (?:result|item|story|article|post|piece|headline|entry)s? "
    r"(?:must|should|have to|has to|need to|needs to)"
    r"|(?:results?|items?|stories|articles|posts|headlines) (?:must|should|have to|need to)"
    r"|only (?:show|include|surface|return)[^.]*\babout\b"
    r"|only about"
    r"|has to (?:mention|include|reference)"
    r"|needs? to (?:mention|include|reference)"
    r"|require (?:that )?(?:every|each|all|the)"
    r")",
    flags=re.IGNORECASE,
)


def _user_requested_must_have(user_text: str) -> bool:
    """True when the user's own text explicitly demands a required term/filter."""
    return bool(_MUST_HAVE_INTENT_RE.search(str(user_text or "")))


def _merge_agent_profile_patch(profile: dict[str, Any], patch: Any, *, user_text: str = "") -> dict[str, Any]:
    updated = dict(profile)
    if not isinstance(patch, dict):
        return _coerce_profile(updated)

    for key in ("scope",):
        value = str(patch.get(key) or "").strip()
        if value:
            updated[key] = value

    cleanup_requested = _requests_strategy_cleanup(user_text)

    for key in ("subtopics", "search_queries", "exclusions", *PODCAST_STRATEGY_FIELDS):
        if key not in patch:
            continue
        if key == "search_queries":
            # Agent-proposed general queries pass through a conservative spelling
            # normalization so obvious typos never reach the adapters.
            incoming_queries = _normalize_query_list_spelling(patch.get(key))
            if bool(patch.get("replace_search_queries")) or cleanup_requested:
                updated[key] = incoming_queries
            else:
                updated[key] = _merge_string_lists(updated.get(key), incoming_queries, limit=20)
        else:
            updated[key] = _merge_string_lists(updated.get(key), patch.get(key), limit=16)
    has_terms_patch = "must_have_terms" in patch
    has_aliases_patch = "must_have_aliases" in patch
    existing_terms = _string_list(updated.get("must_have_terms"), limit=6)
    user_requested = _user_requested_must_have(user_text)
    if has_terms_patch or has_aliases_patch:
        merged_terms = (
            _merge_string_lists(existing_terms, patch.get("must_have_terms"), limit=6)
            if has_terms_patch
            else existing_terms
        )
        terms_limit = _string_list(merged_terms, limit=6)

        old_aliases = _clean_must_have_aliases(updated.get("must_have_aliases"), terms=terms_limit)
        new_aliases = {}
        if has_aliases_patch:
            new_aliases = _clean_must_have_aliases(patch.get("must_have_aliases"), terms=terms_limit)

        merged_aliases = {**old_aliases, **new_aliases}

        canonical_terms, canonical_aliases = _canonicalize_must_have(terms_limit, merged_aliases)

        # Provenance gate: must-haves are an explicit, user-only content gate. The
        # agent may EXPAND a term the user already set (synonyms fold into existing
        # anchors above, enriching their aliases), but it may NOT mint a NEW anchor
        # from inference. Unless the user explicitly demanded a required term this
        # turn, restrict the result to anchors the user already had and drop the rest.
        if user_requested:
            updated["must_have_answered"] = True
        else:
            existing_canonical, _ = _canonicalize_must_have(
                existing_terms,
                _clean_must_have_aliases(updated.get("must_have_aliases"), terms=existing_terms),
            )
            allowed = {term.casefold() for term in existing_canonical}
            canonical_terms = [term for term in canonical_terms if term.casefold() in allowed]
            surviving = {term.casefold() for term in canonical_terms}
            canonical_aliases = {key: value for key, value in canonical_aliases.items() if key in surviving}

        updated["must_have_terms"] = canonical_terms
        updated["must_have_aliases"] = canonical_aliases
    if "keywords" in patch:
        keywords = _string_list(patch.get("keywords"), limit=16)
        if keywords:
            updated["keywords"] = keywords

    depth = _normalize_depth(patch.get("depth"))
    if depth:
        updated["depth"] = depth

    recency = _normalize_recency(patch.get("recency_weighting"))
    if recency:
        updated["recency_weighting"] = recency
    if "lookback_hours" in patch:
        updated["lookback_hours"] = _coerce_lookback_hours(patch.get("lookback_hours"))

    if "source_queries" in patch:
        # Agent-proposed per-source queries are spell-normalized before merging,
        # skipping the foreign_media and markets lanes inside the helper.
        incoming_source_queries = _normalize_source_query_spelling(_clean_source_queries(patch.get("source_queries")))
        if bool(patch.get("replace_source_queries")):
            updated["source_queries"] = incoming_source_queries
        elif cleanup_requested:
            existing = _clean_source_queries(updated.get("source_queries"))
            for source, queries in incoming_source_queries.items():
                existing[source] = queries
            updated["source_queries"] = existing
        else:
            updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), incoming_source_queries)
    if "foreign_language_plan" in patch:
        updated["foreign_language_plan"] = _merge_foreign_language_plan(
            updated.get("foreign_language_plan"),
            patch.get("foreign_language_plan"),
        )
    if "foreign_regions" in patch:
        updated["foreign_regions"] = _merge_string_lists(
            updated.get("foreign_regions"),
            patch.get("foreign_regions"),
            limit=16,
        )
    if "gmail_rules" in patch:
        updated["gmail_rules"] = _normalize_gmail_rules(patch.get("gmail_rules"))

    requested = _normalize_requested_sources(updated.get("requested_sources"))
    requested_patch = [
        source
        for source in _normalize_requested_sources(patch.get("requested_sources"))
        if _requested_source_was_named_by_user(source, user_text)
    ]
    if requested_patch:
        updated["requested_sources"] = _merge_requested_source_lists(requested, requested_patch)
        updated["requested_sources_answered"] = True

    source_feedback = _extract_requested_sources(user_text)
    source_query_hints: dict[str, list[str]] = {}
    if source_feedback:
        updated["requested_sources"] = _merge_requested_source_lists(
            _normalize_requested_sources(updated.get("requested_sources")),
            source_feedback,
        )
        updated["requested_sources_answered"] = True
        for source in source_feedback:
            adapter = str(source.get("adapter") or "").strip()
            ref = str(source.get("ref") or "").strip()
            if adapter and ref:
                source_query_hints.setdefault(adapter, []).append(ref)
    if source_query_hints:
        updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), source_query_hints)

    additions = _extract_user_strategy_additions(user_text)
    if additions:
        updated["keywords"] = _merge_string_lists(updated.get("keywords"), _keywords(" ".join(additions)), limit=24)
        updated["search_queries"] = _merge_string_lists(updated.get("search_queries"), additions, limit=20)
        selected = _source_selection_dict(updated.get("source_selection"))
        source_additions: dict[str, list[str]] = {}
        for source in ("web_search", "youtube", "podcasts", "reddit", "collections"):
            if selected.get(source):
                source_additions[source] = list(additions)
        if source_additions:
            updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), source_additions)
        if selected.get("podcasts"):
            updated["direct_episode_queries"] = _merge_string_lists(updated.get("direct_episode_queries"), additions, limit=16)
            updated["priority_terms"] = _merge_string_lists(updated.get("priority_terms"), _keywords(" ".join(additions)), limit=16)

    if updated.get("subtopics"):
        updated["related_interests_answered"] = True

    combined_text = " ".join(
        [
            str(updated.get("statement") or ""),
            str(updated.get("scope") or ""),
            " ".join(_string_list(updated.get("subtopics"))),
        ]
    )
    updated["requested_sources"] = _normalize_requested_sources(
        _merge_requested_source_hints(updated, combined_text).get("requested_sources")
    )
    return _coerce_profile(updated)


def _requests_strategy_cleanup(user_text: str) -> bool:
    lowered = str(user_text or "").casefold()
    return any(
        phrase in lowered
        for phrase in (
            "no mention of 2024",
            "not mention of 2024",
            "not referencing 2024",
            "stop looking",
            "stale",
            "old references",
            "old queries",
            "last 7 days",
            "last seven days",
            "did not elicit",
            "fails to update",
            "failed to update",
        )
    )


def _extract_user_strategy_additions(user_text: str) -> list[str]:
    text = " ".join(str(user_text or "").split()).strip()
    if not text:
        return []
    lowered = text.casefold()
    if not any(phrase in lowered for phrase in ("add this", "add it", "include this", "include it", "add to", "include in")):
        return []
    candidates: list[str] = []
    for match in re.finditer(r"\*\*(.+?)\*\*", text):
        candidates.append(match.group(1))
    tail_match = re.search(r"(?:add|include)(?:\s+this)?(?:\s+to\s+(?:it|the\s+strategy))?\s*:\s*(.+)$", text, flags=re.IGNORECASE)
    if tail_match:
        candidates.append(tail_match.group(1))
    cleaned: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = re.sub(r"\([^)]*\)", "", candidate)
        value = re.sub(r"\bor\b.*$", "", value, flags=re.IGNORECASE)
        value = " ".join(value.replace("*", "").split()).strip(" .?;:")
        if not value:
            continue
        key = value.casefold()
        if key not in seen:
            cleaned.append(value[:180])
            seen.add(key)
    return cleaned[:4]


def _format_strategy_snapshot(profile: dict[str, Any]) -> str:
    selection = _source_selection_dict(profile.get("source_selection"))
    source_queries = _clean_source_queries(profile.get("source_queries"))
    lines = [
        "Current strategy:",
        f"- Scope: {str(profile.get('scope') or profile.get('statement') or '').strip()}",
        f"- Recency: {_strategy_recency_label(profile)}",
        "- Sources: " + ", ".join(_SOURCE_DISPLAY.get(source, source) for source, selected in selection.items() if selected),
    ]
    search_queries = _string_list(profile.get("search_queries"), limit=8)
    if search_queries:
        lines.append("- General queries: " + "; ".join(search_queries))
    for source, label in _SOURCE_DISPLAY.items():
        if not selection.get(source):
            continue
        queries = source_queries.get(source, [])
        if queries:
            lines.append(f"- {label}: " + "; ".join(queries[:8]))
    podcast_bits = _string_list(profile.get("direct_episode_queries"), limit=6)
    if podcast_bits:
        lines.append("- Podcast direct: " + "; ".join(podcast_bits))
    related = _string_list(profile.get("related_episode_queries"), limit=6)
    if related:
        lines.append("- Podcast related: " + "; ".join(related))
    priority = _string_list(profile.get("priority_terms"), limit=8)
    if priority:
        lines.append("- Priority terms: " + "; ".join(priority))
    foreign = _normalize_foreign_language_plan(profile.get("foreign_language_plan"))
    for item in foreign[:4]:
        lines.append(f"- {item['name']}: {item['native_query']}")
    return "\n".join(line for line in lines if line.strip())


def _strategy_recency_label(profile: dict[str, Any]) -> str:
    lookback_hours = _coerce_lookback_hours(profile.get("lookback_hours"))
    if lookback_hours:
        if lookback_hours <= 48:
            return f"last {lookback_hours} hours"
        days = max(1, round(lookback_hours / 24))
        return f"last {days} days"
    return str(profile.get("recency_weighting") or "recent")


def _is_refinement_closing_language(text: str) -> bool:
    lowered = str(text or "").casefold()
    closing_patterns = (
        r"\bi(?:'|’)m ready to build\b",
        r"\bready to build\b",
        r"\bready for (?:the )?brief\b",
        r"\bready to run\b",
        r"\bi have (?:everything|all) (?:i|we) need\b",
        r"\b(?:we|i) have (?:everything|all) (?:we|i) need\b",
        r"\bthe plan is (?:complete|done|locked)\b",
        r"\bi(?:'|’)ve locked in\b",
        r"\bi have locked in\b",
        r"\bsearch strategy confirmed\b",
    )
    return any(re.search(pattern, lowered) for pattern in closing_patterns)


def _is_generic_actionable_question(question: str) -> bool:
    lowered = question.casefold()
    return (
        "what would make this brief actionable" in lowered
        or (
            "actionable" in lowered
            and "catalysts" in lowered
            and "valuation" in lowered
            and "company" in lowered
        )
    )


def _source_selection_dict(value: Any) -> dict[str, bool]:
    selected = dict(DEFAULT_EXPLORE_SOURCE_SELECTION)
    if isinstance(value, dict):
        for key, flag in value.items():
            source_key = str(key)
            if source_key in VALID_SOURCE_ADAPTERS:
                selected[source_key] = bool(flag)
    return selected


# Bare stopwords / generic filler that carry no search signal. A query made up
# entirely of these tokens (e.g. "either", "various things", "some stuff") names
# no concrete entity or topic and only pollutes the search pipeline. We match on
# this curated set rather than a length heuristic so legitimate short queries —
# ticker symbols ("AAPL"), proper nouns ("Nvidia"), and foreign-language terms —
# survive untouched.
_FILLER_STOPWORDS: frozenset[str] = frozenset(
    {
        # articles / conjunctions / determiners
        "a", "an", "the", "and", "or", "nor", "but", "either", "neither", "both",
        "any", "all", "some", "each", "every", "no", "none", "such", "same",
        "other", "another", "this", "that", "these", "those",
        # pronouns
        "i", "we", "you", "he", "she", "it", "they", "them", "us", "me", "him",
        "her", "his", "its", "our", "your", "their", "who", "whom", "whose",
        # prepositions / particles
        "of", "to", "in", "on", "at", "by", "for", "with", "from", "as", "into",
        "onto", "about", "over", "under", "between", "through", "during", "per",
        # auxiliaries / common verbs
        "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
        "did", "have", "has", "had", "will", "would", "shall", "should", "can",
        "could", "may", "might", "must", "get", "got", "make", "made",
        # generic filler nouns / adverbs / quantifiers
        "thing", "things", "stuff", "various", "etc", "etcetera", "misc",
        "miscellaneous", "general", "generic", "something", "anything",
        "everything", "nothing", "someone", "anyone", "everyone", "somewhere",
        "anywhere", "everywhere", "lot", "lots", "more", "most", "much", "many",
        "few", "less", "least", "very", "really", "quite", "rather", "just",
        "only", "also", "too", "so", "then", "than", "now", "here", "there",
        "where", "when", "why", "how", "what", "which", "while", "because",
        "if", "else", "not", "yes", "ok", "okay", "thing's", "kind", "sort",
        "type", "way", "ways", "stuffs",
    }
)

# Token splitter that keeps alphanumerics together (so "AAPL", "GPT-4" survive)
# but strips surrounding punctuation when deciding whether a query is filler.
_WORD_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _is_filler_query(text: str) -> bool:
    """True when a query carries no descriptive content and should be dropped.

    A query is filler when, after stripping punctuation, it contains no
    alphanumeric token, or every token is a bare stopword/generic-filler word.
    Any token outside the stopword set (a ticker, proper noun, or
    foreign-language word, none of which appear in the English filler set)
    keeps the query.
    """
    tokens = _WORD_TOKEN_RE.findall(text.casefold())
    if not tokens:
        return True
    return all(token in _FILLER_STOPWORDS for token in tokens)


def _string_list(value: Any, *, limit: int = 24) -> list[str]:
    if isinstance(value, str):
        value = [item for item in re.split(r"[,;\n]", value) if item.strip()]
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = " ".join(str(item or "").split()).strip(" .")
        key = text.lower()
        if not text or key in seen:
            continue
        if _is_filler_query(text):
            continue
        cleaned.append(text[:180])
        seen.add(key)
        if len(cleaned) >= limit:
            break
    return cleaned


# High-confidence English misspelling → correction map for the lightweight
# post-validation pass on model-proposed search terms. Kept deliberately small and
# unambiguous: every entry is a common typo with a single obvious correction, so we
# never "fix" a real word into the wrong one. Spelling of proper nouns, brands, and
# people is handled by the prompt instruction, not here, because a dictionary cannot
# safely distinguish a misspelled name from a deliberate one. Foreign-language tokens
# are skipped entirely (see _normalize_query_spelling) so native queries are never
# flagged as misspelled.
_COMMON_QUERY_MISSPELLINGS: dict[str, str] = {
    "teh": "the",
    "adn": "and",
    "ot": "to",
    "recieve": "receive",
    "seperate": "separate",
    "definately": "definitely",
    "occured": "occurred",
    "occurance": "occurrence",
    "goverment": "government",
    "governement": "government",
    "enviroment": "environment",
    "buisness": "business",
    "calender": "calendar",
    "untill": "until",
    "accross": "across",
    "neccessary": "necessary",
    "necesary": "necessary",
    "publically": "publicly",
    "managment": "management",
    "developement": "development",
    "independant": "independent",
    "comittee": "committee",
    "begining": "beginning",
    "beleive": "believe",
    "concious": "conscious",
    "embarass": "embarrass",
    "existance": "existence",
    "maintainance": "maintenance",
    "persistant": "persistent",
    "priviledge": "privilege",
    "refered": "referred",
    "relevent": "relevant",
    "succesful": "successful",
    "successfull": "successful",
    "tommorow": "tomorrow",
    "tarrif": "tariff",
    "tarrifs": "tariffs",
    "semiconducter": "semiconductor",
    "semiconducters": "semiconductors",
    "artifical": "artificial",
    "inteligence": "intelligence",
    "intelligance": "intelligence",
    "techology": "technology",
    "tecnology": "technology",
    "compeitition": "competition",
    "competiton": "competition",
    "annoucement": "announcement",
    "announcment": "announcement",
    "earnigs": "earnings",
    "elecion": "election",
    "regualtion": "regulation",
    "regulaton": "regulation",
}

_QUERY_TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _match_case(correction: str, original: str) -> str:
    if original.isupper() and len(original) > 1:
        return correction.upper()
    if original[:1].isupper():
        return correction[:1].upper() + correction[1:]
    return correction


def _normalize_query_spelling(text: str) -> str:
    """Conservatively correct obvious English typos in a single search query.

    Only ASCII alphabetic tokens whose lowercase form is in the curated misspelling
    map are touched, preserving the original capitalization. Any token containing a
    non-ASCII character (accents, CJK, Cyrillic, etc.) is left untouched so native
    foreign-language queries are never altered.
    """
    if not text or not text.isascii():
        # A string with any non-ASCII character is treated as foreign-language and
        # left verbatim; mixing a typo map into it risks corrupting native terms.
        return text

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        correction = _COMMON_QUERY_MISSPELLINGS.get(token.lower())
        if not correction:
            return token
        return _match_case(correction, token)

    return _QUERY_TOKEN_RE.sub(_replace, text)


def _normalize_query_list_spelling(queries: Any) -> list[str]:
    return [_normalize_query_spelling(q) for q in _string_list(queries, limit=20)]


def _normalize_source_query_spelling(source_queries: dict[str, list[str]]) -> dict[str, list[str]]:
    """Apply the conservative spelling pass to a cleaned source-query map.

    Skips lanes whose queries are intentionally not plain English: foreign_media
    (native-language) and markets (ticker symbols).
    """
    corrected: dict[str, list[str]] = {}
    for source, queries in source_queries.items():
        if source in {"foreign_media", "markets"}:
            corrected[source] = list(queries)
        else:
            corrected[source] = [_normalize_query_spelling(q) for q in queries]
    return corrected


def _merge_string_lists(existing: Any, incoming: Any, *, limit: int) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*_string_list(existing, limit=limit), *_string_list(incoming, limit=limit)]:
        key = item.lower()
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)
        if len(merged) >= limit:
            break
    return merged


def _clean_source_queries(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, list[str]] = {}
    for key, queries in value.items():
        source_key = str(key)
        if source_key not in VALID_SOURCE_ADAPTERS:
            continue
        query_list = _string_list(queries, limit=20)
        if source_key == "markets":
            # The markets lane is the explicit ticker lane: validate each entry as a
            # ticker (company names/cashtags resolved, acronyms/junk dropped).
            query_list = normalize_market_query_tickers(query_list)[:20]
        if query_list:
            cleaned[source_key] = query_list
    return cleaned


def _clean_must_have_aliases(value: Any, *, terms: list[str] | None = None) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    allowed = {term.casefold() for term in terms or [] if term}
    cleaned: dict[str, list[str]] = {}
    for raw_key, raw_values in value.items():
        key = str(raw_key or "").strip().casefold()
        if not key:
            continue
        if allowed and key not in allowed:
            continue
        aliases = _string_list(raw_values, limit=12)
        if aliases:
            cleaned[key] = aliases
    return cleaned


def _merge_source_queries(existing: Any, incoming: Any) -> dict[str, list[str]]:
    merged = _clean_source_queries(existing)
    for key, values in _clean_source_queries(incoming).items():
        merged[key] = _merge_string_lists(merged.get(key), values, limit=20)
    return merged


def _normalize_gmail_rules(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    rules: dict[str, Any] = {}
    intent = " ".join(str(value.get("intent") or value.get("query") or "").split()).strip()
    if intent:
        rules["intent"] = intent[:240]
    gmail_search_query = " ".join(str(value.get("gmail_search_query") or "").split()).strip()
    if gmail_search_query:
        rules["gmail_search_query"] = gmail_search_query[:240]
    selection_criteria = " ".join(str(value.get("selection_criteria") or "").split()).strip()
    if selection_criteria:
        rules["selection_criteria"] = selection_criteria[:300]
    if value.get("ai_managed") is not None:
        rules["ai_managed"] = bool(value.get("ai_managed"))
    lookback_hours = _coerce_lookback_hours(value.get("lookback_hours"))
    if lookback_hours:
        rules["lookback_hours"] = lookback_hours
    include_senders = _email_list(value.get("include_senders"))
    if include_senders:
        rules["include_senders"] = include_senders
    candidates = value.get("candidates")
    if isinstance(candidates, list):
        cleaned_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            sender = str(candidate.get("sender") or "").strip().lower()
            if not sender:
                continue
            try:
                message_count = max(1, int(candidate.get("message_count") or 1))
            except (TypeError, ValueError):
                message_count = 1
            cleaned_candidates.append(
                {
                    "sender": sender,
                    "sender_name": str(candidate.get("sender_name") or "").strip()[:120],
                    "subject": str(candidate.get("subject") or "").strip()[:240],
                    "message_count": message_count,
                    "latest_at": str(candidate.get("latest_at") or "").strip() or None,
                    "ai_rationale": str(candidate.get("ai_rationale") or "").strip()[:240],
                }
            )
            if len(cleaned_candidates) >= 12:
                break
        if cleaned_candidates:
            rules["candidates"] = cleaned_candidates
    return rules


def _email_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [item for item in re.split(r"[,;\n]", value) if item.strip()]
    if not isinstance(value, list):
        return []
    emails: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip().lower()
        matches = re.findall(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}", text)
        for email in matches:
            if email in seen:
                continue
            emails.append(email)
            seen.add(email)
    return emails


def _normalize_foreign_language_plan(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    known_languages = {str(item["code"]): item for item in trusted_language_options()}
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("language") or "").strip().lower()
        if not re.match(r"^[a-z]{2,4}$", code) or code in seen:
            continue
        native_query = " ".join(str(item.get("native_query") or "").split()).strip()
        if not native_query:
            continue
        known = known_languages.get(code)
        name = str(item.get("name") or "").strip() or (str(known["name"]) if known else code.upper())
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
        if len(cleaned) >= 10:
            break
    return cleaned


def _merge_foreign_language_plan(existing: Any, incoming: Any) -> list[dict[str, Any]]:
    merged = _normalize_foreign_language_plan(existing)
    seen = {item["code"] for item in merged}
    for item in _normalize_foreign_language_plan(incoming):
        if item["code"] in seen:
            continue
        merged.append(item)
        seen.add(item["code"])
        if len(merged) >= 10:
            break
    return merged


def _merge_requested_source_lists(
    existing: list[dict[str, str]],
    incoming: list[dict[str, str]],
) -> list[dict[str, str]]:
    merged = list(existing)
    seen = {(source["adapter"], source["ref"].lower()) for source in merged}
    for source in incoming:
        key = (source["adapter"], source["ref"].lower())
        if key in seen:
            continue
        merged.append(source)
        seen.add(key)
    return merged


def _normalize_depth(value: Any) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return ""
    if lowered in {"practitioner", "technical", "deep", "advanced", "hands-on", "implementation", "how-to"}:
        return "practitioner"
    if lowered in {"informed-generalist", "informed_generalist", "generalist", "balanced"}:
        return "informed-generalist"
    return ""


def _normalize_recency(value: Any) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    if lowered in {"breaking", "breaking_news", "latest", "current", "fresh", "new"}:
        return "breaking"
    if lowered in {"recent", "recent_time", "fresh_time", "last_30_days", "last_month", "balanced", "mix", "both"}:
        return "recent"
    if lowered in {"last_year", "within_last_year", "past_year", "last_12_months", "year", "one_year", "a_year", "no_more_than_a_year_old"}:
        return "last_year"
    if lowered in {
        "all_available",
        "as_much_as_possible",
        "all",
        "broad",
        "broadest",
        "maximum",
        "exhaustive",
        "evergreen",
        "background",
        "timeless",
        "foundational",
        "baseline",
    }:
        return "all_available"
    return ""


def _coerce_lookback_hours(value: Any) -> int | None:
    if value is None:
        return None
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return None
    if hours < 1:
        return None
    return min(hours, 262800)


def _run_sync_complete_json(coro: object) -> dict[str, Any]:
    if not hasattr(coro, "__await__"):
        return {}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_event_loop(coro)

    result: dict[str, Any] = {}
    error: BaseException | None = None
    event = threading.Event()

    def _runner() -> None:
        nonlocal result, error
        try:
            # mypy cannot infer awaitables here; this executes in a dedicated thread.
            result = asyncio.run(coro)  # type: ignore[call-arg]
        except BaseException as exc:  # pragma: no cover - defensive path.
            error = exc
        finally:
            event.set()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    event.wait()
    thread.join()
    if error is not None:
        raise error
    return result


def _run_event_loop(coro: object) -> dict[str, Any]:
    return asyncio.run(coro)  # type: ignore[call-arg]


def _run_sync_list(coro: object) -> list[Any]:
    result_holder: dict[str, list[Any]] = {}
    error_holder: dict[str, BaseException] = {}

    def run() -> None:
        try:
            result_holder["result"] = asyncio.run(coro)  # type: ignore[call-arg]
        except BaseException as exc:  # pragma: no cover - defensive bridge.
            error_holder["error"] = exc

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[call-arg]

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join()
    if error_holder:
        raise error_holder["error"]
    return result_holder.get("result", [])


def _fill_defaults(profile: dict[str, Any]) -> dict[str, Any]:
    updated = dict(profile)
    if not str(updated.get("scope") or "").strip():
        updated["scope"] = str(updated.get("statement") or "").strip()
    updated["depth"] = updated.get("depth") or "informed-generalist"
    updated["recency_weighting"] = _normalize_recency(updated.get("recency_weighting")) or "recent"
    if not isinstance(updated.get("exclusions"), list):
        updated["exclusions"] = []
    updated = _ensure_source_query_coverage(updated)
    updated["refinement_diagnostics"] = _baseline_diagnostics(updated)
    return _coerce_profile(updated)


def _ensure_source_query_coverage(profile: dict[str, Any]) -> dict[str, Any]:
    """Give every selected source a real query so connectors don't fall back to a generic blob."""
    selection = _source_selection_dict(profile.get("source_selection"))
    queries = dict(_clean_source_queries(profile.get("source_queries")))
    phrase_queries = [
        q for q in _string_list(profile.get("search_queries"), limit=20)
        if not _is_conversational_sentence(q)
    ]
    if not phrase_queries:
        # Agent produced no usable search_queries — derive compact ones from the
        # statement so we never submit a multi-sentence paragraph as a search term.
        phrase_queries = _queries_from_statement(
            str(profile.get("scope") or profile.get("statement") or "")
        )

    # Filter any existing per-source queries that are conversational sentences.
    for src in list(queries.keys()):
        clean = [q for q in queries[src] if not _is_conversational_sentence(q)]
        if clean:
            queries[src] = clean
        else:
            del queries[src]

    # For markets, always resolve to actual ticker symbols — never use keyword soup.
    profile_text = " ".join(filter(None, [
        str(profile.get("statement") or ""),
        str(profile.get("scope") or ""),
        *_string_list(profile.get("keywords")),
        *_string_list(profile.get("subtopics")),
        *_string_list(profile.get("search_queries")),
    ]))
    resolved_tickers = resolve_tickers_from_text(profile_text)

    foreign_fallback = _foreign_media_fallback_queries(profile)
    for source, enabled in selection.items():
        if not enabled:
            continue
        if source == "foreign_media" and queries.get(source) and foreign_fallback:
            queries[source] = _merge_string_lists(queries[source], foreign_fallback, limit=8)
            continue
        if queries.get(source):
            continue
        fallback = _source_specific_fallback(
            source,
            phrase_queries=phrase_queries,
            resolved_tickers=resolved_tickers,
            foreign_fallback=foreign_fallback,
            keywords=_string_list(profile.get("keywords")),
        )
        if fallback:
            queries[source] = fallback
    return {**profile, "source_queries": queries}


def _foreign_media_fallback_queries(profile: dict[str, Any]) -> list[str]:
    """Native-language fallback for foreign media — never English search phrases."""
    out: list[str] = []
    for lane in _normalize_foreign_language_plan(profile.get("foreign_language_plan")):
        native_query = str(lane.get("native_query") or "").strip()
        if native_query:
            out.append(native_query)
        out.extend(lane.get("native_entity_terms") or [])
    profile_text = " ".join(
        [
            str(profile.get("statement") or ""),
            str(profile.get("scope") or ""),
            " ".join(_string_list(profile.get("keywords"))),
            " ".join(_string_list(profile.get("search_queries"))),
        ]
    ).casefold()
    if any(term in profile_text for term in ("china", "chinese", "qwen", "deepseek", "kimi", "minimax", "moonshot")):
        out.extend(
            [
                "中国大模型 最新进展 DeepSeek 通义千问 Kimi MiniMax",
                "DeepSeek 通义千问 Kimi MiniMax 性能评测",
                "国产大模型 竞争 OpenAI Anthropic 最新消息",
                "月之暗面 Kimi MiniMax 智谱 百度 文心一言 豆包 混元",
                "华为昇腾 国产算力 大模型 训练 推理",
            ]
        )
    return _string_list(out, limit=6)


def _source_specific_fallback(
    source: str,
    *,
    phrase_queries: list[str],
    resolved_tickers: list[str],
    foreign_fallback: list[str],
    keywords: list[str] = None,
) -> list[str]:
    """Shape fallback queries to how each source is actually searched.

    Avoids copying one identical phrase into every source: markets gets ticker
    symbols only (never descriptive text), foreign media gets native-language
    terms only (never English), and community/video/audio sources get lightly
    differentiated phrasing instead of the raw web phrase.
    """
    if source == "markets":
        return list(resolved_tickers)
    if source == "foreign_media":
        return list(foreign_fallback)
    base = phrase_queries[:3]
    if source == "youtube":
        return [f"{query} explained" for query in base]
    if source == "podcasts":
        return _podcast_discovery_fallback_queries(base, keywords=keywords)
    return list(base)


def _podcast_discovery_fallback_queries(base: list[str], keywords: list[str] = None) -> list[str]:
    out: list[str] = []
    # If we have keywords, use them directly as broader search terms
    if keywords:
        for kw in keywords[:6]:
            if kw and len(kw) > 2:
                out.append(kw)
    
    # Also add shorter queries with podcast/interview suffixes
    for query in base[:4]:
        if len(query) < 60:
            out.extend([f"{query} podcast", f"{query} interview"])
            
    # Fallback to general terms if nothing else was added
    if not out:
        for query in base[:4]:
            out.extend(
                [
                    f"{query} podcast",
                    f"{query} interview",
                    f"{query} audio analysis",
                ]
            )
            
    return _string_list(out, limit=8)


def _baseline_diagnostics(profile: dict[str, Any]) -> dict[str, Any]:
    """Deterministic, side-effect-free snapshot of the finalized strategy.

    Records what each source will actually run so a developer can audit why a
    brief retrieved what it did. The model path enriches this with the agent's
    raw patch and the critique diff via _apply_agent_update.
    """
    selection = _source_selection_dict(profile.get("source_selection"))
    source_queries = _clean_source_queries(profile.get("source_queries"))
    availability = {
        source: {
            "selected": bool(selection.get(source)),
            "query_count": len(source_queries.get(source, [])),
        }
        for source in sorted(VALID_SOURCE_ADAPTERS)
    }
    existing = profile.get("refinement_diagnostics")
    diagnostics = dict(existing) if isinstance(existing, dict) else {}
    diagnostics.update(
        {
            "source_availability": availability,
            "final_source_queries": {src: list(qs) for src, qs in source_queries.items()},
            "final_search_queries": _string_list(profile.get("search_queries"), limit=20),
            "final_podcast_strategy": {
                field: _string_list(profile.get(field), limit=16)
                for field in PODCAST_STRATEGY_FIELDS
            },
        }
    )
    diagnostics.setdefault("readiness_reason", "defaults_filled")
    return diagnostics


def _readiness_reason(*, ready_requested: bool, just_go_now: bool, turn_count: int) -> str:
    if just_go_now:
        return "just_go_now"
    if turn_count >= MAX_REFINEMENT_TURNS:
        return "max_turns"
    if ready_requested:
        return "model_ready"
    return "defaults_filled"


def _diagnostics_query_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "search_queries": _string_list(profile.get("search_queries"), limit=20),
        "source_queries": _clean_source_queries(profile.get("source_queries")),
        "podcast_strategy": {
            field: _string_list(profile.get(field), limit=16)
            for field in PODCAST_STRATEGY_FIELDS
        },
    }


def _trim_for_diagnostics(value: Any, *, _depth: int = 0) -> Any:
    if isinstance(value, dict):
        if _depth >= 4:
            return "…"
        return {str(key): _trim_for_diagnostics(item, _depth=_depth + 1) for key, item in list(value.items())[:24]}
    if isinstance(value, list):
        return [_trim_for_diagnostics(item, _depth=_depth + 1) for item in value[:24]]
    if isinstance(value, str):
        return value[:300]
    return value


def _enrich_diagnostics(
    profile: dict[str, Any],
    *,
    model_profile_patch: Any,
    pre_critique: dict[str, Any],
    readiness_reason: str,
) -> dict[str, Any]:
    diagnostics = _baseline_diagnostics(profile)
    diagnostics["readiness_reason"] = readiness_reason
    if isinstance(model_profile_patch, dict):
        diagnostics["model_profile_patch"] = _trim_for_diagnostics(model_profile_patch)
    pre_source = pre_critique.get("source_queries") if isinstance(pre_critique, dict) else {}
    pre_search = pre_critique.get("search_queries") if isinstance(pre_critique, dict) else []
    post_source = _clean_source_queries(profile.get("source_queries"))
    post_search = _string_list(profile.get("search_queries"), limit=20)
    pre_podcast = pre_critique.get("podcast_strategy") if isinstance(pre_critique, dict) else {}
    post_podcast = {
        field: _string_list(profile.get(field), limit=16)
        for field in PODCAST_STRATEGY_FIELDS
    }
    source_added: dict[str, int] = {}
    for src, queries in post_source.items():
        added = max(0, len(queries) - len((pre_source or {}).get(src, [])))
        if added:
            source_added[src] = added
    diagnostics["critique_changes"] = {
        "search_queries_added": max(0, len(post_search) - len(pre_search or [])),
        "source_queries_added": source_added,
        "podcast_strategy_changed": pre_podcast != post_podcast,
    }
    return diagnostics


def _is_conversational_sentence(text: str) -> bool:
    """Return True when text reads like a typed request rather than a search query."""
    t = text.strip()
    if len(t) > 100:
        return True
    lowered = t.lower()
    conversational = (
        "i am ", "i'm ", "i want", "help me", "be sure", "please ", "looking to",
        "looking for", "identify ", "find me ", "give me ", "tell me ", "make sure",
    )
    return any(lowered.startswith(c) or f" {c}" in lowered for c in conversational)


def _queries_from_statement(text: str) -> list[str]:
    """Generate 2–4 compact search queries from a free-form topic statement.

    Extracts named entities (capitalised runs) and the first short phrase, which
    together produce search-engine-ready strings even when the refinement agent
    fails to generate queries.
    """
    text = text.strip()
    if not text:
        return []

    parts: list[str] = []

    # 1. Named entities: (a) capitalised runs, (b) items listed after "like/including/such as".
    _stops = {
        "I", "The", "A", "An", "My", "We", "Help", "Use", "Return", "It", "Is", "In",
        "Companies", "Company", "Please", "Also", "Both", "Each",
    }
    cap_names = [
        n for n in re.findall(r"\b[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*\b", text)
        if n not in _stops and len(n) > 2
    ]
    # Also pull items after "companies like / including / such as" — often lowercase
    list_items: list[str] = []
    for m in re.finditer(
        r"\b(?:companies like|including|such as)\s+([^.!?;]{3,80})",
        text, flags=re.IGNORECASE,
    ):
        raw_items = re.split(r"[,\s]+and\s+|,\s*", m.group(1))
        for raw in raw_items:
            # Keep only the first token(s) that look like a proper name (no verbs/articles).
            clean = re.match(r"^([A-Za-z][A-Za-z0-9&.\-]+(?:\s+[A-Z][A-Za-z0-9&.\-]+)*)", raw.strip())
            item = clean.group(1).strip().title() if clean else ""
            if 2 < len(item) < 25 and item.lower() not in {
                "should", "will", "must", "are", "is", "the", "and", "or",
            }:
                list_items.append(item)
    names = list(dict.fromkeys(cap_names + list_items))
    entity_chunk = " ".join(names[:4])  # up to 4, order-deduped
    if entity_chunk:
        parts.append(f"{entity_chunk} news")

    # 2. Domain theme words combined with entity names.
    theme_words = re.findall(
        r"\b(AI|artificial intelligence|machine learning|semiconductor|HBM|NAND|DRAM|"
        r"investment|portfolio|infrastructure|picks.and.shovels|earnings|growth|"
        r"local model|Apple Silicon|MLX|agents?|LLM|inference)\b",
        text,
        flags=re.IGNORECASE,
    )
    if theme_words:
        theme_chunk = " ".join(dict.fromkeys(w.lower() for w in theme_words[:4]))
        lead = " ".join(dict.fromkeys(names[:2])) if names else ""
        parts.append((f"{lead} {theme_chunk}" if lead else theme_chunk).strip())

    # 3. First short phrase before the first sentence-ending punctuation.
    first_phrase = re.split(r"[.!?;]", text)[0].strip()
    if len(first_phrase) <= 80 and not _is_conversational_sentence(first_phrase):
        parts.append(first_phrase)

    # Dedupe and cap at 4.
    seen: set[str] = set()
    result: list[str] = []
    for q in parts:
        key = q.casefold()
        if q and key not in seen:
            seen.add(key)
            result.append(q)
    return result[:4] or [text[:80].strip()]


def _next_missing(profile: dict[str, Any]) -> str | None:
    for field in FIELD_ORDER:
        value = profile.get(field)
        if field == "scope" and not str(value or "").strip():
            return field
        if field == "related_interests" and not bool(profile.get("related_interests_answered")):
            return field
        if field == "depth" and not bool(profile.get("depth_answered")):
            if _next_priority_field(profile) != field:
                continue
            return field
        if field == "recency_weighting" and not bool(profile.get("source_scope_answered")):
            if _next_priority_field(profile) != field:
                continue
            return field
        if field == "requested_sources" and not bool(profile.get("requested_sources_answered")):
            return field
        if field == "exclusions" and not profile.get("exclusions") and not bool(profile.get("exclusions_answered")):
            return field
        if field == "must_have" and not bool(profile.get("must_have_answered")) and not _string_list(profile.get("must_have_terms"), limit=6):
            return field
    return None


def _required_confirmation_field(profile: dict[str, Any]) -> str | None:
    if not bool(profile.get("depth_answered")):
        return "depth"
    if not bool(profile.get("source_scope_answered")):
        return "recency_weighting"
    return None


_QUESTION_VARIANTS: dict[str, tuple[str, ...]] = {
    "scope": (
        "What angle would make this most useful for you?",
        "If this brief nailed one thing, what would it be?",
        "What's the outcome you're after here — what would make it land?",
    ),
    "related_interests": (
        "Are there adjacent threads worth pulling in, or should I keep it tight?",
        "Anything nearby you'd want folded in, or keep the aperture narrow?",
        "Any related angles I should weave in while I'm at it?",
    ),
    "depth": (
        "Do you want practical, get-things-done coverage, or a deeper expert-level read?",
        "Should this read like a practitioner's working brief or a deeper analytical dive?",
        "Are you after hands-on takeaways or a more thorough, expert-level treatment?",
    ),
    "recency_weighting": (
        "How fresh does the material need to be — the last day or two, the past week, or is older context fine?",
        "What time window matters here: breaking news, the last week or so, or best available regardless of date?",
        "How recent should sources be — latest only, recent, or is evergreen background welcome?",
    ),
    "exclusions": (
        "Anything I should steer clear of so the brief stays focused?",
        "Any sources, angles, or noise you want me to filter out?",
        "Is there anything that would be a waste of space for you here?",
    ),
}


_SOURCE_PROMPT_LABELS: dict[str, str] = {
    "podcasts": "shows or hosts",
    "youtube": "channels or creators",
    "markets": "companies or tickers",
    "foreign_media": "regions or languages",
    "collections": "files or collections",
    "gmail": "newsletters",
    "web_search": "sites or publications",
}


def _pick_variant(field: str, profile: dict[str, Any]) -> str:
    variants = _QUESTION_VARIANTS.get(field)
    if not variants:
        return QUESTIONS.get(field, "What else should I know to make this brief useful?")
    answered = sum(
        1
        for flag in (
            "related_interests_answered",
            "depth_answered",
            "source_scope_answered",
            "requested_sources_answered",
            "exclusions_answered",
        )
        if profile.get(flag)
    )
    index = (len(str(profile.get("statement") or "")) + answered) % len(variants)
    return variants[index]


def _requested_sources_question(profile: dict[str, Any]) -> str:
    selected = [
        source
        for source, enabled in _source_selection_dict(profile.get("source_selection")).items()
        if enabled and source in _SOURCE_PROMPT_LABELS
    ]
    labels = [_SOURCE_PROMPT_LABELS[source] for source in selected]
    if not labels:
        return QUESTIONS["requested_sources"]
    if len(labels) == 1:
        return f"Any specific {labels[0]} I should make sure to include?"
    listed = ", ".join(labels[:-1]) + f", or {labels[-1]}"
    return f"Any specific {listed} I should make sure to include?"


def _deterministic_question(field: str, profile: dict[str, Any]) -> str:
    if field == "requested_sources":
        return _requested_sources_question(profile)
    if field in _QUESTION_VARIANTS:
        return _pick_variant(field, profile)
    return QUESTIONS.get(field, "What else should I know to make this brief useful?")


def _search_strategy_question(profile: dict[str, Any]) -> str:
    selected_sources = [
        source
        for source, enabled in _source_selection_dict(profile.get("source_selection")).items()
        if enabled
    ]
    if "web_search" in selected_sources:
        return "What phrases, people, places, products, or sources should the web search definitely look for?"
    if selected_sources:
        return "What terms or source names should the selected sources definitely search for?"
    return "What should the search definitely include or avoid?"


def _strategy_deepening_question(profile: dict[str, Any], messages: list[dict[str, str]]) -> str:
    text = _user_authored_text(profile, messages)
    markets_selected = _source_selection_dict(profile.get("source_selection")).get("markets", False)
    if markets_selected and _market_tracking_interest(text):
        return _first_unasked_question(
            messages,
            [
                "I have market signals in the plan; which companies, suppliers, or catalysts are missing from the search strategy?",
                "Which evidence should carry the most weight: company filings, earnings commentary, pricing data, supply-chain capacity, customer demand, or analyst revisions?",
                "Should the market section be organized by company, signal type, or near-term catalyst?",
            ],
        )
    if _string_list(profile.get("search_queries")) or _clean_source_queries(profile.get("source_queries")):
        evidence_already_answered = any(
            term in text.casefold()
            for term in (
                "primary reporting",
                "expert analysis",
                "community signal",
                "community signals",
                "practical example",
                "practical examples",
            )
        )
        candidates = [
            "What kind of evidence should I trust most for this brief: primary reporting, expert analysis, community signal, or practical examples?",
            "Should the brief prioritize breadth across sources or depth on the strongest few items?",
            "Looking at this search strategy, what sources, entities, or angles are missing?",
        ]
        if evidence_already_answered:
            candidates = candidates[1:]
        return _first_unasked_question(
            messages,
            candidates,
        )
    return _first_unasked_question(
        messages,
        [
            _search_strategy_question(profile),
            "What sources, entities, or angles should the search strategy add before I build?",
            "Should I prioritize breadth across sources or depth on the strongest few items?",
        ],
    )


def _dedupe_next_question(question: str | None, profile: dict[str, Any], messages: list[dict[str, str]]) -> str | None:
    if not question:
        return None
    if not _question_was_recently_asked(question, messages):
        return question
    replacement = _strategy_deepening_question(profile, messages)
    if replacement and not _question_was_recently_asked(replacement, messages):
        return replacement
    return None


def _first_unasked_question(messages: list[dict[str, str]], candidates: list[str]) -> str:
    for candidate in candidates:
        if candidate and not _question_was_recently_asked(candidate, messages):
            return candidate
    return candidates[-1] if candidates else "What else would make this brief more useful?"


def _question_was_recently_asked(question: str, messages: list[dict[str, str]]) -> bool:
    normalized = _normalize_question_for_repeat_check(question)
    if not normalized:
        return False
    for message in messages[-12:]:
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or ""
        # Prior assistant turns are usually multi-sentence; the repeated question is
        # typically just the trailing sentence, so compare both the whole message and
        # its trailing question rather than only an exact whole-message match.
        if _normalize_question_for_repeat_check(content) == normalized:
            return True
        if _normalize_question_for_repeat_check(_trailing_question_text(content)) == normalized:
            return True
    return False


def _trailing_question_text(text: str) -> str:
    clean = str(text or "").strip()
    if not clean.endswith("?"):
        return ""
    match = re.search(r"([^.!?\n][^.!?\n]*\?)\s*$", clean)
    return match.group(1).strip() if match else clean


def _normalize_question_for_repeat_check(question: str) -> str:
    lowered = str(question or "").lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(lowered.split())


def _question_repeats_answered_constraint(question: str, profile: dict[str, Any]) -> bool:
    field = _field_from_question(question)
    if field == "recency_weighting":
        return bool(profile.get("source_scope_answered")) or bool(_normalize_recency(profile.get("recency_weighting")))
    if field == "exclusions":
        return bool(profile.get("exclusions_answered")) or bool(_string_list(profile.get("exclusions")))
    if field == "must_have":
        return bool(profile.get("must_have_answered")) or bool(_string_list(profile.get("must_have_terms"), limit=6))
    if field == "requested_sources":
        return bool(profile.get("requested_sources_answered")) or bool(_normalize_requested_sources(profile.get("requested_sources")))
    if field == "depth":
        return bool(profile.get("depth_answered"))
    if field == "related_interests":
        return bool(profile.get("related_interests_answered"))
    if field == "scope":
        return bool(str(profile.get("scope") or "").strip())
    return False


def _answered_field_for_current_question(pending: Any, messages: list[dict[str, str]]) -> str:
    field = str(pending or "")
    if field and field != AGENT_PENDING_FIELD:
        return field
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return _field_from_question(message.get("content") or "")
    return ""


def _field_from_question(question: str) -> str:
    lowered = question.lower()
    if (
        "source scope" in lowered
        or "how recent" in lowered
        or "breaking news" in lowered
        or "recent time" in lowered
        or "within last year" in lowered
        or "recency" in lowered
        or "time horizon" in lowered
        or "latest" in lowered
        or "evergreen" in lowered
        or "happening now" in lowered
        or "regardless of date" in lowered
        or "published" in lowered
        or "best material" in lowered
    ):
        return "recency_weighting"
    if (
        "how deep" in lowered
        or "practitioner" in lowered
        or "informed-generalist" in lowered
        or "informed generalist" in lowered
        or "get-things-done" in lowered
        or "get things done" in lowered
        or "expert-level" in lowered
        or "expert level" in lowered
    ):
        return "depth"
    if "must mention" in lowered or "must include" in lowered or "every single item" in lowered or "required term" in lowered:
        return "must_have"
    if "anything to avoid" in lowered or "avoid" in lowered or "exclude" in lowered:
        return "exclusions"
    if "specific source" in lowered or "sources you want" in lowered or "podcast" in lowered or "youtube" in lowered:
        return "requested_sources"
    if "related interests" in lowered or "related" in lowered:
        return "related_interests"
    if "angle" in lowered or "focus" in lowered:
        return "scope"
    return ""


def _next_priority_field(profile: dict[str, Any]) -> str | None:
    depth_confirmed = bool(profile.get("depth_answered"))
    source_scope_confirmed = bool(profile.get("source_scope_answered"))
    if depth_confirmed and source_scope_confirmed:
        if not bool(profile.get("exclusions_answered")):
            return "exclusions"
        return "must_have"
    if not depth_confirmed and not source_scope_confirmed:
        text = _collect_hint_text(profile)
        depth_score = _signal_strength(text, DEPTH_PRACTITIONER_TOKENS)
        recency_score = _signal_strength(text, RECENCY_BREAKING_TOKENS)
        if recency_score > depth_score:
            return "recency_weighting"
        return "depth"
    if not depth_confirmed:
        return "depth"
    if not source_scope_confirmed:
        return "recency_weighting"
    return "exclusions"


def _signal_strength(text: str, tokens: tuple[str, ...]) -> int:
    return sum(1 for token in tokens if token in text)


def _seed_profile_with_hints(profile: dict[str, Any]) -> dict[str, Any]:
    updated = dict(profile)
    text = _collect_hint_text(updated)
    statement = str(updated.get("statement") or "")
    if not updated.get("scope"):
        updated["scope"] = ""
    if not updated.get("depth") and _contains(text, DEPTH_PRACTITIONER_TOKENS):
        updated["depth"] = "practitioner"
    lookback = _extract_lookback_constraint(statement)
    lookback_hours = _extract_lookback_hours(statement)
    if lookback_hours:
        updated["lookback_hours"] = lookback_hours
    if lookback:
        updated["recency_weighting"] = _recency_from_lookback_hours(lookback_hours or 24)
        updated["source_scope_answered"] = True
    elif not updated.get("recency_weighting") and _contains(text, RECENCY_BREAKING_TOKENS):
        updated["recency_weighting"] = "breaking"
    if updated.get("scope") and not updated.get("keywords"):
        updated["keywords"] = _keywords(updated["scope"])
    exclusions = _extract_exclusion_hints(statement)
    if exclusions:
        updated["exclusions"] = _merge_string_lists(updated.get("exclusions"), exclusions, limit=12)
        updated["exclusions_answered"] = True
    return _coerce_profile(updated)


def _coerce_profile(profile: dict[str, Any]) -> dict[str, Any]:
    selected_sources: dict[str, bool] = {**DEFAULT_EXPLORE_SOURCE_SELECTION}
    source_selection = profile.get("source_selection")
    if isinstance(source_selection, dict):
        for key, value in source_selection.items():
            selected_sources[str(key)] = bool(value)
    requested_sources = _normalize_requested_sources(profile.get("requested_sources"))
    for source in requested_sources:
        adapter = str(source.get("adapter") or "")
        if adapter:
            selected_sources[adapter] = True
    source_queries = {
        source: queries
        for source, queries in _clean_source_queries(profile.get("source_queries")).items()
        if selected_sources.get(source, False)
    }
    coerced = {
        "topic_id": str(profile.get("topic_id") or database.new_id()),
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "subtopics": _string_list(profile.get("subtopics")),
        "keywords": _string_list(profile.get("keywords")),
        "search_queries": _string_list(profile.get("search_queries"), limit=20),
        "source_queries": source_queries,
        "foreign_language_plan": _normalize_foreign_language_plan(profile.get("foreign_language_plan")),
        "foreign_regions": _string_list(profile.get("foreign_regions"), limit=16),
        "direct_episode_queries": _string_list(profile.get("direct_episode_queries"), limit=16),
        "related_episode_queries": _string_list(profile.get("related_episode_queries"), limit=16),
        "negative_constraints": _string_list(profile.get("negative_constraints"), limit=16),
        "priority_terms": _string_list(profile.get("priority_terms"), limit=16),
        "must_have_terms": _string_list(profile.get("must_have_terms"), limit=6),
        "must_have_aliases": _clean_must_have_aliases(profile.get("must_have_aliases"), terms=_string_list(profile.get("must_have_terms"), limit=6)),
        "depth": str(profile.get("depth") or ""),
        "recency_weighting": str(profile.get("recency_weighting") or ""),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")),
        "exclusions": _string_list(profile.get("exclusions")),
        "source_selection": selected_sources,
        "requested_sources": requested_sources,
        "gmail_rules": _normalize_gmail_rules(profile.get("gmail_rules")),
        "reasoning_summary": str(profile.get("reasoning_summary") or "").strip()[:600],
        "refinement_diagnostics": dict(profile.get("refinement_diagnostics"))
        if isinstance(profile.get("refinement_diagnostics"), dict)
        else {},
        "strategy_review": dict(profile.get(STRATEGY_REVIEW_PROFILE_KEY))
        if isinstance(profile.get(STRATEGY_REVIEW_PROFILE_KEY), dict)
        else {},
        "related_interests_answered": bool(profile.get("related_interests_answered")),
        "requested_sources_answered": bool(profile.get("requested_sources_answered")),
        "depth_answered": bool(profile.get("depth_answered")),
        "source_scope_answered": bool(profile.get("source_scope_answered")),
        "exclusions_answered": bool(profile.get("exclusions_answered")),
        "must_have_answered": bool(profile.get("must_have_answered")),
        "_revisit_existing": bool(profile.get("_revisit_existing")),
        "promoted_sources": [
            dict(source)
            for source in (profile.get("promoted_sources") or [])
            if isinstance(source, dict)
        ],
        "models": _models(profile.get("models")),
        "schedule": _coerce_schedule(profile.get("schedule")),
        "schedule_config": dict(profile.get("schedule_config") or {}) if isinstance(profile.get("schedule_config"), dict) else {},
        "delivery_config": dict(profile.get("delivery_config") or {}) if isinstance(profile.get("delivery_config"), dict) else {},
        "content_limits": _stable_jsonable(profile.get("content_limits")) if isinstance(profile.get("content_limits"), dict) else {},
        "pipeline_limits": _stable_jsonable(profile.get("pipeline_limits")) if isinstance(profile.get("pipeline_limits"), dict) else {},
    }
    return _sanitize_bounded_recency_query_years(coerced)


_QUERY_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|2100)\b")


def _sanitize_bounded_recency_query_years(profile: dict[str, Any]) -> dict[str, Any]:
    lookback_hours = _coerce_lookback_hours(profile.get("lookback_hours"))
    recency = _normalize_recency(profile.get("recency_weighting"))
    if lookback_hours is None and recency not in {"breaking", "recent"}:
        return profile
    if lookback_hours is not None and lookback_hours > 24 * 90:
        return profile
    current_year = datetime.now(UTC).year

    def clean(value: str) -> str:
        def replace_year(match: re.Match[str]) -> str:
            year = int(match.group(1))
            return str(current_year) if year < current_year else match.group(1)

        return " ".join(_QUERY_YEAR_RE.sub(replace_year, str(value or "")).split()).strip()

    def clean_list(values: Any, *, limit: int = 20) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in _string_list(values, limit=limit):
            query = clean(value)
            key = query.casefold()
            if query and key not in seen:
                cleaned.append(query)
                seen.add(key)
        return cleaned

    updated = dict(profile)
    updated["scope"] = clean(str(profile.get("scope") or ""))
    updated["subtopics"] = clean_list(profile.get("subtopics"), limit=24)
    updated["keywords"] = clean_list(profile.get("keywords"), limit=24)
    updated["search_queries"] = clean_list(profile.get("search_queries"), limit=20)
    updated["direct_episode_queries"] = clean_list(profile.get("direct_episode_queries"), limit=16)
    updated["related_episode_queries"] = clean_list(profile.get("related_episode_queries"), limit=16)
    updated["priority_terms"] = clean_list(profile.get("priority_terms"), limit=16)
    raw_must_have_terms = clean_list(profile.get("must_have_terms"), limit=6)
    raw_must_have_aliases = _clean_must_have_aliases(profile.get("must_have_aliases"), terms=raw_must_have_terms)
    canonical_terms, canonical_aliases = _canonicalize_must_have(raw_must_have_terms, raw_must_have_aliases)
    updated["must_have_terms"] = canonical_terms
    updated["must_have_aliases"] = canonical_aliases
    source_queries = {}
    for source, queries in _clean_source_queries(profile.get("source_queries")).items():
        source_queries[source] = clean_list(queries, limit=20)
    updated["source_queries"] = source_queries
    return updated


def _coerce_schedule(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _collect_hint_text(profile: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in FIELD_HINT_TEXT_SOURCE:
        value = profile.get(key)
        if isinstance(value, list):
            parts.extend([str(item) for item in value])
        elif isinstance(value, str):
            parts.append(value)
    return " ".join(parts).lower()


def _inferred_constraints(profile: dict[str, Any]) -> dict[str, Any]:
    statement = str(profile.get("statement") or "")
    selection = _source_selection_dict(profile.get("source_selection"))
    gmail_selected = selection.get("gmail", False)
    markets_interest = selection.get("markets", False) and _market_tracking_interest(statement)
    gmail_rules = _normalize_gmail_rules(profile.get("gmail_rules"))
    gmail_needs_instructions = (
        gmail_selected
        and not gmail_rules.get("intent")
        and not gmail_rules.get("include_senders")
    )
    return {
        "lookback_window": _extract_lookback_constraint(statement),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")) or _extract_lookback_hours(statement),
        "recency_already_answered": bool(profile.get("source_scope_answered")) or bool(_normalize_recency(profile.get("recency_weighting"))),
        "excluded_publishers_or_source_types": _string_list(profile.get("exclusions")),
        "exclusions_already_answered": bool(profile.get("exclusions_answered")) or bool(_string_list(profile.get("exclusions"))),
        "must_have_terms": _string_list(profile.get("must_have_terms"), limit=6),
        "must_have_already_answered": bool(profile.get("must_have_answered")) or bool(_string_list(profile.get("must_have_terms"), limit=6)),
        "market_tracking_interest": markets_interest,
        "gmail_rules_needed": gmail_needs_instructions,
        "recommended_question_focus": (
            "IMPORTANT: Gmail is selected but has no search instructions yet. Ask ONLY about Gmail in this turn — "
            "what kind of newsletters or email content the user wants (topic, recency). "
            "Example: 'What kind of newsletters should I look for in Gmail? e.g. AI research digests from the last two weeks.'"
            if gmail_needs_instructions
            else (
                "Ask about investable signals, relative comparison, source quality, catalysts, or risks. "
                "Do not ask for recency or exclusions if they are already listed here."
                if markets_interest
                else "Ask for the one ambiguity that would most improve retrieval."
            )
        ),
    }


def _extract_lookback_constraint(text: str) -> str:
    lowered = str(text or "").lower()
    match = re.search(r"\b(?:previous|past|last|within|over|no more than|not older than)\s+(?:the\s+)?(\d{1,3}|a|an|one)\s+(hour|hours|day|days|week|weeks|month|months|year|years)\b", lowered)
    if match:
        amount = _lookback_amount(match.group(1))
        unit = match.group(2)
        normalized_unit = unit if unit.endswith("s") else f"{unit}s"
        return f"previous {amount} {normalized_unit}"
    if re.search(r"\b(?:within|over|during|from|include content from)\s+(?:the\s+)?(?:past|last|most recent|recent)\s+year\b", lowered):
        return "previous 1 year"
    if re.search(r"\b(?:no more than|not older than|within)\s+(?:a|one|the last)\s+year\s+old\b", lowered):
        return "previous 1 year"
    if re.search(r"\b(today|latest|breaking|current|this week)\b", lowered):
        return "current/latest"
    return ""


def _extract_lookback_hours(text: str) -> int | None:
    lowered = str(text or "").lower()
    match = re.search(r"\b(?:previous|past|last|within|over|no more than|not older than)\s+(?:the\s+)?(\d{1,3}|a|an|one)\s+(hour|hours|day|days|week|weeks|month|months|year|years)\b", lowered)
    if not match:
        if re.search(r"\b(?:within|over|during|from|include content from)\s+(?:the\s+)?(?:past|last|most recent|recent)\s+year\b", lowered):
            return 8760
        if re.search(r"\b(?:no more than|not older than|within)\s+(?:a|one|the last)\s+year\s+old\b", lowered):
            return 8760
        if re.search(r"\b(today|latest|breaking|current)\b", lowered):
            return 24
        return None
    amount = _lookback_amount(match.group(1))
    unit = match.group(2)
    if unit.startswith("hour"):
        hours = amount
    elif unit.startswith("day"):
        hours = amount * 24
    elif unit.startswith("week"):
        hours = amount * 7 * 24
    elif unit.startswith("year"):
        hours = amount * 365 * 24
    else:
        hours = amount * 30 * 24
    return _coerce_lookback_hours(hours)


def _lookback_amount(value: str) -> int:
    lowered = str(value or "").strip().lower()
    if lowered in {"a", "an", "one"}:
        return 1
    return int(lowered)


def _recency_from_lookback_hours(lookback_hours: int) -> str:
    hours = _coerce_lookback_hours(lookback_hours) or 24
    if hours <= 48:
        return "breaking"
    if hours >= 365 * 24:
        return "last_year"
    return "recent"


def _extract_exclusion_hints(text: str) -> list[str]:
    lowered = str(text or "").lower()
    exclusions: list[str] = []
    if "msn" in lowered:
        exclusions.append("MSN")
    if "yahoo" in lowered:
        exclusions.append("Yahoo News")
    if re.search(r"\bnot\s+(?:like|from)\b", lowered) and exclusions:
        exclusions.append("syndicated aggregator reposts")
    if "press release" in lowered and re.search(r"\b(no|not|avoid|exclude|without)\b", lowered):
        exclusions.append("press releases")
    return exclusions


def _market_tracking_interest(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in ("investor", "stock", "stocks", "company's performance", "companies performance", "ticker", "market"))


def _contains(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _messages(session: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for message in session.get("messages", []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role in {"assistant", "user"} and content:
            messages.append({"role": role, "content": content})
    return messages


def _user_authored_text(profile: dict[str, Any], messages: list[dict[str, str]]) -> str:
    parts = [str(profile.get("statement") or "")]
    parts.extend(message["content"] for message in messages if message.get("role") == "user")
    return " ".join(parts).lower()


def _requested_source_was_named_by_user(source: dict[str, str], user_text: str) -> bool:
    ref = re.sub(r"\s+", " ", str(source.get("ref") or "").strip().lower())
    if not ref:
        return False
    normalized_text = re.sub(r"\s+", " ", user_text.lower())
    if ref in normalized_text:
        return True
    if str(source.get("adapter") or "") == "markets":
        return re.search(rf"(?<![a-z0-9.]){re.escape(ref)}(?![a-z0-9.])", normalized_text) is not None
    return False


_SOURCE_DISPLAY: dict[str, str] = {
    "web_search": "Web search",
    "foreign_media": "Foreign media",
    "gmail": "Gmail newsletters",
    "podcasts": "Podcasts",
    "youtube": "YouTube",
    "collections": "Your collections",
    "markets": "Markets",
}


def _strategy_preview(profile: dict[str, Any]) -> dict[str, Any]:
    """Plain-language review of where the brief will look, what it ignores, and the exact queries."""
    selection = _source_selection_dict(profile.get("source_selection"))
    source_queries = _clean_source_queries(profile.get("source_queries"))
    looks_at: list[str] = []
    ignores: list[str] = []
    per_source: list[dict[str, Any]] = []
    for source, label in _SOURCE_DISPLAY.items():
        if selection.get(source):
            looks_at.append(label)
            entry: dict[str, Any] = {"source": label, "key": source, "queries": list(source_queries.get(source, []))}
            if source == "gmail":
                entry["approved_senders"] = database.approved_gmail_senders()
                entry["note"] = "Only approved newsletters are read; their linked articles become primary content."
            if source == "podcasts":
                entry["direct_episode_queries"] = _string_list(profile.get("direct_episode_queries"), limit=8)
                entry["related_episode_queries"] = _string_list(profile.get("related_episode_queries"), limit=8)
                entry["negative_constraints"] = _string_list(profile.get("negative_constraints"), limit=8)
                entry["priority_terms"] = _string_list(profile.get("priority_terms"), limit=8)
                entry["note"] = (
                    "Approved shows contribute their latest eligible episode; semantic queries discover related shows and episodes."
                )
            if source == "markets":
                # Resolve the actual tickers that will be fetched so the user can
                # see and verify them in the confirmation card. Prose (statement/scope/
                # keywords) only yields known-company and cashtag/exchange symbols; bare
                # acronyms never become tickers. The model's markets query lane is the
                # explicit ticker lane and is validated separately.
                profile_text = " ".join(filter(None, [
                    str(profile.get("statement") or ""),
                    str(profile.get("scope") or ""),
                    *_string_list(profile.get("keywords")),
                    *_string_list(profile.get("subtopics")),
                ]))
                seen_tickers: set[str] = set()
                resolved = resolve_tickers_from_text(profile_text)
                seen_tickers.update(resolved)
                resolved = resolved + normalize_market_query_tickers(entry["queries"], seen_tickers)
                # Keep the visible markets lane consistent with the resolved tickers so the
                # confirmation card, the snapshot, and the executable plan all agree.
                entry["queries"] = resolved
                if resolved:
                    entry["tickers"] = resolved
                    entry["note"] = "Tracks price, recent news, and key metrics for each ticker."
            per_source.append(entry)
        else:
            ignores.append(label)
    return {
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "looks_at": looks_at,
        "ignores": ignores,
        "search_queries": _string_list(profile.get("search_queries"), limit=8),
        "per_source": per_source,
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")),
        "recency_weighting": str(profile.get("recency_weighting") or ""),
        "exclusions": _string_list(profile.get("exclusions")),
        "must_have_terms": _string_list(profile.get("must_have_terms"), limit=6),
        "must_have_aliases": _clean_must_have_aliases(profile.get("must_have_aliases"), terms=_string_list(profile.get("must_have_terms"), limit=6)),
        "diagnostics": dict(profile.get("refinement_diagnostics"))
        if isinstance(profile.get("refinement_diagnostics"), dict)
        else {},
        "reasoning_summary": str(profile.get("reasoning_summary") or "").strip(),
    }


def _session_response(session: dict[str, Any]) -> dict[str, Any]:
    profile = session.get("profile") if isinstance(session.get("profile"), dict) else {}
    response = {
        "session_id": session["session_id"],
        "statement": session["statement"],
        "status": session["status"],
        "turn_count": session["turn_count"],
        "pending_field": session.get("pending_field"),
        "messages": _messages(session),
        "profile": session["profile"],
        "topic_id": session.get("topic_id"),
        "reasoning_summary": str(profile.get("reasoning_summary") or "").strip(),
        "refinement_diagnostics": dict(profile.get("refinement_diagnostics"))
        if isinstance(profile.get("refinement_diagnostics"), dict)
        else {},
        "strategy_preview": _strategy_preview(profile),
        "pending_strategy_refinement": _pending_strategy_refinement(profile) or None,
        "strategy_review": dict(profile.get(STRATEGY_REVIEW_PROFILE_KEY))
        if isinstance(profile.get(STRATEGY_REVIEW_PROFILE_KEY), dict)
        else None,
    }
    if session.get("topic_id"):
        response["topic_profile"] = database.get_topic_profile(str(session["topic_id"]))
    return response


def _keywords(text: str) -> list[str]:
    return sorted(keyword_set(text))[:12]


def _negative_answer(answer: str) -> bool:
    lowered = answer.strip().lower()
    return lowered in {"no", "none", "nothing", "nope", "n/a", "na"} or "nothing" in lowered


def _split_answer_list(answer: str) -> list[str]:
    parts = [part.strip(" .") for part in re.split(r"[,;\n]", answer) if part.strip(" .")]
    if len(parts) <= 1 and " and " in answer.lower():
        parts = [part.strip(" .") for part in re.split(r"\s+and\s+", answer, flags=re.IGNORECASE) if part.strip(" .")]
    return parts[:8]


def _merge_requested_source_hints(profile: dict[str, Any], text: str) -> dict[str, Any]:
    existing = _normalize_requested_sources(profile.get("requested_sources"))
    discovered = _extract_requested_sources(text)
    if not discovered:
        return profile
    seen = {
        (str(source.get("adapter") or ""), str(source.get("ref") or "").lower())
        for source in existing
    }
    merged = list(existing)
    for source in discovered:
        key = (str(source.get("adapter") or ""), str(source.get("ref") or "").lower())
        if key in seen:
            continue
        merged.append(source)
        seen.add(key)
    return {**profile, "requested_sources": merged}


def _extract_requested_sources(text: str) -> list[dict[str, str]]:
    clean = " ".join(str(text or "").replace("\n", " ").split())
    if not clean:
        return []
    patterns = (
        ("podcasts", r"\b(?:what about|how about|add|include|use|try|pull in|look for|find|subscribe to)\s+(.{2,120}?)\s+(?:for|as)\s+(?:a\s+)?(?:podcast|show|episode)\b"),
        ("podcasts", r"\b(?:what about|how about|add|include|use|try|pull in|look for|find|subscribe to)\s+(.{2,120}?)\s+(?:podcast|show|episode)s?\b"),
        ("podcasts", r"\binclude\s+(?:the\s+)?podcast[:\s]+([^.;,\n]+)"),
        ("podcasts", r"\bpodcast[:\s]+([^.;,\n]+)"),
        ("gmail", r"\binclude\s+(?:the\s+)?newsletter[:\s]+([^.;,\n]+)"),
        ("gmail", r"\bnewsletter[:\s]+([^.;,\n]+)"),
        ("youtube", r"\binclude\s+(?:the\s+)?(?:youtube\s+)?(?:channel|creator|show)[:\s]+([^.;,\n]+)"),
        ("youtube", r"\byoutube\s+(?:channel|creator|show)[:\s]+([^.;,\n]+)"),
        ("collections", r"\binclude\s+(?:the\s+)?collection[:\s]+([^.;,\n]+)"),
        ("collections", r"\bcollection[:\s]+([^.;,\n]+)"),
        ("markets", r"\binclude\s+(?:the\s+)?(?:ticker|stock|company)[:\s]+([A-Za-z.]{1,8})"),
        ("markets", r"\b(?:ticker|stock)[:\s]+([A-Za-z.]{1,8})"),
        ("web_search", r"\binclude\s+(?:the\s+)?(?:site|source|website)[:\s]+(https?://[^\s,;]+|[^.;,\n]+)"),
    )
    requested: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for adapter, pattern in patterns:
        for match in re.finditer(pattern, clean, flags=re.IGNORECASE):
            ref = _clean_requested_source_ref(adapter, match.group(1))
            if not ref:
                continue
            key = (adapter, ref.lower())
            if key in seen:
                continue
            requested.append({"adapter": adapter, "ref": ref})
            seen.add(key)
    return requested


def _clean_requested_source_ref(adapter: str, value: Any) -> str:
    ref = " ".join(str(value or "").split()).strip(" .\"'")
    if not ref:
        return ""
    if adapter == "podcasts":
        ref = re.sub(r"^(?:the\s+)?podcast\s+", "", ref, flags=re.IGNORECASE).strip()
        ref = re.sub(r"\s+(?:for|as)\s+(?:a\s+)?(?:podcast|show|episode)s?$", "", ref, flags=re.IGNORECASE).strip()
        ref = re.sub(r"\s+(?:podcast|show|episode)s?$", "", ref, flags=re.IGNORECASE).strip()
    return ref.strip(" .\"'")


def _normalize_requested_sources(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        adapter = str(item.get("adapter") or "").strip()
        ref = str(item.get("ref") or item.get("source_name") or "").strip()
        if adapter not in VALID_SOURCE_ADAPTERS or not ref or _generic_requested_ref(adapter, ref):
            continue
        key = (adapter, ref.lower())
        if key in seen:
            continue
        normalized.append({"adapter": adapter, "ref": ref})
        seen.add(key)
    return normalized


def _generic_requested_ref(adapter: str, ref: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", ref.lower()).strip("_")
    generic_refs = {
        adapter,
        adapter.replace("_", ""),
        "web",
        "search",
        "web_search",
        "foreign_media",
        "foreignmedia",
        "foreign",
        "youtube",
        "podcast",
        "podcasts",
        "gmail",
        "newsletter",
        "newsletters",
        "collections",
        "collection",
        "markets",
        "market",
    }
    return normalized in generic_refs


def _canonicalize_must_have(
    terms: list[str],
    aliases: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    # 1. Clean terms to remove duplicates/empty strings (using folded comparison)
    cleaned_terms = []
    seen_folded = set()
    for t in terms:
        t_str = str(t or "").strip()
        if not t_str:
            continue
        f = fold_text(t_str)
        if f not in seen_folded:
            cleaned_terms.append(t_str)
            seen_folded.add(f)

    # Map folded key -> original aliases list
    input_aliases_folded: dict[str, list[str]] = {}
    for k, vals in (aliases or {}).items():
        fk = fold_text(k)
        if fk:
            input_aliases_folded.setdefault(fk, []).extend(vals)

    anchors = []
    for t in cleaned_terms:
        f = fold_text(t)
        initial_aliases = input_aliases_folded.get(f, [])
        folded_aliases = {fold_text(a) for a in initial_aliases if fold_text(a)}
        anchors.append({
            "original": t,
            "folded": f,
            "aliases": list(initial_aliases),
            "folded_aliases": folded_aliases,
            "absorbed_by": None,
        })

    # Synonym-aware merging of anchors
    for i in range(len(anchors)):
        anchor_i = anchors[i]
        if anchor_i["absorbed_by"] is not None:
            continue

        f_i = anchor_i["folded"]

        for j in range(i):
            anchor_j = anchors[j]
            if anchor_j["absorbed_by"] is not None:
                continue

            f_j = anchor_j["folded"]

            # Merge if synonym matches in either direction
            if (f_i in anchor_j["folded_aliases"]) or (f_j in anchor_i["folded_aliases"]):
                anchor_i["absorbed_by"] = f_j

                # Merge i's original name and aliases into j
                new_aliases = [anchor_i["original"]] + anchor_i["aliases"]
                seen_in_j = {fold_text(a) for a in anchor_j["aliases"]}
                seen_in_j.add(f_j)

                for a in new_aliases:
                    fa = fold_text(a)
                    if fa and fa not in seen_in_j:
                        anchor_j["aliases"].append(a)
                        anchor_j["folded_aliases"].add(fa)
                        seen_in_j.add(fa)
                break

    # Build final canonical result
    folded_surviving = {a["folded"] for a in anchors if a["absorbed_by"] is None}
    final_terms = []
    final_aliases = {}

    for anchor in anchors:
        if anchor["absorbed_by"] is None:
            final_terms.append(anchor["original"])
            key = anchor["original"].casefold()
            
            clean_aliases = []
            seen_alias_folded = set()
            for alias in anchor["aliases"]:
                fa = fold_text(alias)
                if fa and fa not in folded_surviving and fa not in seen_alias_folded:
                    clean_aliases.append(alias)
                    seen_alias_folded.add(fa)
            if clean_aliases:
                final_aliases[key] = clean_aliases

    return final_terms, final_aliases
