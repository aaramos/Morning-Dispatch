from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.agents.discovery.language_support import trusted_language_options
from backend.agents.discovery.markets import normalize_market_query_tickers, resolve_tickers_from_text
from backend.agents.discovery.types import DEFAULT_EXPLORE_SOURCE_SELECTION
from backend.agents.digestor.gmail import NewsletterCandidate, discover_newsletter_candidates
from backend.agents.librarian.text_utils import keyword_set
from backend.agents.model import ModelClient
from backend.app.core.config import get_settings
from backend.app.core.prompt_loader import load_prompt
from backend.app.db import database
from backend.app.services import explore, gmail_allowlist, model_routing

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
    "requested_sources": "Any specific podcast, YouTube channel, newsletter, site, company, or collection I should try to include?",
    "exclusions": "Anything I should avoid so the brief stays focused?",
    GMAIL_RULES_FIELD: "How do you want me to use Gmail for this brief? For example: AI-related newsletters received in the last 7 days.",
}

VALID_SOURCE_ADAPTERS = {"gmail", "podcasts", "web_search", "foreign_media", "youtube", "collections", "markets", "reddit", "google_news"}
PODCAST_STRATEGY_FIELDS = (
    "direct_episode_queries",
    "related_episode_queries",
    "negative_constraints",
    "priority_terms",
)



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


