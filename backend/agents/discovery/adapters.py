from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.agents.digestor.gmail import fetch_newsletters
from backend.agents.digestor.podcast import fetch_podcast_episodes
from backend.agents.digestor.reddit import RedditSource, fetch_reddit_threads
from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.text_utils import STOPWORDS
from backend.agents.discovery.types import (
    AdapterUnavailable,
    Candidate,
    CostProfile,
    SourceAdapterContext,
    TopicProfile,
)
from backend.app.db import database
from backend.app.core.config import get_settings
from backend.agents.discovery.collections_source import search_collections, sync_collections
from backend.agents.discovery.markets import MarketSnapshot, fetch_market_snapshots, select_market_companies
from backend.agents.discovery.web_search import lookback_to_days, search_web
from backend.agents.discovery.youtube import fetch_youtube_transcript, search_youtube


class GmailSourceAdapter:
    name = "gmail"
    cost_profile = CostProfile(label="fast", timeout_seconds=30.0)
    good_for = ("newsletters", "primary_sources", "curated_links")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        # Strict allowlist: only senders explicitly approved in the managed store are ever fetched.
        senders = _approved_gmail_senders()
        if not senders:
            return []
        payloads = await fetch_newsletters(
            digest_id=context.exploration_id,
            sender_allowlist=senders,
            lookback_hours=context.lookback_hours,
            db_path=str(database.database_path()),
        )
        return [
            Candidate(
                adapter=self.name,
                payload=payload,
                score=_payload_score(payload.metadata),
                reason="Approved Gmail newsletter item.",
            )
            for payload in payloads
        ]

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


class RedditSourceAdapter:
    name = "reddit"
    cost_profile = CostProfile(label="medium", timeout_seconds=25.0)
    good_for = ("community_signal", "sentiment", "emerging_workflows")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        source_digest_id = _digest_id_with_reddit_sources()
        planned_sources = _reddit_sources_from_plan(profile)
        if source_digest_id is None and not planned_sources:
            return []
        try:
            payloads = await fetch_reddit_threads(
                digest_id=source_digest_id or context.exploration_id,
                digest_interest=profile.query_for_source(self.name),
                lookback_hours=context.lookback_hours or 24 * 365,
                sources_override=planned_sources or None,
            )
        except Exception as exc:
            fallback = await _reddit_web_fallback_candidates(profile, context, planned_sources, error=str(exc))
            if fallback:
                return fallback
            raise
        if not payloads:
            fallback = await _reddit_web_fallback_candidates(profile, context, planned_sources)
            if fallback:
                return fallback
            planned = ", ".join(f"r/{source.subreddit}" for source in planned_sources[:6])
            if planned:
                raise AdapterUnavailable(f"Reddit searched {planned} but no usable threads entered the candidate pool.")
            raise AdapterUnavailable("Reddit was selected but no usable threads entered the candidate pool.")
        return [
            Candidate(
                adapter=self.name,
                payload=payload,
                score=_payload_score(payload.metadata),
                reason="Reddit community signal.",
            )
            for payload in payloads
        ]

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


class PodcastSourceAdapter:
    name = "podcasts"
    cost_profile = CostProfile(label="slow", timeout_seconds=75.0)
    good_for = ("deep_context", "interviews", "expert_discussion")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        sources = _approved_podcast_sources()
        for ref in _podcast_discovery_refs(profile):
            sources.append({
                "type": "podcast_search",
                "title": ref,
                "query": ref,
                "aggregator": "podcastindex",
                "transcription": "auto",
            })
        payloads, _decisions = await fetch_podcast_episodes(
            digest_id=context.exploration_id,
            digest_interest=profile.query_for_source(self.name),
            sources=sources,
            lookback_hours=context.lookback_hours,
            inference_run_id=context.exploration_id,
            mark_seen=False,
            seen_requires_published=True,
            include_seen=True,
        )
        if not payloads:
            lanes = _source_plan_refs(profile, "podcasts")[:4]
            lane_text = ", ".join(lanes)
            if lane_text:
                raise AdapterUnavailable(
                    f"Podcast discovery searched {lane_text} but found no usable recent episodes with playable audio."
                )
            raise AdapterUnavailable("Podcast discovery found no usable recent episodes with playable audio.")
        return [
            Candidate(
                adapter=self.name,
                payload=payload,
                score=_payload_score(payload.metadata),
                reason="Podcast episode signal.",
            )
            for payload in payloads
        ]

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


