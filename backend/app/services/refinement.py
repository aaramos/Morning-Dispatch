from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

from backend.agents.discovery.language_support import trusted_language_options
from backend.agents.discovery.types import DEFAULT_EXPLORE_SOURCE_SELECTION
from backend.agents.librarian.text_utils import keyword_set
from backend.agents.model import ModelClient
from backend.app.core.config import get_settings
from backend.app.db import database
from backend.app.services import explore, model_routing

logger = logging.getLogger(__name__)

MIN_REFINEMENT_TURNS = 2
MAX_REFINEMENT_TURNS = 10
AGENT_PENDING_FIELD = "refinement_agent"
FIELD_ORDER = ("scope", "related_interests", "depth", "recency_weighting", "requested_sources", "exclusions")
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
    "recency_weighting": "Should I focus on what is happening now, recent material, the last year, or the best material I can find regardless of date?",
    "requested_sources": "Any specific podcast, YouTube channel, subreddit, newsletter, site, company, or collection I should try to include?",
    "exclusions": "Anything I should avoid so the brief stays focused?",
}

VALID_SOURCE_ADAPTERS = {"gmail", "reddit", "podcasts", "web_search", "foreign_media", "youtube", "collections", "markets"}

REFINEMENT_AGENT_SYSTEM_PROMPT = """
You are Morning Dispatch's interest-refinement agent.

A user has given you a raw curiosity. Your job is to turn it into a strong,
runnable brief plan: a topic profile plus a retrieval strategy that source
adapters can execute directly.

You are an expert interviewer, not a form. Think first, ask second.

== HOW TO THINK (every turn, internally) ==

1. Classify the brief. Silently decide which kind of request this is, because it
   changes what matters:
   - PLANNING / HOW-TO (for example: "plan a Mexico City trip", "set up a home network")
     -> constraints, preferences, and practical specifics dominate.
   - MONITORING / TRACKING (for example: "follow Nvidia's competitive position",
     "keep me current on EU AI regulation")
     -> entities, angles, and recency dominate; depth is usually practitioner.
   - LEARN-A-DOMAIN (for example: "understand mRNA therapeutics", "get into Roman history")
     -> scope boundaries and starting depth dominate; recency usually all_available.
   A brief can be a blend. Use the dominant type to decide what to ask.

2. Infer aggressively. Most of the profile can be filled from the user's words
   plus the brief type. Fill those fields yourself and treat them as defaults.
   Do NOT ask the user a question whose answer you can reasonably infer. If you
   inferred something material, surface it as a quick confirmation, not as an
   open-ended form question.

3. Ask the single highest-value question. Each turn, ask the ONE question that
   would most change the search plan if answered. A good question removes real
   ambiguity about WHAT to retrieve or WHAT to exclude. Skip anything that only
   confirms what you already know.

4. Respect stated constraints as already answered. If the user names a time
   window, excluded publishers/sites, companies, tickers, named sources, or
   preferred evidence quality, do not ask for those again. Convert them into
   retrieval constraints and ask a deeper question about strategy, signal
   quality, decision criteria, or tradeoffs.

== HOW TO ASK ==

- Speak the user's language, never the schema's. The user must never see the
  words "depth", "Source Scope", "recency_weighting", "adapter", or any field
  name. Translate every internal concept into plain language.
- One question per turn. Make it concrete and answerable in a sentence.
- When you offer choices, give 2-3 plain options and a one-line "why this
  matters" so the user is never forced to ask "what does that mean?".
- Match the user's effort. If they're terse, infer more and ask less. If
  they're detailed, you can confirm in batches.
- For market/investor tracking, do not ask "how recent" when the user gives a
  lookback window, and do not ask "anything to avoid" when they name sources to
  avoid. Instead ask about investable signals: earnings/capex/pricing/supply
  chain/customer demand, relative comparison, risk catalysts, or what would make
  the brief actionable.
- For finance requests, resolve company names to likely tradable tickers when
  you can. Put the ticker in markets source queries and show company + ticker in
  the search plan, rather than asking the user to provide the symbol.

== WHAT TO PRODUCE ==

Every turn, output a concrete, runnable search plan, not vague themes. Prefer
specific phrases, entity names, aliases, subtopics, source-specific queries, and
explicit exclusions. The plan should improve measurably with each answer.

== RULES ==

- Do not change source_selection unless the user explicitly asks to include or
  exclude a source type. You may recommend a set and let them accept.
- Only include source_queries for currently selected sources, or for a specific
  requested source the user named.
- Do not add requested_sources unless the user names a specific source: a
  podcast title, YouTube channel, subreddit, newsletter, site, collection,
  company, or ticker.
- Readiness is about sufficiency, not question count. Mark ready_to_build true
  when you can write a search plan you'd defend. Do not pad with filler
  questions to hit a quota, and do not force the user to ratify every inferred
  default.
- If just_go_now is true, or turn_count has reached max_turns, finalize
  immediately using your best inferred defaults.
- Before finalizing, you do not need explicit sign-off on every field, but the
  confirmation card must clearly state what you inferred so the user can correct
  it.
- When ready_to_build is true, make reasoning_summary support a confirmation
  card with three plain-language parts: "Here's what I heard", "How I'll
  search", and "Where I'll look". Include the scope, key constraints,
  exclusions, 3-5 readable example queries, and the recommended source set with
  a short reason.

Return strict JSON only with this shape:
{
  "profile_patch": {
    "scope": "refined brief scope",
    "subtopics": ["related interest or angle"],
    "keywords": ["compact search term"],
    "search_queries": ["general query phrase"],
    "source_queries": {
      "web_search": ["query"],
      "foreign_media": ["native-language query"],
      "reddit": ["query"],
      "youtube": ["query"],
      "podcasts": ["query"],
      "collections": ["query"],
      "markets": ["company or ticker theme"]
    },
    "depth": "practitioner|informed-generalist",
    "recency_weighting": "breaking|recent|last_year|all_available",
    "exclusions": ["thing to avoid"],
    "requested_sources": [{"adapter": "web_search|gmail|reddit|podcasts|foreign_media|youtube|collections|markets", "ref": "source name"}],
    "source_selection": {"web_search": true},
    "foreign_language_plan": [
      {
        "code": "ko",
        "name": "Korean",
        "native_query": "idiomatic native-language query",
        "native_entity_terms": ["native company or topic term"],
        "reason": "why this language helps"
      }
    ]
  },
  "ready_to_build": false,
  "next_question": "one concise, plain-language question, or null if ready",
  "reasoning_summary": "brief note on what you inferred vs. what the answer changed"
}
""".strip()


