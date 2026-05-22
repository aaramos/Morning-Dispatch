from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from backend.agents.digestor.gmail import SCOPES
from backend.app.core.config import Settings, ensure_runtime_dirs, get_settings
from backend.app.db import database
from backend.app.services import mcp_status, model_catalog, model_jobs, scheduler, source_scout, verification

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
    return {
        "configured": settings.gmail_client_secret_path.exists(),
        "connected": settings.gmail_credentials_path.exists(),
        "client_secret_path": str(settings.gmail_client_secret_path),
        "credentials_path": str(settings.gmail_credentials_path),
        "scopes": SCOPES,
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
    digests = [scheduler.decorate_digest_overview(overview) for overview in database.list_digest_overviews()]
    model_cache = database.model_cache_summary()
    inference_metrics = database.inference_metrics_summary()
    agent_decisions = database.agent_decisions_summary()
    source_scout = database.source_scout_summary()
    return {
        "system": {
            "environment": settings.environment,
            "database_path": str(settings.database_path),
            "data_dir": str(settings.data_dir),
            "secrets_dir": str(settings.secrets_dir),
            "public_base_url": settings.public_base_url,
        },
        "delivery": _delivery_status(request, settings),
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
        ),
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
        },
        "mcp": mcp,
        "scheduler": scheduler_status,
        "digests": digests,
        "model_cache": model_cache,
        "inference_metrics": inference_metrics,
        "agent_decisions": agent_decisions,
        "source_scout": source_scout,
        "model_jobs": database.list_model_enrichment_jobs(limit=8),
    }


def _admin_health(
    *,
    gmail: dict[str, Any],
    model: dict[str, Any],
    mcp: dict[str, Any],
    scheduler_status: dict[str, Any],
    digests: list[dict[str, Any]],
    inference_metrics: dict[str, Any],
    source_scout: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, message: str) -> None:
        checks.append({"name": name, "status": status, "message": message})

    add(
        "Gmail",
        "ok" if gmail.get("configured") and gmail.get("connected") else "problem",
        "Connected and ready to read newsletters."
        if gmail.get("configured") and gmail.get("connected")
        else "Gmail login is not complete.",
    )
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
async def run_digest_verification(digest_id: str, publish: bool = False) -> dict[str, Any]:
    result = await verification.run_controlled_verification(digest_id, publish=publish)
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


def _delivery_status(request: Request, settings: Settings) -> dict[str, str]:
    base_url = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return {
        "latest_brief_path": "/brief",
        "latest_brief_url": f"{base_url}/brief",
    }


def _oauth_flow(
    settings: Settings,
    redirect_uri: str,
    state: str | None = None,
    code_verifier: str | None = None,
) -> Any:
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_secrets_file(
        str(settings.gmail_client_secret_path),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )


def _write_secret_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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
        flow.fetch_token(authorization_response=authorization_response)
        _write_secret_json(settings.gmail_credentials_path, json.loads(flow.credentials.to_json()))
        settings.gmail_oauth_state_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Gmail OAuth callback failed: %s", exc)
        raise HTTPException(status_code=400, detail="Gmail connection failed") from exc


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
