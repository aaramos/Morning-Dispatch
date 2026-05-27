from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from backend.agents.digestor.gmail import required_scopes
from backend.agents.digestor.podcast import discover_podcasts
from backend.app.core.config import Settings, ensure_runtime_dirs, get_settings
from backend.app.db import database
from backend.app.services import (
    email_delivery,
    mcp_status,
    model_catalog,
    model_jobs,
    model_routing,
    scheduler,
    secret_health,
    source_scout,
    verification,
)

logger = logging.getLogger(__name__)

TAILSCALE_NETWORKS = (
    ip_network("100.64.0.0/10"),
    ip_network("fd7a:115c:a1e0::/48"),
)


class ClientSecretPayload(BaseModel):
    client_secret_json: str = Field(min_length=20)


class OAuthCompletePayload(BaseModel):
    callback_url: str = Field(min_length=20)


class ModelJobPayload(BaseModel):
    model_name: str = Field(min_length=1, max_length=180)
    limit_count: int = Field(default=100, ge=1, le=1000)
    include_cached: bool = False


class ModelSelectionPayload(BaseModel):
    model_name: str = Field(min_length=1, max_length=180)


class ModelRoutePayload(BaseModel):
    provider: str = Field(default="local", max_length=40)
    model: str | None = Field(default=None, max_length=180)
    allow_private_cloud: bool = False


class ModelRoutesPayload(BaseModel):
    routes: dict[str, ModelRoutePayload] = Field(default_factory=dict)


class OllamaCloudCredentialsPayload(BaseModel):
    api_key: str = Field(min_length=1, max_length=1000)


class DeliverySettingsPayload(BaseModel):
    recipient_email: str = Field(default="", max_length=254)
    enabled: bool = False


class PodcastSourcePayload(BaseModel):
    type: str = Field(default="podcast_rss")
    title: str | None = Field(default=None, max_length=180)
    feed_url: str | None = Field(default=None, max_length=1200)
    site_url: str | None = Field(default=None, max_length=1200)
    author: str | None = Field(default=None, max_length=180)
    query: str | None = Field(default=None, max_length=220)
    aggregator: str | None = Field(default=None, max_length=80)
    transcription: str = Field(default="auto", max_length=40)


class PodcastCredentialsPayload(BaseModel):
    api_key: str = Field(min_length=1, max_length=1000)
    api_secret: str = Field(min_length=1, max_length=1000)


def require_admin_network(request: Request) -> None:
    host = request.client.host if request.client else ""
    try:
        client_ip = ip_address(host)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Admin API requires loopback or Tailscale access") from exc

    if client_ip.is_loopback or any(client_ip in network for network in TAILSCALE_NETWORKS):
        return
    raise HTTPException(status_code=403, detail="Admin API requires loopback or Tailscale access")


router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin_network)])


@router.get("/gmail/status")
def gmail_status(request: Request) -> dict[str, Any]:
    settings = _settings()
    redirect_uri = _callback_url(request, settings)
    redirect_warning = _redirect_warning(redirect_uri)
    token_scopes = email_delivery.gmail_token_scopes(settings)
    scopes = required_scopes(hosted_mcp_enabled=settings.gmail_remote_mcp_enabled)
    missing_scopes = sorted(set(scopes) - token_scopes) if settings.gmail_credentials_path.exists() else []
    cloud_scope = "https://www.googleapis.com/auth/cloud-platform"
    return {
        "configured": settings.gmail_client_secret_path.exists(),
        "connected": settings.gmail_credentials_path.exists(),
        "client_secret_path": str(settings.gmail_client_secret_path),
        "credentials_path": str(settings.gmail_credentials_path),
        "scopes": scopes,
        "token_scopes": sorted(token_scopes),
        "missing_scopes": missing_scopes,
        "requires_reconnect": bool(missing_scopes),
        "hosted_gmail_mcp_enabled": settings.gmail_remote_mcp_enabled,
        "hosted_gmail_mcp_ready": bool(
            settings.gmail_remote_mcp_enabled
            and settings.google_cloud_project_id
            and cloud_scope in token_scopes
        ),
        "google_cloud_project_id": settings.google_cloud_project_id,
        "redirect_uri": redirect_uri,
        "oauth_redirect_ready": redirect_warning is None,
        "redirect_warning": redirect_warning,
        "network": "loopback-or-tailscale",
    }