def start_session(payload: dict[str, Any]) -> dict[str, Any]:
    statement = str(payload.get("statement") or "").strip()
    if not statement:
        raise ValueError("Interest statement is required")
    profile = _seed_profile_with_hints(_initial_profile(payload))
    messages: list[dict[str, str]] = []
    agent_update = _run_refinement_agent(
        profile=profile,
        messages=messages,
        turn_count=0,
        just_go_now=False,
    )
    if agent_update is not None:
        profile, next_question, ready = _apply_agent_update(
            profile=profile,
            messages=messages,
            agent_update=agent_update,
            just_go_now=False,
            turn_count=0,
        )
        pending = None if ready else AGENT_PENDING_FIELD
        messages = [{"role": "assistant", "content": next_question}] if next_question and not ready else []
        status = "finalized" if ready else "active"
    else:
        pending = _next_missing(profile)
        messages = [{"role": "assistant", "content": QUESTIONS[pending]}] if pending else []
        status = "active" if pending else "finalized"
    session = database.create_refinement_session(
        statement=statement,
        profile=profile,
        messages=messages,
        pending_field=pending,
        status=status,
    )
    if status == "finalized":
        saved = explore.save_topic_profile(_fill_defaults(profile))
        updated = database.update_refinement_session(
            session["session_id"],
            profile=_fill_defaults(profile),
            messages=[*messages, {"role": "assistant", "content": "Topic profile is ready."}],
            pending_field=None,
            turn_count=0,
            status="finalized",
            topic_id=str(saved["topic_id"]),
        )
        return _session_response(updated) if updated else _session_response(session)
    return _session_response(session)


