from __future__ import annotations

import json
import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.app.core.config import get_settings
from backend.app.db import database
from backend.app.services import brief_settings, digest_runner, email_delivery, explore, foreign_article_translation, refinement, scheduler
from backend.app.services.brief_strategy import selected_source_labels, summarize_search_strategy
from backend.app.services.brief_title import tight_brief_title

router = APIRouter(prefix="/api")
delivery_router = APIRouter()
ScheduleValue = Literal["hourly", "daily", "weekdays", "weekly", "monthly"]


class DigestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    interest: str = Field(min_length=1)
    schedule: ScheduleValue = "daily"
    sources: list[dict[str, Any]] = Field(default_factory=list)
    threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    profile_id: str | None = None


class DigestUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    interest: str | None = Field(default=None, min_length=1)
    schedule: ScheduleValue | None = None
    sources: list[dict[str, Any]] | None = None
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal["active", "paused", "archived"] | None = None


class FeedbackCreate(BaseModel):
    issue_id: str = Field(min_length=1)
    url: str = Field(min_length=8)
    signal: Literal["up", "down", "click", "love", "like", "neutral", "dislike"]


class TopicProfileCreate(BaseModel):
    topic_id: str | None = None
    statement: str = Field(min_length=1)
    scope: str | None = None
    subtopics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    source_queries: dict[str, list[str]] = Field(default_factory=dict)
    foreign_regions: list[str] = Field(default_factory=list)
    depth: Literal["practitioner", "informed-generalist"] = "informed-generalist"
    recency_weighting: Literal["breaking", "recent", "last_year", "all_available", "balanced", "evergreen"] = "recent"
    lookback_hours: int | None = Field(default=None, ge=1, le=brief_settings.MAX_LOOKBACK_HOURS)
    exclusions: list[str] = Field(default_factory=list)
    source_selection: dict[str, bool] = Field(default_factory=dict)
    requested_sources: list[dict[str, Any]] = Field(default_factory=list)
    promoted_sources: list[dict[str, Any]] = Field(default_factory=list)
    gmail_rules: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, Any] = Field(default_factory=dict)
    schedule: ScheduleValue | None = None
    schedule_config: dict[str, Any] = Field(default_factory=dict)
    delivery_config: dict[str, Any] = Field(default_factory=dict)
    content_limits: dict[str, Any] = Field(default_factory=dict)
    direct_episode_queries: list[str] = Field(default_factory=list)
    related_episode_queries: list[str] = Field(default_factory=list)
    negative_constraints: list[str] = Field(default_factory=list)
    priority_terms: list[str] = Field(default_factory=list)


class ExplorationCreate(BaseModel):
    mode: Literal["show_now", "scheduled"] = "show_now"
    source_selection: dict[str, bool] = Field(default_factory=dict)
    candidate_limit: int = Field(default=150, ge=1, le=brief_settings.MAX_CANDIDATE_BUDGET)
    lookback_hours: int | None = Field(default=None, ge=1, le=brief_settings.MAX_LOOKBACK_HOURS)


class ExplorationRebuildCreate(ExplorationCreate):
    topic_profile: TopicProfileCreate | None = None
    refinement_session_id: str | None = None


class TopicProfileBuildCreate(TopicProfileCreate):
    mode: Literal["show_now", "scheduled"] = "show_now"
    candidate_limit: int = Field(default=150, ge=1, le=brief_settings.MAX_CANDIDATE_BUDGET)
    lookback_hours: int | None = Field(default=None, ge=1, le=brief_settings.MAX_LOOKBACK_HOURS)
    refinement_session_id: str | None = None


class ExploreEmailCreate(BaseModel):
    recipient_email: str | None = Field(default=None, max_length=254)