class WebSearchSourceAdapter:
    name = "web_search"
    cost_profile = CostProfile(label="medium", timeout_seconds=20.0)
    good_for = ("breaking_news", "broad_discovery", "fresh_sources")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        queries = _web_search_queries(profile, _requested_refs(profile, "web_search"), adapter=self.name)
        per_query_limit = max(4, min(20, max(1, context.candidate_limit)))
        days = lookback_to_days(context.lookback_hours)
        results = await asyncio.gather(
            *(search_web(query, limit=per_query_limit, days=days) for query in queries),
            return_exceptions=True,
        )

        hits_by_url: dict[str, tuple[Any, str, int]] = {}
        errors: list[BaseException] = []
        for query_index, (query, result) in enumerate(zip(queries, results, strict=True)):
            if isinstance(result, BaseException):
                errors.append(result)
                continue
            for hit in result:
                if not hit.url:
                    continue
                key = _dedupe_url_key(hit.url)
                existing = hits_by_url.get(key)
                if existing is None or _search_score(hit.score) > _search_score(existing[0].score):
                    hits_by_url[key] = (hit, query, query_index)
        if not hits_by_url and errors:
            raise errors[0]
        if not hits_by_url:
            return []

        payloads = []
        ordered_hits = sorted(
            hits_by_url.values(),
            key=lambda item: (_web_query_boosted_score(item[0].score, item[2]), -item[2]),
            reverse=True,
        )
        for hit, query, query_index in ordered_hits[: max(1, context.candidate_limit)]:
            score = _web_query_boosted_score(hit.score, query_index)
            payloads.append(
                Candidate(
                    adapter=self.name,
                    payload=NormalizedPayload(
                        source_type="gmail_link",
                        source_name=hit.title or hit.url,
                        raw_text=hit.snippet,
                        original_url=hit.url,
                        published_at=hit.published_at,
                        metadata={
                            "link_quality_score": score,
                            "search_query": query,
                            "search_query_rank": query_index + 1,
                            "search_provider": hit.provider,
                        },
                    ),
                    score=score,
                    reason=f"Web result from {hit.provider}.",
                )
            )
        return payloads

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


class YouTubeSourceAdapter:
    name = "youtube"
    cost_profile = CostProfile(label="medium", timeout_seconds=45.0)
    good_for = ("deep_context", "interviews", "expert_discussion", "broad_discovery")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        settings = get_settings()
        queries = _web_search_queries(profile, _requested_refs(profile, "youtube"), adapter=self.name)[:6]
        if not queries:
            return []
        per_query_limit = max(1, min(12, settings.youtube_max_results, max(1, context.candidate_limit)))
        results = await asyncio.gather(
            *(
                search_youtube(
                    api_key=settings.youtube_api_key,
                    query=query,
                    limit=per_query_limit,
                    recency_weighting=profile.recency_weighting,
                    duration_filter=settings.youtube_duration_filter,
                    lookback_hours=context.lookback_hours,
                )
                for query in queries
            ),
            return_exceptions=True,
        )
        videos_by_id: dict[str, Any] = {}
        errors: list[BaseException] = []
        for result in results:
            if isinstance(result, BaseException):
                errors.append(result)
                continue
            for video in result.videos:
                videos_by_id.setdefault(video.video_id, video)
        if not videos_by_id and errors:
            raise errors[0]
        if not videos_by_id:
            return []
        videos = tuple(videos_by_id.values())[: max(1, min(context.candidate_limit, settings.youtube_max_results))]
        return await _youtube_candidates(videos)

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