def advance_session(session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    session = database.get_refinement_session(session_id)
    if session is None:
        return None
    if session["status"] == "finalized":
        return _session_response(session)

    profile = dict(session["profile"])
    messages = _messages(session)
    pending = session.get("pending_field")
    answer = str(payload.get("answer") or "").strip()
    profile = _apply_models(profile, payload.get("models"))
    just_go_now = bool(payload.get("just_go_now"))
    turn_count = int(session.get("turn_count") or 0)
    answered_field = _answered_field_for_current_question(pending, messages) if answer and not just_go_now else ""
    answer_applied = False

    if answer and not just_go_now:
        if answered_field:
            profile = _apply_answer_with_model(profile, answered_field, answer)
            answer_applied = True
        messages.append({"role": "user", "content": answer})
        turn_count += 1

    if just_go_now:
        messages.append({"role": "user", "content": "Just go now."})

    agent_update = _run_refinement_agent(
        profile=profile,
        messages=messages,
        turn_count=turn_count,
        just_go_now=just_go_now,
    )
    if agent_update is not None:
        profile, next_question, ready = _apply_agent_update(
            profile=profile,
            messages=messages,
            agent_update=agent_update,
            just_go_now=just_go_now,
            turn_count=turn_count,
        )
        next_pending = None if ready else AGENT_PENDING_FIELD
        status = "finalized" if ready else "active"
        topic_id = session.get("topic_id")
        if status == "active" and next_question:
            messages.append({"role": "assistant", "content": next_question})
        else:
            profile = _fill_defaults(profile)
            saved = explore.save_topic_profile(profile)
            topic_id = str(saved["topic_id"])
            messages.append({"role": "assistant", "content": "Topic profile is ready."})
    else:
        if answer and not just_go_now and not answer_applied:
            profile = _apply_answer_with_model(profile, answered_field, answer)
        profile = _seed_profile_with_hints(profile)
        profile = _fill_defaults(profile) if just_go_now or turn_count >= MAX_REFINEMENT_TURNS else profile
        if just_go_now or turn_count >= MAX_REFINEMENT_TURNS or answered_field == "exclusions":
            next_pending = None
        else:
            next_pending = _next_missing(profile)
        status = "finalized" if next_pending is None else "active"
        topic_id = session.get("topic_id")
        if status == "active":
            messages.append({"role": "assistant", "content": QUESTIONS[next_pending]})
        else:
            profile = _fill_defaults(profile)
            saved = explore.save_topic_profile(profile)
            topic_id = str(saved["topic_id"])
            messages.append({"role": "assistant", "content": "Topic profile is ready."})

    updated = database.update_refinement_session(
        session_id,
        profile=profile,
        messages=messages,
        pending_field=next_pending,
        turn_count=turn_count,
        status=status,
        topic_id=topic_id,
    )
    return _session_response(updated) if updated else None


def _initial_profile(payload: dict[str, Any]) -> dict[str, Any]:
    statement = str(payload.get("statement") or "").strip()
    topic_id = str(payload.get("topic_id") or "").strip()
    existing_profile: dict[str, Any] | None = None
    if topic_id:
        record = database.get_topic_profile(topic_id)
        if record is not None and isinstance(record.get("profile"), dict):
            existing_profile = dict(record["profile"])
            statement = statement or str(record.get("statement") or existing_profile.get("statement") or "").strip()
    source_selection = payload.get("source_selection")
    if not isinstance(source_selection, dict):
        source_selection = dict(existing_profile.get("source_selection") or DEFAULT_EXPLORE_SOURCE_SELECTION) if existing_profile else dict(DEFAULT_EXPLORE_SOURCE_SELECTION)
    selected_sources = {
        str(key): bool(value)
        for key, value in source_selection.items()
    }
    if existing_profile is not None:
        profile = _coerce_profile(
            {
                **existing_profile,
                "topic_id": topic_id,
                "statement": statement,
                "source_selection": {**existing_profile.get("source_selection", {}), **selected_sources},
                "models": {**(existing_profile.get("models") or {}), **_models(payload.get("models"))},
            }
        )
        if payload.get("revisit"):
            profile = {
                **profile,
                "_revisit_existing": True,
                "related_interests_answered": False,
                "requested_sources_answered": False,
            }
        return profile
    requested_sources = _extract_requested_sources(statement)
    lookback_hours = _extract_lookback_hours(statement)
    return {
        "topic_id": database.new_id(),
        "statement": statement,
        "scope": "",
        "subtopics": [],
        "keywords": _keywords(statement),
        "search_queries": [],
        "source_queries": {},
        "foreign_language_plan": [],
        "depth": "",
        "recency_weighting": "",
        "lookback_hours": lookback_hours,
        "exclusions": [],
        "source_selection": {**DEFAULT_EXPLORE_SOURCE_SELECTION, **selected_sources},
        "requested_sources": requested_sources,
        "related_interests_answered": False,
        "requested_sources_answered": bool(requested_sources),
        "depth_answered": False,
        "source_scope_answered": False,
        "exclusions_answered": False,
        "promoted_sources": [],
        "models": {**{"refinement": None, "brief": None}, **_models(payload.get("models"))},
        "schedule": None,
        "schedule_config": {},
        "delivery_config": {},
    }


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


def _apply_models(profile: dict[str, Any], payload: Any) -> dict[str, Any]:
    updated = dict(profile)
    updates = _models(payload)
    if not updates:
        return updated
    existing = updated.get("models")
    models = {
        "refinement": None,
        "brief": None,
    }
    if isinstance(existing, dict):
        for key in models:
            if key in existing:
                models[key] = existing[key]
    models.update(updates)
    updated["models"] = models
    return updated


def _run_refinement_agent(
    *,
    profile: dict[str, Any],
    messages: list[dict[str, str]],
    turn_count: int,
    just_go_now: bool,
) -> dict[str, Any] | None:
    client = _refinement_model_client(profile)
    if client is None:
        return None
    prompt = _build_refinement_agent_prompt(
        profile=profile,
        messages=messages,
        turn_count=turn_count,
        just_go_now=just_go_now,
    )
    try:
        parsed = _run_sync_complete_json(
            client.complete_json(
                system=REFINEMENT_AGENT_SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=900,
            )
        )
    except Exception:
        logger.exception("Failed to run refinement agent")
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _refinement_model_client(profile: dict[str, Any]) -> Any | None:
    settings = get_settings()
    model_name = str((profile.get("models") or {}).get("refinement") or "").strip() or None
    return model_routing.client_for_agent("refinement", settings=settings, model_override=model_name).client


def _build_refinement_agent_prompt(
    *,
    profile: dict[str, Any],
    messages: list[dict[str, str]],
    turn_count: int,
    just_go_now: bool,
) -> str:
    profile_snapshot = {
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "subtopics": _string_list(profile.get("subtopics")),
        "keywords": _string_list(profile.get("keywords")),
        "search_queries": _string_list(profile.get("search_queries")),
        "source_queries": _clean_source_queries(profile.get("source_queries")),
        "foreign_language_plan": _normalize_foreign_language_plan(profile.get("foreign_language_plan")),
        "depth": _normalize_depth(profile.get("depth")),
        "recency_weighting": _normalize_recency(profile.get("recency_weighting")),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")),
        "exclusions": _string_list(profile.get("exclusions")),
        "source_selection": _source_selection_dict(profile.get("source_selection")),
        "requested_sources": _normalize_requested_sources(profile.get("requested_sources")),
    }
    compact_messages = [
        {"role": message["role"], "content": message["content"][:900]}
        for message in messages[-14:]
        if message.get("role") in {"assistant", "user"} and message.get("content")
    ]
    return json.dumps(
        {
            "task": "Refine this interest into a stronger brief plan and retrieval strategy.",
            "turn_count": turn_count,
            "min_turns": MIN_REFINEMENT_TURNS,
            "max_turns": MAX_REFINEMENT_TURNS,
            "just_go_now": just_go_now,
            "current_profile": profile_snapshot,
            "conversation": compact_messages,
            "source_guidance": {
                "web_search": "Use precise web queries with location/time/source words, aliases, and concrete intent.",
                "foreign_media": (
                    "When selected, propose allowlisted non-English languages and idiomatic native-language queries. "
                    "Use this for public foreign media only."
                ),
                "reddit": "Use community phrasing, local names, problems, recommendations, and comparison language.",
                "youtube": "Use creator/video search phrases for walkthroughs, explainers, interviews, or demos.",
                "podcasts": "Use show/interview/topic phrases for deeper context.",
                "collections": "Use terms likely to appear in local documents.",
                "markets": "Use company names, tickers, and sector themes only when the interest has a market angle.",
            },
            "already_inferred": _inferred_constraints(profile_snapshot),
            "trusted_foreign_languages": [
                {"code": item["code"], "name": item["name"]} for item in trusted_language_options()
            ],
            "question_policy": (
                "Ask the single question that most improves the search plan, in plain language the "
                "user will understand without explanation. Infer every field you reasonably can "
                "from the user's words and the brief type; surface inferences as quick "
                "confirmations, not open questions. Never emit an internal field name. Never ask "
                "about a constraint already present in already_inferred. If recency, exclusions, "
                "companies, or named sources are already present, ask about source quality, "
                "comparison angles, signal types, decision criteria, or what would make the brief useful. Ask at "
                "least min_turns meaningful refinement questions before marking ready unless the "
                "user clicks just_go_now. Mark ready_to_build true as soon as that floor is met "
                "and the plan is defensible; do not pad beyond useful questions. If "
                "just_go_now is true or turn_count == max_turns, finalize now with best inferred "
                "defaults. min_turns is a floor on quality: if intent is already clear, ask a "
                "concrete confirmation or constraint question rather than a generic form question."
            ),
            "revisit_policy": (
                "If this is an existing profile being revisited, ask how to sharpen the search plan "
                "or what would make the rebuilt brief more useful before marking ready."
            ) if profile.get("_revisit_existing") and messages == [] and turn_count == 0 else "",
        },
        ensure_ascii=False,
    )


def _apply_agent_update(
    *,
    profile: dict[str, Any],
    messages: list[dict[str, str]],
    agent_update: dict[str, Any],
    just_go_now: bool,
    turn_count: int,
) -> tuple[dict[str, Any], str | None, bool]:
    patched = _merge_agent_profile_patch(
        profile,
        agent_update.get("profile_patch"),
        user_text=_user_authored_text(profile, messages),
    )
    patched = _seed_profile_with_hints(patched)
    if not patched.get("scope"):
        patched["scope"] = str(profile.get("statement") or "").strip()
    ready_requested = bool(agent_update.get("ready_to_build"))
    ready = ready_requested or just_go_now or turn_count >= MAX_REFINEMENT_TURNS
    next_question = _clean_next_question(agent_update.get("next_question"))
    if next_question and _question_repeats_answered_constraint(next_question, patched):
        next_question = _strategy_deepening_question(patched, messages)

    if ready and not just_go_now and turn_count < MIN_REFINEMENT_TURNS:
        ready = False
        if not next_question:
            fallback_pending = _next_missing(patched)
            next_question = QUESTIONS[fallback_pending] if fallback_pending else _search_strategy_question(patched)

    if not next_question and not ready:
        fallback_pending = _next_missing(patched)
        next_question = QUESTIONS[fallback_pending] if fallback_pending else None
        if next_question and _question_repeats_answered_constraint(next_question, patched):
            next_question = _strategy_deepening_question(patched, messages)
        ready = fallback_pending is None
    if ready:
        patched = _fill_defaults(patched)
        next_question = None
    return _coerce_profile(patched), next_question, ready


def _merge_agent_profile_patch(profile: dict[str, Any], patch: Any, *, user_text: str = "") -> dict[str, Any]:
    updated = dict(profile)
    if not isinstance(patch, dict):
        return _coerce_profile(updated)

    for key in ("scope",):
        value = str(patch.get(key) or "").strip()
        if value:
            updated[key] = value

    for key in ("subtopics", "search_queries", "exclusions"):
        if key not in patch:
            continue
        updated[key] = _merge_string_lists(updated.get(key), patch.get(key), limit=16 if key != "search_queries" else 10)
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
    lookback_hours = _coerce_lookback_hours(patch.get("lookback_hours"))
    if lookback_hours:
        updated["lookback_hours"] = lookback_hours

    if "source_queries" in patch:
        updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), patch.get("source_queries"))
    if "foreign_language_plan" in patch:
        updated["foreign_language_plan"] = _merge_foreign_language_plan(
            updated.get("foreign_language_plan"),
            patch.get("foreign_language_plan"),
        )

    requested = _normalize_requested_sources(updated.get("requested_sources"))
    requested_patch = [
        source
        for source in _normalize_requested_sources(patch.get("requested_sources"))
        if _requested_source_was_named_by_user(source, user_text)
    ]
    if requested_patch:
        updated["requested_sources"] = _merge_requested_source_lists(requested, requested_patch)
        updated["requested_sources_answered"] = True

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


