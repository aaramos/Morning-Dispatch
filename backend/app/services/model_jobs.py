from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any

from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.librarian.enrichment import enrich_article_with_model
from backend.agents.model import ModelClient, ModelClientConfig
from backend.app.core.config import get_settings
from backend.app.db import database

logger = logging.getLogger(__name__)
_RUNNING_TASKS: set[asyncio.Task[None]] = set()


async def start_model_enrichment_job(
    *,
    model_name: str,
    limit_count: int,
    include_cached: bool = False,
) -> dict[str, Any]:
    job = database.create_model_enrichment_job(
        model_name=model_name,
        limit_count=limit_count,
        include_cached=include_cached,
    )
    task = asyncio.create_task(_run_model_enrichment_job(str(job["id"])))
    _RUNNING_TASKS.add(task)
    task.add_done_callback(_RUNNING_TASKS.discard)
    return job


async def _run_model_enrichment_job(job_id: str) -> None:
    job = database.get_model_enrichment_job(job_id)
    if job is None:
        return

    started_at = database.utc_now()
    database.update_model_enrichment_job(job_id, status="running", started_at=started_at)
    settings = get_settings()
    if not settings.model_base_url:
        database.update_model_enrichment_job(
            job_id,
            status="failed",
            completed_at=database.utc_now(),
            error_detail="Model base URL is not configured.",
        )
        return

    client = ModelClient(
        ModelClientConfig(
            base_url=settings.model_base_url,
            model=str(job["model_name"]),
            api_key=settings.model_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            concurrency=settings.model_concurrency,
        )
    )
    candidates = database.list_model_enrichment_candidates(
        model_name=str(job["model_name"]),
        limit_count=int(job["limit_count"]),
        include_cached=bool(job["include_cached"]),
    )

    if not candidates:
        database.update_model_enrichment_job(
            job_id,
            status="completed",
            completed_at=database.utc_now(),
            processed_count=0,
            estimated_100_seconds=None,
        )
        return

    processed = 0
    success = 0
    failure = 0
    batch_started_at = monotonic()
    tasks = [
        asyncio.create_task(_enrich_one(job_id, client, result))
        for result in candidates
    ]

    try:
        for task in asyncio.as_completed(tasks):
            enriched = await task
            processed += 1
            if enriched.enrichment_source in {"model", "model_fallback"}:
                success += 1
                if enriched.enrichment_source == "model":
                    database.cache_model_enrichments([enriched], model_name=str(job["model_name"]))
            else:
                failure += 1
            average_ms = round(((monotonic() - batch_started_at) * 1000) / processed, 2)
            database.update_model_enrichment_job(
                job_id,
                processed_count=processed,
                success_count=success,
                failure_count=failure,
                avg_total_ms=average_ms,
                estimated_100_seconds=round((average_ms * 100) / 1000, 1) if average_ms else None,
            )
    except Exception as exc:  # pragma: no cover - defensive background task guard.
        logger.exception("Model enrichment job %s failed", job_id)
        for task in tasks:
            task.cancel()
        database.update_model_enrichment_job(
            job_id,
            status="failed",
            completed_at=database.utc_now(),
            error_detail=str(exc),
        )
        return

    database.update_model_enrichment_job(
        job_id,
        status="completed",
        processed_count=processed,
        success_count=success,
        failure_count=failure,
        completed_at=database.utc_now(),
    )


async def _enrich_one(job_id: str, client: ModelClient, result: ArticleFetchResult) -> ArticleFetchResult:
    return await enrich_article_with_model(
        result,
        model_client=client,
        metrics_context={
            "run_id": job_id,
            "article_id": _article_id(result),
            "backend": "omlx",
            "mode": "batch",
        },
    )


def _article_id(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    return str(metadata.get("article_id") or result.canonical_url or result.original_url)