class CollectionsSourceAdapter:
    name = "collections"
    cost_profile = CostProfile(label="fast", timeout_seconds=20.0)
    good_for = ("primary_sources", "curated_links", "deep_context")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        settings = get_settings()
        if settings.collections_root is None:
            raise AdapterUnavailable("Collections root is not configured.")
        summary = sync_collections(
            settings.collections_root,
            max_file_bytes=settings.collections_max_file_bytes,
        )
        if not summary.get("root_exists"):
            raise AdapterUnavailable("Create the Collections folder before using this source.")
        if int(summary.get("collection_count") or 0) <= 0:
            raise AdapterUnavailable("Add a top-level folder inside Collections before using this source.")

        requested = _requested_refs(profile, "collections")
        matches = search_collections(
            profile.query_for_source(self.name),
            collection_names=requested or None,
            limit=max(1, min(context.candidate_limit, settings.collections_max_results)),
        )
        return [_collection_candidate(match) for match in matches]

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


class MarketsSourceAdapter:
    name = "markets"
    cost_profile = CostProfile(label="medium", timeout_seconds=45.0)
    good_for = ("market_signal", "public_companies", "financial_context")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        settings = get_settings()
        companies = select_market_companies(
            profile,
            max_core=settings.markets_max_core_companies,
            max_related=settings.markets_max_related_companies,
        )
        if not companies:
            return []
        snapshots = await fetch_market_snapshots(companies)
        return [_market_candidate(snapshot) for snapshot in snapshots[: max(1, min(context.candidate_limit, 20))]]

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


def _approved_gmail_senders() -> list[str]:
    return database.approved_gmail_senders()


def _digest_id_with_reddit_sources() -> str | None:
    for digest in database.list_digests(include_archived=False):
        digest_id = str(digest.get("id") or "")
        if digest_id and database.list_reddit_sources(digest_id, include_retired=False):
            return digest_id
    return None