def _clean_next_question(value: Any) -> str | None:
    question = " ".join(str(value or "").split()).strip()
    if not question:
        return None
    if not question.endswith("?"):
        question = f"{question}?"
    return question[:260]


def _source_selection_dict(value: Any) -> dict[str, bool]:
    selected = dict(DEFAULT_EXPLORE_SOURCE_SELECTION)
    if isinstance(value, dict):
        for key, flag in value.items():
            source_key = str(key)
            if source_key in VALID_SOURCE_ADAPTERS:
                selected[source_key] = bool(flag)
    return selected


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
        cleaned.append(text[:180])
        seen.add(key)
        if len(cleaned) >= limit:
            break
    return cleaned


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
        query_list = _string_list(queries, limit=5)
        if query_list:
            cleaned[source_key] = query_list
    return cleaned


def _merge_source_queries(existing: Any, incoming: Any) -> dict[str, list[str]]:
    merged = _clean_source_queries(existing)
    for key, values in _clean_source_queries(incoming).items():
        merged[key] = _merge_string_lists(merged.get(key), values, limit=5)
    return merged


def _normalize_foreign_language_plan(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    trusted = {str(item["code"]): item for item in trusted_language_options()}
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("language") or "").strip().lower()
        if code not in trusted or code in seen:
            continue
        native_query = " ".join(str(item.get("native_query") or "").split()).strip()
        if not native_query:
            continue
        cleaned.append(
            {
                "code": code,
                "name": str(item.get("name") or trusted[code]["name"]),
                "native_query": native_query[:340],
                "native_entity_terms": _string_list(item.get("native_entity_terms"), limit=8),
                "reason": str(item.get("reason") or item.get("rationale") or "").strip()[:220],
            }
        )
        seen.add(code)
        if len(cleaned) >= 3:
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
        if len(merged) >= 3:
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


