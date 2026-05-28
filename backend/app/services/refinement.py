from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

from backend.agents.discovery.language_support import trusted_language_options
from backend.agents.discovery.markets import resolve_tickers_from_text
from backend.agents.discovery.types import DEFAULT_EXPLORE_SOURCE_SELECTION
from backend.agents.digestor.gmail import NewsletterCandidate, discover_newsletter_candidates
from backend.agents.librarian.text_utils import keyword_set
from backend.agents.model import ModelClient
from backend.app.core.config import get_settings
from backend.app.db import database
from backend.app.services import explore, gmail_allowlist, model_routing

logger = logging.getLogger(__name__)

MIN_REFINEMENT_TURNS = 2
MAX_REFINEMENT_TURNS = 10
AGENT_PENDING_FIELD = "refinement_agent"
GMAIL_RULES_FIELD = "gmail_rules"
GMAIL_SENDER_SELECTION_FIELD = "gmail_sender_selection"
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
    "recency_weighting": "How recent does the source material need to be? For example: last 24 hours, last 3 days, no more than a year old, or best available regardless of date.",
    "requested_sources": "Any specific podcast, YouTube channel, subreddit, newsletter, site, company, or collection I should try to include?",
    "exclusions": "Anything I should avoid so the brief stays focused?",
    GMAIL_RULES_FIELD: "How do you want me to use Gmail for this brief? For example: AI-related newsletters received in the last 7 days.",
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

- Sound like a sharp human collaborator, not a setup wizard. Talk the way a
  great researcher would when sketching out a brief with a colleague: warm,
  curious, specific. Vary your phrasing turn to turn; never reuse a template.
- Speak the user's language, never the schema's. The user must never see the
  words "depth", "Source Scope", "recency_weighting", "adapter", or any field
  name. Translate every internal concept into plain language.
- React to what they actually said before you ask the next thing. A short nod
  to their last answer ("Got it — so you care more about X than Y") makes the
  exchange feel like a conversation, not a form.
- One question per turn. Make it concrete and answerable in a sentence.
- When you offer choices, give 2-3 plain options and a one-line "why this
  matters" so the user is never forced to ask "what does that mean?".
- Let the chosen sources steer the conversation. Open on the angle that those
  sources change most: for markets, the investable signals that matter; for
  podcasts/YouTube, the voices or formats worth following; for foreign media,
  the regions or languages that carry the real signal; for community sources,
  the debates worth listening in on. Make the first question feel like it was
  written for this exact mix of interest and sources.
- Match the user's effort. If they're terse, infer more and ask less. If
  they're detailed, you can confirm in batches.
- For market/investor tracking, do not ask "how recent" when the user gives a
  lookback window, and do not ask "anything to avoid" when they name sources to
  avoid. Instead ask about investable signals: earnings/capex/pricing/supply
  chain/customer demand, relative comparison, risk catalysts, or what would make
  the brief actionable.
- When the user answers how recent the content should be, interpret natural
  language into a concrete source window. Examples: "last 24 hours" means 24
  hours, "last 3 days" means 72 hours, and "no more than a year old" means the
  last year. Confirm the interpreted window in plain language before building.
- For finance requests, resolve company names to likely tradable tickers when
  you can. Put the ticker in markets source queries and show company + ticker in
  the search plan, rather than asking the user to provide the symbol.

== WHAT TO PRODUCE ==

Every turn, output a concrete, runnable search plan, not vague themes. Prefer
specific phrases, entity names, aliases, subtopics, source-specific queries, and
explicit exclusions. The plan should improve measurably with each answer.

== QUERY CRAFT ==

The quality of the brief is decided here, so treat query writing as the core
skill, for ANY topic — a city trip, a scientific field, a company, a hobby, a
policy fight.

- Map the topic's facets first. Identify the distinct angles that matter
  (key entities/people/places, sub-questions, competing viewpoints, time
  sensitivity) and make sure the query set covers them rather than circling one
  facet.
