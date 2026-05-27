from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.agents.digestor import reddit_mcp_client
from backend.agents.digestor.base import NormalizedPayload, pii_filter
from backend.agents.librarian.text_utils import keyword_set
from backend.app.db import database

logger = logging.getLogger(__name__)

MAX_REDDIT_THREADS = 25
MAX_ACTIVE_SOURCES = 20
POSTS_PER_ACTIVE_SOURCE = 12
SEARCH_ONLY_LIMIT = 50
MIN_THREAD_SCORE = 0.30


@dataclass(frozen=True)
class RedditSource:
    subreddit: str
    state: str
    score: float
    category: str | None = None


async def fetch_reddit_threads(
    *,
    digest_id: str,
    digest_interest: str,
    lookback_hours: int,
    max_threads: int = MAX_REDDIT_THREADS,
) -> list[NormalizedPayload]:
    """Fetch Reddit threads from Source Scout-approved communities."""
    sources = _sources_for_digest(digest_id)
    if not sources:
        return []

    time_filter = _reddit_time_filter(lookback_hours)
    active_sources = [source for source in sources if source.state == "active"][:MAX_ACTIVE_SOURCES]
    search_only_sources = [source for source in sources if source.state == "search_only"]

    browse_tasks = [
        _browse_source(source, time_filter=time_filter)
        for source in active_sources
    ]
    search_task = _search_sources(search_only_sources, digest_interest, time_filter=time_filter)
    browse_results, search_results = await asyncio.gather(
        asyncio.gather(*browse_tasks) if browse_tasks else _empty_post_batches(),
        search_task,
    )

    source_by_name = {source.subreddit.lower(): source for source in sources}
    candidates: dict[str, tuple[float, NormalizedPayload]] = {}
    for post in [post for batch in browse_results for post in batch] + search_results:
        payload_score = _score_post(post, digest_interest, source_by_name)
        if payload_score < MIN_THREAD_SCORE:
            continue
        payload = _payload_from_post(post, payload_score)
        if not pii_filter(payload):
            continue
        key = _post_key(post, payload)
        existing = candidates.get(key)
        if existing is None or payload_score > existing[0]:
            candidates[key] = (payload_score, payload)

    ranked = sorted(candidates.values(), key=lambda item: item[0], reverse=True)
    return [payload for _score, payload in ranked[:max(1, max_threads)]]


def _sources_for_digest(digest_id: str) -> list[RedditSource]:
    rows = database.list_reddit_sources(digest_id, include_retired=False)
    sources: list[RedditSource] = []
    for row in rows:
        state = str(row.get("state") or "")
        if state not in {"active", "search_only"}:
            continue
        subreddit = str(row.get("subreddit") or "").strip()
        if not subreddit:
            continue
        sources.append(
            RedditSource(
                subreddit=subreddit,
                state=state,
                score=float(row.get("score") or 0),
                category=row.get("category"),
            )
        )
    return sources


async def _browse_source(source: RedditSource, *, time_filter: str) -> list[dict[str, Any]]:
    try:
        return await reddit_mcp_client.browse_subreddit(
            source.subreddit,
            sort="top",
            time=time_filter,
            limit=POSTS_PER_ACTIVE_SOURCE,
        )
    except Exception as exc:  # pragma: no cover - client already handles recoverable errors.
        logger.info("Reddit browse failed for r/%s: %s", source.subreddit, exc)
        return []


async def _search_sources(
    sources: list[RedditSource],
    digest_interest: str,
    *,
    time_filter: str,
) -> list[dict[str, Any]]:
    if not sources:
        return []
    query = _search_query(digest_interest)
    if not query:
        return []
    try:
        return await reddit_mcp_client.search_reddit(
            query,
            subreddits=[source.subreddit for source in sources],
            sort="relevance",
            time=time_filter,
            limit=SEARCH_ONLY_LIMIT,
        )
    except Exception as exc:  # pragma: no cover - client already handles recoverable errors.
        logger.info("Reddit search failed for Source Scout communities: %s", exc)
        return []


async def _empty_post_batches() -> list[list[dict[str, Any]]]:
    return []