class ForeignArticleTranslationCreate(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    summary: str | None = Field(default=None, max_length=4000)
    source_language: str | None = Field(default=None, max_length=16)
    source_language_name: str | None = Field(default=None, max_length=80)
    original_title: str | None = Field(default=None, max_length=500)
    original_summary: str | None = Field(default=None, max_length=4000)


class RefinementStart(BaseModel):
    statement: str = Field(min_length=1)
    topic_id: str | None = None
    revisit: bool = False
    source_selection: dict[str, bool] = Field(default_factory=dict)
    models: dict[str, Any] = Field(default_factory=dict)


class RefinementMessage(BaseModel):
    answer: str = ""
    models: dict[str, Any] = Field(default_factory=dict)
    just_go_now: bool = False


class RefinementStreamMessage(BaseModel):
    session_id: str | None = None
    statement: str = ""
    source_selection: dict[str, bool] = Field(default_factory=dict)
    foreign_regions: list[str] = Field(default_factory=list)
    answer: str = ""
    models: dict[str, Any] = Field(default_factory=dict)
    just_go_now: bool = False


class StrategyRefinementMessage(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    models: dict[str, Any] = Field(default_factory=dict)


class StrategyRefinementStreamMessage(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    models: dict[str, Any] = Field(default_factory=dict)


class StrategyReviewStreamMessage(BaseModel):
    profile: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, Any] = Field(default_factory=dict)


class StrategyReviewMessage(BaseModel):
    profile: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, Any] = Field(default_factory=dict)


class StrategyRefinementConfirm(BaseModel):
    apply: bool = True


class TopicProfileSchedule(BaseModel):
    schedule: ScheduleValue | None = None
    time_of_day: str | None = Field(default=None, max_length=8)
    timezone: str | None = Field(default=None, max_length=80)
    email_enabled: bool | None = None


class TopicProfileContentLimitsUpdate(BaseModel):
    content_limits: dict[str, Any] = Field(default_factory=dict)
    lookback_hours: int | None = Field(default=None, ge=1, le=brief_settings.MAX_LOOKBACK_HOURS)
    pipeline_limits: dict[str, Any] = Field(default_factory=dict)


class PodcastShowRef(BaseModel):
    feed_url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="Podcast", max_length=400)


class PodcastSubscriptionUpdate(BaseModel):
    shows: list[PodcastShowRef] = Field(default_factory=list)


class SourceSetupPayload(BaseModel):
    provider: Literal["tavily", "brave", "serpapi", "serper"] = "serper"
    api_key: str = Field(min_length=1, max_length=1000)


class ApiKeyPayload(BaseModel):
    api_key: str = Field(min_length=1, max_length=1000)





@router.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.environment,
        "database_path": str(settings.database_path),
        "data_dir": str(settings.data_dir),
        "secrets_dir": str(settings.secrets_dir),
    }


@router.get("/profiles")
def profiles() -> list[dict[str, Any]]:
    return database.list_profiles()


@router.get("/digests")
def digests(include_archived: bool = Query(default=False)) -> list[dict[str, Any]]:
    return database.list_digests(include_archived=include_archived)


@router.get("/explore/topic-profiles")
def topic_profiles() -> list[dict[str, Any]]:
    return database.list_topic_profiles()


@router.get("/explore/source-status")
async def explore_source_status() -> dict[str, Any]:
    return await explore.source_status()


@router.get("/explore/explorations")
def explorations(limit: int = Query(default=25, ge=1, le=200)) -> list[dict[str, Any]]:
    database.purge_expired_deleted_explorations()
    return database.list_explorations(limit=limit)


@router.get("/explore/scheduled-topic-profiles")
def scheduled_topic_profiles() -> list[dict[str, Any]]:
    return [_scheduled_topic_profile_response(topic) for topic in database.list_scheduled_topic_profiles()]