- Write DIVERSE queries, not paraphrases. Each query should chase a different
  facet or use different vocabulary (synonyms, insider terms, proper nouns,
  acronyms spelled out). Never emit two queries that would return basically the
  same results.
- Tailor each source's queries to how that source is searched: precise web
  phrases with names/places/time for web_search; community phrasing and
  problems for reddit; creator/format phrasing for youtube/podcasts; native,
  idiomatic phrasing for foreign_media; company names plus resolved tickers for
  markets. Do not paste the same string into every source.
- Resolve named entities to their canonical forms and useful aliases yourself
  (e.g. a company to its ticker, a person to their full name and role) so the
  queries hit. Do not ask the user for identifiers you can infer.
- A few sharp queries beat many dull ones. Aim for breadth of coverage with
  the smallest set that achieves it.

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


CRITIQUE_SYSTEM_PROMPT = """
You are a senior research editor reviewing a draft search plan before it runs.

The plan below was drafted to gather high-quality material on the user's topic.
Your job is to make it stronger, for ANY kind of topic. Look for concrete
weaknesses and fix them:
- Coverage gaps: an obvious facet, entity, angle, or counter-viewpoint the
  queries miss.
- Redundancy: near-duplicate queries that would return the same results; replace
  them with queries that reach new material.
- Source fit: each selected source should have queries phrased the way that
  source is actually searched, not a generic blob copied across sources.
- Precision: vague queries that should name specific people, places, products,
  organizations, tickers, or time windows.

Only suggest queries for sources listed in selected_sources. Keep the user's
intent; do not invent constraints they did not express. Prefer a small, sharp,
diverse set over a long dull one.

Return strict JSON only:
{
  "search_queries": ["improved general query phrases"],
  "source_queries": {"web_search": ["query"], "reddit": ["query"]},
  "subtopics": ["facet the plan should also cover"],
  "notes": "one line on what you strengthened"
}
Return only sources you are improving; omit keys you are not changing.
""".strip()


def start_session(payload: dict[str, Any]) -> dict[str, Any]:
    statement = str(payload.get("statement") or "").strip()
    if not statement:
        raise ValueError("Interest statement is required")
    profile = _seed_profile_with_hints(_initial_profile(payload))
    messages: list[dict[str, str]] = []
    gmail_question = _gmail_refinement_question(profile)
    if gmail_question:
        messages = [{"role": "assistant", "content": gmail_question}]
        session = database.create_refinement_session(
            statement=statement,
            profile=profile,
            messages=messages,
            pending_field=GMAIL_RULES_FIELD,
            status="active",
        )
        return _session_response(session)
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
        messages = [{"role": "assistant", "content": _deterministic_question(pending, profile)}] if pending else []
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

    gmail_result = _advance_gmail_refinement(profile, pending, answer=answer, just_go_now=just_go_now)
    if gmail_result is not None:
        profile, assistant_message, gmail_next_pending, gmail_status = gmail_result
        if assistant_message:
            messages.append({"role": "assistant", "content": assistant_message})
        if gmail_status == "continue":
            # Gmail is one step of the interview, not the whole thing. Hand control
            # back to the questioning engine so scope/depth/recency/related
            # interests/exclusions still get asked before we finalize.
            answer = ""
            answered_field = ""
            answer_applied = True
            pending = gmail_next_pending
        else:
            topic_id = session.get("topic_id")
            if gmail_status == "finalized":
                profile = _fill_defaults(profile)
                saved = explore.save_topic_profile(profile)
                topic_id = str(saved["topic_id"])
                messages.append({"role": "assistant", "content": "Topic profile is ready."})
            updated = database.update_refinement_session(
                session_id,
                profile=profile,
                messages=messages,
                pending_field=gmail_next_pending,
                turn_count=turn_count,
                status=gmail_status,
                topic_id=topic_id,
            )
            return _session_response(updated) if updated else None

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
            messages.append({"role": "assistant", "content": _deterministic_question(next_pending, profile)})
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