async def astream_refinement(
    *,
    session_id: str | None,
    statement: str,
    source_selection: dict[str, Any] | None,
    models: Any,
    answer: str,
    just_go_now: bool,
    foreign_regions: list[str] | None = None,
    recency_weighting: str | None = None,
    lookback_hours: int | None = None,
):
    """AI-led streaming refinement turn.

    Yields event dicts: ``session`` (the live session id), ``token`` (a prose delta the
    user sees), ``plan`` (the persisted session snapshot), ``done`` (final snapshot), and
    ``error``. The model leads the whole conversation here -- there is no deterministic
    question bank on this path. If the model client is unavailable we fall back to the
    deterministic ``advance_session`` engine for a single graceful turn.
    """
    clean_answer = str(answer or "").strip()
    session = database.get_refinement_session(session_id) if session_id else None
    if session is None:
        clean_statement = str(statement or "").strip()
        if not clean_statement:
            yield {"type": "error", "message": "Interest statement is required"}
            return
        profile = _seed_profile_with_hints(
            _initial_profile(
                {
                    "statement": clean_statement,
                    "source_selection": source_selection or {},
                    "foreign_regions": foreign_regions or [],
                    "recency_weighting": recency_weighting,
                    "lookback_hours": lookback_hours,
                    "models": models,
                }
            )
        )

        selection = _source_selection_dict(profile.get("source_selection"))
        if selection.get("gmail"):
            rules = _gmail_rules_from_answer(clean_statement, profile)
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
                statement=clean_statement,
                profile=profile,
                messages=messages,
                pending_field=GMAIL_SENDER_SELECTION_FIELD,
                status="active",
            )
            session_id = session["session_id"]
            yield {"type": "session", "session_id": session_id}
            yield {"type": "token", "text": question}
            response = _session_response(session)
            
            candidate_dicts = rules["candidates"]
            yield {
                "type": "gmail_candidates",
                "candidates": candidate_dicts,
                "intro": question,
                "criteria": str(rules.get("selection_criteria") or ""),
                "search_phrase": str(rules.get("gmail_search_query") or clean_statement),
                "lookback_hours": rules["lookback_hours"],
            }
            yield {"type": "plan", "session": response}
            yield {"type": "done", "session": response, "ready": False, "trigger_build": False}
            return

        session = database.create_refinement_session(
            statement=clean_statement,
            profile=profile,
            messages=[],
            pending_field=AGENT_PENDING_FIELD,
            status="active",
        )
        session_id = session["session_id"]
        messages: list[dict[str, str]] = []
    else:
        session_id = session["session_id"]
        if session["status"] == "finalized":
            if clean_answer and not just_go_now:
                session = {**session, "status": "active", "pending_field": AGENT_PENDING_FIELD}
            else:
                yield {"type": "done", "session": _session_response(session), "ready": True, "trigger_build": False}
                return
        profile = dict(session["profile"])
        messages = _messages(session)

    pending_field = session.get("pending_field") or ""

    # Detect if source selection toggled Gmail on/off
    prev_selection = _source_selection_dict(profile.get("source_selection"))
    incoming_selection = source_selection or {}
    profile["source_selection"] = {**prev_selection, **incoming_selection}
    if recency_weighting == "all_available":
        profile["recency_weighting"] = "all_available"
        profile["lookback_hours"] = None
    elif lookback_hours is not None:
        coerced_lookback = _coerce_lookback_hours(lookback_hours)
        if coerced_lookback is not None:
            profile["lookback_hours"] = coerced_lookback
            profile["recency_weighting"] = _recency_from_lookback_hours(coerced_lookback)
    elif recency_weighting:
        profile["recency_weighting"] = recency_weighting

    if profile["source_selection"].get("gmail") and not prev_selection.get("gmail"):
        profile["gmail_rules"] = {}
        pending_field = ""
        session = {**session, "pending_field": None}
    elif not profile["source_selection"].get("gmail") and prev_selection.get("gmail"):
        if pending_field == GMAIL_SENDER_SELECTION_FIELD or pending_field == GMAIL_RULES_FIELD:
            pending_field = ""
            session = {**session, "pending_field": None}

    profile = _apply_models(profile, models)
    turn_count = int(session.get("turn_count") or 0)
    pending_field = session.get("pending_field") or ""

    yield {"type": "session", "session_id": session_id}

    # --- Gmail discovery intercept (early check) -----------------------------------
    gmail_enabled = _source_selection_dict(profile.get("source_selection")).get("gmail")
    gmail_rules = _normalize_gmail_rules(profile.get("gmail_rules"))
    has_candidates = bool(gmail_rules.get("candidates"))
    has_senders = bool(gmail_rules.get("include_senders"))
    has_intent = bool(gmail_rules.get("intent"))

    trigger_gmail_discovery = (
        gmail_enabled
        and not has_candidates
        and not has_senders
        and not has_intent
        and pending_field != GMAIL_SENDER_SELECTION_FIELD
    )

    if not just_go_now and trigger_gmail_discovery:
        discovery_query = clean_answer or profile.get("statement") or statement or ""
        gmail_candidates_event = await _astream_gmail_discovery(
            patched=profile,
            answer=discovery_query,
        )
        if gmail_candidates_event is not None:
            patched = _coerce_profile(gmail_candidates_event["_patched_profile"])
            gmail_candidates_event.pop("_patched_profile", None)

            intro = gmail_candidates_event.get("intro", "")
            messages.append({"role": "assistant", "content": intro})

            updated_gmail = database.update_refinement_session(
                session_id,
                profile=patched,
                messages=messages,
                pending_field=GMAIL_SENDER_SELECTION_FIELD,
                turn_count=turn_count,
                status="active",
                topic_id=session.get("topic_id"),
            )
            response_gmail = _session_response(updated_gmail) if updated_gmail else None
            if response_gmail is None:
                yield {"type": "error", "message": "Failed to persist Gmail discovery step"}
                return

            yield {"type": "token", "text": intro}
            yield {"type": "plan", "session": response_gmail}
            yield {**gmail_candidates_event, "type": "gmail_candidates"}
            yield {"type": "done", "session": response_gmail, "ready": False, "trigger_build": False}
            return

    # --- Gmail sender-approval intercept -------------------------------------------
    # When the previous turn left pending_field=GMAIL_SENDER_SELECTION_FIELD the user
    # is responding with their sender approval (numbers, names, "all", "none"). Handle
    # this without involving the AI.
    if (
        pending_field == GMAIL_SENDER_SELECTION_FIELD
        and clean_answer
        and not just_go_now
        and _is_gmail_approval_response(clean_answer, gmail_rules)
    ):
        async for event in _astream_gmail_approval(
            session_id=session_id,
            session=session,
            profile=profile,
            messages=messages,
            answer=clean_answer,
            turn_count=turn_count,
        ):
            yield event
        return

    client = _refinement_model_client(profile)
    if client is None:
        async for event in _astream_fallback(session_id, clean_answer, just_go_now, models):
            yield event
        return

    # Track the turn the model is responding to (the deterministic fallback re-appends
    # the user message itself, so we only mutate the in-memory copy here).
    if clean_answer and not just_go_now:
        messages.append({"role": "user", "content": clean_answer})
        turn_count += 1
    elif just_go_now:
        messages.append({"role": "user", "content": "Search strategy confirmed."})

    prompt = _build_refinement_chat_prompt(
        profile=profile,
        messages=messages,
        turn_count=turn_count,
        just_go_now=just_go_now,
    )
    queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

    async def _run() -> None:
        try:
            await client.complete_response(
                system=load_prompt("refinement_chat"),
                prompt=prompt,
                max_tokens=2200,
                on_token=lambda text: queue.put_nowait(("token", text)),
                json_mode=False,
            )
            queue.put_nowait(("end", None))
        except Exception as exc:  # pragma: no cover - network timing dependent
            logger.exception("Streaming refinement turn failed")
            queue.put_nowait(("error", str(exc)))

    task = asyncio.create_task(_run())
    full_text = ""
    error_message: str | None = None
    while True:
        kind, value = await queue.get()
        if kind == "error":
            error_message = value or "Model streaming failed"
            break
        if kind == "end":
            break
        full_text += value or ""
    await task

    if error_message is not None:
        async for event in _astream_fallback(session_id, clean_answer, just_go_now, models, prefix_error=True):
            yield event
        return

    final_visible, _ = _visible_prose(full_text, final=True)

    assistant_text = final_visible.strip()
    patch, ready_flag, intent = _parse_chat_payload(full_text)
    intent = _normalize_refinement_intent(intent)
    if just_go_now:
        intent = "build"
    ready = intent == "build"

    patched = _merge_agent_profile_patch(profile, patch, user_text=_user_authored_text(profile, messages))
    patched = _seed_profile_with_hints(patched)
    if not str(patched.get("scope") or "").strip():
        patched["scope"] = str(profile.get("statement") or "").strip()
    if clean_answer and not just_go_now:
        patched = _ensure_reply_updates_strategy(profile, patched, clean_answer)
    if assistant_text:
        assistant_text = _refinement_reply_with_required_question(
            assistant_text,
            profile=patched,
            messages=messages,
            just_go_now=just_go_now,
        )
        patched["reasoning_summary"] = assistant_text[:600]
        messages.append({"role": "assistant", "content": assistant_text})
        yield {"type": "token", "text": assistant_text}

    topic_id = session.get("topic_id")

    if intent in {"confirm_changes", "discard_changes"} and _pending_strategy_refinement(profile):
        persisted = database.update_refinement_session(
            session_id,
            profile=_coerce_profile(profile),
            messages=messages,
            pending_field=session.get("pending_field"),
            turn_count=turn_count,
            status="active",
            topic_id=topic_id,
        )
        if persisted is None:
            yield {"type": "error", "message": "Failed to persist strategy confirmation turn"}
            return
        try:
            confirmed = confirm_strategy_refinement(session_id, {"apply": intent == "confirm_changes"})
        except Exception as exc:
            logger.exception("Failed to resolve pending strategy proposal from chat intent")
            yield {"type": "error", "message": str(exc) or "Could not resolve pending strategy proposal"}
            return
        if confirmed is None:
            yield {"type": "error", "message": "Refinement session not found"}
            return
        yield {"type": "plan", "session": confirmed}
        yield {"type": "done", "session": confirmed, "ready": False, "trigger_build": False}
        return

    # --- Gmail discovery intercept -------------------------------------------------
    # When the AI has set gmail in source_selection but no include_senders are
    # configured yet, treat the user's answer as Gmail search instructions, run the
    # candidate discovery pipeline, and hand control to the sender-approval step.
    gmail_candidates_event: dict[str, Any] | None = None
    if (
        not just_go_now
        and not ready
        and clean_answer
        and _source_selection_dict(patched.get("source_selection")).get("gmail")
        and not _normalize_gmail_rules(patched.get("gmail_rules")).get("include_senders")
        and not _normalize_gmail_rules(patched.get("gmail_rules")).get("intent")
        and pending_field != GMAIL_SENDER_SELECTION_FIELD
    ):
        gmail_candidates_event = await _astream_gmail_discovery(
            patched=patched,
            answer=clean_answer,
        )
        if gmail_candidates_event is not None:
            # Update patched profile with the gmail_rules we just built.
            patched = _coerce_profile(gmail_candidates_event["_patched_profile"])
            gmail_candidates_event.pop("_patched_profile", None)
            # Persist with pending_field=GMAIL_SENDER_SELECTION_FIELD so next turn
            # knows it is an approval reply.
            updated_gmail = database.update_refinement_session(
                session_id,
                profile=patched,
                messages=messages,
                pending_field=GMAIL_SENDER_SELECTION_FIELD,
                turn_count=turn_count,
                status="active",
                topic_id=topic_id,
            )
            response_gmail = _session_response(updated_gmail) if updated_gmail else None
            if response_gmail is None:
                yield {"type": "error", "message": "Failed to persist Gmail discovery step"}
                return
            yield {"type": "plan", "session": response_gmail}
            yield {**gmail_candidates_event, "type": "gmail_candidates"}
            yield {"type": "done", "session": response_gmail, "ready": False, "trigger_build": False}
            return

    if ready:
        patched = _fill_defaults(patched)
        pre_critique = _diagnostics_query_snapshot(patched)
        patched = _critique_search_plan(patched)
        patched["refinement_diagnostics"] = _enrich_diagnostics(
            patched,
            model_profile_patch=patch,
            pre_critique=pre_critique,
            readiness_reason=_readiness_reason(
                ready_requested=bool(ready_flag),
                just_go_now=just_go_now,
                turn_count=turn_count,
            ),
        )
        patched = _coerce_profile(patched)
        saved = explore.save_topic_profile(patched)
        topic_id = str(saved["topic_id"])
        status = "finalized"
        pending = None
    else:
        if not assistant_text:
            fallback_question = _strategy_deepening_question(patched, messages)
            messages.append({"role": "assistant", "content": fallback_question})
            yield {"type": "token", "text": fallback_question}
        patched = _coerce_profile(patched)
        status = "active"
        pending = AGENT_PENDING_FIELD

    updated = database.update_refinement_session(
        session_id,
        profile=patched,
        messages=messages,
        pending_field=pending,
        turn_count=turn_count,
        status=status,
        topic_id=topic_id,
    )
    response = _session_response(updated) if updated else None
    if response is None:
        yield {"type": "error", "message": "Failed to persist refinement session"}
        return
    yield {"type": "plan", "session": response}
    yield {"type": "done", "session": response, "ready": ready, "trigger_build": ready}


