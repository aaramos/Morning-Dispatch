from __future__ import annotations

import asyncio
import logging
import re
import math
import time
import feedparser
from datetime import datetime, UTC, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.agents.discovery.reddit_expander import expand_reddit_targets, clean_subreddit_name

logger = logging.getLogger(__name__)

from backend.agents.digestor.gmail import fetch_newsletters
from backend.agents.digestor.podcast import fetch_podcast_episodes, fetch_subscribed_show_latest

# Audio-transcription budget for the first podcast discovery pass. Kept well under
# the adapter's 120s timeout so the lane returns partial results (transcript-feed /
# show-notes) rather than timing out to zero. The refined pass does no audio
# transcription (budget 0) so two passes stay within the adapter timeout.
_PODCAST_TRANSCRIPTION_BUDGET_SECONDS = 75.0
# Overall wall-clock budget for the podcast lane, kept under the adapter's 120s
# timeout so discovery/resolution returns partial results instead of being killed
# all-or-nothing by the runner's asyncio.wait_for.
_PODCAST_OVERALL_BUDGET_SECONDS = 100.0
# Minimum remaining time required to attempt the (expensive) refined second pass.
_PODCAST_REFINED_PASS_MIN_SECONDS = 30.0
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
from backend.agents.discovery.markets import (
    MarketSnapshot,
    fetch_market_snapshots,
    select_market_companies,
    fetch_sec_filings,
    fetch_fred_macro_data,
)
from backend.agents.discovery.web_search import SerperBackend, lookback_to_days, search_web
from backend.agents.discovery.youtube import (
    YOUTUBE_DAILY_QUOTA_UNITS,
    YOUTUBE_QUOTA_WARNING_UNITS,
    fetch_youtube_transcript,
    search_youtube,
)

_YOUTUBE_SEARCH_PASS_UNITS = 101


class GmailSourceAdapter:
    name = "gmail"
    cost_profile = CostProfile(label="fast", timeout_seconds=30.0)
    good_for = ("newsletters", "primary_sources", "curated_links")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        # Strict allowlist: only senders explicitly approved in the managed store are ever fetched.
        senders = _profile_gmail_senders(profile) or _approved_gmail_senders()
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