def _approved_podcast_sources() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for digest in database.list_digests(include_archived=False):
        for source in _sources(digest):
            if source.get("type") not in {"podcast_rss", "podcast_search"}:
                continue
            key = str(source.get("feed_url") or source.get("query") or source.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            records.append(dict(source))
            seen.add(key)
    return records


def _source_plan_refs(profile: TopicProfile, adapter: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for value in [*_requested_refs(profile, adapter), *profile.source_queries.get(adapter, ())]:
        ref = _trim_query(str(value or ""), limit=180)
        key = ref.casefold()
        if ref and key not in seen:
            refs.append(ref)
            seen.add(key)
    return refs


def _reddit_sources_from_plan(profile: TopicProfile) -> list[RedditSource]:
    sources: list[RedditSource] = []
    seen: set[str] = set()
    for ref in _source_plan_refs(profile, "reddit"):
        subreddit = _subreddit_from_ref(ref)
        if not subreddit:
            continue
        key = subreddit.lower()
        if key in seen:
            continue
        sources.append(RedditSource(subreddit=subreddit, state="search_only", score=0.62, category="strategy_plan"))
        seen.add(key)
        if len(sources) >= 20:
            break
    return sources


def _subreddit_from_ref(value: str) -> str:
    match = re.search(r"(?:^|\s)r/([A-Za-z0-9_]{2,40})\b", value)
    if match:
        return match.group(1)
    stripped = value.strip().strip("/")
    if re.fullmatch(r"[A-Za-z0-9_]{2,40}", stripped):
        return stripped
    return ""


def _sources(digest: dict[str, Any]) -> list[dict[str, Any]]:
    sources = digest.get("sources")
    return [source for source in sources if isinstance(source, dict)] if isinstance(sources, list) else []


def _requested_refs(profile: TopicProfile, adapter: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for source in profile.requested_sources:
        if str(source.get("adapter") or "").strip() != adapter:
            continue
        ref = str(source.get("ref") or source.get("source_name") or "").strip()
        key = ref.lower()
        if ref and key not in seen:
            refs.append(ref)
            seen.add(key)
    return refs


def _payload_score(metadata: dict[str, Any] | None) -> float:
    metadata = metadata or {}
    for key in (
        "link_quality_score",
        "thread_quality_score",
        "episode_quality_score",
        "youtube_quality_score",
        "collection_quality_score",
        "market_quality_score",
    ):
        try:
            return float(metadata[key])
        except (KeyError, TypeError, ValueError):
            continue
    return 0.5


async def _reddit_web_fallback_candidates(
    profile: TopicProfile,
    context: SourceAdapterContext,
    planned_sources: list[RedditSource],
    *,
    error: str | None = None,
) -> list[Candidate]:
    refs = _source_plan_refs(profile, "reddit") or [profile.query_for_source("reddit")]
    days = lookback_to_days(context.lookback_hours)
    queries: list[tuple[str, str]] = []
    for ref in refs[:6]:
        subreddit = _subreddit_from_ref(ref)
        clean_ref = re.sub(r"(?:^|\s)r/[A-Za-z0-9_]{2,40}\b", " ", ref).strip() or profile.statement
        if subreddit:
            queries.append((f"site:reddit.com/r/{subreddit} {clean_ref}", subreddit))
        else:
            queries.append((f"site:reddit.com/r/ {ref} reddit discussion", "reddit"))
    if not queries and planned_sources:
        queries = [(f"site:reddit.com/r/{source.subreddit} {profile.statement}", source.subreddit) for source in planned_sources[:6]]
    results = await asyncio.gather(
        *(search_web(query, limit=6, days=days) for query, _subreddit in queries),
        return_exceptions=True,
    )
    candidates: dict[str, Candidate] = {}
    for (query, planned_subreddit), result in zip(queries, results, strict=True):
        if isinstance(result, BaseException):
            continue
        for hit in result:
            if not _is_reddit_thread_url(hit.url):
                continue
            key = _dedupe_url_key(hit.url)
            subreddit = _subreddit_from_url(hit.url) or planned_subreddit
            score = max(0.46, min(0.82, _search_score(hit.score) * 0.9))
            payload = NormalizedPayload(
                source_type="reddit_thread",
                source_name=f"r/{subreddit}" if subreddit and subreddit != "reddit" else "Reddit",
                raw_text="\n\n".join(part for part in (hit.title, hit.snippet, hit.url) if part),
                original_url=hit.url,
                published_at=hit.published_at,
                metadata={
                    "thread_quality_score": score,
                    "title": hit.title,
                    "subreddit": subreddit,
                    "search_query": query,
                    "search_provider": hit.provider,
                    "fallback_source": "web_search",
                    "fallback_reason": error,
                },
            )
            candidate = Candidate(
                adapter="reddit",
                payload=payload,
                score=score,
                reason=f"Reddit thread discovered via {hit.provider} fallback.",
            )
            existing = candidates.get(key)
            if existing is None or candidate.score > existing.score:
                candidates[key] = candidate
    return sorted(candidates.values(), key=lambda candidate: candidate.score, reverse=True)[
        : max(1, min(context.candidate_limit, 20))
    ]


def _podcast_discovery_refs(profile: TopicProfile) -> list[str]:
    refs = list(_source_plan_refs(profile, "podcasts"))
    if not refs:
        refs.extend(_trim_query(query, limit=140) for query in profile.search_queries)
        if profile.keywords:
            refs.append(_trim_query(" ".join(profile.keywords[:8]), limit=140))
        refs.extend(
            _trim_query(value, limit=140)
            for value in (profile.scope, profile.statement, profile.query_for_source("podcasts"))
        )
    seen: set[str] = set()
    cleaned: list[str] = []
    for ref in refs:
        key = ref.casefold()
        if ref and key not in seen:
            cleaned.append(ref)
            seen.add(key)
    return cleaned[:8]


def _search_score(value: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.58
    return round(max(0.0, min(score, 0.98)), 3)


def _is_reddit_thread_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower()
    return host.endswith("reddit.com") and "/r/" in parsed.path.lower()


def _subreddit_from_url(url: str) -> str:
    match = re.search(r"/r/([^/\s]+)/", urlparse(str(url or "")).path, re.IGNORECASE)
    return match.group(1) if match else ""


async def _youtube_candidates(videos: tuple[Any, ...]) -> list[Candidate]:
    semaphore = asyncio.Semaphore(4)

    async def build(video: Any) -> Candidate | None:
        async with semaphore:
            transcript = await fetch_youtube_transcript(video.video_id)
        url = f"https://www.youtube.com/watch?v={video.video_id}"
        if transcript is None:
            return Candidate(
                adapter="youtube",
                payload=NormalizedPayload(
                    source_type="youtube_video",
                    source_name=video.channel_name,
                    raw_text=video.description,
                    original_url=url,
                    published_at=video.published_at,
                    metadata={
                        "youtube_quality_score": video.score,
                        "video_id": video.video_id,
                        "youtube_title": video.title,
                        "title": video.title,
                        "channel_name": video.channel_name,
                        "thumbnail_url": video.thumbnail_url,
                        "duration_seconds": video.duration_seconds,
                        "transcript_source": "unavailable",
                        "content_basis": "youtube_metadata",
                        "description": video.description,
                        "youtube_url": url,
                    },
                ),
                score=video.score,
                reason="YouTube metadata signal.",
            )
        return Candidate(
            adapter="youtube",
            payload=NormalizedPayload(
                source_type="youtube_video",
                source_name=video.channel_name,
                raw_text=transcript.text,
                original_url=url,
                published_at=video.published_at,
                metadata={
                    "youtube_quality_score": video.score,
                    "video_id": video.video_id,
                    "youtube_title": video.title,
                    "title": video.title,
                    "channel_name": video.channel_name,
                    "thumbnail_url": video.thumbnail_url,
                    "duration_seconds": video.duration_seconds,
                    "transcript_source": transcript.source,
                    "transcript_segments": list(transcript.segments),
                    "description": video.description,
                    "youtube_url": url,
                },
            ),
            score=video.score,
            reason="YouTube transcript signal.",
        )

    candidates = await asyncio.gather(*(build(video) for video in videos))
    return [candidate for candidate in candidates if candidate is not None]


def _collection_candidate(match: Any) -> Candidate:
    file_url = Path(match.file_path).expanduser().resolve().as_uri()
    title = f"{match.collection_name}: {match.relative_path}"
    return Candidate(
        adapter="collections",
        payload=NormalizedPayload(
            source_type="collection_chunk",
            source_name=match.collection_name,
            raw_text=match.text,
            original_url=file_url,
            metadata={
                "collection_quality_score": match.score,
                "collection_name": match.collection_name,
                "file_path": match.file_path,
                "relative_path": match.relative_path,
                "chunk_index": match.chunk_index,
                "matched_terms": list(match.matched_terms),
                "title": title,
            },
        ),
        score=match.score,
        reason="Local collection file match.",
    )


def _market_candidate(snapshot: MarketSnapshot) -> Candidate:
    title = f"{snapshot.company_name} ({snapshot.ticker})"
    score = 0.82 if snapshot.tier == "core" else 0.72
    if snapshot.change_30d_pct is not None:
        score += min(abs(snapshot.change_30d_pct) / 100, 0.08)
    score = round(min(score, 0.96), 3)
    return Candidate(
        adapter="markets",
        payload=NormalizedPayload(
            source_type="market_snapshot",
            source_name=title,
            raw_text=snapshot.summary_text(),
            original_url=snapshot.source_url,
            published_at=snapshot.fetched_at,
            metadata={
                "market_quality_score": score,
                "ticker": snapshot.ticker,
                "company_name": snapshot.company_name,
                "tier": snapshot.tier,
                "selection_rationale": snapshot.rationale,
                "current_price": snapshot.current_price,
                "currency": snapshot.currency,
                "market_cap": snapshot.market_cap,
                "change_1d_pct": snapshot.change_1d_pct,
                "change_7d_pct": snapshot.change_7d_pct,
                "change_30d_pct": snapshot.change_30d_pct,
                "change_3m_pct": snapshot.change_3m_pct,
                "price_history": list(snapshot.price_history),
                "analyst_rating": snapshot.analyst_rating,
                "sector": snapshot.sector,
                "industry": snapshot.industry,
                "recent_news": list(snapshot.recent_news),
                "title": title,
            },
        ),
        score=score,
        reason=f"{snapshot.tier.title()} public-market signal.",
    )


_WEB_QUERY_DROP_TERMS = STOPWORDS | {
    "advice",
    "advise",
    "area",
    "brief",
    "curate",
    "general",
    "good",
    "interest",
    "know",
    "like",
    "male",
    "might",
    "need",
    "old",
    "provide",
    "see",
    "things",
    "traveler",
    "traveling",
    "well",
    "year",
}


def _web_search_query(profile: TopicProfile, requested_refs: list[str], *, adapter: str = "web_search") -> str:
    agent_query = profile.query_for_source(adapter)
    if agent_query and (profile.search_queries or profile.source_queries):
        return _trim_query(" ".join([agent_query, *requested_refs]))

    raw = " ".join(
        str(part or "")
        for part in (
            profile.statement,
            profile.scope,
            *profile.subtopics,
            *profile.keywords,
            *requested_refs,
        )
    )
    normalized = _normalize_search_spelling(raw)
    terms: list[str] = []
    seen: set[str] = set()
    for match in re.findall(r"[a-z0-9][a-z0-9']+", normalized.lower()):
        if match in _WEB_QUERY_DROP_TERMS or len(match) <= 2:
            continue
        if match in seen:
            continue
        terms.append(match)
        seen.add(match)
        if len(terms) >= 24:
            break
    query = " ".join(terms).strip()
    return _trim_query(query or profile.scope or profile.statement)


def _web_search_queries(profile: TopicProfile, requested_refs: list[str], *, adapter: str = "web_search") -> list[str]:
    raw_queries: list[str] = []
    raw_queries.extend(profile.source_queries.get(adapter, ()))
    raw_queries.extend(profile.search_queries)
    for ref in requested_refs:
        raw_queries.append(ref)
        raw_queries.append(" ".join(part for part in (profile.scope or profile.statement, ref) if part))
    raw_queries.append(_web_search_query(profile, requested_refs, adapter=adapter))

    queries: list[str] = []
    seen: set[str] = set()
    for raw_query in raw_queries:
        query = _trim_query(_normalize_search_spelling(str(raw_query or "")), limit=340)
        key = query.casefold()
        if query and key not in seen:
            queries.append(query)
            seen.add(key)
        if len(queries) >= 20:
            break
    return queries or [_trim_query(profile.scope or profile.statement)]


def _web_query_boosted_score(value: float, query_index: int) -> float:
    return round(min(0.98, _search_score(value) + max(0.0, 0.06 - (query_index * 0.01))), 3)


def _dedupe_url_key(value: str) -> str:
    parsed = re.sub(r"#.*$", "", str(value or "").strip())
    return parsed.rstrip("/").lower()


def _normalize_search_spelling(value: str) -> str:
    replacements = {
        "musuems": "museums",
        "thorugh": "through",
    }
    lowered = value.lower()
    for typo, corrected in replacements.items():
        lowered = lowered.replace(typo, corrected)
    return lowered


def _trim_query(value: str, *, limit: int = 340) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    if len(cleaned) <= limit:
        return cleaned
    clipped = cleaned[:limit].rsplit(" ", 1)[0].strip()
    return clipped or cleaned[:limit].strip()