async def _astream_gmail_discovery(
    *,
    patched: dict[str, Any],
    answer: str,
) -> dict[str, Any] | None:
    """Run Gmail candidate discovery from the user's search-instruction answer.

    Returns a dict to merge into the gmail_candidates SSE event (plus a
    ``_patched_profile`` key with the updated profile), or None if discovery
    produced nothing useful.
    """
    try:
        rules = await asyncio.to_thread(_gmail_rules_from_answer, answer, patched)
        candidates = await asyncio.to_thread(_discover_gmail_candidates, rules, patched)
    except Exception:
        logger.exception("Gmail discovery failed in streaming path")
        return None

    notes = rules.pop("_ai_candidate_notes", {})
    candidate_dicts = [
        {
            **candidate.to_dict(),
            **({"ai_rationale": notes.get(candidate.sender.lower())} if notes.get(candidate.sender.lower()) else {}),
        }
        for candidate in candidates
    ]
    if candidates:
        gmail_allowlist.record_candidates(candidate_dicts, source="refinement")

    rules["candidates"] = candidate_dicts
    updated_profile = dict(patched)
    updated_profile["gmail_rules"] = rules
    updated_profile["source_queries"] = _merge_source_queries(
        updated_profile.get("source_queries"), {"gmail": [rules["gmail_search_query"]]}
    )
    updated_profile["lookback_hours"] = rules["lookback_hours"]
    updated_profile["recency_weighting"] = _recency_from_lookback_hours(int(rules["lookback_hours"]))
    updated_profile["source_scope_answered"] = True

    if not candidates:
        return {
            "_patched_profile": updated_profile,
            "candidates": [],
            "intro": (
                "I searched Gmail for that newsletter pattern but didn't find clear newsletter senders. "
                "Name any sender or newsletter you want included, or say 'none' to continue without Gmail."
            ),
            "criteria": str(rules.get("selection_criteria") or ""),
            "search_phrase": str(rules.get("gmail_search_query") or answer),
            "lookback_hours": rules["lookback_hours"],
        }

    return {
        "_patched_profile": updated_profile,
        "candidates": candidate_dicts,
        "intro": (
            f"I searched Gmail for '{rules['gmail_search_query']}' and found these newsletter senders. "
            "Which ones should I approve? Only approved senders are ever read into your brief."
        ),
        "criteria": str(rules.get("selection_criteria") or ""),
        "search_phrase": str(rules.get("gmail_search_query") or answer),
        "lookback_hours": rules["lookback_hours"],
    }


async def _astream_gmail_approval(
    *,
    session_id: str,
    session: dict[str, Any],
    profile: dict[str, Any],
    messages: list[dict[str, str]],
    answer: str,
    turn_count: int,
):
    """Handle the sender-approval reply after Gmail candidates were shown.

    Yields ``token`` (confirmation prose), ``gmail_approved`` (approved senders),
    ``plan`` (session snapshot), and ``done``.
    """
    rules = _normalize_gmail_rules(profile.get("gmail_rules"))
    intent_match = re.search(r"Instructions:\s*(.*)", answer, re.DOTALL | re.IGNORECASE)
    if intent_match:
        extraction_instructions = intent_match.group(1).strip()
        sender_part = answer[:intent_match.start()].strip()
    else:
        extraction_instructions = ""
        sender_part = answer
    include_senders = _selected_gmail_senders(sender_part, rules)
    messages = [*messages, {"role": "user", "content": answer}]

    updated_profile = dict(profile)
    if include_senders:
        approved = gmail_allowlist.approve_senders(include_senders, source="refinement")
        rules["include_senders"] = approved or include_senders
        if extraction_instructions:
            rules["intent"] = extraction_instructions
        updated_profile["requested_sources"] = _merge_requested_source_lists(
            _normalize_requested_sources(updated_profile.get("requested_sources")),
            [{"adapter": "gmail", "ref": sender} for sender in include_senders],
        )
        updated_profile["requested_sources_answered"] = True
        updated_profile["gmail_rules"] = rules
        confirmation = (
            f"Added {', '.join(include_senders)} to your Gmail allowlist. "
            "Their newsletters will be read as discovery feeds — the articles they link to feed directly into this brief."
        )
    else:
        rules["include_senders"] = []
        updated_profile["gmail_rules"] = rules
        updated_profile["source_selection"] = {
            **_source_selection_dict(updated_profile.get("source_selection")),
            "gmail": False,
        }
        confirmation = "Got it — I'll continue without Gmail for this brief."

    messages.append({"role": "assistant", "content": confirmation})

    next_pending = AGENT_PENDING_FIELD if include_senders else None
    ready = not include_senders
    follow_up = ""
    if include_senders:
        missing = _next_missing(updated_profile)
        follow_up = (
            _deterministic_question(missing, updated_profile)
            if missing
            else _strategy_deepening_question(updated_profile, messages)
        )
        if follow_up:
            messages.append({"role": "assistant", "content": follow_up})

    yield {"type": "token", "text": confirmation}
    if follow_up:
        yield {"type": "token", "text": f"\n\n{follow_up}"}

    updated = database.update_refinement_session(
        session_id,
        profile=_coerce_profile(updated_profile),
        messages=messages,
        pending_field=next_pending,
        turn_count=turn_count + 1,
        status="finalized" if ready else "active",
        topic_id=session.get("topic_id"),
    )
    response = _session_response(updated) if updated else None
    if response is None:
        yield {"type": "error", "message": "Failed to persist Gmail approval"}
        return
    yield {"type": "gmail_approved", "senders": include_senders}
    yield {"type": "plan", "session": response}
    yield {"type": "done", "session": response, "ready": ready, "trigger_build": ready}


async def _astream_fallback(
    session_id: str,
    answer: str,
    just_go_now: bool,
    models: Any,
    *,
    prefix_error: bool = False,
):
    """Stream a single deterministic turn when live model streaming is unavailable."""
    if prefix_error:
        yield {"type": "token", "text": "Live streaming was unavailable, so I finished this step without it.\n\n"}
    result = await asyncio.to_thread(
        advance_session,
        session_id,
        {"answer": answer, "just_go_now": just_go_now, "models": _models(models)},
    )
    if result is None:
        yield {"type": "error", "message": "Refinement session not found"}
        return
    messages = result.get("messages") or []
    assistant = next(
        (str(message.get("content") or "") for message in reversed(messages) if message.get("role") == "assistant"),
        "",
    )
    if assistant:
        yield {"type": "token", "text": assistant}
    yield {"type": "plan", "session": result}
    ready = result.get("status") == "finalized"
    yield {"type": "done", "session": result, "ready": ready, "trigger_build": bool(just_go_now and ready)}


async def astream_refine_strategy(
    *,
    session_id: str,
    instruction: str,
    models: Any,
):
    """Stream the strategy-refinement conversation turn (the 'Refine search strategy' modal).

    Yields: ``token`` (prose delta), ``proposal`` (session snapshot with pending_strategy_refinement),
    ``done``, and ``error``. The prose streams live; after it finishes the critique/preview pass runs
    (one additional non-streaming call) before ``proposal`` is emitted.
    Falls back to the sync ``refine_strategy`` engine if streaming is unavailable.
    """
    session = database.get_refinement_session(session_id)
    if session is None:
        yield {"type": "error", "message": "Refinement session not found"}
        return

    profile = _apply_models(dict(session["profile"]), models)
    current_pending = _pending_strategy_refinement(profile)
    proposal_base = current_pending.get("proposed_profile") if current_pending else None
    if isinstance(proposal_base, dict):
        proposal_base = {**proposal_base, "models": profile.get("models")}
    else:
        proposal_base = profile
    prior_context = _pending_strategy_context(current_pending)
    effective_instruction = f"{prior_context}\n\nUser follow-up: {instruction}" if prior_context else instruction

    client = _refinement_model_client(profile)
    if client is None:
        async for event in _astream_strategy_fallback(session_id, effective_instruction, models):
            yield event
        return

    prompt = _build_strategy_refinement_prompt(
        profile=proposal_base,
        instruction=effective_instruction,
        task="Revise the current search strategy. Reply conversationally, then emit the json plan block.",
    )

    queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

    async def _run() -> None:
        try:
            await client.complete_response(
                system=load_prompt("strategy_refinement_chat"),
                prompt=prompt,
                max_tokens=1800,
                on_token=lambda text: queue.put_nowait(("token", text)),
                json_mode=False,
            )
            queue.put_nowait(("end", None))
        except Exception as exc:
            logger.exception("Strategy streaming turn failed")
            queue.put_nowait(("error", str(exc)))

    task = asyncio.create_task(_run())
    full_text = ""
    emitted = 0
    error_message: str | None = None
    while True:
        kind, value = await queue.get()
        if kind == "error":
            error_message = value or "Model streaming failed"
            break
        if kind == "end":
            break
        full_text += value or ""
        visible, _ = _visible_prose(full_text)
        new = visible[emitted:]
        if new:
            emitted += len(new)
            yield {"type": "token", "text": new}
    await task

    if error_message is not None:
        async for event in _astream_strategy_fallback(session_id, effective_instruction, models, prefix_error=True):
            yield event
        return

    final_visible, _ = _visible_prose(full_text, final=True)
    if len(final_visible) > emitted:
        tail = final_visible[emitted:]
        if tail.strip():
            yield {"type": "token", "text": tail}

    prose = final_visible.strip()
    patch_dict, _, _ = _parse_chat_payload(full_text)
    raw_block = _extract_json_block(full_text)
    requires_changes = bool((raw_block or {}).get("requires_changes", True))
    findings = _string_list((raw_block or {}).get("findings"), limit=8)

    if not requires_changes or not patch_dict:
        assistant_response = prose or "Strategy looks good — no changes needed."
        messages = [
            *_messages(session),
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": assistant_response},
        ]
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            "status": "passed",
            "assistant_response": assistant_response,
            "findings": findings,
        }
        updated = database.update_refinement_session(
            session_id,
            profile=profile,
            messages=messages,
            pending_field=None,
            turn_count=int(session.get("turn_count") or 0),
            status=session.get("status") or "finalized",
            topic_id=session.get("topic_id"),
        )
        result = _session_response(updated) if updated else None
        if result:
            yield {"type": "proposal", "session": result}
            yield {"type": "done", "session": result, "has_proposal": False}
        return

    agent_update = {
        "profile_patch": patch_dict,
        "requires_changes": True,
        "findings": findings,
        "assistant_response": prose,
        "reasoning_summary": prose[:300] if prose else "",
    }
    pending = _build_pending_strategy_refinement(
        proposal_base,
        instruction=effective_instruction,
        agent_update=agent_update,
        readiness_reason="strategy_refinement_proposed",
        review_mode=None,
    )
    pending["conversation"] = [
        *[item for item in current_pending.get("conversation", []) if isinstance(item, dict)],
        {"role": "user", "content": instruction},
        {"role": "assistant", "content": str(pending.get("assistant_response") or prose or "")},
    ]
    profile[PENDING_STRATEGY_PROFILE_KEY] = pending
    messages = [
        *_messages(session),
        {"role": "user", "content": instruction},
        {"role": "assistant", "content": str(pending.get("assistant_response") or "")},
    ]
    updated = database.update_refinement_session(
        session_id,
        profile=profile,
        messages=messages,
        pending_field=PENDING_STRATEGY_FIELD,
        turn_count=int(session.get("turn_count") or 0),
        status=session.get("status") or "finalized",
        topic_id=session.get("topic_id"),
    )
    result = _session_response(updated) if updated else None
    if result is None:
        yield {"type": "error", "message": "Failed to persist strategy refinement"}
        return
    yield {"type": "proposal", "session": result}
    yield {"type": "done", "session": result, "has_proposal": True}