def _score_post(
    post: dict[str, Any],
    digest_interest: str,
    source_by_name: dict[str, RedditSource],
) -> float:
    text = _post_text(post)
    tokens = keyword_set(text)
    interest_tokens = keyword_set(digest_interest)
    source = source_by_name.get(str(post.get("subreddit") or "").lower())
    source_score = source.score if source else 0.35

    if interest_tokens:
        overlap = len(tokens & interest_tokens) / max(1, len(interest_tokens))
        title_overlap = len(keyword_set(str(post.get("title") or "")) & interest_tokens) / max(
            1,
            len(keyword_set(str(post.get("title") or ""))) or 1,
        )
    else:
        overlap = 0.4
        title_overlap = 0.2

    comments = _float(post.get("num_comments"))
    score = _float(post.get("score"))
    engagement = min(1.0, ((comments / 60) + (score / 250)) / 2)
    state_bonus = 0.08 if source and source.state == "active" else 0.0
    raw = (0.42 * overlap) + (0.18 * title_overlap) + (0.18 * source_score) + (0.14 * engagement) + state_bonus
    if bool(post.get("stickied")) or bool(post.get("nsfw")):
        raw -= 0.35
    return round(max(0.0, min(raw, 1.0)), 3)


def _payload_from_post(post: dict[str, Any], score: float) -> NormalizedPayload:
    subreddit = str(post.get("subreddit") or "reddit").strip() or "reddit"
    title = _clean_text(str(post.get("title") or "Reddit thread"))
    permalink = str(post.get("permalink") or post.get("url") or "").strip()
    published_at = _published_at(post.get("created_utc"))
    author = str(post.get("author") or "unknown").strip()
    comments = int(_float(post.get("num_comments")))
    post_score = int(_float(post.get("score")))
    body = _clean_text(str(post.get("content") or ""))
    raw_text = "\n\n".join(
        part
        for part in (
            title,
            f"r/{subreddit} discussion by u/{author}. Score {post_score}; {comments} comment(s).",
            body,
        )
        if part
    )
    return NormalizedPayload(
        source_type="reddit_thread",
        source_name=f"r/{subreddit}",
        raw_text=raw_text,
        original_url=permalink or None,
        published_at=published_at,
        metadata={
            "reddit_thread_id": str(post.get("id") or ""),
            "subreddit": subreddit,
            "author": author,
            "title": title,
            "score": post_score,
            "num_comments": comments,
            "thread_quality_score": score,
            "link_flair_text": post.get("link_flair_text"),
        },
    )


def _search_query(digest_interest: str) -> str:
    tokens = [
        token
        for token in keyword_set(digest_interest)
        if token
        in {
            "agent",
            "agentic",
            "agents",
            "ai",
            "coding",
            "codex",
            "cursor",
            "gemini",
            "local",
            "llm",
            "llms",
            "mcp",
            "model",
            "models",
            "mlx",
            "ollama",
            "openai",
            "product",
            "workflow",
        }
    ]
    if not tokens:
        return "agentic AI local LLM"
    return " ".join(tokens[:8])


def _post_key(post: dict[str, Any], payload: NormalizedPayload) -> str:
    thread_id = str(post.get("id") or "").strip()
    if thread_id:
        return thread_id
    return str(payload.original_url or payload.metadata.get("title") or payload.id).lower()


def _post_text(post: dict[str, Any]) -> str:
    return " ".join(
        str(value)
        for value in (
            post.get("title"),
            post.get("content"),
            post.get("subreddit"),
            post.get("link_flair_text"),
        )
        if value
    )


def _reddit_time_filter(lookback_hours: int) -> str:
    if lookback_hours <= 1:
        return "hour"
    if lookback_hours <= 48:
        return "day"
    if lookback_hours <= 24 * 10:
        return "week"
    if lookback_hours <= 24 * 40:
        return "month"
    return "year"


def _published_at(created_utc: Any) -> str | None:
    timestamp = _float(created_utc)
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _clean_text(value: str) -> str:
    value = re.sub(r"https?://preview\\.redd\\.it/\\S+", "", value)
    value = re.sub(r"https?://i\\.redd\\.it/\\S+", "", value)
    value = re.sub(r"\\s+", " ", value)
    return value.strip()


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