def _apply_answer(profile: dict[str, Any], field: str, answer: str) -> dict[str, Any]:
    updated = dict(profile)
    if field == "scope":
        updated["scope"] = answer
        updated["keywords"] = _keywords(" ".join([str(updated.get("statement") or ""), answer]))
    elif field == "related_interests":
        updated["related_interests_answered"] = True
        if _negative_answer(answer):
            updated["subtopics"] = []
        else:
            updated["subtopics"] = _split_answer_list(answer)
            updated["keywords"] = _keywords(" ".join([str(updated.get("statement") or ""), str(updated.get("scope") or ""), answer]))
    elif field == "depth":
        lowered = answer.lower()
        if any(token in lowered for token in ("practitioner", "technical", "deep", "expert", "hands-on")):
            updated["depth"] = "practitioner"
        else:
            updated["depth"] = "informed-generalist"
        updated["depth_answered"] = True
    elif field == "recency_weighting":
        updated["recency_weighting"] = _normalize_recency(answer) or "recent"
        updated["source_scope_answered"] = True
    elif field == "exclusions":
        updated["exclusions_answered"] = True
        lowered = answer.lower()
        if lowered in {"no", "none", "nothing", "nope", "n/a"} or "nothing" in lowered:
            updated["exclusions"] = []
        else:
            updated["exclusions"] = [
                part.strip(" .")
                for part in re.split(r"[,;]", answer)
                if part.strip(" .")
            ]
    elif field == "requested_sources":
        updated["requested_sources_answered"] = True
    return _coerce_profile(_merge_requested_source_hints(updated, answer))


def _apply_answer_with_model(profile: dict[str, Any], field: str, answer: str) -> dict[str, Any]:
    if field:
        model_updates = _extract_model_updates(
            profile=profile,
            field=field,
            answer=answer,
        )
        if model_updates:
            profile = _coerce_profile(_merge_requested_source_hints({**profile, **model_updates}, answer))
            return profile
    return _apply_answer(profile, field, answer)