class PodcastSourceAdapter:
    name = "podcasts"
    cost_profile = CostProfile(label="slow", timeout_seconds=120.0)
    good_for = ("deep_context", "interviews", "expert_discussion")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        deadline = time.monotonic() + _PODCAST_OVERALL_BUDGET_SECONDS

        # Curated subscription model: when the user has confirmed shows, the brief
        # includes each subscribed show's LATEST episode (topic/recency gates do not
        # apply; only a show-level staleness cutoff), instead of episode-first search.
        subscribed = _subscribed_podcast_shows(profile)
        if subscribed:
            settings = get_settings()
            payloads, decisions = await fetch_subscribed_show_latest(
                subscribed,
                digest_id=context.exploration_id,
                staleness_days=int(getattr(settings, "podcast_staleness_days", 60) or 60),
                inference_run_id=context.exploration_id,
                transcription_budget_seconds=_PODCAST_TRANSCRIPTION_BUDGET_SECONDS,
                deadline=deadline,
            )
            if not payloads:
                stale_count = sum(1 for d in decisions if getattr(d, "decision", "") == "stale_show")
                raise AdapterUnavailable(
                    "Subscribed podcast shows had no episode within the staleness window."
                    if stale_count
                    else "Subscribed podcast shows returned no usable recent episodes with playable audio."
                )
            return [
                Candidate(
                    adapter=self.name,
                    payload=payload,
                    score=_payload_score(payload.metadata),
                    reason="Latest episode from a confirmed podcast subscription.",
                )
                for payload in payloads
            ]

        sources = _approved_podcast_sources()
        for ref in _podcast_discovery_refs(profile):
            sources.append({
                "type": "podcast_search",
                "title": ref,
                "query": ref,
                "aggregator": "podcastindex",
                "transcription": "auto",
            })
        payloads, decisions = await fetch_podcast_episodes(
            digest_id=context.exploration_id,
            digest_interest=profile.query_for_source(self.name),
            sources=sources,
            lookback_hours=context.lookback_hours,
            inference_run_id=context.exploration_id,
            mark_seen=False,
            seen_requires_published=True,
            include_seen=True,
            profile=profile,
            transcription_budget_seconds=_PODCAST_TRANSCRIPTION_BUDGET_SECONDS,
            deadline=deadline,
        )
        # Skip the expensive refined second pass when there is not enough time left;
        # returning the first pass's partial results beats a hard adapter timeout.
        if len(payloads) < 3 and (deadline - time.monotonic()) >= _PODCAST_REFINED_PASS_MIN_SECONDS:
            try:
                from backend.agents.discovery.query_refiner import refine_queries_for_adapter
                initial_queries = _podcast_discovery_refs(profile)
                refined_queries = await refine_queries_for_adapter(
                    adapter_name=self.name,
                    profile=profile,
                    initial_results=payloads,
                    initial_queries=initial_queries,
                    lookback_hours=context.lookback_hours,
                )
            except Exception as exc:
                logger.warning("Failed to refine queries for podcast adapter: %s", exc)
                refined_queries = []
            if refined_queries:
                refined_sources = []
                for ref in refined_queries:
                    refined_sources.append({
                        "type": "podcast_search",
                        "title": ref,
                        "query": ref,
                        "aggregator": "podcastindex",
                        "transcription": "auto",
                    })
                refined_payloads, refined_decisions = await fetch_podcast_episodes(
                    digest_id=context.exploration_id,
                    digest_interest=profile.query_for_source(self.name),
                    sources=refined_sources,
                    lookback_hours=context.lookback_hours,
                    inference_run_id=context.exploration_id,
                    mark_seen=False,
                    seen_requires_published=True,
                    include_seen=True,
                    profile=profile,
                    # Refined pass relies on transcript-feed / show-notes only so the
                    # two passes together stay within the adapter's 120s timeout.
                    transcription_budget_seconds=0.0,
                    deadline=deadline,
                )
                seen_episode_ids = {p.metadata.get("episode_id") or p.original_url for p in payloads if p.metadata}
                for rp in refined_payloads:
                    rp_id = rp.metadata.get("episode_id") or rp.original_url if rp.metadata else rp.original_url
                    if rp_id not in seen_episode_ids:
                        if rp.metadata is None:
                            rp.metadata = {}
                        rp.metadata["is_refined_query"] = True
                        payloads.append(rp)
                        seen_episode_ids.add(rp_id)
                decisions.extend(refined_decisions)

        if not payloads:
            lanes = _source_plan_refs(profile, "podcasts")[:4]
            lane_text = ", ".join(lanes)
            diagnostic = _podcast_diagnostic_summary(decisions)
            if lane_text:
                raise AdapterUnavailable(
                    f"Podcast discovery searched {lane_text} but found no usable recent episodes with playable audio."
                    + (f" {diagnostic}" if diagnostic else "")
                )
            raise AdapterUnavailable(
                "Podcast discovery found no usable recent episodes with playable audio."
                + (f" {diagnostic}" if diagnostic else "")
            )
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
        per_query_limit = max(4, min(25, max(1, context.candidate_limit)))
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

        # Calculate in-window dated hits count
        from backend.app.services.explore import _parse_datetime_hint
        cutoff = None
        if context.lookback_hours is not None:
            cutoff = datetime.now(UTC) - timedelta(hours=max(1, context.lookback_hours))
        
        in_window_count = 0
        for hit, _, _ in hits_by_url.values():
            dt = _parse_datetime_hint(hit.published_at)
            if dt is not None:
                if cutoff is None or dt >= cutoff:
                    in_window_count += 1

        target_yield = min(3, context.candidate_limit)
        if in_window_count < target_yield:
            try:
                from backend.agents.discovery.query_refiner import refine_queries_for_adapter
                refined_queries = await refine_queries_for_adapter(
                    adapter_name=self.name,
                    profile=profile,
                    initial_results=[item[0] for item in hits_by_url.values()],
                    initial_queries=queries,
                    lookback_hours=context.lookback_hours,
                )
            except Exception as exc:
                logger.warning("Failed to refine queries for web search adapter: %s", exc)
                refined_queries = []
            if refined_queries:
                refined_queries = refined_queries[:3]
                refined_results = await asyncio.gather(
                    *(search_web(query, limit=per_query_limit, days=days) for query in refined_queries),
                    return_exceptions=True,
                )
                for query_index, (query, result) in enumerate(zip(refined_queries, refined_results, strict=True)):
                    if isinstance(result, BaseException):
                        errors.append(result)
                        continue
                    for hit in result:
                        if not hit.url:
                            continue
                        key = _dedupe_url_key(hit.url)
                        existing = hits_by_url.get(key)
                        overall_index = query_index + len(queries)
                        if existing is None or _search_score(hit.score) > _search_score(existing[0].score):
                            hits_by_url[key] = (hit, query, overall_index)

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
            is_refined = query_index >= len(queries)
            metadata = {
                "link_quality_score": score,
                "search_query": query,
                "search_query_rank": query_index + 1,
                "search_provider": hit.provider,
                "title": hit.title or hit.url,
                "link_text": hit.title or hit.url,
            }
            if is_refined:
                metadata["is_refined_query"] = True

            payloads.append(
                Candidate(
                    adapter=self.name,
                    payload=NormalizedPayload(
                        source_type="gmail_link",
                        source_name=hit.title or hit.url,
                        raw_text=hit.snippet,
                        original_url=hit.url,
                        published_at=hit.published_at,
                        metadata=metadata,
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
        quota_used = _youtube_quota_units_used()
        if quota_used + _YOUTUBE_SEARCH_PASS_UNITS > YOUTUBE_DAILY_QUOTA_UNITS:
            raise AdapterUnavailable(
                f"YouTube daily quota is exhausted today ({quota_used}/{YOUTUBE_DAILY_QUOTA_UNITS} units used)."
            )

        degraded_for_quota = quota_used >= YOUTUBE_QUOTA_WARNING_UNITS
        max_initial_queries = 2 if degraded_for_quota else 6
        remaining_query_slots = max(0, (YOUTUBE_DAILY_QUOTA_UNITS - quota_used) // _YOUTUBE_SEARCH_PASS_UNITS)
        queries = _web_search_queries(profile, _requested_refs(profile, "youtube"), adapter=self.name)[
            : max(0, min(max_initial_queries, remaining_query_slots))
        ]
        if not queries:
            return []
        per_query_limit = max(1, min(12, settings.youtube_max_results, max(1, context.candidate_limit)))
        videos_by_id: dict[str, Any] = {}
        errors: list[BaseException] = []
        target_video_count = max(1, min(context.candidate_limit, settings.youtube_max_results, per_query_limit))
        for query in queries:
            if quota_used + _YOUTUBE_SEARCH_PASS_UNITS > YOUTUBE_DAILY_QUOTA_UNITS:
                break
            try:
                result = await search_youtube(
                    api_key=settings.youtube_api_key,
                    query=query,
                    limit=per_query_limit,
                    recency_weighting=profile.recency_weighting,
                    duration_filter=settings.youtube_duration_filter,
                    lookback_hours=context.lookback_hours,
                )
            except Exception as exc:
                errors.append(exc)
                if isinstance(exc, AdapterUnavailable):
                    break
                continue
            quota_used += max(0, int(result.quota_units or 0))
            for video in result.videos:
                videos_by_id.setdefault(video.video_id, video)
            if len(videos_by_id) >= target_video_count:
                break

        refined_video_ids: set[str] = set()
        if len(videos_by_id) < 3 and not degraded_for_quota and quota_used + _YOUTUBE_SEARCH_PASS_UNITS <= YOUTUBE_DAILY_QUOTA_UNITS:
            try:
                from backend.agents.discovery.query_refiner import refine_queries_for_adapter
                refined_queries = await refine_queries_for_adapter(
                    adapter_name=self.name,
                    profile=profile,
                    initial_results=list(videos_by_id.values()),
                    initial_queries=queries,
                    lookback_hours=context.lookback_hours,
                )
            except Exception as exc:
                logger.warning("Failed to refine queries for YouTube adapter: %s", exc)
                refined_queries = []
            if refined_queries:
                remaining_query_slots = max(0, (YOUTUBE_DAILY_QUOTA_UNITS - quota_used) // _YOUTUBE_SEARCH_PASS_UNITS)
                refined_queries = refined_queries[: max(0, min(2, remaining_query_slots))]
                refined_recency = profile.recency_weighting if context.lookback_hours is not None else "all_available"
                for query in refined_queries:
                    if quota_used + _YOUTUBE_SEARCH_PASS_UNITS > YOUTUBE_DAILY_QUOTA_UNITS:
                        break
                    try:
                        result = await search_youtube(
                            api_key=settings.youtube_api_key,
                            query=query,
                            limit=per_query_limit,
                            recency_weighting=refined_recency,
                            duration_filter="any",
                            lookback_hours=context.lookback_hours,
                        )
                    except Exception as exc:
                        errors.append(exc)
                        if isinstance(exc, AdapterUnavailable):
                            break
                        continue
                    quota_used += max(0, int(result.quota_units or 0))
                    for video in result.videos:
                        if video.video_id not in videos_by_id:
                            videos_by_id[video.video_id] = video
                            refined_video_ids.add(video.video_id)
                    if len(videos_by_id) >= target_video_count:
                        break

        if not videos_by_id and errors:
            raise errors[0]
        if not videos_by_id:
            return []
        videos = tuple(videos_by_id.values())[: max(1, min(context.candidate_limit, settings.youtube_max_results))]
        return await _youtube_candidates(videos, refined_video_ids=refined_video_ids)

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


def _youtube_quota_units_used() -> int:
    try:
        return int(database.youtube_quota_summary().get("units_used") or 0)
    except Exception as exc:
        logger.warning("Unable to read YouTube quota summary; proceeding without preflight: %s", exc)
        return 0


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
        candidates = []

        # 1. Market Snapshots
        if companies:
            snapshots = await fetch_market_snapshots(companies)
            for snapshot in snapshots:
                candidate = _market_candidate(snapshot)
                if snapshot.explicit_ticker:
                    candidate.payload.metadata["explicit_ticker"] = True
                candidates.append(candidate)

            # 2. SEC Filings
            sec_tasks = [
                asyncio.to_thread(fetch_sec_filings, company.ticker, company.company_name)
                for company in companies
            ]
            sec_results = await asyncio.gather(*sec_tasks, return_exceptions=True)
            for res in sec_results:
                if isinstance(res, list):
                    for filing in res:
                        candidates.append(_sec_filing_candidate(filing))

        # 3. FRED Macro Data
        if settings.fred_api_key:
            fred_data = await asyncio.to_thread(fetch_fred_macro_data, settings.fred_api_key)
            for series in fred_data:
                candidates.append(_fred_series_candidate(series))

        return candidates[:context.candidate_limit]

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


def _approved_gmail_senders() -> list[str]:
    return database.approved_gmail_senders()


def _profile_gmail_senders(profile: TopicProfile) -> list[str]:
    rules = profile.gmail_rules if isinstance(profile.gmail_rules, dict) else {}
    raw = rules.get("include_senders")
    if not isinstance(raw, (list, tuple, set)):
        return []
    senders: list[str] = []
    seen: set[str] = set()
    for sender in raw:
        value = str(sender or "").strip().lower()
        if "@" not in value or value in seen:
            continue
        senders.append(value)
        seen.add(value)
    return senders





def _subscribed_podcast_shows(profile: TopicProfile) -> list[dict[str, Any]]:
    """Confirmed podcast shows (with a feed URL) the user subscribed to.

    Drawn from requested_sources (explicit picker confirmations) and promoted_sources
    (feedback loop), deduped by feed URL. Only shows with a feed URL are usable for
    the latest-episode subscription model.
    """
    shows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in [*profile.requested_sources, *(getattr(profile, "promoted_sources", ()) or ())]:
        if not isinstance(source, dict):
            continue
        if str(source.get("adapter") or "").strip() != "podcasts":
            continue
        feed_url = str(source.get("feed_url") or "").strip()
        if not feed_url or feed_url.lower() in seen:
            continue
        seen.add(feed_url.lower())
        shows.append(
            {
                "feed_url": feed_url,
                "title": str(source.get("ref") or source.get("source_name") or "Podcast").strip(),
            }
        )
    return shows


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


def _podcast_diagnostic_summary(decisions: list[Any]) -> str:
    if not decisions:
        return ""

    diag_decision = next((d for d in decisions if getattr(d, "decision", "") == "diagnostics"), None)
    if diag_decision and hasattr(diag_decision, "metadata") and diag_decision.metadata:
        m = diag_decision.metadata
        parts = []
        if m.get("episode_pages_found"):
            parts.append(f"{m['episode_pages_found']} episode page(s) found")
        if m.get("low_relevance_rejects"):
            parts.append(f"{m['low_relevance_rejects']} low-relevance reject(s)")
        if m.get("feed_resolved"):
            parts.append(f"{m['feed_resolved']} feed(s) resolved")
        if m.get("episode_matched"):
            parts.append(f"{m['episode_matched']} episode(s) matched")
        if m.get("no_audio_rejects"):
            parts.append(f"{m['no_audio_rejects']} no-audio reject(s)")
        if m.get("date_rejects"):
            parts.append(f"{m['date_rejects']} date reject(s)")
        if m.get("feed_error"):
            parts.append(f"{m['feed_error']} feed error(s)")
        if m.get("already_seen"):
            parts.append(f"{m['already_seen']} previously shown episode(s)")
        if parts:
            return "Diagnostics: " + ", ".join(parts) + "."

    counts: dict[str, int] = {}
    for decision in decisions:
        key = str(getattr(decision, "decision", "") or "checked")
        counts[key] = counts.get(key, 0) + 1
    parts: list[str] = []
    if counts.get("watch"):
        parts.append(f"{counts['watch']} feed(s) discovered")
    if counts.get("feed_error"):
        parts.append(f"{counts['feed_error']} feed error(s)")
    if counts.get("skip"):
        parts.append(f"{counts['skip']} low-fit episode(s)")
    if counts.get("already_seen"):
        parts.append(f"{counts['already_seen']} previously shown episode(s)")
    return "Diagnostics: " + ", ".join(parts) + "." if parts else ""


def _source_plan_refs(profile: TopicProfile, adapter: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    # Promoted refs (item 6): sources/terms that previously produced included
    # content are folded back into query construction so the feedback loop is
    # acted on when picking new content — not just used as a late scoring boost.
    for value in [
        *_requested_refs(profile, adapter),
        *profile.source_queries.get(adapter, ()),
        *_promoted_refs(profile, adapter),
    ]:
        ref = _trim_query(str(value or ""), limit=180)
        key = ref.casefold()
        if ref and key not in seen:
            refs.append(ref)
            seen.add(key)
    return refs





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


def _promoted_refs(profile: TopicProfile, adapter: str) -> list[str]:
    """Refs from sources promoted by the feedback loop (item 6).

    These are shows/domains/subreddits/senders that previously contributed
    included content. Feeding them back into discovery queries lets every source
    learn from what performed, mirroring the original podcast-only behavior.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for source in getattr(profile, "promoted_sources", ()) or ():
        if not isinstance(source, dict):
            continue
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





def _podcast_discovery_refs(profile: TopicProfile) -> list[str]:
    refs: list[str] = []
    for value in [
        *profile.direct_episode_queries,
        *profile.related_episode_queries,
        *profile.source_queries.get("podcasts", ()),
        *_source_plan_refs(profile, "podcasts"),
        *profile.priority_terms,
        *profile.keywords[:8],
        *profile.search_queries[:4],
        profile.query_for_source("podcasts"),
        profile.scope,
        profile.statement,
    ]:
        refs.extend(_podcast_show_query_variants(value))
    seen: set[str] = set()
    cleaned: list[str] = []
    for ref in refs:
        key = ref.casefold()
        if ref and key not in seen:
            cleaned.append(ref)
            seen.add(key)
    return cleaned[:8]


def _podcast_show_query_variants(value: Any) -> list[str]:
    query = _trim_query(str(value or ""), limit=140)
    if not query or _looks_like_podcast_instruction_noise(query):
        return []
    variants: list[str] = []
    words = re.findall(r"[\wÀ-ÿ]+", query, flags=re.UNICODE)
    if 1 <= len(words) <= 7:
        variants.append(query)
    lowered = query.casefold()
    phrase_patterns = (
        (r"\bmexico city\b|\bcdmx\b", "Mexico City"),
        (r"\bsolo travel\b", "solo travel"),
        (r"\bstreet food\b", "street food"),
        (r"\btacos?\b", "tacos"),
        (r"\blocal culture\b", "local culture"),
    )
    for pattern, phrase in phrase_patterns:
        if re.search(pattern, lowered):
            variants.append(phrase)
    return variants


def _looks_like_podcast_instruction_noise(query: str) -> bool:
    lowered = query.casefold()
    noise_markers = (
        "ticker symbol",
        "foreign media must",
        "native-language quer",
        "must use native",
        "source selection",
    )
    if any(marker in lowered for marker in noise_markers):
        return True
    if "," in query and len(re.findall(r"[\wÀ-ÿ]+", query, flags=re.UNICODE)) > 6:
        return True
    return False


def _search_score(value: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.58
    return round(max(0.0, min(score, 0.98)), 3)





async def _youtube_candidates(videos: tuple[Any, ...], refined_video_ids: set[str] | None = None) -> list[Candidate]:
    semaphore = asyncio.Semaphore(4)

    async def build(video: Any) -> Candidate | None:
        async with semaphore:
            transcript = await fetch_youtube_transcript(video.video_id)
        url = f"https://www.youtube.com/watch?v={video.video_id}"
        is_refined = refined_video_ids is not None and video.video_id in refined_video_ids
        metadata = {
            "youtube_quality_score": video.score,
            "video_id": video.video_id,
            "youtube_title": video.title,
            "title": video.title,
            "channel_name": video.channel_name,
            "thumbnail_url": video.thumbnail_url,
            "duration_seconds": video.duration_seconds,
            "transcript_source": "unavailable" if transcript is None else transcript.source,
            "description": video.description,
            "youtube_url": url,
        }
        if transcript is None:
            metadata["content_basis"] = "youtube_metadata"
        else:
            metadata["transcript_segments"] = list(transcript.segments)
        if is_refined:
            metadata["is_refined_query"] = True

        raw_text = video.description if transcript is None else transcript.text
        reason = "YouTube metadata signal." if transcript is None else "YouTube transcript signal."

        return Candidate(
            adapter="youtube",
            payload=NormalizedPayload(
                source_type="youtube_video",
                source_name=video.channel_name,
                raw_text=raw_text,
                original_url=url,
                published_at=video.published_at,
                metadata=metadata,
            ),
            score=video.score,
            reason=reason,
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


def _sec_filing_candidate(filing: dict[str, Any]) -> Candidate:
    title = f"SEC Filing: {filing['company_name']} ({filing['ticker']}) - {filing['form_label']}"
    raw_text = (
        f"SEC corporate filing for {filing['company_name']} ({filing['ticker']}). "
        f"Form: {filing['form']}. Filing Date: {filing['filing_date']}. "
        f"Description: {filing['description']}."
    )
    return Candidate(
        adapter="markets",
        payload=NormalizedPayload(
            source_type="sec_filing",
            source_name=title,
            raw_text=raw_text,
            original_url=filing["url"],
            published_at=filing["filing_date"],
            metadata={
                "ticker": filing["ticker"],
                "company_name": filing["company_name"],
                "form": filing["form"],
                "filing_date": filing["filing_date"],
                "description": filing["description"],
                "title": title,
            }
        ),
        score=0.85,
        reason="Corporate SEC filing event.",
    )


def _fred_series_candidate(series: dict[str, Any]) -> Candidate:
    title = f"Macro Indicator: {series['label']} ({series['series_id']})"
    raw_text = (
        f"Macroeconomic indicator {series['label']} ({series['series_id']}). "
        f"Latest Value: {series['current_value']} as of {series['current_date']}. "
        f"1-period change: {series['change_1period']:+.4f}."
    )
    return Candidate(
        adapter="markets",
        payload=NormalizedPayload(
            source_type="fred_series",
            source_name=title,
            raw_text=raw_text,
            original_url=series["url"],
            published_at=series["current_date"],
            metadata={
                "series_id": series["series_id"],
                "label": series["label"],
                "current_value": series["current_value"],
                "current_date": series["current_date"],
                "change_1period": series["change_1period"],
                "history": list(series["history"]),
                "title": title,
            }
        ),
        score=0.88,
        reason="FRED macroeconomic indicator snapshot.",
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


class RedditSourceAdapter:
    name = "reddit"
    cost_profile = CostProfile(label="medium", timeout_seconds=45.0)
    good_for = ("community_discussion", "emerging_topics", "expert_opinion", "broad_discovery")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        settings = get_settings()
        min_post_score = getattr(settings, "reddit_min_post_score", 10)
        limit_per_source = getattr(settings, "reddit_fetch_limit_per_source", 25)
        comments_limit = getattr(settings, "reddit_fetch_comments", 10)
        request_timeout = getattr(settings, "reddit_request_timeout_seconds", 10.0)
        reddit_candidate_cap = min(max(1, context.candidate_limit), 100)

        # 1. Expand search targets
        targets = await expand_reddit_targets(profile)

        # Determine lookback cutoff
        cutoff = None
        if context.lookback_hours is not None:
            cutoff = datetime.now(UTC) - timedelta(hours=max(1, context.lookback_hours))

        days = lookback_to_days(context.lookback_hours)

        # Requested subreddits for scoring boost
        requested_subs = {
            clean_subreddit_name(src.get("ref") or src.get("source_name"))
            for src in profile.requested_sources
            if isinstance(src, dict) and src.get("adapter") == "reddit"
        }
        requested_subs = {s for s in requested_subs if s}

        # Shared client and semaphores
        # Post list fetching semaphore (max 4 concurrent)
        fetch_semaphore = asyncio.Semaphore(4)
        # Comment fetching semaphore (max 2 concurrent)
        comments_semaphore = asyncio.Semaphore(2)

        ua = "MorningDispatchScout/1.0 (contact: scout@morningdispatch.com)"
        headers = {
            "User-Agent": ua,
            "Accept": "application/xml, application/rss+xml, text/xml, */*",
        }

        import httpx

        async def fetch_subreddit_rss(client: httpx.AsyncClient, sub: str) -> list[dict[str, Any]]:
            urls = [f"https://www.reddit.com/r/{sub}/hot/.rss"]
            if context.lookback_hours is not None:
                urls.append(f"https://www.reddit.com/r/{sub}/new/.rss")

            async def fetch_one(url: str) -> list[dict[str, Any]]:
                async with fetch_semaphore:
                    try:
                        logger.info("Fetching subreddit RSS: %s", url)
                        response = await client.get(url, headers=headers, timeout=request_timeout)
                        if response.status_code != 200:
                            logger.warning("Reddit RSS returned status %d for %s", response.status_code, url)
                            return []

                        feed = feedparser.parse(response.text)
                        posts = []
                        for entry in feed.entries:
                            link = entry.get("link", "")
                            match = re.search(r"/comments/([a-z0-9]+)/", link)
                            post_id = match.group(1) if match else str(hash(link))
                            title = entry.get("title", "Reddit Post")

                            published_at = None
                            # Reddit's Atom feed usually exposes <updated> (updated_parsed)
                            # and not always <published>; fall back so hot posts carry a
                            # date and the recency gate can actually filter them.
                            date_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                            if date_struct:
                                try:
                                    published_at = datetime(*date_struct[:6], tzinfo=UTC).isoformat(timespec="seconds")
                                except Exception:
                                    pass

                            posts.append({
                                "id": post_id,
                                "title": title,
                                "selftext": entry.get("content", [{"value": entry.get("summary", "")}])[0].get("value", ""),
                                "url": link,
                                "permalink": link,
                                "score": 10,  # fallback score
                                "upvote_ratio": 1.0,
                                "num_comments": 0,
                                "flair": None,
                                "subreddit": sub,
                                "published_at": published_at,
                                "is_self": True,
                                "fetch_mode": "subreddit",
                            })
                        return posts
                    except Exception as exc:
                        logger.warning("Exception fetching RSS url %s: %s", url, exc)
                        return []

            results = await asyncio.gather(*(fetch_one(url) for url in urls), return_exceptions=True)
            merged = {}
            for res in results:
                if isinstance(res, Exception):
                    logger.warning("Exception during Reddit RSS fetch gather for r/%s: %s", sub, res)
                    continue
                for post in res:
                    pid = post["id"]
                    if pid not in merged:
                        merged[pid] = post
            return list(merged.values())

        async def fetch_search_via_web(q: str) -> list[dict[str, Any]]:
            try:
                logger.info("Searching Reddit via web search: %s", q)
                query = f"site:reddit.com {q}"
                hits = await search_web(query, limit=10, days=days)
                
                posts = []
                for hit in hits:
                    url = hit.url
                    match = re.search(r"/r/([a-zA-Z0-9_]+)/comments/([a-z0-9]+)/", url)
                    if match:
                        sub = match.group(1)
                        post_id = match.group(2)
                    else:
                        match_short = re.search(r"/comments/([a-z0-9]+)/", url)
                        if match_short:
                            sub = "reddit"
                            post_id = match_short.group(1)
                        else:
                            continue
                            
                    posts.append({
                        "id": post_id,
                        "title": hit.title,
                        "selftext": hit.snippet,
                        "url": url,
                        "permalink": url,
                        "score": int(hit.score * 100) if hit.score else 10,
                        "upvote_ratio": 1.0,
                        "num_comments": 0,
                        "flair": None,
                        "subreddit": sub,
                        "published_at": hit.published_at,
                        "is_self": True,
                        "fetch_mode": "search",
                    })
                return posts
            except Exception as exc:
                logger.warning("Exception searching Reddit via web for query %s: %s", q, exc)
                return []

        async def fetch_comments_for_post(client: httpx.AsyncClient, sub: str, post_id: str) -> tuple[str, str | None]:
            """Fetch a post's comment thread and derive a recency date for it.

            Returns (comments_text, thread_date_iso). Reddit's authoritative JSON
            (created_utc) is 403-blocked for unauthenticated requests, but the comment
            thread's Atom feed (already fetched here) is allowed. Feed-level updated
            timestamps can be misleading for recency, so derive the thread date from
            the earliest usable comment timestamp when possible. Fails open to
            (text, None).
            """
            url = f"https://www.reddit.com/r/{sub}/comments/{post_id}.rss"
            async with comments_semaphore:
                try:
                    response = await client.get(url, headers=headers, timeout=request_timeout)
                    if response.status_code != 200:
                        logger.warning("Comments RSS returned status %d for post %s, continuing without comments", response.status_code, post_id)
                        return "", None

                    feed = feedparser.parse(response.text)
                    thread_date: str | None = None
                    comment_dates: list[datetime] = []
                    for entry in feed.entries:
                        date_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                        if not date_struct:
                            continue
                        try:
                            comment_dates.append(datetime(*date_struct[:6], tzinfo=UTC))
                        except Exception:
                            continue
                    date_value: datetime | None = min(comment_dates) if comment_dates else None
                    if date_value is None:
                        date_struct = feed.feed.get("published_parsed") or feed.feed.get("updated_parsed")
                        if date_struct:
                            try:
                                date_value = datetime(*date_struct[:6], tzinfo=UTC)
                            except Exception:
                                date_value = None
                    if date_value is not None:
                        thread_date = date_value.isoformat(timespec="seconds")
                    lines = ["--- Top Comments ---"]
                    count = 0
                    for entry in feed.entries:
                        author = entry.get("author") or "anonymous"
                        if author.startswith("/u/"):
                            author = author[3:]
                        elif author.startswith("u/"):
                            author = author[2:]
                        
                        if author.lower() in {"automoderator", "moderator"}:
                            continue
                            
                        html_content = ""
                        if "content" in entry and entry.content:
                            html_content = entry.content[0].value
                        else:
                            html_content = entry.get("summary") or ""
                            
                        if not html_content:
                            continue
                            
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(html_content, "html.parser")
                        body = soup.get_text().strip()
                        body = " ".join(body.split())
                        
                        if not body:
                            continue

                        lines.append(f"[{author}]: {body}")
                        count += 1
                        if count >= comments_limit:
                            break
                    
                    comments_text = "\n".join(lines) if len(lines) > 1 else ""
                    return comments_text, thread_date
                except Exception as exc:
                    logger.warning("Exception fetching comments RSS for post %s: %s", post_id, exc)
                    return "", None

        # Run initial fetch. The client-level timeout is a safety net so one stuck
        # connection cannot hold the stage's asyncio.gather open indefinitely (P9);
        # individual reddit requests pass a stricter per-request timeout.
        reddit_timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(http2=True, follow_redirects=True, timeout=reddit_timeout) as client:
            subreddit_tasks = [fetch_subreddit_rss(client, sub) for sub in targets.subreddits]
            search_tasks = [fetch_search_via_web(q) for q in targets.search_queries]

            results = await asyncio.gather(*(subreddit_tasks + search_tasks), return_exceptions=True)

            # Aggregate and deduplicate by permalink URL
            seen_permalinks = set()
            raw_candidates = []
            for res in results:
                if isinstance(res, list):
                    for post in res:
                        permalink = post["permalink"]
                        # Build absolute permalink URL
                        discussion_url = permalink
                        if permalink.startswith("/"):
                            discussion_url = "https://www.reddit.com" + permalink

                        # Deduplicate
                        if discussion_url.lower() in seen_permalinks:
                            continue
                        seen_permalinks.add(discussion_url.lower())

                        # Apply minimum post score filter
                        if post["score"] < min_post_score:
                            continue

                        # Apply lookback cutoff filter
                        if cutoff is not None and post["published_at"]:
                            try:
                                pub_dt = datetime.fromisoformat(post["published_at"])
                                if pub_dt < cutoff:
                                    continue
                            except Exception:
                                pass

                        post["discussion_url"] = discussion_url
                        raw_candidates.append(post)

            # Sort raw candidates preliminarily to select the top candidate_limit
            # before fetching comments, to conserve API requests.
            # Base log scaling for score:
            for post in raw_candidates:
                log_score = math.log10(max(1, post["score"]))
                norm_score = 0.15 + (min(3.0, max(0.0, log_score - 1.0)) / 3.0) * 0.60
                norm_ratio = max(0.0, min(1.0, post["upvote_ratio"] or 0.8)) * 0.18
                score = norm_score + norm_ratio

                # Small boosts
                sub_lower = str(post["subreddit"]).lower()
                boost = 0.0
                if sub_lower in requested_subs:
                    boost += 0.05
                if sub_lower in targets.subreddits:
                    boost += 0.02
                if post["fetch_mode"] == "subreddit":
                    boost += 0.02

                post["prelim_score"] = min(0.98, score + boost)

            raw_candidates.sort(key=lambda x: x["prelim_score"], reverse=True)
            target_raw = raw_candidates[:reddit_candidate_cap]

            # Fetch comments in parallel for selected posts. Each fetch also returns the
            # thread's recency date so search-path posts (which arrive without one) get a
            # usable timestamp instead of being dropped downstream as "undated".
            comment_tasks = [fetch_comments_for_post(client, post["subreddit"], post["id"]) for post in target_raw]
            comments_results = await asyncio.gather(*comment_tasks)

            # Build Candidates list
            candidates = []
            for post, (comments_str, thread_date) in zip(target_raw, comments_results, strict=True):
                if not post["published_at"] and thread_date:
                    post["published_at"] = thread_date
                # Construct NormalizedPayload
                original_url = post["discussion_url"] if post["is_self"] else post["url"]

                # Build raw_text
                text_parts = [post["title"]]
                if post["selftext"]:
                    text_parts.append(post["selftext"])
                if comments_str:
                    text_parts.append(comments_str)
                raw_text = "\n\n".join(text_parts)

                metadata = {
                    "subreddit": post["subreddit"],
                    "post_score": post["score"],
                    "upvote_ratio": post["upvote_ratio"],
                    "num_comments": post["num_comments"],
                    "flair": post["flair"],
                    "discussion_url": post["discussion_url"],
                    "fetch_mode": post["fetch_mode"],
                }

                payload = NormalizedPayload(
                    source_type="reddit_post",
                    source_name=f"r/{post['subreddit']}",
                    raw_text=raw_text,
                    original_url=original_url,
                    published_at=post["published_at"],
                    metadata=metadata,
                )

                candidates.append(
                    Candidate(
                        adapter=self.name,
                        payload=payload,
                        score=post["prelim_score"],
                        reason=f"Reddit post from r/{post['subreddit']} with score {post['score']}.",
                    )
                )

        # 4. Query Refinement (Low-Yield Recovery)
        if len(candidates) < 3:
            logger.info("Low-yield detected for Reddit adapter. Initiating query refinement...")
            try:
                from backend.agents.discovery.query_refiner import refine_queries_for_adapter
                refined_queries = await refine_queries_for_adapter(
                    adapter_name=self.name,
                    profile=profile,
                    initial_results=candidates,
                    initial_queries=targets.search_queries,
                    lookback_hours=context.lookback_hours,
                )
            except Exception as exc:
                logger.warning("Failed to refine queries for Reddit adapter: %s", exc)
                refined_queries = []

            if refined_queries:
                refined_queries = refined_queries[:3]
                async with httpx.AsyncClient(http2=True, follow_redirects=True, timeout=reddit_timeout) as client:
                    ref_search_tasks = [fetch_search_via_web(q) for q in refined_queries]
                    ref_results = await asyncio.gather(*ref_search_tasks, return_exceptions=True)

                    ref_raw_candidates = []
                    for res in ref_results:
                        if isinstance(res, list):
                            for post in res:
                                permalink = post["permalink"]
                                discussion_url = permalink
                                if permalink.startswith("/"):
                                    discussion_url = "https://www.reddit.com" + permalink

                                # Deduplicate against all seen permalinks
                                if discussion_url.lower() in seen_permalinks:
                                    continue
                                seen_permalinks.add(discussion_url.lower())

                                # Filters
                                if post["score"] < min_post_score:
                                    continue
                                if cutoff is not None and post["published_at"]:
                                    try:
                                        pub_dt = datetime.fromisoformat(post["published_at"])
                                        if pub_dt < cutoff:
                                            continue
                                    except Exception:
                                        pass

                                post["discussion_url"] = discussion_url
                                ref_raw_candidates.append(post)

                    # Score and sort refined raw candidates
                    for post in ref_raw_candidates:
                        log_score = math.log10(max(1, post["score"]))
                        norm_score = 0.15 + (min(3.0, max(0.0, log_score - 1.0)) / 3.0) * 0.60
                        norm_ratio = max(0.0, min(1.0, post["upvote_ratio"] or 0.8)) * 0.18
                        score = norm_score + norm_ratio

                        # Small boosts
                        sub_lower = str(post["subreddit"]).lower()
                        boost = 0.0
                        if sub_lower in requested_subs:
                            boost += 0.05
                        if post["fetch_mode"] == "subreddit":
                            boost += 0.02

                        post["prelim_score"] = min(0.98, score + boost)

                    ref_raw_candidates.sort(key=lambda x: x["prelim_score"], reverse=True)
                    remaining_capacity = max(0, reddit_candidate_cap - len(candidates))
                    ref_target_raw = ref_raw_candidates[:remaining_capacity]

                    # Fetch comments for refined
                    ref_comment_tasks = [fetch_comments_for_post(client, post["subreddit"], post["id"]) for post in ref_target_raw]
                    ref_comments_results = await asyncio.gather(*ref_comment_tasks)

                    # Build refined candidates
                    for post, (comments_str, thread_date) in zip(ref_target_raw, ref_comments_results, strict=True):
                        original_url = post["discussion_url"] if post["is_self"] else post["url"]
                        if not post["published_at"] and thread_date:
                            post["published_at"] = thread_date

                        text_parts = [post["title"]]
                        if post["selftext"]:
                            text_parts.append(post["selftext"])
                        if comments_str:
                            text_parts.append(comments_str)
                        raw_text = "\n\n".join(text_parts)

                        metadata = {
                            "subreddit": post["subreddit"],
                            "post_score": post["score"],
                            "upvote_ratio": post["upvote_ratio"],
                            "num_comments": post["num_comments"],
                            "flair": post["flair"],
                            "discussion_url": post["discussion_url"],
                            "fetch_mode": post["fetch_mode"],
                            "is_refined_query": True,
                        }

                        payload = NormalizedPayload(
                            source_type="reddit_post",
                            source_name=f"r/{post['subreddit']}",
                            raw_text=raw_text,
                            original_url=original_url,
                            published_at=post["published_at"],
                            metadata=metadata,
                        )

                        candidates.append(
                            Candidate(
                                adapter=self.name,
                                payload=payload,
                                score=post["prelim_score"],
                                reason=f"Refined Reddit search result from r/{post['subreddit']} with score {post['score']}.",
                            )
                        )

        # 5. Handle AdapterUnavailable
        if not candidates:
            searched_subreddits = ", ".join(targets.subreddits)
            searched_queries = ", ".join(targets.search_queries)
            diagnostic = f"Tried subreddits: [{searched_subreddits}]. Tried queries: [{searched_queries}]."
            raise AdapterUnavailable(
                f"Reddit adapter found zero recent candidates. {diagnostic}"
            )

        return candidates[:reddit_candidate_cap]

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


class GoogleNewsSourceAdapter:
    name = "google_news"
    cost_profile = CostProfile(label="slow", timeout_seconds=120.0)
    good_for = ("breaking_news", "headlines", "broad_discovery", "mainstream_coverage")

    async def query(self, profile: TopicProfile, context: SourceAdapterContext) -> list[Candidate]:
        import httpx
        from backend.agents.discovery import google_news
        
        queries = _web_search_queries(profile, _requested_refs(profile, "google_news"), adapter=self.name)
        settings = get_settings()
        max_queries = getattr(settings, "google_news_max_queries", 5)
        queries = queries[:max_queries]
        
        if not queries:
            return []
            
        locale_str = getattr(settings, "google_news_locale", "en-US:US")
        hl, gl, ceid = "en-US", "US", "US:en"
        if ":" in locale_str:
            hl_part, gl_part = locale_str.split(":", 1)
            hl = hl_part
            gl = gl_part
            ceid = f"{gl_part}:{hl_part.split('-')[0]}"

        request_timeout = float(getattr(settings, "google_news_request_timeout_seconds", 10.0))
        request_delay = float(getattr(settings, "google_news_request_delay_seconds", 3.0))
        deadline = time.monotonic() + max(1.0, self.cost_profile.timeout_seconds - 3.0)
        degraded_reason: str | None = None

        def remaining_seconds() -> float:
            return deadline - time.monotonic()

        def has_time(min_seconds: float = 0.75) -> bool:
            return remaining_seconds() > min_seconds

        async def fetch_query_hits(query_list: list[str]) -> list[Any]:
            nonlocal degraded_reason
            if not has_time(1.25):
                degraded_reason = degraded_reason or "time_budget"
                return []
            timeout = max(
                0.5,
                min((request_timeout * 2) + (request_delay * 2) + 1.0, remaining_seconds() - 0.25),
            )

            async def fetch_one(query: str) -> list[Any]:
                return await asyncio.wait_for(
                    google_news.fetch_google_news(
                        query,
                        lookback_hours=context.lookback_hours,
                        limit=10,
                        hl=hl,
                        gl=gl,
                        ceid=ceid,
                    ),
                    timeout=timeout,
                )

            results = await asyncio.gather(*(fetch_one(query) for query in query_list), return_exceptions=True)
            hits: list[Any] = []
            for query, result in zip(query_list, results, strict=False):
                if isinstance(result, TimeoutError):
                    degraded_reason = degraded_reason or "time_budget"
                    logger.warning("Google News query timed out inside adapter deadline: %s", query)
                    continue
                if isinstance(result, Exception):
                    logger.warning("Error fetching Google News for query %s: %s", query, result)
                    continue
                hits.extend(result)
            return hits

        hits = await fetch_query_hits(queries)

        hits_by_url: dict[str, tuple[Any, str, int]] = {}
        seen_titles: set[str] = set()
        
        def normalize_title(title: str) -> str:
            cleaned = title.lower()
            cleaned = re.sub(r"[^\w\s]", "", cleaned)
            return "".join(cleaned.split())
            
        for i, hit in enumerate(hits):
            url_key = _dedupe_url_key(hit.url)
            title_key = normalize_title(hit.title)
            
            if title_key in seen_titles:
                continue
                
            existing = hits_by_url.get(url_key)
            if existing is None:
                hits_by_url[url_key] = (hit, url_key, i)
                seen_titles.add(title_key)
                
        unique_hits = [item[0] for item in hits_by_url.values()]
        if len(unique_hits) < 3 and has_time(10.0):
            logger.info("Low-yield detected for Google News adapter. Initiating query refinement...")
            try:
                from backend.agents.discovery.query_refiner import refine_queries_for_adapter
                refined_queries = await refine_queries_for_adapter(
                    adapter_name=self.name,
                    profile=profile,
                    initial_results=unique_hits,
                    initial_queries=queries,
                    lookback_hours=context.lookback_hours,
                )
            except Exception as exc:
                logger.warning("Failed to refine queries for Google News adapter: %s", exc)
                refined_queries = []
                
            if refined_queries:
                refined_queries = refined_queries[:3]
                ref_hits = await fetch_query_hits(refined_queries)
                for hit in ref_hits:
                    url_key = _dedupe_url_key(hit.url)
                    title_key = normalize_title(hit.title)
                    
                    if title_key in seen_titles:
                        continue
                        
                    existing = hits_by_url.get(url_key)
                    if existing is None:
                        hits_by_url[url_key] = (hit, url_key, -1)
                        seen_titles.add(title_key)
        elif len(unique_hits) < 3:
            degraded_reason = degraded_reason or "time_budget"

        candidate_limit = max(1, context.candidate_limit)
        
        def get_score(pos: int) -> float:
            if pos == -1:
                return 0.75
            return max(0.55, 0.90 - pos * 0.02)
            
        ordered_items = sorted(
            hits_by_url.values(),
            key=lambda x: get_score(x[2]),
            reverse=True
        )
        
        top_items = ordered_items[:candidate_limit]
        
        unfurl_enabled = getattr(settings, "google_news_unfurl_links", True)
        unfurl_delay = getattr(settings, "google_news_request_delay_seconds", 3.0)
        
        decoded_items = []
        max_unfurl = min(len(top_items), 10)
        decode_state = google_news.GoogleNewsDecodeState()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for idx, (hit, url_key, pos) in enumerate(top_items):
                decoded_url = None
                resolution = "google_news_proxy"
                reason_code: str | None = None
                if unfurl_enabled and idx < max_unfurl:
                    cached_url = google_news.cached_decoded_google_news_url(hit.url)
                    if cached_url:
                        decoded_url = cached_url
                        resolution = "google_news_decode_cache"
                    else:
                        cached_failure = google_news.cached_google_news_decode_failure(hit.url)
                        if cached_failure:
                            reason_code = cached_failure
                        elif decode_state.blocked:
                            reason_code = decode_state.reason or "decode_blocked"
                    if not decoded_url and reason_code is None and has_time(1.25):
                        if idx > 0:
                            sleep_for = min(unfurl_delay, max(0.0, remaining_seconds() - 1.25))
                            if sleep_for > 0:
                                await asyncio.sleep(sleep_for)
                        if has_time(1.25):
                            try:
                                timeout = max(0.5, min(request_timeout, remaining_seconds() - 0.25))
                                decoded_url = await asyncio.wait_for(
                                    google_news.decode_google_news_url(hit.url, client=client, state=decode_state),
                                    timeout=timeout,
                                )
                                if decoded_url:
                                    resolution = "google_news_decode"
                                else:
                                    reason_code = (
                                        google_news.cached_google_news_decode_failure(hit.url)
                                        or decode_state.reason
                                        or "decode_failed"
                                    )
                            except TimeoutError:
                                reason_code = "time_budget"
                                logger.warning("Google News URL decode timed out inside adapter deadline: %s", hit.url)
                            except Exception as exc:
                                reason_code = "decode_failed"
                                logger.warning("Error decoding Google News URL %s: %s", hit.url, exc)
                    elif not decoded_url and reason_code is None:
                        reason_code = "time_budget"
                    if not decoded_url and getattr(settings, "google_news_serper_fallback", True) and has_time(1.25):
                        try:
                            timeout = max(0.5, min(8.0, remaining_seconds() - 0.25))
                            fallback_url = await asyncio.wait_for(
                                _google_news_serper_fallback_url(
                                    hit,
                                    settings=settings,
                                    lookback_hours=context.lookback_hours,
                                ),
                                timeout=timeout,
                            )
                            if fallback_url:
                                decoded_url = fallback_url
                                resolution = "serper_fallback"
                                reason_code = None
                        except TimeoutError:
                            reason_code = reason_code or "time_budget"
                elif unfurl_enabled and idx >= max_unfurl:
                    resolution = "decode_deferred"
                
                if not decoded_url and reason_code:
                    resolution = reason_code
                decoded_items.append((hit, decoded_url or hit.url, pos, resolution, reason_code))
                
        candidates = []
        for hit, final_url, pos, resolution, reason_code in decoded_items:
            score = get_score(pos)
            is_refined = (pos == -1)
            
            metadata = {
                "link_quality_score": score,
                "search_query_rank": pos + 1 if not is_refined else 1,
                "search_provider": "google_news_rss",
                "publisher": hit.publisher,
                "google_news_url": hit.url,
                "title": hit.title,
                "link_text": hit.title,
                "google_news_resolution": resolution,
            }
            if reason_code:
                metadata["adapter_reason_code"] = reason_code
            if is_refined:
                metadata["is_refined_query"] = True
                
            payload = NormalizedPayload(
                source_type="gmail_link",
                source_name=hit.publisher,
                raw_text=hit.snippet,
                original_url=final_url,
                published_at=hit.published_at,
                metadata=metadata,
            )
            
            candidates.append(Candidate(
                adapter=self.name,
                payload=payload,
                score=score,
                reason=f"Google News article from {hit.publisher}.",
            ))
            
        return candidates

    async def fetch(self, candidate: Candidate) -> Any:
        return candidate.payload


async def _google_news_serper_fallback_url(
    hit: Any,
    *,
    settings: Any,
    lookback_hours: int | None,
) -> str | None:
    key = _serper_api_key(settings)
    if not key:
        return None
    title = " ".join(str(getattr(hit, "title", "") or "").split()).strip()
    publisher = " ".join(str(getattr(hit, "publisher", "") or "").split()).strip()
    if not title:
        return None
    query = f'"{title}" {publisher}'.strip()
    try:
        backend = SerperBackend(api_key=key, timeout_seconds=8.0)
        results = await backend.search(query, limit=5, days=lookback_to_days(lookback_hours))
    except Exception as exc:
        logger.info("Google News Serper fallback failed for %s: %s", title, exc)
        return None
    for result in results:
        if _serper_result_matches_publisher(result, publisher):
            return result.url
    return None


def _serper_api_key(settings: Any) -> str:
    key = str(getattr(settings, "web_search_serper_api_key", None) or "").strip()
    if key:
        return key
    try:
        return (settings.secrets_dir / "serper" / "api_key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _serper_result_matches_publisher(result: Any, publisher: str) -> bool:
    publisher_tokens = _publisher_tokens(publisher)
    if not publisher_tokens:
        return True
    parsed = urlparse(str(getattr(result, "url", "") or ""))
    haystack = " ".join(
        [
            parsed.netloc.lower().removeprefix("www."),
            str(getattr(result, "title", "") or "").lower(),
            str(getattr(result, "snippet", "") or "").lower(),
        ]
    )
    return any(token in haystack for token in publisher_tokens)


def _publisher_tokens(value: str) -> set[str]:
    stop = {"the", "news", "daily", "journal", "post", "finance", "media", "online", "com"}
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) >= 4 and token not in stop
    }
