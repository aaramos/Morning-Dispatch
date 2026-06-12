"""AI-led SSE streaming refinement chat (astream_refinement and helpers).

Code moved verbatim from refinement.py (M7 split).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from backend.app.core.config import get_settings
from backend.app.core.prompt_loader import load_prompt
from backend.app.db import database
from backend.app.services import explore, gmail_allowlist, model_routing

from backend.app.services import refinement_session

from backend.app.services.profile_patch import (
    AGENT_PENDING_FIELD,
    GMAIL_RULES_FIELD,
    GMAIL_SENDER_SELECTION_FIELD,
    _QUERY_YEAR_RE,
    _canonicalize_must_have,
    _clean_must_have_aliases,
    _clean_source_queries,
    _coerce_lookback_hours,
    _coerce_profile,
    _deterministic_question,
    _diagnostics_query_snapshot,
    _enrich_diagnostics,
    _fill_defaults,
    _format_strategy_snapshot,
    _inferred_constraints,
    _is_refinement_closing_language,
    _keywords,
    _merge_agent_profile_patch,
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
    _normalize_refinement_intent,
    _normalize_requested_sources,
    _parse_chat_payload,
    _pending_strategy_refinement,
    _question_repeats_answered_constraint,
    _question_was_recently_asked,
    _readiness_reason,
    _recency_from_lookback_hours,
    _search_strategy_question,
    _seed_profile_with_hints,
    _session_response,
    _source_selection_dict,
    _strategy_deepening_question,
    _string_list,
    _user_authored_text,
    _visible_prose,
)

from backend.app.services.refinement_session import (
    _apply_models,
    _discover_gmail_candidates,
    _gmail_candidate_question,
    _gmail_rules_from_answer,
    _initial_profile,
    _is_gmail_approval_response,
    _refinement_model_client,
    _selected_gmail_senders,
    advance_session,
)

from backend.app.services.strategy_refinement import _strategy_fingerprint, confirm_strategy_refinement


logger = logging.getLogger(__name__)


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
    source_scope_touched: bool = False,
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
    if source_scope_touched:
        profile["source_scope_answered"] = True
    explicit_source_scope = (
        {
            "recency_weighting": profile.get("recency_weighting"),
            "lookback_hours": profile.get("lookback_hours"),
        }
        if source_scope_touched
        else None
    )

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
    user_build_requested = bool(clean_answer and not just_go_now and _user_requested_build(clean_answer))

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
        if not user_build_requested:
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
    if user_build_requested:
        messages.append({"role": "user", "content": clean_answer})
        turn_count += 1
        async for event in _astream_confirm_build_request(
            session_id=session_id,
            session=session,
            profile=profile,
            messages=messages,
            turn_count=turn_count,
        ):
            yield event
        return

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
        async for event in _astream_fallback(session_id, clean_answer, just_go_now, models, prefix_error=True):
            yield event
        return

    final_visible, _ = _visible_prose(full_text, final=True)
    if len(final_visible) > emitted:
        tail = final_visible[emitted:]
        if tail:
            emitted += len(tail)
            yield {"type": "token", "text": tail}

    assistant_text = final_visible.strip()
    patch, ready_flag, intent = _parse_chat_payload(full_text)
    intent = _normalize_refinement_intent(intent)
    if just_go_now:
        intent = "build"
    ready = intent == "build"

    patched = _merge_agent_profile_patch(profile, patch, user_text=_user_authored_text(profile, messages))
    if explicit_source_scope is not None:
        patched["recency_weighting"] = explicit_source_scope["recency_weighting"]
        patched["lookback_hours"] = explicit_source_scope["lookback_hours"]
        patched["source_scope_answered"] = True
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
        streamed_visible = final_visible[:emitted].rstrip()
        if not streamed_visible:
            yield {"type": "token", "text": assistant_text}
        elif assistant_text.startswith(streamed_visible):
            final_tail = assistant_text[len(streamed_visible):]
            if final_tail.strip():
                yield {"type": "token", "text": final_tail}

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
        canonical_terms, canonical_aliases = await expand_must_have_aliases(patched)
        patched["must_have_terms"] = canonical_terms
        patched["must_have_aliases"] = canonical_aliases
        patched["foreign_language_plan"] = await _ensure_foreign_language_plan(patched)
        pre_critique = _diagnostics_query_snapshot(patched)
        patched = refinement_session._critique_search_plan(patched)
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


def _user_requested_build(answer: str) -> bool:
    text = " ".join(str(answer or "").casefold().split())
    if not text:
        return False
    if re.search(r"\b(?:do not|don't|dont|never|not yet|no)\s+(?:build|run|start|generate|create|make|proceed)\b", text):
        return False
    build_patterns = (
        r"\bbuild\s+(?:the\s+)?brief\b",
        r"\b(?:create|generate|make)\s+(?:the\s+)?brief\b",
        r"\b(?:start|run)\s+(?:the\s+)?(?:brief|build)\b",
        r"\bgo ahead\s+(?:and\s+)?(?:build|run|start|create|generate|make)\b",
        r"\b(?:please\s+)?(?:build|run|start)\s+it\b",
        r"\b(?:let's|lets)\s+(?:build|run|start)\b",
        r"\b(?:use|respect)\s+(?:this|that|my|the current)\s+.*\b(?:build|brief)\b",
    )
    return any(re.search(pattern, text) for pattern in build_patterns)


async def _astream_confirm_build_request(
    *,
    session_id: str,
    session: dict[str, Any],
    profile: dict[str, Any],
    messages: list[dict[str, str]],
    turn_count: int,
):
    confirmation = "Confirmed. I’ll build using the current search strategy."
    patched = _fill_defaults(_seed_profile_with_hints(profile))
    if not str(patched.get("scope") or "").strip():
        patched["scope"] = str(patched.get("statement") or session.get("statement") or "").strip()
    canonical_terms, canonical_aliases = await expand_must_have_aliases(patched)
    patched["must_have_terms"] = canonical_terms
    patched["must_have_aliases"] = canonical_aliases
    patched["foreign_language_plan"] = await _ensure_foreign_language_plan(patched)
    pre_critique = _diagnostics_query_snapshot(patched)
    patched = refinement_session._critique_search_plan(patched)
    patched["refinement_diagnostics"] = _enrich_diagnostics(
        patched,
        model_profile_patch={},
        pre_critique=pre_critique,
        readiness_reason=_readiness_reason(
            ready_requested=True,
            just_go_now=True,
            turn_count=turn_count,
        ),
    )
    patched = _coerce_profile(patched)
    saved = explore.save_topic_profile(patched)
    topic_id = str(saved["topic_id"])
    messages.append({"role": "assistant", "content": confirmation})
    updated = database.update_refinement_session(
        session_id,
        profile=patched,
        messages=messages,
        pending_field=None,
        turn_count=turn_count,
        status="finalized",
        topic_id=topic_id,
    )
    response = _session_response(updated) if updated else None
    if response is None:
        yield {"type": "error", "message": "Failed to persist refinement session"}
        return
    yield {"type": "token", "text": confirmation}
    yield {"type": "plan", "session": response}
    yield {"type": "done", "session": response, "ready": True, "trigger_build": True}


async def expand_must_have_aliases(profile: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    terms = _string_list(profile.get("must_have_terms"), limit=6)
    if not terms:
        return [], {}
    existing = _clean_must_have_aliases(profile.get("must_have_aliases"), terms=terms)
    term_keys = {term.casefold() for term in terms}
    if term_keys and term_keys.issubset(existing.keys()):
        return _canonicalize_must_have(terms, existing)

    settings = get_settings()
    try:
        resolution = model_routing.client_for_agent("refinement", settings=settings)
        client = resolution.client
    except Exception as exc:  # pragma: no cover - routing varies by deployment
        logger.warning("Could not route must-have alias expansion: %s", exc)
        return _canonicalize_must_have(terms, existing)
    if client is None:
        return _canonicalize_must_have(terms, existing)

    languages = []
    for item in _normalize_foreign_language_plan(profile.get("foreign_language_plan")):
        languages.append({"code": item.get("code"), "name": item.get("name")})
    prompt = {
        "statement": str(profile.get("statement") or ""),
        "scope": str(profile.get("scope") or ""),
        "must_have_terms": terms,
        "foreign_languages": languages,
        "foreign_regions": _string_list(profile.get("foreign_regions"), limit=16),
        "instructions": (
            "Generate official names, unambiguous abbreviations, and native-language renderings for each must-have term. "
            "Do not include broader categories or parent geographies as aliases."
        ),
    }
    try:
        payload = await client.complete_json(
            system=load_prompt("must_have_alias_expansion"),
            prompt=json.dumps(prompt, ensure_ascii=False),
            max_tokens=600,
        )
    except Exception as exc:  # pragma: no cover - provider failures should not disable the gate
        logger.warning("Must-have alias expansion failed: %s", exc)
        return _canonicalize_must_have(terms, existing)
    aliases = _clean_must_have_aliases(payload.get("aliases"), terms=terms) if isinstance(payload, dict) else {}
    merged_aliases = {**existing, **aliases}
    return _canonicalize_must_have(terms, merged_aliases)


async def _ensure_foreign_language_plan(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate and persist the foreign-media language plan at confirm time.

    Building the plan here (instead of lazily mid-build) removes a model call
    from the discovery hot path, makes the native queries reviewable in the
    strategy preview, and keeps the result deterministic across builds. Fails
    open to whatever plan already exists so a model hiccup never blocks confirm.
    """
    existing = profile.get("foreign_language_plan")
    existing_list = list(existing) if isinstance(existing, (list, tuple)) else []
    selection = profile.get("source_selection")
    foreign_selected = bool(isinstance(selection, dict) and selection.get("foreign_media"))
    if not foreign_selected or existing_list:
        return existing_list
    try:
        from backend.agents.discovery.foreign_media import foreign_language_plan_for_profile
        from backend.agents.discovery.types import TopicProfile

        topic_profile = TopicProfile.from_dict(profile)
        plan = await foreign_language_plan_for_profile(topic_profile)
        return [dict(entry) for entry in plan]
    except Exception as exc:  # pragma: no cover - provider/model failures must not block confirm
        logger.warning("Foreign language plan generation failed: %s", exc)
        return existing_list


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
        "must_have_terms": _string_list(profile.get("must_have_terms"), limit=6),
        "must_have_aliases": _clean_must_have_aliases(profile.get("must_have_aliases"), terms=_string_list(profile.get("must_have_terms"), limit=6)),
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
            "question_policy": (
                "Ask at most one next question. Do not ask about anything already present in already_inferred, "
                "especially recency, exclusions, selected sources, named companies, locations, or stated constraints. "
                "If the user asks to build, set intent='build', ready_to_build=true, and respond with a brief confirmation."
            ),
            "current_date_hint": f"Today is {current_utc} (UTC). Use this when judging freshness windows.",
        },
        ensure_ascii=False,
    )


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
        prefix, question = _split_trailing_question(cleaned)
        if _is_redundant_refinement_question(question, profile, messages):
            replacement = _strategy_deepening_question(profile, messages) or _search_strategy_question(profile)
            if replacement and not _is_redundant_refinement_question(replacement, profile, messages):
                cleaned = f"{prefix} {replacement}".strip() if prefix else replacement
            else:
                cleaned = prefix.strip()
        if not cleaned:
            cleaned = _strategy_deepening_question(profile, messages) or _search_strategy_question(profile)
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


def _split_trailing_question(text: str) -> tuple[str, str]:
    clean = str(text or "").strip()
    match = re.search(r"(?s)^(.*?)([^.!?\n][^?\n]*\?)\s*$", clean)
    if not match:
        return clean, clean
    return match.group(1).strip(), match.group(2).strip()


def _is_redundant_refinement_question(
    question: str,
    profile: dict[str, Any],
    messages: list[dict[str, str]],
) -> bool:
    clean = str(question or "").strip()
    if not clean:
        return False
    return _question_repeats_answered_constraint(clean, profile) or _question_was_recently_asked(clean, messages)


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