def _extract_model_updates(
    *,
    profile: dict[str, Any],
    field: str,
    answer: str,
) -> dict[str, Any] | None:
    model_name = str((profile.get("models") or {}).get("refinement") or "").strip()
    if not model_name:
        return None

    settings = get_settings()
    model_client = model_routing.client_for_agent("refinement", settings=settings, model_override=model_name).client
    if model_client is None:
        return None

    prompt = _build_refinement_prompt(field=field, answer=answer, profile=profile)
    try:
        parsed = _run_sync_complete_json(
            model_client.complete_json(
                system="You extract one refinement field into strict JSON.",
                prompt=prompt,
                max_tokens=220,
            )
        )
    except Exception:
        logger.exception("Failed to run refinement model parse for field %s", field)
        return None
    if not isinstance(parsed, dict):
        return None
    updates = _coerce_model_field_updates(
        field=field,
        parsed=parsed,
        raw_answer=answer,
    )
    if updates is not None and updates:
        return updates
    return None


def _coerce_model_field_updates(
    *,
    field: str,
    parsed: dict[str, Any],
    raw_answer: str,
) -> dict[str, Any] | None:
    if field == "scope":
        scope = str(parsed.get("scope") or "").strip()
        if not scope:
            return None
        keywords = _keywords(scope)
        return {
            "scope": scope,
            **({"keywords": list(keywords)} if keywords else {}),
        }

    if field == "depth":
        normalized = _normalize_depth(parsed.get("depth"))
        if not normalized:
            normalized = _normalize_depth(raw_answer)
        if not normalized:
            return None
        return {"depth": normalized, "depth_answered": True}

    if field == "recency_weighting":
        normalized = _normalize_recency(parsed.get("recency_weighting"))
        if not normalized:
            normalized = _normalize_recency(raw_answer)
        if not normalized:
            return None
        return {"recency_weighting": normalized, "source_scope_answered": True}

    if field == "exclusions":
        exclusions = parsed.get("exclusions")
        if isinstance(exclusions, str):
            exclusions = [item for item in re.split(r"[,;\n]", exclusions) if item.strip(" .")]
        if isinstance(exclusions, list):
            return {"exclusions": [str(item).strip(" .") for item in exclusions if str(item).strip(" .")], "exclusions_answered": True}
        lowered = str(raw_answer).strip().lower()
        if lowered in {"no", "none", "nothing", "nope", "n/a"} or "nothing" in lowered:
            return {"exclusions": [], "exclusions_answered": True}
        if raw_answer.strip():
            return {"exclusions": [raw_answer.strip(" .")], "exclusions_answered": True}

    return None


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
    if lowered in {"last_year", "within_last_year", "past_year", "last_12_months", "year"}:
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
    return min(hours, 8760)


def _build_refinement_prompt(*, field: str, answer: str, profile: dict[str, Any]) -> str:
    profile_snapshot = {
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "depth": str(profile.get("depth") or ""),
        "recency_weighting": str(profile.get("recency_weighting") or ""),
    }
    if field == "exclusions":
        return (
            "Return strict JSON with one key: exclusions.\n"
            'Example response: {"exclusions": ["noise", "rumors"]}.\n'
            'If there are no exclusions, return {"exclusions": []}.\n'
            f"\nCurrent profile snapshot: {profile_snapshot}\n"
            f"User answer: {answer}"
        )
    if field == "depth":
        return (
            "Return strict JSON with one key: depth.\n"
            'Allowed values: practitioner, informed-generalist.\n'
            f"\nCurrent profile snapshot: {profile_snapshot}\n"
            f"User answer: {answer}"
        )
    if field == "recency_weighting":
        return (
            "Return strict JSON with one key: recency_weighting.\n"
            'Allowed values: breaking, recent, last_year, all_available.\n'
            f"\nCurrent profile snapshot: {profile_snapshot}\n"
            f"User answer: {answer}"
        )

    return (
        "Return strict JSON with one key: scope.\n"
        'Example response: {"scope": "Small team deployment patterns."}\n'
        f"\nCurrent profile snapshot: {profile_snapshot}\n"
        f"User answer: {answer}"
    )


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


def _fill_defaults(profile: dict[str, Any]) -> dict[str, Any]:
    updated = dict(profile)
    if not str(updated.get("scope") or "").strip():
        updated["scope"] = str(updated.get("statement") or "").strip()
    updated["depth"] = updated.get("depth") or "informed-generalist"
    updated["recency_weighting"] = _normalize_recency(updated.get("recency_weighting")) or "recent"
    if not isinstance(updated.get("exclusions"), list):
        updated["exclusions"] = []
    return _coerce_profile(updated)


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
    return None


def _required_confirmation_field(profile: dict[str, Any]) -> str | None:
    if not bool(profile.get("depth_answered")):
        return "depth"
    if not bool(profile.get("source_scope_answered")):
        return "recency_weighting"
    return None


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
    companies = _extract_market_entities(text)
    if _market_tracking_interest(text) and companies:
        company_text = ", ".join(company["name"] for company in companies[:4])
        return (
            f"For {company_text}, which signals should I prioritize: earnings and guidance, "
            "memory pricing/supply-demand, customer wins, capex and capacity moves, or competitive risk?"
        )
    if _market_tracking_interest(text):
        return "What would make this brief actionable for you: catalysts, risks, valuation context, or company-by-company comparisons?"
    if _string_list(profile.get("search_queries")) or _clean_source_queries(profile.get("source_queries")):
        return "What kind of evidence should I trust most for this brief: primary reporting, expert analysis, community signal, or practical examples?"
    return _search_strategy_question(profile)