@router.get("/status")
async def admin_status(request: Request) -> dict[str, Any]:
    settings = _settings()
    catalog = await model_catalog.catalog_status(settings)
    mcp = await mcp_status.status(settings)
    gmail = gmail_status(request)
    scheduler_status = scheduler.status()
    secret_status = secret_health.status(settings)
    digests = [scheduler.decorate_digest_overview(overview) for overview in database.list_digest_overviews()]
    delivery_status = _delivery_status(request, settings, digests)
    podcast_status = _podcast_status(settings, digests)
    model_cache = database.model_cache_summary()
    inference_metrics = database.inference_metrics_summary()
    podcast_metrics = database.podcast_metrics_summary()
    agent_decisions = database.agent_decisions_summary()
    source_scout = database.source_scout_summary()
    fetch_failures = database.fetch_failure_breakdown(limit=5)
    brief_review = database.brief_review(limit=8)
    digest_stats = database.latest_digest_stats()
    return {
        "system": {
            "environment": settings.environment,
            "database_path": str(settings.database_path),
            "data_dir": str(settings.data_dir),
            "secrets_dir": str(settings.secrets_dir),
            "public_base_url": settings.public_base_url,
        },
        "delivery": delivery_status,
        "podcasts": podcast_status,
        "health": _admin_health(
            gmail=gmail,
            model={
                "enabled": settings.librarian_use_model,
                "model": settings.librarian_model,
                "api_key_configured": bool(settings.model_api_key),
                "catalog": catalog,
            },
            mcp=mcp,
            scheduler_status=scheduler_status,
            digests=digests,
            inference_metrics=inference_metrics,
            source_scout=source_scout,
            delivery=delivery_status,
            secret_status=secret_status,
        ),
        "secret_health": secret_status,
        "gmail": gmail,
        "model": {
            "enabled": settings.librarian_use_model,
            "model": settings.librarian_model,
            "base_url": settings.model_base_url,
            "api_key_configured": bool(settings.model_api_key),
            "max_items": settings.librarian_model_max_items,
            "selection_source": model_catalog.selected_model_source(settings),
            "settings_path": str(settings.model_settings_path),
            "catalog": catalog,
            "routing": model_routing.routes_status(settings),
        },
        "mcp": mcp,
        "scheduler": scheduler_status,
        "digests": digests,
        "model_cache": model_cache,
        "inference_metrics": inference_metrics,
        "podcast_metrics": podcast_metrics,
        "agent_decisions": agent_decisions,
        "source_scout": source_scout,
        "fetch_failures": fetch_failures,
        "brief_review": brief_review,
        "digest_stats": digest_stats,
        "model_jobs": database.list_model_enrichment_jobs(limit=8),
    }


@router.get("/secrets/status")
def secrets_status() -> dict[str, Any]:
    return secret_health.status(_settings())