async def astream_review_strategy(
    *,
    session_id: str,
    profile_payload: dict[str, Any] | None,
    models: Any,
):
    """Stream the pre-build strategy quality review.

    Yields: ``token`` (prose delta), ``proposal`` (session snapshot), ``done``, ``error``.
    Falls back to sync ``review_strategy`` if streaming unavailable.
    """
    session = database.get_refinement_session(session_id)
    if session is None:
        yield {"type": "error", "message": "Refinement session not found"}
        return

    profile = dict(session["profile"])
    if _pending_strategy_refinement(profile):
        result = _session_response(session)
        yield {"type": "proposal", "session": result}
        yield {"type": "done", "session": result, "has_proposal": bool(result.get("pending_strategy_refinement"))}
        return

    review_profile = _profile_for_strategy_review(
        profile, profile_payload, models=models
    )
    current_fingerprint = _strategy_fingerprint(review_profile)
    existing_review = profile.get(STRATEGY_REVIEW_PROFILE_KEY)
    if (
        isinstance(existing_review, dict)
        and existing_review.get("fingerprint") == current_fingerprint
        and str(existing_review.get("status") or "") in STRATEGY_REVIEW_RESOLVED_STATUSES
    ):
        result = _session_response(session)
        yield {"type": "proposal", "session": result}
        yield {"type": "done", "session": result, "has_proposal": False}
        return

    instruction = _pre_build_strategy_review_instruction(review_profile)
    client = _refinement_model_client(review_profile)
    if client is None:
        result = await asyncio.to_thread(
            review_strategy, session_id, {"profile": profile_payload or {}, "models": _models(models)}
        )
        if result is None:
            yield {"type": "error", "message": "Pre-build review session not found"}
            return
        yield {"type": "proposal", "session": result}
        yield {"type": "done", "session": result, "has_proposal": bool(result.get("pending_strategy_refinement"))}
        return

    prompt = _build_strategy_refinement_prompt(
        profile=review_profile,
        instruction=instruction,
        task="Review the current search strategy immediately before the brief build.",
    )
    queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

    async def _run_review() -> None:
        try:
            await client.complete_response(
                system=load_prompt("strategy_refinement_chat"),
                prompt=prompt,
                max_tokens=1200,
                on_token=lambda text: queue.put_nowait(("token", text)),
                json_mode=False,
            )
            queue.put_nowait(("end", None))
        except Exception as exc:
            logger.exception("Pre-build strategy review streaming failed")
            queue.put_nowait(("error", str(exc)))

    task = asyncio.create_task(_run_review())
    full_text = ""
    emitted = 0
    error_message: str | None = None
    while True:
        kind, value = await queue.get()
        if kind == "error":
            error_message = value or "Model streaming failed"
            break
        if kind == "end":
            break
        full_text += value or ""
        visible, _ = _visible_prose(full_text)
        new = visible[emitted:]
        if new:
            emitted += len(new)
            yield {"type": "token", "text": new}
    await task

    if error_message is not None:
        result = await asyncio.to_thread(
            review_strategy, session_id, {"profile": profile_payload or {}, "models": _models(models)}
        )
        if result is None:
            yield {"type": "error", "message": "Pre-build review session not found"}
            return
        yield {"type": "proposal", "session": result}
        yield {"type": "done", "session": result, "has_proposal": bool(result.get("pending_strategy_refinement"))}
        return

    final_visible, _ = _visible_prose(full_text, final=True)
    if len(final_visible) > emitted:
        tail = final_visible[emitted:]
        if tail.strip():
            yield {"type": "token", "text": tail}

    prose = final_visible.strip()
    patch_dict, _, _ = _parse_chat_payload(full_text)
    raw_block = _extract_json_block(full_text)
    requires_changes = bool((raw_block or {}).get("requires_changes", False))
    reviewed_at = datetime.now(UTC).isoformat(timespec="seconds")

    if not requires_changes or not patch_dict:
        assistant_response = prose or "Strategy looks good for this build."
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            "status": "passed",
            "assistant_response": assistant_response,
            "findings": _string_list((raw_block or {}).get("findings"), limit=8),
            "reviewed_at": reviewed_at,
            "fingerprint": current_fingerprint,
        }
        messages = _messages(session)
        messages.append({"role": "assistant", "content": assistant_response})
        updated = database.update_refinement_session(
            session_id,
            profile=profile,
            messages=messages,
            pending_field=None,
            turn_count=int(session.get("turn_count") or 0),
            status=session.get("status") or "finalized",
            topic_id=session.get("topic_id"),
        )
        result = _session_response(updated) if updated else None
        if result:
            yield {"type": "proposal", "session": result}
            yield {"type": "done", "session": result, "has_proposal": False}
        return

    agent_update = {
        "profile_patch": patch_dict,
        "requires_changes": True,
        "findings": _string_list((raw_block or {}).get("findings"), limit=8),
        "assistant_response": prose,
        "reasoning_summary": prose[:300] if prose else "",
    }
    pending = _build_pending_strategy_refinement(
        review_profile,
        instruction=instruction,
        agent_update=agent_update,
        readiness_reason="pre_build_strategy_review_proposed",
        review_mode="pre_build_review",
    )
    proposed_fingerprint = str(pending.get("proposal_fingerprint") or "")
    if not proposed_fingerprint or proposed_fingerprint == current_fingerprint or _proposal_was_resolved(profile, proposed_fingerprint):
        assistant_response = "Strategy quality check resolved for this plan; building can continue."
        profile.pop(PENDING_STRATEGY_PROFILE_KEY, None)
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            "status": "suppressed",
            "assistant_response": assistant_response,
            "findings": pending.get("findings", []),
            "reviewed_at": reviewed_at,
            "fingerprint": current_fingerprint,
        }
        messages = _messages(session)
        messages.append({"role": "assistant", "content": assistant_response})
        updated = database.update_refinement_session(
            session_id,
            profile=profile,
            messages=messages,
            pending_field=None,
            turn_count=int(session.get("turn_count") or 0),
            status=session.get("status") or "finalized",
            topic_id=session.get("topic_id"),
        )
        result = _session_response(updated) if updated else None
        if result:
            yield {"type": "proposal", "session": result}
            yield {"type": "done", "session": result, "has_proposal": False}
        return

    profile[STRATEGY_REVIEW_PROFILE_KEY] = {
        "status": "proposed",
        "assistant_response": pending["assistant_response"],
        "findings": pending.get("findings", []),
        "reviewed_at": reviewed_at,
        "fingerprint": current_fingerprint,
        "proposal_fingerprint": proposed_fingerprint,
    }
    profile[PENDING_STRATEGY_PROFILE_KEY] = pending
    messages = _messages(session)
    messages.append({"role": "assistant", "content": pending["assistant_response"]})
    updated = database.update_refinement_session(
        session_id,
        profile=profile,
        messages=messages,
        pending_field=PENDING_STRATEGY_FIELD,
        turn_count=int(session.get("turn_count") or 0),
        status=session.get("status") or "finalized",
        topic_id=session.get("topic_id"),
    )
    result = _session_response(updated) if updated else None
    if result is None:
        yield {"type": "error", "message": "Failed to persist pre-build review"}
        return
    yield {"type": "proposal", "session": result}
    yield {"type": "done", "session": result, "has_proposal": True}