def _question_repeats_answered_constraint(question: str, profile: dict[str, Any]) -> bool:
    field = _field_from_question(question)
    if field == "recency_weighting":
        return bool(profile.get("source_scope_answered")) or bool(_normalize_recency(profile.get("recency_weighting")))
    if field == "exclusions":
        return bool(profile.get("exclusions_answered")) or bool(_string_list(profile.get("exclusions")))
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
        return "exclusions"
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
        updated["recency_weighting"] = "breaking"
        updated["source_scope_answered"] = True
    elif not updated.get("recency_weighting") and _contains(text, RECENCY_BREAKING_TOKENS):
        updated["recency_weighting"] = "breaking"
    if updated.get("scope") and not updated.get("keywords"):
        updated["keywords"] = _keywords(updated["scope"])
    exclusions = _extract_exclusion_hints(statement)
    if exclusions:
        updated["exclusions"] = _merge_string_lists(updated.get("exclusions"), exclusions, limit=12)
        updated["exclusions_answered"] = True
    market_entities = _extract_market_entities(statement)
    if market_entities:
        requested = _normalize_requested_sources(updated.get("requested_sources"))
        requested.extend({"adapter": "markets", "ref": entity["ref"]} for entity in market_entities)
        updated["requested_sources"] = _merge_requested_source_lists([], requested)
        updated["requested_sources_answered"] = True
        query_terms = [_market_entity_query_term(entity) for entity in market_entities]
        ticker_terms = [entity["ref"] for entity in market_entities]
        keyword_terms = [item for entity in market_entities for item in (entity["name"], entity["ref"])]
        updated["keywords"] = _merge_string_lists(updated.get("keywords"), keyword_terms, limit=16)
        updated["subtopics"] = _merge_string_lists(
            updated.get("subtopics"),
            [
                "company performance and catalysts",
                "memory pricing and supply-demand signals",
                "competitive positioning across memory and storage suppliers",
            ],
            limit=16,
        )
        updated["source_queries"] = _merge_source_queries(
            updated.get("source_queries"),
            _market_source_queries(query_terms, ticker_terms=ticker_terms, lookback=lookback, exclusions=exclusions),
        )
        updated["search_queries"] = _merge_string_lists(
            updated.get("search_queries"),
            _market_search_queries(query_terms, lookback=lookback, exclusions=exclusions),
            limit=10,
        )
    if exclusions or lookback or market_entities:
        updated["related_interests_answered"] = bool(market_entities) or bool(updated.get("related_interests_answered"))
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
    return {
        "topic_id": str(profile.get("topic_id") or database.new_id()),
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "subtopics": _string_list(profile.get("subtopics")),
        "keywords": _string_list(profile.get("keywords")),
        "search_queries": _string_list(profile.get("search_queries"), limit=10),
        "source_queries": source_queries,
        "foreign_language_plan": _normalize_foreign_language_plan(profile.get("foreign_language_plan")),
        "depth": str(profile.get("depth") or ""),
        "recency_weighting": str(profile.get("recency_weighting") or ""),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")),
        "exclusions": _string_list(profile.get("exclusions")),
        "source_selection": selected_sources,
        "requested_sources": requested_sources,
        "related_interests_answered": bool(profile.get("related_interests_answered")),
        "requested_sources_answered": bool(profile.get("requested_sources_answered")),
        "depth_answered": bool(profile.get("depth_answered")),
        "source_scope_answered": bool(profile.get("source_scope_answered")),
        "exclusions_answered": bool(profile.get("exclusions_answered")),
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
    }


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
    companies = _extract_market_entities(statement)
    return {
        "lookback_window": _extract_lookback_constraint(statement),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")) or _extract_lookback_hours(statement),
        "recency_already_answered": bool(profile.get("source_scope_answered")) or bool(_normalize_recency(profile.get("recency_weighting"))),
        "excluded_publishers_or_source_types": _string_list(profile.get("exclusions")),
        "exclusions_already_answered": bool(profile.get("exclusions_answered")) or bool(_string_list(profile.get("exclusions"))),
        "named_companies_or_tickers": [_market_entity_label(company) for company in companies],
        "market_tracking_interest": _market_tracking_interest(statement),
        "recommended_question_focus": (
            "Ask about investable signals, relative comparison, source quality, catalysts, or risks. "
            "Do not ask for recency or exclusions if they are already listed here."
            if _market_tracking_interest(statement)
            else "Ask for the one ambiguity that would most improve retrieval."
        ),
    }