def _gmail_refinement_question(profile: dict[str, Any]) -> str | None:
    selection = _source_selection_dict(profile.get("source_selection"))
    rules = _normalize_gmail_rules(profile.get("gmail_rules"))
    if not selection.get("gmail"):
        return None
    if rules.get("include_senders"):
        return None
    return (
        "How do you want me to use Gmail for this brief? "
        "I'm looking for newsletter rules, like: AI-related newsletters received in the last 7 days."
    )


def _advance_gmail_refinement(
    profile: dict[str, Any],
    pending: Any,
    *,
    answer: str,
    just_go_now: bool,
) -> tuple[dict[str, Any], str | None, str | None, str] | None:
    if just_go_now:
        return None
    pending_field = str(pending or "")
    if pending_field == GMAIL_RULES_FIELD and answer.strip():
        updated = dict(profile)
        rules = _gmail_rules_from_answer(answer, updated)
        candidates = _discover_gmail_candidates(rules)
        rules["candidates"] = [candidate.to_dict() for candidate in candidates]
        if candidates:
            gmail_allowlist.record_candidates(rules["candidates"], source="refinement")
        updated["gmail_rules"] = rules
        updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), {"gmail": [rules["intent"]]})
        updated["lookback_hours"] = rules["lookback_hours"]
        updated["recency_weighting"] = _recency_from_lookback_hours(int(rules["lookback_hours"]))
        updated["source_scope_answered"] = True
        if candidates:
            return (
                _coerce_profile(updated),
                _gmail_candidate_question(candidates, rules),
                GMAIL_SENDER_SELECTION_FIELD,
                "active",
            )
        return (
            _coerce_profile(updated),
            (
                "I searched Gmail for that newsletter pattern but didn’t find clear newsletter senders. "
                "Name any sender or newsletter you want included, or say to continue without Gmail."
            ),
            GMAIL_SENDER_SELECTION_FIELD,
            "active",
        )

    if pending_field == GMAIL_SENDER_SELECTION_FIELD:
        updated = dict(profile)
        rules = _normalize_gmail_rules(updated.get("gmail_rules"))
        include_senders = _selected_gmail_senders(answer, rules)
        if include_senders:
            approved = gmail_allowlist.approve_senders(include_senders, source="refinement")
            rules["include_senders"] = approved or include_senders
            updated["requested_sources"] = _merge_requested_source_lists(
                _normalize_requested_sources(updated.get("requested_sources")),
                [{"adapter": "gmail", "ref": sender} for sender in include_senders],
            )
            updated["requested_sources_answered"] = True
            updated["gmail_rules"] = rules
            return (
                _coerce_profile(updated),
                (
                    f"Approved {', '.join(include_senders)} to the Gmail allowlist. "
                    "These newsletters become discovery feeds, and the articles they link to become primary content for this brief."
                ),
                None,
                "continue",
            )
        rules["include_senders"] = []
        updated["gmail_rules"] = rules
        updated["source_selection"] = {**_source_selection_dict(updated.get("source_selection")), "gmail": False}
        return (
            _coerce_profile(updated),
            "Got it. I'll continue without Gmail for this brief.",
            None,
            "continue",
        )

    return None


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
        "gmail_rules": {},
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
                max_tokens=2000,
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
    return model_routing.client_for_agent(
        "refinement",
        settings=settings,
        items=_privacy_markers_for_refinement(profile),
        model_override=model_name,
    ).client