async def _astream_strategy_fallback(
    session_id: str,
    instruction: str,
    models: Any,
    *,
    prefix_error: bool = False,
):
    """Sync fallback for strategy streaming when the model client is unavailable."""
    if prefix_error:
        yield {"type": "token", "text": "Live streaming was unavailable for this update.\n\n"}
    result = await asyncio.to_thread(
        refine_strategy,
        session_id,
        {"instruction": instruction, "models": _models(models)},
    )
    if result is None:
        yield {"type": "error", "message": "Refinement session not found"}
        return
    messages = result.get("messages") or []
    assistant = next(
        (str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "assistant"),
        "",
    )
    if assistant:
        yield {"type": "token", "text": assistant}
    yield {"type": "proposal", "session": result}
    yield {"type": "done", "session": result, "has_proposal": bool(result.get("pending_strategy_refinement"))}


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


def _build_refinement_chat_prompt(
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
            "task": "Lead the brief-setup chat. Reply conversationally to the user, then emit the json plan block.",
            "turn_count": turn_count,
            "just_go_now": just_go_now,
            "is_first_turn": compact_messages == [],
            "current_profile": profile_snapshot,
            "conversation": compact_messages,
            "source_guidance": {
                "web_search": "Precise web queries with location/time/source words, aliases, concrete intent.",
                "foreign_media": "When selected, propose any non-English language the topic warrants with idiomatic native-language queries.",
                "youtube": "Creator/video search phrases for walkthroughs, explainers, interviews, demos.",
                "podcasts": "Show/interview/topic phrases likely to find playable audio.",
                "collections": "Terms likely to appear in local documents.",
                "markets": "Exchange ticker symbols only (e.g. 'NVDA', '000660.KS'); resolve company names to tickers, one per entry.",
            },
            "already_inferred": _inferred_constraints(profile_snapshot),
            "current_date_hint": f"Today is {current_utc} (UTC). Use this when judging freshness windows.",
        },
        ensure_ascii=False,
    )


def refine_strategy(session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    session = database.get_refinement_session(session_id)
    if session is None:
        return None
    instruction = " ".join(str(payload.get("instruction") or "").split()).strip()
    if not instruction:
        raise ValueError("Strategy refinement instruction is required")
    profile = dict(session["profile"])
    profile = _apply_models(profile, payload.get("models"))
    current_pending = _pending_strategy_refinement(profile)
    proposal_base = current_pending.get("proposed_profile") if current_pending else None
    if isinstance(proposal_base, dict):
        proposal_base = {**proposal_base, "models": profile.get("models")}
    else:
        proposal_base = profile
    prior_context = _pending_strategy_context(current_pending)
    effective_instruction = f"{prior_context}\n\nUser follow-up: {instruction}" if prior_context else instruction
    agent_update = _run_strategy_refinement_agent(profile=proposal_base, instruction=effective_instruction)
    if not isinstance(agent_update, dict):
        raise ValueError("AI strategy refinement is unavailable; no changes were applied.")
    model_patch = agent_update.get("profile_patch") if isinstance(agent_update, dict) else None
    if not isinstance(model_patch, dict) or not model_patch:
        raise ValueError("AI strategy refinement did not return a usable proposal; no changes were applied.")
    pending = _build_pending_strategy_refinement(
        proposal_base,
        instruction=effective_instruction,
        agent_update=agent_update,
        readiness_reason="strategy_refinement_proposed",
        review_mode=None,
    )
    pending["conversation"] = [
        *[item for item in current_pending.get("conversation", []) if isinstance(item, dict)],
        {"role": "user", "content": instruction},
        {"role": "assistant", "content": str(pending.get("assistant_response") or "")},
    ]
    profile[PENDING_STRATEGY_PROFILE_KEY] = pending
    messages = [
        *_messages(session),
        {"role": "user", "content": instruction},
        {"role": "assistant", "content": str(pending.get("assistant_response") or "")},
    ]
    updated = database.update_refinement_session(
        session_id,
        profile=profile,
        messages=messages,
        pending_field=PENDING_STRATEGY_FIELD,
        turn_count=int(session.get("turn_count") or 0),
        status=session.get("status") or "finalized",
        topic_id=session.get("topic_id"),
    )
    return _session_response(updated) if updated else None


def review_strategy(session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    session = database.get_refinement_session(session_id)
    if session is None:
        return None
    profile = dict(session["profile"])
    if _pending_strategy_refinement(profile):
        return _session_response(session)
    review_profile = _profile_for_strategy_review(
        profile,
        payload.get("profile"),
        models=payload.get("models"),
    )
    current_fingerprint = _strategy_fingerprint(review_profile)
    existing_review = profile.get(STRATEGY_REVIEW_PROFILE_KEY)
    if (
        isinstance(existing_review, dict)
        and existing_review.get("fingerprint") == current_fingerprint
        and str(existing_review.get("status") or "") in STRATEGY_REVIEW_RESOLVED_STATUSES
    ):
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            **existing_review,
            "status": str(existing_review.get("status") or "passed"),
            "assistant_response": str(
                existing_review.get("assistant_response")
                or "Strategy was already reviewed for this version and is ready to build."
            ),
            "fingerprint": current_fingerprint,
        }
        updated = database.update_refinement_session(
            session_id,
            profile=profile,
            messages=_messages(session),
            pending_field=None,
            turn_count=int(session.get("turn_count") or 0),
            status=session.get("status") or "finalized",
            topic_id=session.get("topic_id"),
        )
        return _session_response(updated) if updated else None

    instruction = _pre_build_strategy_review_instruction(review_profile)
    agent_update = _run_strategy_refinement_agent(
        profile=review_profile,
        instruction=instruction,
        task="Review the current search strategy immediately before the brief build.",
    )
    messages = _messages(session)
    reviewed_at = datetime.now(UTC).isoformat(timespec="seconds")
    if not isinstance(agent_update, dict):
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            "status": "unavailable",
            "assistant_response": "AI strategy review was unavailable, so no strategy changes were proposed.",
            "findings": [],
            "reviewed_at": reviewed_at,
            "fingerprint": current_fingerprint,
        }
        updated = database.update_refinement_session(
            session_id,
            profile=profile,
            messages=messages,
            pending_field=session.get("pending_field"),
            turn_count=int(session.get("turn_count") or 0),
            status=session.get("status") or "finalized",
            topic_id=session.get("topic_id"),
        )
        return _session_response(updated) if updated else None

    model_patch = agent_update.get("profile_patch")
    requires_changes = bool(agent_update.get("requires_changes")) or (isinstance(model_patch, dict) and bool(model_patch))
    if not requires_changes or not isinstance(model_patch, dict) or not model_patch:
        assistant_response = str(agent_update.get("assistant_response") or "").strip()
        if not assistant_response:
            assistant_response = "AI reviewed the strategy against the current date, source mix, and recency window; no changes are needed."
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            "status": "passed",
            "assistant_response": assistant_response,
            "findings": _string_list(agent_update.get("findings"), limit=8),
            "reviewed_at": reviewed_at,
            "fingerprint": current_fingerprint,
        }
        messages.append({"role": "assistant", "content": assistant_response})
        updated = database.update_refinement_session(
            session_id,
            profile=profile,
            messages=messages,
            pending_field=None,
            turn_count=int(session.get("turn_count") or 0),
            status=session.get("status") or "finalized",
            topic_id=session.get("topic_id"),
        )
        return _session_response(updated) if updated else None

    pending = _build_pending_strategy_refinement(
        review_profile,
        instruction=instruction,
        agent_update=agent_update,
        readiness_reason="pre_build_strategy_review_proposed",
        review_mode="pre_build_review",
    )
    proposed_fingerprint = str(pending.get("proposal_fingerprint") or "")
    if not proposed_fingerprint or proposed_fingerprint == current_fingerprint or _proposal_was_resolved(profile, proposed_fingerprint):
        assistant_response = "Strategy quality check is already resolved for this plan; building can continue."
        profile.pop(PENDING_STRATEGY_PROFILE_KEY, None)
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            "status": "suppressed",
            "assistant_response": assistant_response,
            "findings": pending.get("findings", []),
            "reviewed_at": reviewed_at,
            "fingerprint": current_fingerprint,
            "suppressed_proposal_fingerprint": proposed_fingerprint,
        }
        messages.append({"role": "assistant", "content": assistant_response})
        updated = database.update_refinement_session(
            session_id,
            profile=profile,
            messages=messages,
            pending_field=None,
            turn_count=int(session.get("turn_count") or 0),
            status=session.get("status") or "finalized",
            topic_id=session.get("topic_id"),
        )
        return _session_response(updated) if updated else None
    profile[STRATEGY_REVIEW_PROFILE_KEY] = {
        "status": "proposed",
        "assistant_response": pending["assistant_response"],
        "findings": pending.get("findings", []),
        "reviewed_at": reviewed_at,
        "fingerprint": current_fingerprint,
        "proposal_fingerprint": proposed_fingerprint,
    }
    profile[PENDING_STRATEGY_PROFILE_KEY] = pending
    messages.append({"role": "assistant", "content": pending["assistant_response"]})
    updated = database.update_refinement_session(
        session_id,
        profile=profile,
        messages=messages,
        pending_field=PENDING_STRATEGY_FIELD,
        turn_count=int(session.get("turn_count") or 0),
        status=session.get("status") or "finalized",
        topic_id=session.get("topic_id"),
    )
    return _session_response(updated) if updated else None