def _extract_lookback_constraint(text: str) -> str:
    lowered = str(text or "").lower()
    match = re.search(r"\b(?:previous|past|last|within)\s+(\d{1,3})\s+(hour|hours|day|days|week|weeks|month|months)\b", lowered)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        normalized_unit = unit if unit.endswith("s") else f"{unit}s"
        return f"previous {amount} {normalized_unit}"
    if re.search(r"\b(today|latest|breaking|current|this week)\b", lowered):
        return "current/latest"
    return ""


def _extract_lookback_hours(text: str) -> int | None:
    lowered = str(text or "").lower()
    match = re.search(r"\b(?:previous|past|last|within)\s+(\d{1,3})\s+(hour|hours|day|days|week|weeks|month|months)\b", lowered)
    if not match:
        if re.search(r"\b(today|latest|breaking|current)\b", lowered):
            return 24
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("hour"):
        hours = amount
    elif unit.startswith("day"):
        hours = amount * 24
    elif unit.startswith("week"):
        hours = amount * 7 * 24
    else:
        hours = amount * 30 * 24
    return _coerce_lookback_hours(hours)


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
    return any(token in lowered for token in ("investor", "stock", "stocks", "company's performance", "companies performance", "performance", "ticker", "market"))


def _extract_market_entities(text: str) -> list[dict[str, str]]:
    clean = str(text or "")
    patterns = (
        ("Micron", "MU", r"\bmicron\b|\bmu\b"),
        ("SK Hynix", "000660.KS", r"\b(?:sk\s+)?hynix\b|\b000660(?:\.ks)?\b"),
        ("Kioxia", "285A.T", r"\bkioxia\b|\b285a(?:\.t)?\b|\bkxhcf\b"),
        ("SanDisk", "SNDK", r"\bsandisk\b|\bsndk\b"),
    )
    entities: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, ref, pattern in patterns:
        if re.search(pattern, clean, flags=re.IGNORECASE):
            key = ref.lower()
            if key not in seen:
                entities.append({"name": name, "ref": ref})
                seen.add(key)
    return entities


def _market_entity_label(entity: dict[str, str]) -> str:
    name = str(entity.get("name") or "").strip()
    ref = str(entity.get("ref") or "").strip()
    if name and ref and name.lower() != ref.lower():
        return f"{name} ({ref})"
    return name or ref


def _market_entity_query_term(entity: dict[str, str]) -> str:
    name = str(entity.get("name") or "").strip()
    ref = str(entity.get("ref") or "").strip()
    if name and ref and name.lower() != ref.lower():
        return f"{name} {ref}"
    return name or ref


def _market_search_queries(terms: list[str], *, lookback: str, exclusions: list[str]) -> list[str]:
    if not terms:
        return []
    company_group = " ".join(terms)
    time_phrase = lookback or "latest"
    exclude_phrase = " ".join(f"-{term.replace(' ', '')}" for term in exclusions if term.lower() in {"msn", "yahoo news"})
    return [
        f"{company_group} memory chip news {time_phrase} {exclude_phrase}".strip(),
        f"{company_group} earnings guidance NAND DRAM HBM pricing {time_phrase} {exclude_phrase}".strip(),
        f"{company_group} supply demand capacity capex customer wins {time_phrase} {exclude_phrase}".strip(),
    ]


def _market_source_queries(
    terms: list[str],
    *,
    ticker_terms: list[str] | None = None,
    lookback: str,
    exclusions: list[str],
) -> dict[str, list[str]]:
    queries = _market_search_queries(terms, lookback=lookback, exclusions=exclusions)
    return {
        "web_search": queries,
        "markets": ticker_terms or terms,
        "reddit": [f"{' '.join(terms)} memory stocks analysis", "DRAM NAND HBM stock discussion"],
        "youtube": [f"{' '.join(terms)} stock analysis memory market", "DRAM NAND HBM market analysis"],
    }


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


def _session_response(session: dict[str, Any]) -> dict[str, Any]:
    response = {
        "session_id": session["session_id"],
        "statement": session["statement"],
        "status": session["status"],
        "turn_count": session["turn_count"],
        "pending_field": session.get("pending_field"),
        "messages": _messages(session),
        "profile": session["profile"],
        "topic_id": session.get("topic_id"),
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
        ("podcasts", r"\binclude\s+(?:the\s+)?podcast[:\s]+([^.;,\n]+)"),
        ("podcasts", r"\bpodcast[:\s]+([^.;,\n]+)"),
        ("gmail", r"\binclude\s+(?:the\s+)?newsletter[:\s]+([^.;,\n]+)"),
        ("gmail", r"\bnewsletter[:\s]+([^.;,\n]+)"),
        ("reddit", r"\binclude\s+(?:the\s+)?(?:subreddit|reddit)[:\s]+(?:r/)?([A-Za-z0-9_][A-Za-z0-9_ -]{1,80})"),
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
            ref = match.group(1).strip(" .\"'")
            if adapter == "reddit":
                ref = ref.replace(" ", "")
                if ref.lower().startswith("r/"):
                    ref = ref[2:]
                ref = f"r/{ref}"
            if not ref:
                continue
            key = (adapter, ref.lower())
            if key in seen:
                continue
            requested.append({"adapter": adapter, "ref": ref})
            seen.add(key)
    return requested


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
        "reddit",
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
