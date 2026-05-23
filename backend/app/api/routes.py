from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from backend.app.core.config import get_settings
from backend.app.db import database
from backend.app.services import digest_runner

router = APIRouter(prefix="/api")
delivery_router = APIRouter()


class DigestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    interest: str = Field(min_length=1)
    schedule: Literal["hourly", "daily", "weekly", "monthly"] = "daily"
    sources: list[dict[str, Any]] = Field(default_factory=list)
    threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    profile_id: str | None = None


class DigestUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    interest: str | None = Field(default=None, min_length=1)
    schedule: Literal["hourly", "daily", "weekly", "monthly"] | None = None
    sources: list[dict[str, Any]] | None = None
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal["active", "paused", "archived"] | None = None


class FeedbackCreate(BaseModel):
    issue_id: str = Field(min_length=1)
    url: str = Field(min_length=8)
    signal: Literal["up", "down"]


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


@router.post("/digests/{digest_id}/run", status_code=202)
async def run_digest(digest_id: str) -> dict[str, Any]:
    run = await digest_runner.run_digest(digest_id)
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


def _issue_html_for_display(html: str, generated_at: str | None = None) -> str:
    cleaned = database.clean_issue_html_for_display(html)
    with_footer = database.ensure_generated_footer(cleaned, generated_at)
    return _with_issue_overflow_guards(with_footer)


def _with_issue_overflow_guards(html: str) -> str:
    if not html or ("overflow-x: hidden" in html and "overflow-wrap: anywhere" in html):
        return html

    guard = """
  <style id="morning-dispatch-issue-overflow-guard">
    *, *::before, *::after { box-sizing: border-box; }
    html, body { width: 100%; max-width: 100%; overflow-x: hidden; }
    main { max-width: 100%; }
    img, video, iframe, table { max-width: 100%; }
    h1, h2, h3, p, a, .meta { overflow-wrap: anywhere; }
    .grid, .section, .article-card, .newsletter, .link-item { min-width: 0; }
  </style>
"""
    if "</head>" in html:
        return html.replace("</head>", f"{guard}</head>", 1)
    return f"{guard}{html}"