def _build_pending_strategy_refinement(
    profile: dict[str, Any],
    *,
    instruction: str,
    agent_update: dict[str, Any],
    readiness_reason: str,
    review_mode: str | None,
) -> dict[str, Any]:
    model_patch = agent_update.get("profile_patch") if isinstance(agent_update, dict) else None
    if not isinstance(model_patch, dict):
        model_patch = {}
    proposed_profile = _merge_agent_profile_patch(profile, model_patch, user_text=instruction)
    reasoning_summary = str((agent_update or {}).get("reasoning_summary") or "").strip()
    if reasoning_summary:
        proposed_profile["reasoning_summary"] = reasoning_summary
    proposed_profile = _fill_defaults(proposed_profile)
    pre_critique = _diagnostics_query_snapshot(proposed_profile)
    proposed_profile = _critique_search_plan(proposed_profile)
    proposed_profile["refinement_diagnostics"] = _enrich_diagnostics(
        proposed_profile,
        model_profile_patch=model_patch,
        pre_critique=pre_critique,
        readiness_reason=readiness_reason,
    )
    assistant_response = str(agent_update.get("assistant_response") or reasoning_summary or "").strip()
    if not assistant_response:
        assistant_response = _strategy_proposal_summary(model_patch)
    pending: dict[str, Any] = {
        "instruction": instruction,
        "assistant_response": assistant_response,
        "reasoning_summary": reasoning_summary,
        "profile_patch": _trim_for_diagnostics(model_patch),
        "proposed_profile": proposed_profile,
        "strategy_preview": _strategy_preview(proposed_profile),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "findings": _string_list(agent_update.get("findings"), limit=8),
        "base_fingerprint": _strategy_fingerprint(profile),
        "proposal_fingerprint": _strategy_fingerprint(proposed_profile),
    }
    if review_mode:
        pending["review_mode"] = review_mode
    return pending


def _pending_strategy_refinement(profile: dict[str, Any]) -> dict[str, Any]:
    pending = profile.get(PENDING_STRATEGY_PROFILE_KEY)
    return dict(pending) if isinstance(pending, dict) else {}


def _pending_strategy_context(pending: dict[str, Any]) -> str:
    if not pending:
        return ""
    pieces: list[str] = []
    assistant_response = str(pending.get("assistant_response") or "").strip()
    if assistant_response:
        pieces.append(f"Existing proposed update: {assistant_response}")
    findings = _string_list(pending.get("findings"), limit=6)
    if findings:
        pieces.append("Existing findings: " + "; ".join(findings))
    prior_instruction = str(pending.get("instruction") or "").strip()
    if prior_instruction:
        pieces.append(f"Previous instruction/context: {prior_instruction}")
    if pieces:
        pieces.append("Revise the existing proposal rather than starting a separate proposal.")
    return "\n".join(pieces)


def _strategy_proposal_summary(patch: dict[str, Any]) -> str:
    changed: list[str] = []
    if patch.get("search_queries"):
        changed.append("general searches")
    source_queries = patch.get("source_queries")
    if isinstance(source_queries, dict):
        changed.extend(format_source for source in source_queries.keys() if (format_source := _SOURCE_DISPLAY.get(str(source), str(source))))
    if patch.get("lookback_hours") or patch.get("recency_weighting"):
        changed.append("recency window")
    if patch.get("exclusions"):
        changed.append("exclusions")
    if changed:
        return f"I prepared a proposed update covering {', '.join(changed[:5])}. Review it before applying."
    return "I prepared a proposed search strategy update. Review it before applying."


def confirm_strategy_refinement(session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    session = database.get_refinement_session(session_id)
    if session is None:
        return None
    profile = dict(session["profile"])
    pending = _pending_strategy_refinement(profile)
    if not pending:
        raise ValueError("There is no pending strategy refinement to confirm.")
    apply_change = bool(payload.get("apply", True))
    messages = _messages(session)
    topic_id = session.get("topic_id")
    reviewed_at = datetime.now(UTC).isoformat(timespec="seconds")
    base_fingerprint = str(pending.get("base_fingerprint") or _strategy_fingerprint(profile))
    proposal_fingerprint = str(pending.get("proposal_fingerprint") or "")
    if apply_change:
        proposed = pending.get("proposed_profile")
        if not isinstance(proposed, dict):
            raise ValueError("Pending strategy refinement is invalid.")
        profile = dict(proposed)
        profile.pop(PENDING_STRATEGY_PROFILE_KEY, None)
        profile.pop(STRATEGY_REVIEW_PROFILE_KEY, None)
        profile = _fill_defaults(profile)
        proposal_fingerprint = _strategy_fingerprint(profile)
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            "status": "applied",
            "assistant_response": "Applied the proposed search strategy changes.",
            "findings": _string_list(pending.get("findings"), limit=8),
            "reviewed_at": reviewed_at,
            "fingerprint": proposal_fingerprint,
            "base_fingerprint": base_fingerprint,
            "proposal_fingerprint": proposal_fingerprint,
        }
        saved = explore.save_topic_profile(profile)
        topic_id = str(saved["topic_id"])
        messages.append({"role": "assistant", "content": "Applied the proposed search strategy changes."})
    else:
        profile.pop(PENDING_STRATEGY_PROFILE_KEY, None)
        current_fingerprint = _strategy_fingerprint(profile)
        profile[STRATEGY_REVIEW_PROFILE_KEY] = {
            "status": "discarded",
            "assistant_response": "Discarded the proposed search strategy changes.",
            "findings": _string_list(pending.get("findings"), limit=8),
            "reviewed_at": reviewed_at,
            "fingerprint": current_fingerprint,
            "base_fingerprint": base_fingerprint,
            "proposal_fingerprint": proposal_fingerprint,
        }
        messages.append({"role": "assistant", "content": "Discarded the proposed search strategy changes."})
    updated = database.update_refinement_session(
        session_id,
        profile=profile,
        messages=messages,
        pending_field=None,
        turn_count=int(session.get("turn_count") or 0),
        status="finalized",
        topic_id=topic_id,
    )
    return _session_response(updated) if updated else None