@router.post("/explore/refinement-sessions", status_code=201)
def start_refinement(payload: RefinementStart) -> dict[str, Any]:
    try:
        return refinement.start_session(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/explore/refinement-sessions/stream")
async def stream_refinement(payload: RefinementStreamMessage) -> StreamingResponse:
    async def event_source() -> Any:
        try:
            async for event in refinement.astream_refinement(
                session_id=payload.session_id,
                statement=payload.statement,
                source_selection=payload.source_selection,
                foreign_regions=payload.foreign_regions,
                models=payload.models,
                answer=payload.answer,
                just_go_now=payload.just_go_now,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # pragma: no cover - defensive: surface a clean SSE error
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/explore/refinement-sessions/{session_id}/messages")
def answer_refinement(session_id: str, payload: RefinementMessage) -> dict[str, Any]:
    session = refinement.advance_session(session_id, payload.model_dump())
    if session is None:
        raise HTTPException(status_code=404, detail="Refinement session not found")
    return session


@router.post("/explore/refinement-sessions/{session_id}/strategy")
def refine_search_strategy(session_id: str, payload: StrategyRefinementMessage) -> dict[str, Any]:
    try:
        session = refinement.refine_strategy(session_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="Refinement session not found")
    return session


@router.post("/explore/refinement-sessions/{session_id}/strategy/stream")
async def stream_refine_strategy(session_id: str, payload: StrategyRefinementStreamMessage) -> StreamingResponse:
    async def event_source() -> Any:
        try:
            async for event in refinement.astream_refine_strategy(
                session_id=session_id,
                instruction=payload.instruction,
                models=payload.models,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/explore/refinement-sessions/{session_id}/strategy/review/stream")
async def stream_review_strategy(session_id: str, payload: StrategyReviewStreamMessage) -> StreamingResponse:
    async def event_source() -> Any:
        try:
            async for event in refinement.astream_review_strategy(
                session_id=session_id,
                profile_payload=payload.profile or None,
                models=payload.models,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/explore/refinement-sessions/{session_id}/strategy/review")
def review_search_strategy(session_id: str, payload: StrategyReviewMessage) -> dict[str, Any]:
    try:
        session = refinement.review_strategy(session_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="Refinement session not found")
    return session


@router.post("/explore/refinement-sessions/{session_id}/strategy/confirm")
def confirm_search_strategy_refinement(session_id: str, payload: StrategyRefinementConfirm) -> dict[str, Any]:
    try:
        session = refinement.confirm_strategy_refinement(session_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="Refinement session not found")
    return session


@router.post("/explore/topic-profiles", status_code=201)
def create_topic_profile(payload: TopicProfileCreate) -> dict[str, Any]:
    try:
        return explore.save_topic_profile(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/explore/topic-profiles/build", status_code=202)
async def create_topic_profile_and_queue_build(payload: TopicProfileBuildCreate) -> dict[str, Any]:
    data = payload.model_dump()
    mode = data.pop("mode")
    candidate_limit = data.pop("candidate_limit")
    lookback_hours = data.pop("lookback_hours")
    refinement_session_id = data.pop("refinement_session_id", None)
    if lookback_hours is not None:
        data["lookback_hours"] = lookback_hours
    try:
        topic_profile = explore.save_topic_profile(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    exploration = explore.start_show_now(
        str(topic_profile["topic_id"]),
        mode=mode,
        source_selection=payload.source_selection,
        candidate_limit=candidate_limit,
        lookback_hours=lookback_hours,
    )
    if exploration is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    if refinement_session_id:
        database.delete_refinement_session(refinement_session_id)
    return {"topic_profile": topic_profile, "exploration": exploration}


@router.post("/explore/explorations/{exploration_id}/clone-topic-profile", status_code=201)
def clone_exploration_topic_profile(exploration_id: str) -> dict[str, Any]:
    exploration = database.get_exploration(exploration_id)
    if exploration is None:
        raise HTTPException(status_code=404, detail="Exploration not found")
    source_topic = database.get_topic_profile(str(exploration["topic_id"]))
    if source_topic is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")

    source_profile = dict(source_topic.get("profile") or {})
    clone_payload = {
        **source_profile,
        "topic_id": None,
        "statement": source_topic.get("statement") or source_profile.get("statement") or "",
        "source_selection": dict(exploration.get("source_selection") or source_profile.get("source_selection") or {}),
        "schedule": None,
        "schedule_config": {},
        "delivery_config": {},
        "status": "active",
        "archived": False,
        "deleted": False,
    }
    try:
        cloned_topic = explore.save_topic_profile(clone_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "topic_profile": cloned_topic,
        "source_exploration_id": exploration_id,
        "source_topic_id": source_topic["topic_id"],
    }


@router.get("/explore/topic-profiles/{topic_id}")
def get_topic_profile(topic_id: str) -> dict[str, Any]:
    profile = database.get_topic_profile(topic_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    return profile


@router.post("/explore/topic-profiles/{topic_id}/schedule", status_code=201)
def schedule_topic_profile(topic_id: str, payload: TopicProfileSchedule) -> dict[str, Any]:
    record = database.get_topic_profile(topic_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    schedule = str(payload.schedule or "").strip() or None
    completed_show_now = database.get_latest_exploration(topic_id=topic_id, mode="show_now", status="complete")
    completed_scheduled = database.get_latest_exploration(topic_id=topic_id, mode="scheduled", status="complete")
    existing_schedule = str(record.get("schedule") or "").strip()
    if schedule and completed_show_now is None and completed_scheduled is None and not existing_schedule:
        raise HTTPException(status_code=400, detail="Build the brief before scheduling it as a digest")
    schedule_config = {
        "frequency": schedule,
        "time_of_day": (payload.time_of_day or "08:00").strip() or "08:00",
        "timezone": (payload.timezone or "America/Los_Angeles").strip() or "America/Los_Angeles",
    } if schedule else {}
    delivery_config = dict(record["profile"].get("delivery_config") or {})
    for key in (
        "delivery_disabled_after_failure",
        "last_delivery_status",
        "last_delivery_error",
        "last_error",
        "last_delivery_attempted_at",
    ):
        delivery_config.pop(key, None)
    if payload.email_enabled is not None:
        delivery_config["email_enabled"] = bool(payload.email_enabled)
    profile = {
        **record["profile"],
        "topic_id": topic_id,
        "statement": record["statement"],
        "schedule": schedule,
        "schedule_config": schedule_config,
        "delivery_config": delivery_config,
        "status": "active" if schedule else record["profile"].get("status", "active"),
        "archived": False,
        "deleted": False,
    }
    return database.upsert_topic_profile(profile)


@router.post("/explore/topic-profiles/{topic_id}/content-limits")
def update_topic_profile_content_limits(topic_id: str, payload: TopicProfileContentLimitsUpdate) -> dict[str, Any]:
    record = database.get_topic_profile(topic_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    profile = {
        **record["profile"],
        "topic_id": topic_id,
        "statement": record["statement"],
        "content_limits": brief_settings.normalize_content_limits(payload.content_limits),
    }
    if payload.pipeline_limits:
        profile["pipeline_limits"] = brief_settings.normalize_pipeline_limits(payload.pipeline_limits)
    elif record["profile"].get("pipeline_limits"):
        profile["pipeline_limits"] = brief_settings.normalize_pipeline_limits(record["profile"].get("pipeline_limits"))
    if payload.lookback_hours is not None:
        profile["lookback_hours"] = payload.lookback_hours
    try:
        return explore.save_topic_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/explore/topic-profiles/{topic_id}/podcast-shows")
async def list_podcast_shows(topic_id: str) -> dict[str, Any]:
    result = await explore.podcast_show_candidates(topic_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    return result


@router.post("/explore/topic-profiles/{topic_id}/podcast-shows")
def save_podcast_shows(topic_id: str, payload: PodcastSubscriptionUpdate) -> dict[str, Any]:
    result = explore.save_podcast_subscriptions(
        topic_id, [show.model_dump() for show in payload.shows]
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    return result


@router.post("/explore/topic-profiles/{topic_id}/pause")
def pause_topic_profile(topic_id: str) -> dict[str, Any]:
    record = database.get_topic_profile(topic_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    if not str(record.get("schedule") or "").strip():
        raise HTTPException(status_code=400, detail="Only scheduled digests can be paused")
    profile = {
        **record["profile"],
        "topic_id": topic_id,
        "statement": record["statement"],
        "status": "paused",
        "archived": False,
        "deleted": False,
    }
    return database.upsert_topic_profile(profile)


@router.post("/explore/topic-profiles/{topic_id}/archive")
def archive_topic_profile(topic_id: str) -> dict[str, Any]:
    record = database.get_topic_profile(topic_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    profile = {
        **record["profile"],
        "topic_id": topic_id,
        "statement": record["statement"],
        "schedule": None,
        "status": "archived",
        "archived": True,
    }
    return database.upsert_topic_profile(profile)


@router.delete("/explore/topic-profiles/{topic_id}")
def delete_topic_profile(topic_id: str) -> dict[str, Any]:
    record = database.get_topic_profile(topic_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    profile = {
        **record["profile"],
        "topic_id": topic_id,
        "statement": record["statement"],
        "schedule": None,
        "status": "deleted",
        "archived": True,
        "deleted": True,
    }
    return database.upsert_topic_profile(profile)


@router.post("/explore/topic-profiles/{topic_id}/discover", status_code=202)
async def discover_topic(topic_id: str, payload: ExplorationCreate) -> dict[str, Any]:
    result = await explore.run_discovery(
        topic_id,
        mode=payload.mode,
        source_selection=payload.source_selection,
        candidate_limit=payload.candidate_limit,
        lookback_hours=payload.lookback_hours,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    return result


@router.post("/explore/topic-profiles/{topic_id}/run", status_code=202)
async def run_topic_exploration(topic_id: str, payload: ExplorationCreate) -> dict[str, Any]:
    result = explore.start_show_now(
        topic_id,
        mode=payload.mode,
        source_selection=payload.source_selection,
        candidate_limit=payload.candidate_limit,
        lookback_hours=payload.lookback_hours,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Topic profile not found")
    return {"exploration": result}


@router.get("/explore/explorations/{exploration_id}")
def get_exploration(exploration_id: str) -> dict[str, Any]:
    exploration = database.get_exploration(exploration_id)
    if exploration is None:
        raise HTTPException(status_code=404, detail="Exploration not found")
    return exploration


@router.post("/explore/explorations/{exploration_id}/cancel")
def cancel_exploration(exploration_id: str) -> dict[str, Any]:
    exploration = explore.cancel_exploration(exploration_id)
    if exploration is None:
        raise HTTPException(status_code=404, detail="Exploration not found")
    return {
        "status": "cancelled" if exploration.get("progress", {}).get("cancel_requested") else exploration.get("status"),
        "exploration": exploration,
    }


@router.delete("/explore/explorations/{exploration_id}")
def delete_exploration(exploration_id: str) -> dict[str, Any]:
    exploration = database.soft_delete_exploration(exploration_id)
    if exploration is None:
        raise HTTPException(status_code=404, detail="Exploration not found")
    return {
        "status": "deleted",
        "exploration": exploration,
        "undo_available_until": exploration.get("delete_after"),
    }


@router.post("/explore/explorations/{exploration_id}/restore")
def restore_exploration(exploration_id: str) -> dict[str, Any]:
    exploration = database.restore_exploration(exploration_id)
    if exploration is None:
        raise HTTPException(status_code=410, detail="Exploration can no longer be restored")
    return {
        "status": "restored",
        "exploration": exploration,
    }


@router.get("/explore/explorations/{exploration_id}/brief/html", response_class=HTMLResponse)
def exploration_brief_html(exploration_id: str) -> HTMLResponse:
    html = explore.read_brief_html(exploration_id)
    if html is None:
        raise HTTPException(status_code=404, detail="Exploration brief not found")
    exploration = database.get_exploration(exploration_id) or {}
    return HTMLResponse(_issue_html_for_display(html, exploration.get("finished_at"), exploration=exploration))


@router.get("/explore/explorations/{exploration_id}/report")
def get_exploration_report(exploration_id: str) -> list[dict[str, Any]]:
    from backend.app.services.reporting import get_or_build_reporting_log
    existing = database.get_exploration(exploration_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Exploration not found")
    return get_or_build_reporting_log(exploration_id)


@router.post("/explore/explorations/{exploration_id}/rebuild", status_code=202)
async def rebuild_exploration(exploration_id: str, payload: ExplorationRebuildCreate) -> dict[str, Any]:
    if payload.topic_profile is not None:
        existing = database.get_exploration(exploration_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Exploration not found")
        profile_data = payload.topic_profile.model_dump()
        profile_data["topic_id"] = profile_data.get("topic_id") or existing["topic_id"]
        if str(profile_data["topic_id"]) != str(existing["topic_id"]):
            raise HTTPException(status_code=400, detail="Refined profile does not match this exploration")
        try:
            explore.save_topic_profile(profile_data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if payload.refinement_session_id:
            database.delete_refinement_session(payload.refinement_session_id)
    result = explore.start_rebuild(
        exploration_id,
        source_selection=payload.topic_profile.source_selection if payload.topic_profile is not None else payload.source_selection,
        candidate_limit=payload.candidate_limit,
        lookback_hours=payload.lookback_hours,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Exploration not found")
    return {"exploration": result}


@router.post("/explore/explorations/{exploration_id}/email")
def email_exploration_brief(exploration_id: str, payload: ExploreEmailCreate) -> dict[str, Any]:
    recipient = (payload.recipient_email or "").strip()
    if recipient and "@" not in recipient:
        raise HTTPException(status_code=400, detail="Enter a valid recipient email address")
    result = email_delivery.send_exploration_brief(
        exploration_id,
        recipient_email=recipient or None,
    )
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("error") or "Exploration not found")
    if result.get("status") == "failed":
        raise HTTPException(status_code=400, detail=result.get("error") or "Email delivery failed")
    return result


@router.post("/explore/explorations/{exploration_id}/foreign-article/translation")
async def foreign_article_translation_view(exploration_id: str, payload: ForeignArticleTranslationCreate) -> dict[str, Any]:
    exploration = database.get_exploration(exploration_id)
    if exploration is None:
        raise HTTPException(status_code=404, detail="Exploration not found")
    html = explore.read_brief_html(exploration_id)
    if html is None:
        raise HTTPException(status_code=404, detail="Exploration brief not found")
    if not _brief_contains_url(html, payload.url):
        raise HTTPException(status_code=403, detail="This article is not part of the saved brief")
    try:
        return await foreign_article_translation.translate_foreign_article(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/explore/explorations/{exploration_id}/re-enrich")
async def re_enrich_exploration(exploration_id: str) -> dict[str, Any]:
    try:
        updated = await explore.re_enrich_deterministic_articles(exploration_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Exploration not found")
        return {"status": "success", "exploration": updated}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/explorations/{exploration_id}/issues")
def admin_exploration_issues(exploration_id: str) -> dict[str, Any]:
    exploration = database.get_exploration(exploration_id)
    if exploration is None:
        raise HTTPException(status_code=404, detail="Exploration not found")
    issues = explore.exploration_build_issues(exploration)
    return {
        "exploration_id": exploration_id,
        "built_with_issues": bool(issues),
        "issues": issues,
    }


@router.post("/admin/web-search/credentials")
def save_web_search_credentials(payload: SourceSetupPayload) -> dict[str, Any]:
    return explore.save_web_search_credentials(provider=payload.provider, api_key=payload.api_key)


@router.post("/admin/youtube/credentials")
def save_youtube_credentials(payload: ApiKeyPayload) -> dict[str, Any]:
    return explore.save_youtube_credentials(api_key=payload.api_key)





@router.post("/admin/collections/setup")
def setup_collections() -> dict[str, Any]:
    return explore.setup_collections()


@router.get("/admin/library")
def admin_library() -> dict[str, Any]:
    database.purge_expired_deleted_explorations()
    return {
        "explorations": database.list_explorations(limit=200),
        "deleted_explorations": database.list_explorations(limit=200, only_deleted=True),
        "topics": database.list_topic_profiles(),
        "digests": [
            _scheduled_topic_profile_response(topic)
            for topic in database.list_scheduled_topic_profiles(include_paused=True)
        ],
        "legacy_digests": database.list_digests(include_archived=True),
    }


def _scheduled_topic_profile_response(topic: dict[str, Any]) -> dict[str, Any]:
    topic_id = str(topic["topic_id"])
    latest = database.get_latest_exploration(topic_id=topic_id, mode="scheduled")
    return {
        **topic,
        "latest_exploration": latest,
        "next_run_at": scheduler.next_topic_profile_run_at(topic, latest).isoformat(timespec="seconds"),
    }


@router.post("/digests", status_code=201)
def create_digest(payload: DigestCreate) -> dict[str, Any]:
    return database.create_digest(payload.model_dump())


@router.get("/digests/{digest_id}")
def get_digest(digest_id: str) -> dict[str, Any]:
    digest = database.get_digest(digest_id)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return digest


@router.patch("/digests/{digest_id}")
def update_digest(digest_id: str, payload: DigestUpdate) -> dict[str, Any]:
    digest = database.update_digest(digest_id, payload.model_dump(exclude_unset=True))
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return digest


@router.delete("/digests/{digest_id}")
def delete_digest(digest_id: str) -> dict[str, Any]:
    deleted = database.delete_digest(digest_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Digest not found")
    return {"digest_id": digest_id, "deleted": True}


@router.post("/digests/{digest_id}/run", status_code=202)
async def run_digest(
    digest_id: str,
    lookback_hours: int | None = Query(default=None, ge=1, le=brief_settings.MAX_LOOKBACK_HOURS),
) -> dict[str, Any]:
    try:
        run = await digest_runner.run_digest(digest_id, lookback_hours=lookback_hours)
    except digest_runner.DigestRunAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail="Digest run already in progress") from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return run


@router.get("/digests/{digest_id}/runs")
def digest_runs(digest_id: str) -> list[dict[str, Any]]:
    if database.get_digest(digest_id) is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return database.list_runs(digest_id)


@router.get("/digests/{digest_id}/issues/latest")
def latest_issue(digest_id: str) -> dict[str, Any]:
    if database.get_digest(digest_id) is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    issue = database.get_latest_issue(digest_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="No issue found for digest")
    return issue


@router.get("/issues/{issue_id}")
def issue(issue_id: str) -> dict[str, Any]:
    record = database.get_issue(issue_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return record


@router.get("/issues/{issue_id}/html", response_class=HTMLResponse)
def issue_html(issue_id: str) -> HTMLResponse:
    record = database.get_issue(issue_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return HTMLResponse(_issue_html_for_display(record.get("html_content") or "", record.get("created_at")))


@router.post("/feedback", status_code=201)
def create_feedback(payload: FeedbackCreate) -> dict[str, Any]:
    record = database.record_feedback(
        issue_id=payload.issue_id,
        url=payload.url,
        signal=payload.signal,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Article was not found for this brief")
    return record


@delivery_router.get("/brief", response_class=HTMLResponse, include_in_schema=False)
def latest_brief_html() -> HTMLResponse:
    digest = _canonical_digest()
    if digest is None:
        return HTMLResponse(_empty_brief_html("No active digest is configured yet."), status_code=404)

    issue = database.get_latest_issue(str(digest["id"]))
    if issue is None:
        return HTMLResponse(_empty_brief_html("No completed brief is available yet."), status_code=404)

    return HTMLResponse(_issue_html_for_display(issue.get("html_content") or "", issue.get("created_at")))


def _canonical_digest() -> dict[str, Any] | None:
    digests = database.list_digests()
    active_digests = [digest for digest in digests if (digest.get("status") or "active") == "active"]
    return (active_digests or digests or [None])[0]


def _empty_brief_html(message: str) -> str:
    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Morning Dispatch</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: #253333;
        background: #f4f6f1;
      }}
      main {{
        width: min(680px, calc(100% - 32px));
        border: 1px solid #dfe6dc;
        border-radius: 8px;
        padding: 28px;
        background: white;
      }}
      h1 {{ margin: 0 0 8px; font-size: 1.45rem; }}
      p {{ margin: 0; color: #66756f; line-height: 1.5; }}
    </style>
  </head>
  <body>
    <main>
      <h1>Morning Dispatch</h1>
      <p>{message}</p>
    </main>
  </body>
</html>
"""


def _issue_html_for_display(
    html: str,
    generated_at: str | None = None,
    *,
    exploration: dict[str, Any] | None = None,
) -> str:
    cleaned = database.clean_issue_html_for_display(html)
    cleaned = _rewrite_legacy_recency_hours(cleaned)
    cleaned = _rewrite_legacy_sidebar_labels(cleaned)
    cleaned = _rewrite_legacy_story_link_targets(cleaned)
    cleaned = _rewrite_legacy_image_strip_links(cleaned)
    cleaned = _remove_legacy_snapshot_paragraph(cleaned)
    cleaned = _rewrite_legacy_brief_title(cleaned, exploration=exploration)
    cleaned = _rewrite_legacy_search_strategy(cleaned, exploration=exploration)
    cleaned = _remove_legacy_issue_footer(cleaned)
    cleaned = _remove_legacy_ai_warning(cleaned)
    return _with_issue_overflow_guards(cleaned)


def _brief_contains_url(html: str, url: str) -> bool:
    escaped_url = (
        url.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return url in html or escaped_url in html


def _inject_requested_source_warning(html: str, exploration_id: str) -> str:
    warning = (
        '<aside class="requested-source-warning" '
        'style="margin:16px 0 24px;padding:12px 14px;border:1px solid #d9a441;'
        'background:#fff7df;font:800 0.82rem Arial,sans-serif;color:#5b3d00;">'
        f'<a href="/admin?tab=library&issue_run={exploration_id}" '
        'style="color:#173f63;text-decoration:underline;">'
        "Issue Built without request sources; click here for details"
        "</a></aside>"
    )
    if '<p class="snapshot">' in html:
        return html.replace('<p class="snapshot">', f'{warning}\n    <p class="snapshot">', 1)
    if "<main>" in html:
        return html.replace("<main>", f"<main>\n    {warning}", 1)
    return f"{warning}{html}"


def _rewrite_legacy_recency_hours(html: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        hours = int(match.group("hours"))
        return f'{match.group("prefix")}{_format_recency_value(hours)}{match.group("suffix")}'

    return re.sub(
        r'(?P<prefix><div class="side-stat"><span>Recency</span><strong class="side-value">)'
        r'(?P<hours>\d+)h'
        r'(?P<suffix></strong></div>)',
        replacement,
        html,
    )


def _rewrite_legacy_sidebar_labels(html: str) -> str:
    return html.replace(
        '<div class="side-stat"><span>Sources</span><strong class="side-value">',
        '<div class="side-stat"><span>Sources searched</span><strong class="side-value">',
    )


def _rewrite_legacy_story_link_targets(html: str) -> str:
    return re.sub(
        r'(<h[23] class="(?:lead-title|story-title)"><a href="[^"]+") target="_blank" rel="noreferrer"',
        r"\1",
        html,
    )


def _rewrite_legacy_image_strip_links(html: str) -> str:
    story_links = {
        match.group("title"): match.group("href")
        for match in re.finditer(
            r'<h[23] class="(?:lead-title|story-title)"><a href="(?P<href>[^"#][^"]*)"[^>]*>(?P<title>.*?)</a></h[23]>',
            html,
        )
    }
    if not story_links:
        return html

    def replacement(match: re.Match[str]) -> str:
        img_tag = match.group("img")
        alt = match.group("alt")
        href = story_links.get(alt)
        if not href:
            return match.group(0)
        return (
            '<figure class="strip-frame">\n'
            f'              <a class="strip-link" href="{href}">{img_tag}</a>\n'
            "            </figure>"
        )

    return re.sub(
        r'<figure class="strip-frame">\s*(?P<img><img [^>]*alt="(?P<alt>[^"]+)"[^>]*/>)\s*</figure>',
        replacement,
        html,
    )


def _remove_legacy_snapshot_paragraph(html: str) -> str:
    return re.sub(r'\s*<p class="snapshot">.*?</p>', "", html, flags=re.DOTALL)


def _rewrite_legacy_brief_title(html: str, *, exploration: dict[str, Any] | None = None) -> str:
    profile = _profile_for_exploration(exploration)
    compact_title = ""

    def replacement(match: re.Match[str]) -> str:
        nonlocal compact_title
        title = match.group("title")
        if "Morning Dispatch Issue" not in title and len(title.split()) <= 10:
            return match.group(0)
        seed = str(profile.get("scope") or profile.get("statement") or title) if profile else title
        keywords = tuple(str(keyword) for keyword in profile.get("keywords") or ()) if profile else ()
        compact_title = tight_brief_title(seed, keywords=keywords)
        return f"{match.group('prefix')}{compact_title}{match.group('suffix')}"

    rewritten = re.sub(
        r'(?P<prefix><section class="brief-header">\s*<div class="dateline">.*?</div>\s*<h1>)'
        r'(?P<title>.*?)'
        r'(?P<suffix></h1>)',
        replacement,
        html,
        flags=re.DOTALL,
    )
    if not compact_title:
        return rewritten
    return re.sub(r"<title>.*?</title>", f"<title>{compact_title}</title>", rewritten, count=1, flags=re.DOTALL)


def _remove_legacy_issue_footer(html: str) -> str:
    cleaned = re.sub(r'\s*<footer class="issue-footer">.*?</footer>', "", html, flags=re.DOTALL)
    cleaned = re.sub(r'\s*<style id="morning-dispatch-generated-footer-style">.*?</style>', "", cleaned, flags=re.DOTALL)
    return re.sub(r"\s*\.issue-footer\s*\{[^}]*\}", "", cleaned)


def _remove_legacy_ai_warning(html: str) -> str:
    return re.sub(
        r'\s*<p class="meta">AI warning: .*?</p>',
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _rewrite_legacy_search_strategy(html: str, *, exploration: dict[str, Any] | None = None) -> str:
    profile = _profile_for_exploration(exploration)

    def replacement(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        if not body.startswith("Focused on "):
            return match.group(0)
        statement = str(profile.get("scope") or profile.get("statement") or body)
        sources = selected_source_labels(
            profile.get("source_selection") if isinstance(profile.get("source_selection"), dict) else {}
        )
        if not sources and isinstance(exploration, dict):
            selection = exploration.get("source_selection")
            sources = selected_source_labels(selection if isinstance(selection, dict) else {})
        source_scope = _format_recency_value(int(profile.get("lookback_hours") or 168))
        source_scope = f"the last {source_scope}" if not source_scope.startswith("last ") else source_scope
        summary = summarize_search_strategy(
            statement=statement,
            sources=sources,
            source_scope=source_scope,
            exclusions=[str(item) for item in profile.get("exclusions") or ()],
            keywords=[str(item) for item in profile.get("keywords") or ()],
        )
        return f'{match.group("prefix")}{summary}{match.group("suffix")}'

    return re.sub(
        r'(?P<prefix><div class="side-note">\s*<h3>Search strategy</h3>\s*<p>)'
        r'(?P<body>.*?)'
        r'(?P<suffix></p>\s*</div>)',
        replacement,
        html,
        flags=re.DOTALL,
    )


def _profile_for_exploration(exploration: dict[str, Any] | None) -> dict[str, Any]:
    if not exploration:
        return {}
    topic_id = str(exploration.get("topic_id") or "")
    if not topic_id:
        return {}
    topic = database.get_topic_profile(topic_id)
    profile = topic.get("profile") if isinstance(topic, dict) else None
    return profile if isinstance(profile, dict) else {}


def _format_recency_value(lookback_hours: int) -> str:
    hours = max(1, int(lookback_hours))
    if hours < 24:
        return "1 hour" if hours == 1 else f"{hours} hours"
    if hours % 24:
        return f"{hours} hours"
    days = hours // 24
    if days < 14:
        return "1 day" if days == 1 else f"{days} days"
    if days >= 60:
        months = max(1, round(days / 30))
        return "1 month" if months == 1 else f"{months} months"
    if days % 7 == 0:
        weeks = days // 7
        return "1 week" if weeks == 1 else f"{weeks} weeks"
    return f"{days} days"


def _with_issue_overflow_guards(html: str) -> str:
    if not html:
        return html

    has_overflow_guard = "overflow-x: hidden" in html and "overflow-wrap: anywhere" in html
    has_modal_close_guard = "body:not(.modal-open) .podcast-modal" in html
    if has_overflow_guard and has_modal_close_guard:
        return html

    guard = """
  <style id="morning-dispatch-issue-overflow-guard">
    *, *::before, *::after { box-sizing: border-box; }
    html, body { width: 100%; max-width: 100%; overflow-x: hidden; }
    main { max-width: 100%; }
    img, video, iframe, table { max-width: 100%; }
    h1, h2, h3, p, a, .meta { overflow-wrap: anywhere; }
    .grid, .section, .article-card, .newsletter, .link-item { min-width: 0; }
    body:not(.modal-open) .podcast-modal { display: none !important; }
  </style>
"""
    if "</head>" in html:
        return html.replace("</head>", f"{guard}</head>", 1)
    return f"{guard}{html}"