def _admin_health(
    *,
    gmail: dict[str, Any],
    model: dict[str, Any],
    mcp: dict[str, Any],
    scheduler_status: dict[str, Any],
    digests: list[dict[str, Any]],
    inference_metrics: dict[str, Any],
    source_scout: dict[str, Any],
    delivery: dict[str, Any],
    secret_status: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, message: str) -> None:
        checks.append({"name": name, "status": status, "message": message})

    if not gmail.get("configured") or not gmail.get("connected"):
        add("Gmail", "problem", "Gmail login is not complete.")
    elif gmail.get("requires_reconnect"):
        add("Gmail", "warning", "Gmail needs a reconnect to grant the required permissions.")
    else:
        add("Gmail", "ok", "Connected and ready to read newsletters.")
    add(
        "Reddit",
        "ok" if (mcp.get("reddit") or {}).get("connected") else "warning",
        "Reddit MCP is connected."
        if (mcp.get("reddit") or {}).get("connected")
        else "Reddit MCP is not connected; community signals may be skipped.",
    )

    model_catalog = model.get("catalog") if isinstance(model.get("catalog"), dict) else {}
    model_ready = bool(model.get("enabled") and model.get("api_key_configured") and model_catalog.get("available"))
    add(
        "Local model",
        "ok" if model_ready else "problem",
        f"Using {model.get('model')}."
        if model_ready
        else "The local model is not fully available for AI enrichment.",
    )

    scheduler_ready = bool(scheduler_status.get("enabled") and scheduler_status.get("running"))
    add(
        "Scheduler",
        "ok" if scheduler_ready else "warning",
        "Morning run is scheduled."
        if scheduler_ready
        else "Scheduler is not running; manual runs still work.",
    )

    latest_digest = digests[0] if digests else {}
    latest_status = latest_digest.get("latest_run_status")
    latest_failures = int(latest_digest.get("latest_failed_count") or 0)
    latest_fallbacks = int(latest_digest.get("latest_fallback_count") or 0)
    if not latest_digest:
        add("Latest run", "warning", "No digest has run yet.")
    elif latest_status != "completed":
        add("Latest run", "problem", "The latest digest did not complete.")
    elif latest_failures or latest_fallbacks:
        add(
            "Latest run",
            "warning",
            f"Completed with {latest_failures} fetch failure(s) and {latest_fallbacks} fallback item(s).",
        )
    else:
        add("Latest run", "ok", "Latest digest completed cleanly.")

    recent_capacity_errors = sum(
        1 for row in inference_metrics.get("recent", []) if row.get("status") == "model_capacity"
    )
    if recent_capacity_errors:
        add(
            "Model capacity",
            "problem",
            f"Recent oMLX capacity errors: {recent_capacity_errors}. Reduce model size or cache pressure before sleeping.",
        )
    else:
        add("Model capacity", "ok", "No recent model-capacity errors recorded.")

    scout_run = source_scout.get("latest_run") if isinstance(source_scout.get("latest_run"), dict) else None
    if scout_run and scout_run.get("status") == "partial":
        add("Source Scout", "warning", "Last Reddit source review was partial; some communities were kept conservative.")
    elif scout_run and scout_run.get("status") == "completed":
        add("Source Scout", "ok", "Reddit source review completed.")
    else:
        add("Source Scout", "warning", "Reddit source review has not completed yet.")

    email_status = delivery.get("email") if isinstance(delivery.get("email"), dict) else {}
    if email_status.get("enabled") and email_status.get("gmail_send_ready"):
        add("Email delivery", "ok", "Morning digest email delivery is enabled.")
    elif email_status.get("enabled"):
        add("Email delivery", "warning", "Reconnect Gmail to grant send permission before email delivery works.")
    else:
        add("Email delivery", "warning", "Digest email delivery is not enabled yet.")

    secret_summary = secret_status.get("summary") if isinstance(secret_status.get("summary"), dict) else {}
    secret_warnings = int(secret_summary.get("warning_count") or 0)
    add(
        "Secrets",
        "ok" if secret_warnings == 0 else "warning",
        "Secrets are stored with owner-only app permissions."
        if secret_warnings == 0
        else f"Secret storage has {secret_warnings} item(s) to review.",
    )

    problem_count = sum(1 for check in checks if check["status"] == "problem")
    warning_count = sum(1 for check in checks if check["status"] == "warning")
    safe_for_overnight = problem_count == 0
    return {
        "status": "ready" if safe_for_overnight else "needs_attention",
        "safe_for_overnight": safe_for_overnight,
        "headline": "Ready for overnight run" if safe_for_overnight else "Needs attention before overnight run",
        "problem_count": problem_count,
        "warning_count": warning_count,
        "checks": checks,
    }


@router.get("/model/catalog")
async def get_model_catalog() -> dict[str, Any]:
    return await model_catalog.catalog_status(_settings())


@router.post("/model/selection")
async def select_model(payload: ModelSelectionPayload) -> dict[str, Any]:
    settings = _settings()
    selected_model = payload.model_name.strip()
    try:
        models = await model_catalog.fetch_available_models(settings)
    except model_catalog.ModelCatalogError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    model_ids = {str(model["id"]) for model in models}
    if selected_model not in model_ids:
        raise HTTPException(status_code=400, detail="Select a model currently available in oMLX")

    model_catalog.save_selected_model(settings, selected_model)
    updated_settings = _settings()
    return {
        "model": updated_settings.librarian_model,
        "selection_source": model_catalog.selected_model_source(updated_settings),
        "catalog": await model_catalog.catalog_status(updated_settings),
    }


@router.post("/model/routes")
def save_model_routes(payload: ModelRoutesPayload) -> dict[str, Any]:
    routes_payload = {agent: route.model_dump() for agent, route in payload.routes.items()}
    return model_routing.save_routes(_settings(), routes_payload)