def _critique_search_plan(profile: dict[str, Any]) -> dict[str, Any]:
    """Second LLM pass that strengthens the draft query set before building."""
    client = _refinement_model_client(profile)
    if client is None:
        return profile
    selected_sources = [
        source
        for source, enabled in _source_selection_dict(profile.get("source_selection")).items()
        if enabled
    ]
    plan = {
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "subtopics": _string_list(profile.get("subtopics")),
        "keywords": _string_list(profile.get("keywords")),
        "search_queries": _string_list(profile.get("search_queries"), limit=20),
        "source_queries": _clean_source_queries(profile.get("source_queries")),
        "selected_sources": selected_sources,
        "exclusions": _string_list(profile.get("exclusions")),
    }
    prompt = json.dumps(
        {
            "task": "Review and strengthen this search plan before it runs.",
            "plan": plan,
        },
        ensure_ascii=False,
    )
    try:
        parsed = _run_sync_complete_json(
            client.complete_json(
                system=CRITIQUE_SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=1200,
            )
        )
    except Exception:
        logger.exception("Failed to run search-plan critique")
        return profile
    if not isinstance(parsed, dict):
        return profile
    return _apply_critique(profile, parsed)


def _apply_critique(profile: dict[str, Any], critique: dict[str, Any]) -> dict[str, Any]:
    updated = dict(profile)
    if "search_queries" in critique:
        updated["search_queries"] = _merge_string_lists(
            updated.get("search_queries"), critique.get("search_queries"), limit=20
        )
    if "subtopics" in critique:
        updated["subtopics"] = _merge_string_lists(
            updated.get("subtopics"), critique.get("subtopics"), limit=16
        )
    if "source_queries" in critique:
        # Only fold in queries for sources the user actually selected.
        selection = _source_selection_dict(updated.get("source_selection"))
        allowed = {
            source: queries
            for source, queries in _clean_source_queries(critique.get("source_queries")).items()
            if selection.get(source)
        }
        if allowed:
            updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), allowed)
    return _coerce_profile(updated)


