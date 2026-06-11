"""Legacy turn-based refinement session engine (start/advance) and gmail refinement.

Code moved verbatim from refinement.py (M7 split).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from backend.agents.discovery.types import DEFAULT_EXPLORE_SOURCE_SELECTION
from backend.agents.digestor.gmail import NewsletterCandidate, discover_newsletter_candidates
from backend.app.core.config import get_settings
from backend.app.core.prompt_loader import load_prompt
from backend.app.db import database
from backend.app.services import explore, gmail_allowlist, model_routing

from backend.app.services.profile_patch import (
    AGENT_PENDING_FIELD,
    GMAIL_RULES_FIELD,
    GMAIL_SENDER_SELECTION_FIELD,
    MAX_REFINEMENT_TURNS,
    MIN_REFINEMENT_TURNS,
    _answered_field_for_current_question,
    _canonicalize_must_have,
    _clean_must_have_aliases,
    _clean_source_queries,
    _coerce_lookback_hours,
    _coerce_profile,
    _dedupe_next_question,
    _deterministic_question,
    _diagnostics_query_snapshot,
    _email_list,
    _enrich_diagnostics,
    _extract_lookback_hours,
    _extract_requested_sources,
    _fill_defaults,
    _inferred_constraints,
    _is_generic_actionable_question,
    _is_refinement_closing_language,
    _keywords,
    _merge_agent_profile_patch,
    _merge_requested_source_hints,
    _merge_requested_source_lists,
    _merge_source_queries,
    _merge_string_lists,
    _messages,
    _models,
    _negative_answer,
    _next_missing,
    _normalize_depth,
    _normalize_foreign_language_plan,
    _normalize_gmail_rules,
    _normalize_recency,
    _normalize_requested_sources,
    _question_repeats_answered_constraint,
    _readiness_reason,
    _recency_from_lookback_hours,
    _run_sync_complete_json,
    _run_sync_list,
    _search_strategy_question,
    _seed_profile_with_hints,
    _session_response,
    _source_selection_dict,
    _split_answer_list,
    _strategy_deepening_question,
    _string_list,
    _user_authored_text,
)


logger = logging.getLogger(__name__)


def start_session(payload: dict[str, Any]) -> dict[str, Any]:
    statement = str(payload.get("statement") or "").strip()
    if not statement:
        raise ValueError("Interest statement is required")
    profile = _seed_profile_with_hints(_initial_profile(payload))
    messages: list[dict[str, str]] = []
    
    selection = _source_selection_dict(profile.get("source_selection"))
    if selection.get("gmail"):
        rules = _gmail_rules_from_answer(statement, profile)
        candidates = _discover_gmail_candidates(rules, profile)
        notes = rules.pop("_ai_candidate_notes", {})
        rules["candidates"] = [
            {
                **candidate.to_dict(),
                **({"ai_rationale": notes.get(candidate.sender.lower())} if notes.get(candidate.sender.lower()) else {}),
            }
            for candidate in candidates
        ]
        if candidates:
            gmail_allowlist.record_candidates(rules["candidates"], source="refinement")
        profile["gmail_rules"] = rules
        profile["source_queries"] = _merge_source_queries(profile.get("source_queries"), {"gmail": [rules["gmail_search_query"]]})
        profile["lookback_hours"] = rules["lookback_hours"]
        profile["recency_weighting"] = _recency_from_lookback_hours(int(rules["lookback_hours"]))
        profile["source_scope_answered"] = True

        if candidates:
            question = _gmail_candidate_question(candidates, rules)
        else:
            question = (
                "I searched Gmail for that newsletter pattern but didn’t find clear newsletter senders. "
                "Name any sender or newsletter you want included, or say to continue without Gmail."
            )
        question += "\n\n*You can also add optional instructions for how I should extract information from these newsletters (e.g., 'Extract developer tools and ignore sponsorships').*"
        
        messages = [{"role": "assistant", "content": question}]
        session = database.create_refinement_session(
            statement=statement,
            profile=profile,
            messages=messages,
            pending_field=GMAIL_SENDER_SELECTION_FIELD,
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
        profile, next_question, _ready = _apply_agent_update(
            profile=profile,
            messages=messages,
            agent_update=agent_update,
            just_go_now=False,
            turn_count=0,
        )
        pending = AGENT_PENDING_FIELD
        messages = [{"role": "assistant", "content": next_question or _strategy_deepening_question(profile, messages)}]
        status = "active"
    else:
        pending = AGENT_PENDING_FIELD
        missing = _next_missing(profile)
        messages = [{"role": "assistant", "content": _deterministic_question(missing, profile) if missing else _strategy_deepening_question(profile, messages)}]
        status = "active"
    session = database.create_refinement_session(
        statement=statement,
        profile=profile,
        messages=messages,
        pending_field=pending,
        status=status,
    )
    return _session_response(session)


def advance_session(session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    session = database.get_refinement_session(session_id)
    if session is None:
        return None
    answer = str(payload.get("answer") or "").strip()
    just_go_now = bool(payload.get("just_go_now"))
    if session["status"] == "finalized":
        if answer and not just_go_now:
            session = {**session, "status": "active", "pending_field": AGENT_PENDING_FIELD}
        else:
            return _session_response(session)

    profile = dict(session["profile"])
    messages = _messages(session)
    pending = session.get("pending_field")
    profile = _apply_models(profile, payload.get("models"))
    turn_count = int(session.get("turn_count") or 0)

    # Update source selection and detect toggled-on/off Gmail
    prev_selection = _source_selection_dict(profile.get("source_selection"))
    incoming_selection = payload.get("source_selection") or {}
    profile["source_selection"] = {**prev_selection, **incoming_selection}

    if profile["source_selection"].get("gmail") and not prev_selection.get("gmail"):
        profile["gmail_rules"] = {}
        pending = None  # Force rediscovery/reprompt
    elif not profile["source_selection"].get("gmail") and prev_selection.get("gmail"):
        if pending == GMAIL_SENDER_SELECTION_FIELD or pending == GMAIL_RULES_FIELD:
            pending = None

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
        if pending in {GMAIL_SENDER_SELECTION_FIELD, GMAIL_RULES_FIELD}:
            rules = _normalize_gmail_rules(profile.get("gmail_rules"))
            rules["include_senders"] = []
            profile["gmail_rules"] = rules
            profile["source_selection"] = {**_source_selection_dict(profile.get("source_selection")), "gmail": False}
            pending = None
            messages.append({"role": "assistant", "content": "Got it. I'll continue without Gmail for this brief."})

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
            if pending == AGENT_PENDING_FIELD:
                profile = _apply_freeform_refinement_answer(profile, answer)
            else:
                profile = _apply_answer_with_model(profile, answered_field, answer)
        profile = _seed_profile_with_hints(profile)
        profile = _fill_defaults(profile) if just_go_now else profile
        if just_go_now:
            next_pending = None
        else:
            next_pending = AGENT_PENDING_FIELD
        status = "finalized" if next_pending is None else "active"
        topic_id = session.get("topic_id")
        if status == "active":
            missing = _next_missing(profile)
            messages.append({"role": "assistant", "content": _deterministic_question(missing, profile) if missing else _strategy_deepening_question(profile, messages)})
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

    selection = _source_selection_dict(profile.get("source_selection"))
    gmail_rules = _normalize_gmail_rules(profile.get("gmail_rules"))

    # Immediate candidates discovery scan if Gmail is active but rules/candidates are empty:
    trigger_gmail_discovery = (
        selection.get("gmail")
        and not gmail_rules.get("candidates")
        and not gmail_rules.get("include_senders")
        and not gmail_rules.get("intent")
        and pending_field != GMAIL_SENDER_SELECTION_FIELD
    )

    if trigger_gmail_discovery:
        updated = dict(profile)
        query_text = answer.strip() or updated.get("statement") or ""
        rules = _gmail_rules_from_answer(query_text, updated)
        candidates = _discover_gmail_candidates(rules, updated)
        notes = rules.pop("_ai_candidate_notes", {})
        rules["candidates"] = [
            {
                **candidate.to_dict(),
                **({"ai_rationale": notes.get(candidate.sender.lower())} if notes.get(candidate.sender.lower()) else {}),
            }
            for candidate in candidates
        ]
        if candidates:
            gmail_allowlist.record_candidates(rules["candidates"], source="refinement")
        updated["gmail_rules"] = rules
        updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), {"gmail": [rules["gmail_search_query"]]})
        updated["lookback_hours"] = rules["lookback_hours"]
        updated["recency_weighting"] = _recency_from_lookback_hours(int(rules["lookback_hours"]))
        updated["source_scope_answered"] = True

        if candidates:
            intro = _gmail_candidate_question(candidates, rules)
        else:
            intro = (
                "I searched Gmail for that newsletter pattern but didn’t find clear newsletter senders. "
                "Name any sender or newsletter you want included, or say to continue without Gmail."
            )
        intro += "\n\n*You can also add optional instructions for how I should extract information from these newsletters (e.g., 'Extract developer tools and ignore sponsorships').*"

        return (
            _coerce_profile(updated),
            intro,
            GMAIL_SENDER_SELECTION_FIELD,
            "active",
        )

    if pending_field == GMAIL_SENDER_SELECTION_FIELD:
        updated = dict(profile)
        rules = _normalize_gmail_rules(updated.get("gmail_rules"))
        if not _is_gmail_approval_response(answer, rules):
            return None

        intent_match = re.search(r"Instructions:\s*(.*)", answer, re.DOTALL | re.IGNORECASE)
        if intent_match:
            extraction_instructions = intent_match.group(1).strip()
            sender_part = answer[:intent_match.start()].strip()
        else:
            extraction_instructions = ""
            sender_part = answer

        include_senders = _selected_gmail_senders(sender_part, rules)
        if include_senders:
            approved = gmail_allowlist.approve_senders(include_senders, source="refinement")
            rules["include_senders"] = approved or include_senders
            if extraction_instructions:
                rules["intent"] = extraction_instructions
            updated["requested_sources"] = _merge_requested_source_lists(
                _normalize_requested_sources(updated.get("requested_sources")),
                [{"adapter": "gmail", "ref": sender} for sender in include_senders],
            )
            updated["requested_sources_answered"] = True
            updated["gmail_rules"] = rules
            
            confirmation = (
                f"Approved {', '.join(include_senders)} to the Gmail allowlist. "
                "These newsletters become discovery feeds, and the articles they link to become primary content for this brief."
            )
            if extraction_instructions:
                confirmation += f" I will extract items based on your instructions: '{extraction_instructions}'."
            
            return (
                _coerce_profile(updated),
                confirmation,
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
            "finalized",
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
    payload_lookback = _coerce_lookback_hours(payload.get("lookback_hours"))
    payload_recency = str(payload.get("recency_weighting") or "").strip()
    lookback_hours = payload_lookback if payload_lookback is not None else _extract_lookback_hours(statement)
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
        "recency_weighting": payload_recency,
        "lookback_hours": lookback_hours,
        "exclusions": [],
        "must_have_terms": [],
        "must_have_aliases": {},
        "source_selection": {**DEFAULT_EXPLORE_SOURCE_SELECTION, **selected_sources},
        "requested_sources": requested_sources,
        "gmail_rules": {},
        "related_interests_answered": False,
        "requested_sources_answered": bool(requested_sources),
        "depth_answered": False,
        "source_scope_answered": False,
        "exclusions_answered": False,
        "must_have_answered": False,
        "promoted_sources": [],
        "models": {**{"refinement": None, "brief": None}, **_models(payload.get("models"))},
        "schedule": None,
        "schedule_config": {},
        "delivery_config": {},
    }


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
                system=load_prompt("refinement_agent"),
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
                system=load_prompt("critique_agent"),
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
    current_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    profile_snapshot = {
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "subtopics": _string_list(profile.get("subtopics")),
        "keywords": _string_list(profile.get("keywords")),
        "search_queries": _string_list(profile.get("search_queries")),
        "source_queries": _clean_source_queries(profile.get("source_queries")),
        "foreign_language_plan": _normalize_foreign_language_plan(profile.get("foreign_language_plan")),
        "foreign_regions": _string_list(profile.get("foreign_regions"), limit=16),
        "direct_episode_queries": _string_list(profile.get("direct_episode_queries"), limit=16),
        "related_episode_queries": _string_list(profile.get("related_episode_queries"), limit=16),
        "negative_constraints": _string_list(profile.get("negative_constraints"), limit=16),
        "priority_terms": _string_list(profile.get("priority_terms"), limit=16),
        "must_have_terms": _string_list(profile.get("must_have_terms"), limit=6),
        "must_have_aliases": _clean_must_have_aliases(profile.get("must_have_aliases"), terms=_string_list(profile.get("must_have_terms"), limit=6)),
        "depth": _normalize_depth(profile.get("depth")),
        "recency_weighting": _normalize_recency(profile.get("recency_weighting")),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")),
        "exclusions": _string_list(profile.get("exclusions")),
        "source_selection": _source_selection_dict(profile.get("source_selection")),
        "current_date_utc": current_utc,
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
                    "When selected, propose any non-English language the topic warrants and write idiomatic native-language queries. "
                    "Use this for public foreign media only."
                ),
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
                "comparison angles, signal types, decision criteria, or what would make the brief useful. "
                "Always include one next question that would further elicit the user's requirements. "
                "Do not say the plan is complete, ready, or that you have what you need. There is no "
                "minimum or maximum question count. Only the user's explicit search-strategy confirmation "
                "button ends refinement; if just_go_now is true, finalize now with best inferred defaults."
            ),
            "revisit_policy": (
                "If this is an existing profile being revisited, ask how to sharpen the search plan "
                "or what would make the rebuilt brief more useful before marking ready."
            ) if profile.get("_revisit_existing") and messages == [] and turn_count == 0 else "",
            "current_date_hint": f"Today is {current_utc} (UTC). Use this date when judging freshness windows.",
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
    ready = just_go_now
    next_question = _clean_next_question(agent_update.get("next_question"))
    if next_question and _is_generic_actionable_question(next_question):
        next_question = _strategy_deepening_question(patched, messages)
    if next_question and _question_repeats_answered_constraint(next_question, patched):
        next_question = _strategy_deepening_question(patched, messages)
    next_question = _dedupe_next_question(next_question, patched, messages)

    if not next_question and not ready:
        fallback_pending = _next_missing(patched)
        next_question = _deterministic_question(fallback_pending, patched) if fallback_pending else _strategy_deepening_question(patched, messages)
        if next_question and _question_repeats_answered_constraint(next_question, patched):
            next_question = _strategy_deepening_question(patched, messages)
        next_question = _dedupe_next_question(next_question, patched, messages)
        if not next_question:
            next_question = _search_strategy_question(patched)
    if ready:
        readiness_reason = _readiness_reason(
            ready_requested=ready_requested,
            just_go_now=just_go_now,
            turn_count=turn_count,
        )
        patched = _fill_defaults(patched)
        pre_critique = _diagnostics_query_snapshot(patched)
        patched = _critique_search_plan(patched)
        patched["refinement_diagnostics"] = _enrich_diagnostics(
            patched,
            model_profile_patch=agent_update.get("profile_patch"),
            pre_critique=pre_critique,
            readiness_reason=readiness_reason,
        )
        next_question = None
    return _coerce_profile(patched), next_question, ready


def _clean_next_question(value: Any) -> str | None:
    question = " ".join(str(value or "").split()).strip()
    if not question:
        return None
    if not question.endswith("?"):
        question = f"{question}?"
    if _is_refinement_closing_language(question):
        return None
    return question[:260]


def _ai_gmail_discovery_plan(intent: str, *, lookback_hours: int, profile: dict[str, Any]) -> dict[str, Any]:
    client = _refinement_model_client(profile)
    if client is None or not intent.strip():
        return {}
    prompt = json.dumps(
        {
            "task": "Craft the Gmail search query and screening criteria for newsletter sender discovery.",
            "user_request": intent,
            "brief_intent": {
                "statement": str(profile.get("statement") or ""),
                "scope": str(profile.get("scope") or ""),
                "keywords": _string_list(profile.get("keywords"), limit=12),
                "subtopics": _string_list(profile.get("subtopics"), limit=12),
                "exclusions": _string_list(profile.get("exclusions"), limit=12),
            },
            "lookback_hours": lookback_hours,
            "constraints": [
                "Return strict JSON only.",
                "gmail_search_query should be a compact natural-language topic phrase, not Gmail operators.",
                "selection_criteria should say what would make a sender relevant.",
                "Reject consumer, ticketing, automotive, sports, shopping, and unrelated hobby senders unless the user explicitly asked for them.",
            ],
            "schema": {
                "gmail_search_query": "string",
                "selection_criteria": "string",
            },
        },
        ensure_ascii=False,
    )
    try:
        parsed = _run_sync_complete_json(
            client.complete_json(
                system=load_prompt("refinement_agent"),
                prompt=prompt,
                max_tokens=700,
            )
        )
    except Exception:
        logger.exception("Failed to run Gmail discovery planning agent")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "gmail_search_query": str(parsed.get("gmail_search_query") or "").strip(),
        "selection_criteria": str(parsed.get("selection_criteria") or "").strip(),
    }


def _ai_rank_gmail_candidates(rules: dict[str, Any], candidates: list[NewsletterCandidate], profile: dict[str, Any]) -> list[NewsletterCandidate]:
    if not candidates:
        return []
    client = _refinement_model_client(profile)
    if client is None:
        return candidates[:8]
    candidate_payload = [candidate.to_dict() for candidate in candidates[:16]]
    prompt = json.dumps(
        {
            "task": "Rank Gmail newsletter sender candidates for the user's requested brief.",
            "gmail_search_query": rules.get("gmail_search_query") or rules.get("intent"),
            "selection_criteria": rules.get("selection_criteria"),
            "candidates": candidate_payload,
            "constraints": [
                "Return strict JSON only.",
                "Only recommend senders likely to satisfy the brief intent.",
                "Exclude clearly unrelated consumer commerce, ticketing, automotive, sports, or hobby senders.",
            ],
            "schema": {
                "selected": [
                    {
                        "sender": "email address from candidates",
                        "score": "0.0 to 1.0",
                        "rationale": "short reason",
                    }
                ],
                "rejected": [
                    {
                        "sender": "email address from candidates",
                        "reason": "short reason",
                    }
                ],
            },
        },
        ensure_ascii=False,
    )
    try:
        parsed = _run_sync_complete_json(
            client.complete_json(
                system=load_prompt("refinement_agent"),
                prompt=prompt,
                max_tokens=1000,
            )
        )
    except Exception:
        logger.exception("Failed to run Gmail candidate screening agent")
        return candidates[:8]
    selected = parsed.get("selected") if isinstance(parsed, dict) else None
    if not isinstance(selected, list):
        return candidates[:8]
    by_sender = {candidate.sender.lower(): candidate for candidate in candidates}
    ranked: list[tuple[float, NewsletterCandidate, str]] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        sender = str(item.get("sender") or "").strip().lower()
        candidate = by_sender.get(sender)
        if candidate is None:
            continue
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score < 0.45:
            continue
        ranked.append((score, candidate, str(item.get("rationale") or "").strip()))
    ranked.sort(key=lambda item: item[0], reverse=True)
    rules["_ai_candidate_notes"] = {
        candidate.sender.lower(): rationale
        for _score, candidate, rationale in ranked
        if rationale
    }
    return [candidate for _score, candidate, _rationale in ranked] or candidates[:8]


def _gmail_rules_from_answer(answer: str, profile: dict[str, Any]) -> dict[str, Any]:
    intent = " ".join(answer.split()).strip()
    lookback_hours = _extract_lookback_hours(answer) or _coerce_lookback_hours(profile.get("lookback_hours")) or 168
    ai_plan = _ai_gmail_discovery_plan(intent, lookback_hours=lookback_hours, profile=profile)
    query_text = str(ai_plan.get("gmail_search_query") or intent).strip() or intent
    return {
        "intent": intent,
        "gmail_search_query": query_text,
        "selection_criteria": str(ai_plan.get("selection_criteria") or "").strip(),
        "ai_managed": bool(ai_plan),
        "lookback_hours": lookback_hours,
    }


def _discover_gmail_candidates(rules: dict[str, Any], profile: dict[str, Any]) -> list[NewsletterCandidate]:
    intent = str(rules.get("gmail_search_query") or rules.get("intent") or "").strip()
    lookback_hours = int(rules.get("lookback_hours") or 168)
    try:
        candidates = _run_sync_list(
            discover_newsletter_candidates(
                query_text=intent,
                lookback_hours=lookback_hours,
                limit=16,
            )
        )
        ranked = _ai_rank_gmail_candidates(rules, candidates, profile)
        return ranked[:8] if ranked else candidates[:8]
    except Exception:
        logger.exception("Failed to discover Gmail newsletter candidates")
        return []


def _gmail_candidate_question(candidates: list[NewsletterCandidate], rules: dict[str, Any]) -> str:
    ai_notes = {
        str(item.get("sender") or "").lower(): str(item.get("ai_rationale") or "").strip()
        for item in rules.get("candidates", [])
        if isinstance(item, dict)
    }
    sender_lines = [
        (
            f"{index}. {candidate.sender_name or candidate.sender} <{candidate.sender}> "
            f"({candidate.message_count} found; latest subject: {candidate.subject})"
            + (f" — {ai_notes.get(candidate.sender.lower())}" if ai_notes.get(candidate.sender.lower()) else "")
        )
        for index, candidate in enumerate(candidates[:8], start=1)
    ]
    lookback = _lookback_label(int(rules.get("lookback_hours") or 168))
    criteria = str(rules.get("selection_criteria") or "").strip()
    search_phrase = str(rules.get("gmail_search_query") or rules.get("intent") or "").strip()
    intro = (
        f"I asked the AI to craft the Gmail search and screen candidate senders. "
        f"It searched Gmail for {search_phrase} across {lookback}"
    )
    if criteria:
        intro += f", prioritizing {criteria}"
    return (
        intro
        + " and found newsletter candidates:\n"
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


def _is_gmail_approval_response(answer: str, rules: dict[str, Any]) -> bool:
    clean = str(answer or "").strip()
    if not clean:
        return False
    lowered = clean.lower()
    if any(token in lowered for token in ("none", "no gmail", "skip gmail", "without gmail")):
        return True
    if _email_list(clean):
        return True

    candidates = [candidate for candidate in rules.get("candidates", []) if isinstance(candidate, dict)]
    if not candidates:
        return False
    if re.search(r"\ball\b|\bevery\b", lowered):
        return True
    for number in re.findall(r"\b\d+\b", clean):
        index = int(number) - 1
        if 0 <= index < len(candidates):
            return True
    for candidate in candidates:
        sender = str(candidate.get("sender") or "").strip().lower()
        name = str(candidate.get("sender_name") or "").strip().lower()
        if sender and sender in lowered:
            return True
        if name and name in lowered:
            return True
    return False


def _lookback_label(hours: int) -> str:
    if hours % 168 == 0 and hours >= 168:
        weeks = hours // 168
        return f"the last {weeks} week{'s' if weeks != 1 else ''}"
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return f"the last {days} day{'s' if days != 1 else ''}"
    return f"the last {hours} hour{'s' if hours != 1 else ''}"


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
            if updated["recency_weighting"] == "last_year":
                updated["lookback_hours"] = 8760
            elif updated["recency_weighting"] == "all_available":
                updated["lookback_hours"] = None
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
    elif field == "must_have":
        updated["must_have_answered"] = True
        lowered = answer.lower().strip()
        if lowered in {"no", "none", "nothing", "nope", "n/a", "skip"} or "nothing" in lowered:
            updated["must_have_terms"] = []
            updated["must_have_aliases"] = {}
        else:
            raw_terms = [
                part.strip(" .")
                for part in re.split(r"[,;\n]", answer)
                if part.strip(" .")
            ][:6]
            raw_aliases = _clean_must_have_aliases(
                updated.get("must_have_aliases"),
                terms=_string_list(raw_terms, limit=6),
            )
            canonical_terms, canonical_aliases = _canonicalize_must_have(raw_terms, raw_aliases)
            updated["must_have_terms"] = canonical_terms
            updated["must_have_aliases"] = canonical_aliases
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


def _apply_freeform_refinement_answer(profile: dict[str, Any], answer: str) -> dict[str, Any]:
    clean = " ".join(str(answer or "").split()).strip()
    if not clean:
        return profile

    updated = dict(profile)
    statement = str(updated.get("statement") or "").strip()
    scope = str(updated.get("scope") or "").strip()
    if not scope or scope == statement:
        updated["scope"] = clean[:220]
        updated["scope_answered"] = True
    else:
        related = _merge_string_lists(updated.get("related_interests"), [clean], limit=12)
        if related:
            updated["related_interests"] = related
            updated["related_interests_answered"] = True

    keywords = _merge_string_lists(updated.get("keywords"), _keywords(clean), limit=24)
    if keywords:
        updated["keywords"] = keywords

    queries = _merge_string_lists(updated.get("search_queries"), [clean], limit=12)
    if queries:
        updated["search_queries"] = queries

    return _coerce_profile(_merge_requested_source_hints(updated, clean))


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
        if normalized == "last_year":
            updates["lookback_hours"] = 8760
        elif normalized == "all_available":
            updates["lookback_hours"] = None
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

    if field == "must_have":
        terms = parsed.get("must_have_terms") or parsed.get("must_have")
        if isinstance(terms, str):
            terms = [item for item in re.split(r"[,;\n]", terms) if item.strip(" .")]
        lowered = str(raw_answer).strip().lower()
        if isinstance(terms, list):
            cleaned = [str(item).strip(" .") for item in terms if str(item).strip(" .")][:6]
            raw_aliases = _clean_must_have_aliases(parsed.get("must_have_aliases"), terms=cleaned)
            canonical_terms, canonical_aliases = _canonicalize_must_have(cleaned, raw_aliases)
            return {
                "must_have_terms": canonical_terms,
                "must_have_aliases": canonical_aliases,
                "must_have_answered": True,
            }
        if lowered in {"no", "none", "nothing", "nope", "n/a", "skip"} or "nothing" in lowered:
            return {"must_have_terms": [], "must_have_aliases": {}, "must_have_answered": True}
        if raw_answer.strip():
            raw_terms = [raw_answer.strip(" .")]
            canonical_terms, canonical_aliases = _canonicalize_must_have(raw_terms, {})
            return {
                "must_have_terms": canonical_terms,
                "must_have_aliases": canonical_aliases,
                "must_have_answered": True,
            }

    return None


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
    if field == "must_have":
        return (
            "Return strict JSON with must_have_terms and optional must_have_aliases.\n"
            "Every entry in must_have_terms must be a DISTINCT required concept (logical AND); "
            "synonyms, abbreviations, and translations must go in must_have_aliases, never as additional terms.\n"
            'Example response: {"must_have_terms": ["Mexico City"], "must_have_aliases": {"mexico city": ["CDMX", "Ciudad de México"]}}.\n'
            'If there is no required anchor term, return {"must_have_terms": [], "must_have_aliases": {}}.\n'
            "Only include terms the user explicitly confirms every item must mention.\n"
            f"\nCurrent profile snapshot: {profile_snapshot}\n"
            f"User answer: {answer}"
        )

    return (
        "Return strict JSON with one key: scope.\n"
        'Example response: {"scope": "Small team deployment patterns."}\n'
        f"\nCurrent profile snapshot: {profile_snapshot}\n"
        f"User answer: {answer}"
    )