@router.post("/model/ollama-cloud/credentials")
def save_ollama_cloud_credentials(payload: OllamaCloudCredentialsPayload) -> dict[str, Any]:
    return model_routing.save_ollama_api_key(_settings(), payload.api_key)


@router.get("/model/jobs")
def list_model_jobs() -> dict[str, Any]:
    return {
        "jobs": database.list_model_enrichment_jobs(limit=20),
        "inference_metrics": database.inference_metrics_summary(),
    }


@router.get("/agent-decisions")
def list_agent_decisions() -> dict[str, Any]:
    return {
        "decisions": database.list_agent_decisions(limit=40),
        "summary": database.agent_decisions_summary(),
    }


@router.get("/fetch-failures")
def list_fetch_failures() -> dict[str, Any]:
    return database.fetch_failure_breakdown(limit=25)


@router.get("/brief-review")
def get_brief_review() -> dict[str, Any]:
    return database.brief_review(limit=25)


@router.get("/digest-stats")
def get_digest_stats() -> dict[str, Any]:
    return database.latest_digest_stats()


@router.get("/podcasts/discover")
async def discover_podcast_sources(query: str = "", limit: int = 8) -> dict[str, Any]:
    settings = _settings()
    configured = bool(settings.podcastindex_api_key and settings.podcastindex_api_secret)
    if not configured:
        return {
            "configured": False,
            "results": [],
            "message": "Podcast Index credentials are not configured. Manual RSS feeds still work.",
        }
    results = await discover_podcasts(query, limit=limit)
    return {"configured": True, "results": results, "message": None}


@router.post("/podcasts/credentials")
def save_podcast_credentials(payload: PodcastCredentialsPayload) -> dict[str, Any]:
    api_key = payload.api_key.strip()
    api_secret = payload.api_secret.strip()
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="Podcast Index API key and secret are required")
    settings = _settings()
    _write_secret_text(settings.secrets_dir / "podcastindex" / "api_key", api_key)
    _write_secret_text(settings.secrets_dir / "podcastindex" / "api_secret", api_secret)
    refreshed_settings = _settings()
    digests = [scheduler.decorate_digest_overview(overview) for overview in database.list_digest_overviews()]
    return _podcast_status(refreshed_settings, digests)