def _privacy_markers_for_refinement(profile: dict[str, Any]) -> list[dict[str, str]]:
    selection = _source_selection_dict(profile.get("source_selection"))
    markers: list[dict[str, str]] = []
    if selection.get("gmail"):
        markers.append({"source_type": "gmail"})
    if selection.get("collections"):
        markers.append({"source_type": "collection_chunk"})
    return markers


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
        "gmail_rules": _normalize_gmail_rules(profile.get("gmail_rules")),
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
                "markets": (
                    "List specific exchange ticker symbols to track (e.g. 'MU', 'NVDA', '000660.KS', '285A.T'). "
                    "Resolve every company name in the profile to its primary exchange ticker. "
                    "One ticker per query entry. Do not use descriptive phrases or keyword soup — "
                    "only tickers and, if needed, sector ETF symbols (e.g. 'SOXX', 'XLK'). "
                    "If the user mentions a company without a well-known ticker, ask for clarification."
                ),
            },
            "already_inferred": _inferred_constraints(profile_snapshot),
            "trusted_foreign_languages": [
                {"code": item["code"], "name": item["name"]} for item in trusted_language_options()
            ],
            "question_policy": (
                "Ask the single question that most improves the search plan, in plain language the "
                "user will understand without explanation. Phrase it like a curious human "
                "collaborator who just heard the user's last answer, not like a wizard stepping "
                "through fields; vary your wording every turn and let the selected sources shape "
                "what you open with. Infer every field you reasonably can "
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
    reasoning_summary = str(agent_update.get("reasoning_summary") or "").strip()
    if reasoning_summary:
        patched["reasoning_summary"] = reasoning_summary
    ready_requested = bool(agent_update.get("ready_to_build"))
    ready = ready_requested or just_go_now or turn_count >= MAX_REFINEMENT_TURNS
    next_question = _clean_next_question(agent_update.get("next_question"))
    if next_question and _question_repeats_answered_constraint(next_question, patched):
        next_question = _strategy_deepening_question(patched, messages)

    if ready and not just_go_now and turn_count < MIN_REFINEMENT_TURNS:
        ready = False
        if not next_question:
            fallback_pending = _next_missing(patched)
            next_question = _deterministic_question(fallback_pending, patched) if fallback_pending else _search_strategy_question(patched)

    if not next_question and not ready:
        fallback_pending = _next_missing(patched)
        next_question = _deterministic_question(fallback_pending, patched) if fallback_pending else None
        if next_question and _question_repeats_answered_constraint(next_question, patched):
            next_question = _strategy_deepening_question(patched, messages)
        ready = fallback_pending is None
    if ready:
        patched = _fill_defaults(patched)
        patched = _critique_search_plan(patched)
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
        updated[key] = _merge_string_lists(updated.get(key), patch.get(key), limit=16 if key != "search_queries" else 20)
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
        query_list = _string_list(queries, limit=20)
        if query_list:
            cleaned[source_key] = query_list
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
        email = str(item or "").strip().lower()
        if "@" not in email or email in seen:
            continue
        emails.append(email)
        seen.add(email)
    return emails


def _gmail_rules_from_answer(answer: str, profile: dict[str, Any]) -> dict[str, Any]:
    intent = " ".join(answer.split()).strip()
    lookback_hours = _extract_lookback_hours(answer) or _coerce_lookback_hours(profile.get("lookback_hours")) or 168
    return {
        "intent": intent,
        "lookback_hours": lookback_hours,
    }


def _discover_gmail_candidates(rules: dict[str, Any]) -> list[NewsletterCandidate]:
    intent = str(rules.get("intent") or "").strip()
    lookback_hours = int(rules.get("lookback_hours") or 168)
    try:
        return _run_sync_list(
            discover_newsletter_candidates(
                query_text=intent,
                lookback_hours=lookback_hours,
                limit=8,
            )
        )
    except Exception:
        logger.exception("Failed to discover Gmail newsletter candidates")
        return []


def _gmail_candidate_question(candidates: list[NewsletterCandidate], rules: dict[str, Any]) -> str:
    sender_lines = [
        f"{index}. {candidate.sender_name or candidate.sender} <{candidate.sender}> ({candidate.message_count} found; latest subject: {candidate.subject})"
        for index, candidate in enumerate(candidates[:8], start=1)
    ]
    lookback = _lookback_label(int(rules.get("lookback_hours") or 168))
    return (
        f"I searched Gmail for {rules.get('intent')} across {lookback} and found newsletter candidates:\n"
        + "\n".join(sender_lines)
        + "\nWhich should I approve to the Gmail allowlist? Only approved senders are ever read. "
        "Reply with numbers, sender names, 'all', or 'none'."
    )


def _selected_gmail_senders(answer: str, rules: dict[str, Any]) -> list[str]:
    candidates = [candidate for candidate in rules.get("candidates", []) if isinstance(candidate, dict)]
    if not candidates:
        return _email_list(answer)
    lowered = answer.lower()
    if any(token in lowered for token in ("none", "no gmail", "skip gmail", "without gmail")):
        return []
    if re.search(r"\ball\b|\bevery\b", lowered):
        return _email_list([candidate.get("sender") for candidate in candidates])
    selected: list[str] = []
    for number in re.findall(r"\b\d+\b", answer):
        index = int(number) - 1
        if 0 <= index < len(candidates):
            selected.extend(_email_list([candidates[index].get("sender")]))
    for candidate in candidates:
        sender = str(candidate.get("sender") or "").strip().lower()
        name = str(candidate.get("sender_name") or "").strip().lower()
        if sender and (sender in lowered or (name and name in lowered)):
            selected.extend(_email_list([sender]))
    selected.extend(_email_list(answer))
    return _email_list(selected)


def _lookback_label(hours: int) -> str:
    if hours % 168 == 0 and hours >= 168:
        weeks = hours // 168
        return f"the last {weeks} week{'s' if weeks != 1 else ''}"
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return f"the last {days} day{'s' if days != 1 else ''}"
    return f"the last {hours} hour{'s' if hours != 1 else ''}"


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
        lookback_hours = _extract_lookback_hours(answer)
        if lookback_hours:
            updated["lookback_hours"] = lookback_hours
            updated["recency_weighting"] = _recency_from_lookback_hours(lookback_hours)
        else:
            updated["recency_weighting"] = _normalize_recency(answer) or "recent"
            if updated["recency_weighting"] in {"last_year", "all_available"}:
                updated["lookback_hours"] = 8760
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
    model_client = model_routing.client_for_agent(
        "refinement",
        settings=settings,
        items=_privacy_markers_for_refinement(profile),
        model_override=model_name,
    ).client
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
        lookback_hours = _coerce_lookback_hours(parsed.get("lookback_hours")) or _extract_lookback_hours(raw_answer)
        if lookback_hours:
            return {
                "recency_weighting": _recency_from_lookback_hours(lookback_hours),
                "lookback_hours": lookback_hours,
                "source_scope_answered": True,
            }
        if not normalized:
            normalized = _normalize_recency(raw_answer)
        if not normalized:
            return None
        updates: dict[str, Any] = {"recency_weighting": normalized, "source_scope_answered": True}
        if normalized in {"last_year", "all_available"}:
            updates["lookback_hours"] = 8760
        return updates

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
            "Return strict JSON with recency_weighting and, when the user gives a concrete window, lookback_hours.\n"
            'Allowed values: breaking, recent, last_year, all_available.\n'
            'Examples: "last 24 hours" -> {"recency_weighting": "breaking", "lookback_hours": 24}; '
            '"last 3 days" -> {"recency_weighting": "recent", "lookback_hours": 72}; '
            '"no more than a year old" -> {"recency_weighting": "last_year", "lookback_hours": 8760}.\n'
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

    for source, enabled in selection.items():
        if not enabled or queries.get(source):
            continue
        if source == "markets":
            fallback = resolved_tickers or phrase_queries[:2]
        else:
            fallback = phrase_queries[:3]
        if fallback:
            queries[source] = list(fallback)
    return {**profile, "source_queries": queries}


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
        parts.append(f"{lead} {theme_chunk} 2025".strip())

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
    "reddit": "subreddits or communities",
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
    return {
        "topic_id": str(profile.get("topic_id") or database.new_id()),
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "subtopics": _string_list(profile.get("subtopics")),
        "keywords": _string_list(profile.get("keywords")),
        "search_queries": _string_list(profile.get("search_queries"), limit=20),
        "source_queries": source_queries,
        "foreign_language_plan": _normalize_foreign_language_plan(profile.get("foreign_language_plan")),
        "depth": str(profile.get("depth") or ""),
        "recency_weighting": str(profile.get("recency_weighting") or ""),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")),
        "exclusions": _string_list(profile.get("exclusions")),
        "source_selection": selected_sources,
        "requested_sources": requested_sources,
        "gmail_rules": _normalize_gmail_rules(profile.get("gmail_rules")),
        "reasoning_summary": str(profile.get("reasoning_summary") or "").strip()[:600],
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
    return {
        "lookback_window": _extract_lookback_constraint(statement),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")) or _extract_lookback_hours(statement),
        "recency_already_answered": bool(profile.get("source_scope_answered")) or bool(_normalize_recency(profile.get("recency_weighting"))),
        "excluded_publishers_or_source_types": _string_list(profile.get("exclusions")),
        "exclusions_already_answered": bool(profile.get("exclusions_answered")) or bool(_string_list(profile.get("exclusions"))),
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
    return any(token in lowered for token in ("investor", "stock", "stocks", "company's performance", "companies performance", "performance", "ticker", "market"))


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
    "reddit": "Reddit",
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
            if source == "markets":
                # Resolve the actual tickers that will be fetched so the user can
                # see and verify them in the confirmation card.
                profile_text = " ".join(filter(None, [
                    str(profile.get("statement") or ""),
                    str(profile.get("scope") or ""),
                    *_string_list(profile.get("keywords")),
                    *_string_list(profile.get("subtopics")),
                    *entry["queries"],
                ]))
                resolved = resolve_tickers_from_text(profile_text)
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
        "strategy_preview": _strategy_preview(profile),
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