def _profile_for_strategy_review(base_profile: dict[str, Any], payload_profile: Any, *, models: Any) -> dict[str, Any]:
    merged = dict(base_profile)
    if isinstance(payload_profile, dict):
        for key in (
            "topic_id",
            "statement",
            "scope",
            "subtopics",
            "keywords",
            "search_queries",
            "source_queries",
            "foreign_language_plan",
            "foreign_regions",
            "requested_sources",
            *PODCAST_STRATEGY_FIELDS,
            "depth",
            "recency_weighting",
            "lookback_hours",
            "exclusions",
            "source_selection",
            "gmail_rules",
            "schedule",
            "schedule_config",
            "delivery_config",
            "content_limits",
            "pipeline_limits",
        ):
            if key in payload_profile:
                merged[key] = payload_profile.get(key)
    return _apply_models(_coerce_profile(merged), models)


def _proposal_was_resolved(profile: dict[str, Any], proposal_fingerprint: str) -> bool:
    if not proposal_fingerprint:
        return False
    review = profile.get(STRATEGY_REVIEW_PROFILE_KEY)
    if not isinstance(review, dict):
        return False
    if str(review.get("proposal_fingerprint") or "") != proposal_fingerprint:
        return False
    return str(review.get("status") or "") in {"applied", "discarded", "suppressed"}