@router.post("/digests/{digest_id}/podcast-sources")
def add_podcast_source(digest_id: str, payload: PodcastSourcePayload) -> dict[str, Any]:
    try:
        digest = database.add_podcast_source(digest_id, payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return {"digest": digest, "sources": database.list_podcast_sources(digest_id)}


@router.delete("/digests/{digest_id}/podcast-sources/{source_key}")
def remove_podcast_source(digest_id: str, source_key: str) -> dict[str, Any]:
    digest = database.remove_podcast_source(digest_id, source_key)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return {"digest": digest, "sources": database.list_podcast_sources(digest_id)}


@router.patch("/digests/{digest_id}/delivery")
def update_digest_delivery(digest_id: str, payload: DeliverySettingsPayload) -> dict[str, Any]:
    email = payload.recipient_email.strip()
    if payload.enabled and not _looks_like_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid recipient email address")
    updated = database.update_delivery_settings(
        digest_id=digest_id,
        recipient_email=email,
        enabled=payload.enabled,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return {
        **updated,
        **email_delivery.delivery_capability(_settings()),
    }


@router.post("/digests/{digest_id}/delivery/send-test")
def send_latest_digest_email(digest_id: str) -> dict[str, Any]:
    if database.get_digest(digest_id) is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    result = email_delivery.send_latest_digest(digest_id)
    if result.get("status") == "failed":
        raise HTTPException(status_code=400, detail=result.get("error") or "Email delivery failed")
    return result


@router.get("/source-scout")
def list_source_scout() -> dict[str, Any]:
    return {
        "summary": database.source_scout_summary(),
        "sources": database.list_reddit_sources(include_retired=True),
        "decisions": database.list_source_scout_decisions(limit=60),
    }


@router.post("/digests/{digest_id}/source-scout")
async def run_source_scout(digest_id: str, live_sample: bool = True) -> dict[str, Any]:
    result = await source_scout.run_source_scout(digest_id, live_sample=live_sample)
    if result is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return result


@router.post("/model/jobs")
async def start_model_job(payload: ModelJobPayload) -> dict[str, Any]:
    job = await model_jobs.start_model_enrichment_job(
        model_name=payload.model_name.strip(),
        limit_count=payload.limit_count,
        include_cached=payload.include_cached,
    )
    return job


@router.post("/digests/{digest_id}/verification-run")
async def run_digest_verification(
    digest_id: str,
    publish: bool = False,
    force_podcast_refresh: bool = False,
) -> dict[str, Any]:
    result = await verification.run_controlled_verification(
        digest_id,
        publish=publish,
        force_podcast_refresh=force_podcast_refresh,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return result


@router.post("/gmail/client-secret")
def save_client_secret(payload: ClientSecretPayload) -> dict[str, Any]:
    settings = _settings()
    try:
        client_config = json.loads(payload.client_secret_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Client secret must be valid JSON") from exc

    if not isinstance(client_config, dict) or not (client_config.get("installed") or client_config.get("web")):
        raise HTTPException(status_code=400, detail="Client secret JSON must contain an installed or web OAuth client")

    _write_secret_json(settings.gmail_client_secret_path, client_config)
    return {"configured": True, "client_secret_path": str(settings.gmail_client_secret_path)}


@router.post("/gmail/oauth/start")
def start_gmail_oauth(request: Request) -> dict[str, str]:
    settings = _settings()
    if not settings.gmail_client_secret_path.exists():
        raise HTTPException(status_code=400, detail="Upload a Google OAuth client secret before connecting Gmail")
    redirect_uri = _callback_url(request, settings)
    redirect_warning = _redirect_warning(redirect_uri)
    if redirect_warning:
        raise HTTPException(status_code=400, detail=redirect_warning)

    try:
        flow = _oauth_flow(settings, redirect_uri)
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
    except Exception as exc:
        logger.warning("Could not start Gmail OAuth flow: %s", exc)
        raise HTTPException(status_code=400, detail="Could not start Gmail OAuth flow") from exc

    _write_secret_json(
        settings.gmail_oauth_state_path,
        {
            "state": state,
            "code_verifier": flow.code_verifier,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    return {"authorization_url": authorization_url}


@router.get("/gmail/oauth/callback", response_class=HTMLResponse)
def gmail_oauth_callback(request: Request) -> HTMLResponse:
    settings = _settings()
    _finish_gmail_oauth(settings, request, str(request.url))

    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
          <head><meta charset="utf-8" /><title>Gmail Connected</title></head>
          <body style="font-family: system-ui; padding: 32px;">
            <h1>Gmail connected</h1>
            <p>Morning Dispatch saved the Gmail token. You can return to the admin screen.</p>
            <p><a href="/admin">Back to Admin</a></p>
          </body>
        </html>
        """
    )


@router.post("/gmail/oauth/complete")
def complete_gmail_oauth(payload: OAuthCompletePayload, request: Request) -> dict[str, bool]:
    settings = _settings()
    _finish_gmail_oauth(settings, request, payload.callback_url)
    return {"connected": True}


@router.post("/gmail/disconnect")
def disconnect_gmail() -> dict[str, bool]:
    settings = _settings()
    settings.gmail_credentials_path.unlink(missing_ok=True)
    settings.gmail_oauth_state_path.unlink(missing_ok=True)
    return {"connected": False}


def _settings() -> Settings:
    settings = get_settings()
    ensure_runtime_dirs(settings)
    settings.gmail_client_secret_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


def _callback_url(request: Request, settings: Settings) -> str:
    base_url = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base_url}/api/admin/gmail/oauth/callback"


def _delivery_status(request: Request, settings: Settings, digests: list[dict[str, Any]]) -> dict[str, Any]:
    base_url = (settings.public_base_url or str(request.base_url)).rstrip("/")
    digest_id = str(digests[0]["id"]) if digests else ""
    delivery_settings = database.get_delivery_settings(digest_id) if digest_id else {
        "digest_id": None,
        "recipient_email": "",
        "enabled": False,
        "last_delivery_status": None,
        "last_delivered_at": None,
        "last_error": None,
        "updated_at": None,
    }
    return {
        "latest_brief_path": "/brief",
        "latest_brief_url": f"{base_url}/brief",
        "email": {
            **delivery_settings,
            **email_delivery.delivery_capability(settings),
        },
    }


def _podcast_status(settings: Settings, digests: list[dict[str, Any]]) -> dict[str, Any]:
    digest_id = str(digests[0]["id"]) if digests else None
    return {
        "aggregator_configured": bool(settings.podcastindex_api_key and settings.podcastindex_api_secret),
        "transcription_configured": bool(settings.podcast_transcribe_command),
        "sources": database.list_podcast_sources(digest_id) if digest_id else [],
        "audio_cache_dir": str(settings.data_dir / "podcast-audio"),
        "transcript_cache_dir": str(settings.data_dir / "podcast-transcripts"),
    }


def _looks_like_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value))


def _oauth_flow(
    settings: Settings,
    redirect_uri: str,
    state: str | None = None,
    code_verifier: str | None = None,
) -> Any:
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_secrets_file(
        str(settings.gmail_client_secret_path),
        scopes=required_scopes(hosted_mcp_enabled=settings.gmail_remote_mcp_enabled),
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )


def _write_secret_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        logger.warning("Could not tighten permissions on %s", path.parent)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not tighten permissions on %s", path)


def _write_secret_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        logger.warning("Could not tighten permissions on %s", path.parent)
    path.write_text(value.strip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not tighten permissions on %s", path)


def _read_oauth_state(path: Path) -> str | None:
    payload = _read_oauth_state_payload(path)
    state = payload.get("state")
    return str(state) if state else None


def _read_oauth_state_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _finish_gmail_oauth(settings: Settings, request: Request, authorization_response: str) -> None:
    oauth_state = _read_oauth_state_payload(settings.gmail_oauth_state_path)
    expected_state = oauth_state.get("state")
    code_verifier = oauth_state.get("code_verifier")
    returned_state = _state_from_authorization_response(authorization_response)
    if not expected_state or returned_state != expected_state:
        raise HTTPException(status_code=400, detail="OAuth state did not match. Start the Gmail connection again.")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="OAuth session expired. Start the Gmail connection again.")

    try:
        redirect_uri = _callback_url(request, settings)
        flow = _oauth_flow(settings, redirect_uri, state=str(expected_state), code_verifier=str(code_verifier))
        _allow_private_http_redirect(redirect_uri)
        _fetch_token_accepting_extra_scopes(flow, authorization_response)
        credentials = flow.credentials
        _verify_gmail_credentials_scopes(credentials, settings)
        _write_secret_json(settings.gmail_credentials_path, json.loads(credentials.to_json()))
        settings.gmail_oauth_state_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Gmail OAuth callback failed: %s", exc)
        raise HTTPException(status_code=400, detail="Gmail connection failed") from exc


def _fetch_token_accepting_extra_scopes(flow: Any, authorization_response: str) -> None:
    previous_value = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow.fetch_token(authorization_response=authorization_response)
    finally:
        if previous_value is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous_value


def _verify_gmail_credentials_scopes(credentials: Any, settings: Settings) -> None:
    required = set(required_scopes(hosted_mcp_enabled=settings.gmail_remote_mcp_enabled))
    granted = _credential_scope_set(credentials)
    if granted and not required.issubset(granted):
        missing = sorted(required - granted)
        raise RuntimeError(f"Gmail did not grant required scope(s): {', '.join(missing)}")


def _credential_scope_set(credentials: Any) -> set[str]:
    raw_scopes = getattr(credentials, "granted_scopes", None) or getattr(credentials, "scopes", None) or []
    if isinstance(raw_scopes, str):
        return {scope for scope in raw_scopes.split() if scope}
    if isinstance(raw_scopes, list | tuple | set):
        return {str(scope) for scope in raw_scopes if scope}
    return set()


def _state_from_authorization_response(authorization_response: str) -> str | None:
    parsed = urlparse(authorization_response)
    query = parsed.query
    if not query:
        return None
    state = parse_qs(query).get("state", [None])[0]
    return str(state) if state else None


def _allow_private_http_redirect(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http":
        return
    host = parsed.hostname or ""
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".ts.net"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def _redirect_warning(redirect_uri: str) -> str | None:
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or ""
    if parsed.scheme == "https":
        try:
            ip_address(host)
        except ValueError:
            return None
        return "Google OAuth redirect URIs cannot use a raw IP address. Use the HTTPS MagicDNS name instead."

    if parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return None
    return "Google OAuth needs a localhost redirect or an HTTPS MagicDNS redirect URL."
