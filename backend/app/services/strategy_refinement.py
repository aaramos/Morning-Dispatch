"""Strategy refinement and pre-build strategy review (sync + streaming variants).

Code moved verbatim from refinement.py (M7 split).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.app.core.prompt_loader import load_prompt
from backend.app.db import database
from backend.app.services import explore

from backend.app.services import refinement_session

from backend.app.services.profile_patch import (
    PENDING_STRATEGY_FIELD,
    PENDING_STRATEGY_PROFILE_KEY,
    PODCAST_STRATEGY_FIELDS,
    STRATEGY_REVIEW_PROFILE_KEY,
    STRATEGY_REVIEW_RESOLVED_STATUSES,
    _SOURCE_DISPLAY,
    _clean_must_have_aliases,
    _clean_source_queries,
    _coerce_lookback_hours,
    _coerce_profile,
    _diagnostics_query_snapshot,
    _enrich_diagnostics,
    _extract_json_block,
    _fill_defaults,
    _merge_agent_profile_patch,
    _messages,
    _models,
    _normalize_foreign_language_plan,
    _normalize_gmail_rules,
    _normalize_recency,
    _normalize_requested_sources,
    _parse_chat_payload,
    _pending_strategy_refinement,
    _prune_unselected_source_fields,
    _run_sync_complete_json,
    _session_response,
    _source_selection_dict,
    _stable_jsonable,
    _strategy_preview,
    _string_list,
    _trim_for_diagnostics,
    _visible_prose,
)

from backend.app.services.refinement_session import _apply_models, _refinement_model_client


logger = logging.getLogger(__name__)


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
    proposed_profile = refinement_session._critique_search_plan(proposed_profile)
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
    if patch.get("must_have_terms") or patch.get("must_have_aliases"):
        changed.append("must-have terms")
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
    payload_selection = (
        _source_selection_dict(payload_profile.get("source_selection"))
        if isinstance(payload_profile, dict) and isinstance(payload_profile.get("source_selection"), dict)
        else None
    )
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
            "must_have_terms",
            "must_have_aliases",
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
    coerced = _coerce_profile(merged)
    if payload_selection is not None:
        coerced["source_selection"] = payload_selection
    return _apply_models(_prune_unselected_source_fields(coerced), models)


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
        "must_have_terms": _string_list(profile.get("must_have_terms"), limit=6),
        "must_have_aliases": _clean_must_have_aliases(profile.get("must_have_aliases"), terms=_string_list(profile.get("must_have_terms"), limit=6)),
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
        "Check spelling: every search term must use the correct, standard spelling of proper nouns, place names, brands, "
        "products, and people; foreign-language queries must use correct native-language spelling. Correct any misspelled term. "
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
                "must_have_terms": _string_list(profile.get("must_have_terms"), limit=6),
                "must_have_aliases": _clean_must_have_aliases(profile.get("must_have_aliases"), terms=_string_list(profile.get("must_have_terms"), limit=6)),
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
                "Spell every search term correctly. Use the correct, standard spelling of proper nouns, place names, "
                "brands, products, and people, and the correct native-language spelling for foreign-language terms. "
                "Never emit a misspelled term.",
                "Every search query and source query must be specific and descriptive: each must name a "
                "concrete entity, organization, person, product, place, or topic. Never emit bare "
                "stopwords, conjunctions, or generic filler such as \"either\", \"various\", \"things\", "
                "\"stuff\", \"general\", or \"misc\" — a query made only of such words names nothing "
                "searchable and will be discarded.",
            ],
        },
        ensure_ascii=False,
    )