def _strategy_fingerprint(profile: dict[str, Any]) -> str:
    comparable = {
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "subtopics": _string_list(profile.get("subtopics"), limit=24),
        "keywords": _string_list(profile.get("keywords"), limit=24),
        "search_queries": _string_list(profile.get("search_queries"), limit=20),
        "source_queries": _clean_source_queries(profile.get("source_queries")),
        "foreign_language_plan": _normalize_foreign_language_plan(profile.get("foreign_language_plan")),
        "foreign_regions": _string_list(profile.get("foreign_regions"), limit=16),
        "requested_sources": _normalize_requested_sources(profile.get("requested_sources")),
        "direct_episode_queries": _string_list(profile.get("direct_episode_queries"), limit=16),
        "related_episode_queries": _string_list(profile.get("related_episode_queries"), limit=16),
        "negative_constraints": _string_list(profile.get("negative_constraints"), limit=16),
        "priority_terms": _string_list(profile.get("priority_terms"), limit=16),
        "depth": str(profile.get("depth") or ""),
        "recency_weighting": str(profile.get("recency_weighting") or ""),
        "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")),
        "exclusions": _string_list(profile.get("exclusions"), limit=24),
        "source_selection": _source_selection_dict(profile.get("source_selection")),
        "gmail_rules": _normalize_gmail_rules(profile.get("gmail_rules")),
        "content_limits": _stable_jsonable(profile.get("content_limits")),
        "pipeline_limits": _stable_jsonable(profile.get("pipeline_limits")),
    }
    encoded = json.dumps(comparable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable_jsonable(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_stable_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _pre_build_strategy_review_instruction(profile: dict[str, Any]) -> str:
    current = datetime.now(UTC)
    lookback_hours = _coerce_lookback_hours(profile.get("lookback_hours"))
    if lookback_hours:
        cutoff = (current - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
        recency_text = f"The confirmed source window is the last {lookback_hours} hours, with an approximate cutoff of {cutoff} UTC."
    else:
        recency_text = "The confirmed source window is all available history."
    selected_sources = [
        _SOURCE_DISPLAY.get(source, source)
        for source, enabled in _source_selection_dict(profile.get("source_selection")).items()
        if enabled
    ]
    return (
        "Pre-build review: inspect the strategy before the user builds the brief. "
        f"Today is {current.strftime('%Y-%m-%d')} UTC. {recency_text} "
        f"Selected sources: {', '.join(selected_sources) or 'none'}. "
        "Check whether any general or per-source query contradicts the confirmed recency window, "
        "especially explicit stale years or phrases that would pull results outside the window. "
        "Check source fit: markets must use ticker symbols, foreign media must use native-language queries, "
        "podcasts should use show/episode/topic phrasing likely to find playable audio, YouTube should use video/channel/topic phrasing, "
        "and web search should use current/fresh wording. "
        "If corrections are needed, return a proposal with profile_patch only; use replace_search_queries or replace_source_queries "
        "when stale or conflicting query lists should be replaced rather than appended. "
        "If the strategy is already consistent with the user's intent, current date, selected sources, and recency window, "
        "return requires_changes false and an empty profile_patch."
    )


def _run_strategy_refinement_agent(
    *,
    profile: dict[str, Any],
    instruction: str,
    task: str = "Revise the current search strategy using the user's instruction.",
) -> dict[str, Any] | None:
    client = _refinement_model_client(profile)
    if client is None:
        return None
    prompt = _build_strategy_refinement_prompt(profile=profile, instruction=instruction, task=task)
    try:
        parsed = _run_sync_complete_json(
            client.complete_json(
                system=load_prompt("strategy_refinement"),
                prompt=prompt,
                max_tokens=1600,
            )
        )
    except Exception:
        logger.exception("Failed to run strategy refinement agent")
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_strategy_refinement_prompt(*, profile: dict[str, Any], instruction: str, task: str) -> str:
    selected_sources = [
        source
        for source, enabled in _source_selection_dict(profile.get("source_selection")).items()
        if enabled
    ]
    current_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    return json.dumps(
        {
            "task": task,
            "instruction": instruction,
            "current_profile": {
                "statement": str(profile.get("statement") or ""),
                "scope": str(profile.get("scope") or ""),
                "subtopics": _string_list(profile.get("subtopics")),
                "keywords": _string_list(profile.get("keywords")),
                "search_queries": _string_list(profile.get("search_queries"), limit=20),
                "source_queries": _clean_source_queries(profile.get("source_queries")),
                "foreign_language_plan": _normalize_foreign_language_plan(profile.get("foreign_language_plan")),
                "foreign_regions": _string_list(profile.get("foreign_regions"), limit=16),
                "direct_episode_queries": _string_list(profile.get("direct_episode_queries"), limit=16),
                "related_episode_queries": _string_list(profile.get("related_episode_queries"), limit=16),
                "negative_constraints": _string_list(profile.get("negative_constraints"), limit=16),
                "priority_terms": _string_list(profile.get("priority_terms"), limit=16),
                "requested_sources": _normalize_requested_sources(profile.get("requested_sources")),
                "source_selection": _source_selection_dict(profile.get("source_selection")),
                "selected_sources": selected_sources,
                "recency_weighting": _normalize_recency(profile.get("recency_weighting")),
                "lookback_hours": _coerce_lookback_hours(profile.get("lookback_hours")),
                "exclusions": _string_list(profile.get("exclusions")),
            },
            "current_date_utc": current_utc,
            "constraints": [
                "Use the provided current date when interpreting natural-language time windows. "
                f"Today is {current_utc} (UTC). "
                "Honor the existing lookback window and avoid introducing stale-year artifacts.",
                "Only add source_queries for selected sources unless the instruction explicitly names a source type to add.",
                "Do not remove existing useful queries unless the instruction asks to narrow or exclude them.",
                "Specific named sources may be added to requested_sources.",
                "Foreign media queries must be native-language or idiomatic local-language terms.",
            ],
        },
        ensure_ascii=False,
    )


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
        if key == "search_queries" and (bool(patch.get("replace_search_queries")) or cleanup_requested):
            updated[key] = _string_list(patch.get(key), limit=20)
        else:
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
    if "lookback_hours" in patch:
        updated["lookback_hours"] = _coerce_lookback_hours(patch.get("lookback_hours"))

    if "source_queries" in patch:
        if bool(patch.get("replace_source_queries")):
            updated["source_queries"] = _clean_source_queries(patch.get("source_queries"))
        elif cleanup_requested:
            existing = _clean_source_queries(updated.get("source_queries"))
            incoming = _clean_source_queries(patch.get("source_queries"))
            for source, queries in incoming.items():
                existing[source] = queries
            updated["source_queries"] = existing
        else:
            updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), patch.get("source_queries"))
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


def _ensure_reply_updates_strategy(
    before: dict[str, Any],
    patched: dict[str, Any],
    user_text: str,
) -> dict[str, Any]:
    """Guarantee that a substantive user reply moves the search strategy (spec step 6).

    The model usually emits a profile_patch that changes the plan. When it doesn't —
    or the change was a no-op — we deterministically fold the user's own words into the
    executable plan (a narrow query from their phrasing + broad keywords + the selected
    source lanes) so the side panel can never claim an update that didn't happen.
    Skipped for empty/negative answers and for meta requests like "show me the strategy".
    """
    text = " ".join(str(user_text or "").split()).strip()
    if not text:
        return patched
    if _negative_answer(text) or _user_requested_strategy_snapshot([{"role": "user", "content": text}]):
        return patched
    if _strategy_fingerprint(before) != _strategy_fingerprint(patched):
        return patched  # the model already advanced the plan this turn

    updated = dict(patched)
    phrase = text[:180]
    updated["search_queries"] = _merge_string_lists(updated.get("search_queries"), [phrase], limit=20)
    keywords = _keywords(text)
    if keywords:
        updated["keywords"] = _merge_string_lists(updated.get("keywords"), keywords, limit=24)
    selected = _source_selection_dict(updated.get("source_selection"))
    source_additions: dict[str, list[str]] = {}
    for source in ("web_search", "youtube", "podcasts", "reddit", "collections"):
        if selected.get(source):
            source_additions[source] = [phrase]
    if source_additions:
        updated["source_queries"] = _merge_source_queries(updated.get("source_queries"), source_additions)
    if selected.get("podcasts"):
        updated["direct_episode_queries"] = _merge_string_lists(updated.get("direct_episode_queries"), [phrase], limit=16)
        if keywords:
            updated["priority_terms"] = _merge_string_lists(updated.get("priority_terms"), keywords, limit=16)
    return _coerce_profile(updated)


def _clean_next_question(value: Any) -> str | None:
    question = " ".join(str(value or "").split()).strip()
    if not question:
        return None
    if not question.endswith("?"):
        question = f"{question}?"
    if _is_refinement_closing_language(question):
        return None
    return question[:260]


def _refinement_reply_with_required_question(
    text: str,
    *,
    profile: dict[str, Any],
    messages: list[dict[str, str]],
    just_go_now: bool,
) -> str:
    cleaned = _sanitize_recent_visible_text_years(_strip_refinement_closing_language(text), profile)
    if just_go_now:
        return cleaned or "Confirmed. I’ll build using the current search strategy."
    strategy_snapshot = _format_strategy_snapshot(profile) if _user_requested_strategy_snapshot(messages) else ""
    if _ends_with_question(cleaned):
        if strategy_snapshot:
            return f"{cleaned}\n\n{strategy_snapshot}"
        return cleaned
    question = _strategy_deepening_question(profile, messages) or _search_strategy_question(profile)
    if strategy_snapshot:
        if cleaned:
            return f"{cleaned}\n\n{strategy_snapshot}\n\n{question}"
        return f"{strategy_snapshot}\n\n{question}"
    if cleaned:
        return f"{cleaned} {question}"
    return question


def _user_requested_strategy_snapshot(messages: list[dict[str, str]]) -> bool:
    last = _last_user_message(messages).casefold()
    return any(
        phrase in last
        for phrase in (
            "show me the strategy",
            "show the strategy",
            "see the strategy",
            "output the strategy",
            "print the strategy",
            "what is the strategy",
            "show me",
        )
    )


def _last_user_message(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


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


def _sanitize_recent_visible_text_years(text: str, profile: dict[str, Any]) -> str:
    lookback_hours = _coerce_lookback_hours(profile.get("lookback_hours"))
    recency = _normalize_recency(profile.get("recency_weighting"))
    if lookback_hours is None and recency not in {"breaking", "recent"}:
        return text
    if lookback_hours is not None and lookback_hours > 24 * 90:
        return text
    current_year = datetime.now(UTC).year

    def replace_year(match: re.Match[str]) -> str:
        year = int(match.group(1))
        return str(current_year) if year < current_year else match.group(1)

    # Preserve the model's paragraph/line structure; only the stale year tokens change.
    return _QUERY_YEAR_RE.sub(replace_year, str(text or "")).strip()


def _ends_with_question(text: str) -> bool:
    return str(text or "").rstrip().endswith("?")


def _strip_refinement_closing_language(text: str) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""
    # Drop sentences that announce completion ("ready to build", etc.) while preserving
    # the model's paragraph structure so the chat reply keeps its formatting.
    kept_blocks: list[str] = []
    for block in re.split(r"\n{2,}", raw):
        sentences = re.split(r"(?<=[.!?])\s+", block.strip())
        kept = [sentence for sentence in sentences if sentence.strip() and not _is_refinement_closing_language(sentence)]
        if kept:
            kept_blocks.append(" ".join(kept))
    return "\n\n".join(kept_blocks).strip()


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
        if source_key == "markets":
            # The markets lane is the explicit ticker lane: validate each entry as a
            # ticker (company names/cashtags resolved, acronyms/junk dropped).
            query_list = normalize_market_query_tickers(query_list)[:20]
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
    return min(hours, 262800)


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
    if _market_tracking_interest(text):
        return _first_unasked_question(
            messages,
            [
                "I have market signals in the plan; which companies, suppliers, or catalysts are missing from the search strategy?",
                "Which evidence should carry the most weight: company filings, earnings commentary, pricing data, supply-chain capacity, customer demand, or analyst revisions?",
                "Should the market section be organized by company, signal type, or near-term catalyst?",
            ],
        )
    if _string_list(profile.get("search_queries")) or _clean_source_queries(profile.get("source_queries")):
        return _first_unasked_question(
            messages,
            [
                "What kind of evidence should I trust most for this brief: primary reporting, expert analysis, community signal, or practical examples?",
                "Should the brief prioritize breadth across sources or depth on the strongest few items?",
                "Looking at this search strategy, what sources, entities, or angles are missing?",
            ],
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
        if _normalize_question_for_repeat_check(message.get("content") or "") == normalized:
            return True
    return False


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
    gmail_selected = _source_selection_dict(profile.get("source_selection")).get("gmail", False)
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
        "market_tracking_interest": _market_tracking_interest(statement),
        "gmail_rules_needed": gmail_needs_instructions,
        "recommended_question_focus": (
            "IMPORTANT: Gmail is selected but has no search instructions yet. Ask ONLY about Gmail in this turn — "
            "what kind of newsletters or email content the user wants (topic, recency). "
            "Example: 'What kind of newsletters should I look for in Gmail? e.g. AI research digests from the last two weeks.'"
            if gmail_needs_instructions
            else (
                "Ask about investable signals, relative comparison, source quality, catalysts, or risks. "
                "Do not ask for recency or exclusions if they are already listed here."
                if _market_tracking_interest(statement)
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
