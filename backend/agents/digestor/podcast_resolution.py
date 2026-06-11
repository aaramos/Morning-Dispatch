"""Episode -> feed resolution helpers for the podcast pipeline.

Extracted from backend/agents/digestor/podcast.py (M8) — pure moves, zero
behavior change. podcast.py re-exports these names for compatibility.

The DB-backed resolution cache (database.get_cached_podcast_resolution /
set_cached_podcast_resolution) is exposed here via thin pass-throughs so
resolution callers go through this module; database.py is untouched.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from backend.agents.agentic import AgentDecision
from backend.agents.digestor.podcast_http import _normalize_url_for_match, discover_podcasts
from backend.app.db import database

if TYPE_CHECKING:
    from backend.agents.digestor.podcast import PodcastEpisode

logger = logging.getLogger(__name__)


def get_cached_resolution(url_norm: str) -> dict[str, Any] | None:
    """Pass-through to the DB-backed episode->feed resolution cache."""
    return database.get_cached_podcast_resolution(url_norm)


def set_cached_resolution(
    url_norm: str,
    feed_url: str | None,
    episode_guid: str | None,
    apple_url: str | None,
    ttl_seconds: int,
) -> None:
    """Pass-through to the DB-backed episode->feed resolution cache."""
    database.set_cached_podcast_resolution(url_norm, feed_url, episode_guid, apple_url, ttl_seconds)


def _decision(
    *,
    target: str,
    decision: str,
    action: str,
    confidence: float,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> AgentDecision:
    return AgentDecision(
        agent="podcast_scout",
        target=target,
        decision=decision,
        action=action,
        confidence=confidence,
        reason=reason,
        metadata=metadata or {},
    )


async def _resolve_feed_url(
    client: httpx.AsyncClient,
    url: str,
    title: str,
    decisions: list[AgentDecision],
) -> str | None:
    if "podcasts.apple.com" in url or "itunes.apple.com" in url:
        match = re.search(r"/id(\d+)", url)
        if match:
            itunes_id = match.group(1)
            try:
                response = await client.get(
                    "https://itunes.apple.com/lookup",
                    params={"id": itunes_id},
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])
                if results and isinstance(results[0], dict):
                    feed_url = results[0].get("feedUrl")
                    if feed_url:
                        decisions.append(
                            _decision(
                                target=title,
                                decision="resolved",
                                action="itunes_lookup",
                                confidence=0.95,
                                reason=f"Resolved RSS feed from Apple iTunes ID {itunes_id}.",
                                metadata={"feed_url": feed_url},
                            )
                        )
                        return feed_url
            except Exception as exc:
                logger.info("iTunes lookup failed for ID %s: %s", itunes_id, exc)

    show_name = _extract_show_name_from_hit_title(title)
    if show_name:
        try:
            results = await discover_podcasts(show_name, limit=3)
            if results:
                feed_url = results[0].get("feed_url")
                if feed_url:
                    decisions.append(
                        _decision(
                            target=title,
                            decision="resolved",
                            action="podcast_index_lookup",
                            confidence=0.85,
                            reason=f"Resolved RSS feed from Podcast Index show search for '{show_name}'.",
                            metadata={"feed_url": feed_url},
                        )
                    )
                    return feed_url
        except Exception as exc:
            logger.info("Podcast Index lookup failed for show '%s': %s", show_name, exc)

    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            link_type = str(link.get("type") or "").lower()
            link_href = str(link.get("href") or "").strip()
            if ("rss" in link_type or "xml" in link_type) and link_href:
                from urllib.parse import urljoin
                feed_url = urljoin(url, link_href)
                decisions.append(
                    _decision(
                        target=title,
                        decision="resolved",
                        action="rss_autodiscovery",
                        confidence=0.88,
                        reason="Resolved RSS feed via autodiscovery link on page.",
                        metadata={"feed_url": feed_url},
                    )
                )
                return feed_url
    except Exception as exc:
        logger.info("RSS Autodiscovery failed for URL %s: %s", url, exc)

    if show_name:
        web_query = f"{show_name} podcast RSS feed"
        try:
            from backend.agents.discovery.web_search import lookback_to_days, search_web
            days = lookback_to_days(24 * 365)
            hits = await search_web(web_query, limit=3, days=days)
            for hit in hits:
                feed_url = _feed_url_from_search_hit(hit.url)
                if feed_url:
                    decisions.append(
                        _decision(
                            target=title,
                            decision="resolved",
                            action="rss_web_search",
                            confidence=0.80,
                            reason=f"Resolved RSS feed via web search for '{show_name} RSS feed'.",
                            metadata={"feed_url": feed_url},
                        )
                    )
                    return feed_url
        except Exception as exc:
            logger.info("RSS web search fallback failed for show '%s': %s", show_name, exc)

    return None


def _extract_show_name_from_hit_title(title: str) -> str:
    cleaned = re.sub(r"\s*[-|•:|]\s*(apple podcasts|spotify|podcast addict|listen notes|podcasts?|youtube)\s*$", "", title, flags=re.I).strip()
    for sep in ("|", "-", "•", ":"):
        if sep in cleaned:
            parts = cleaned.split(sep)
            for part in parts:
                p = part.strip()
                if "episode" not in p.lower() and "interview" not in p.lower() and len(p.split()) <= 5 and len(p) > 2:
                    return p
    return cleaned


def _feed_url_from_search_hit(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    lowered = url.lower()
    if any(marker in lowered for marker in ("rss", "feed", ".xml", "podcast.xml")):
        return url
    return ""


def _match_episode_in_feed(episodes: list[PodcastEpisode], hit: Any) -> PodcastEpisode | None:
    cand_url = _normalize_url_for_match(hit.url).lower()
    for ep in episodes:
        if ep.episode_url:
            ep_url_norm = _normalize_url_for_match(ep.episode_url).lower()
            if ep_url_norm in cand_url or cand_url in ep_url_norm:
                return ep
        if ep.audio_url:
            ep_audio_norm = _normalize_url_for_match(ep.audio_url).lower()
            if ep_audio_norm in cand_url or cand_url in ep_audio_norm:
                return ep

    # Try Szymkiewicz-Simpson token overlap matching
    from backend.agents.librarian.text_utils import keyword_set
    cand_tokens = keyword_set(hit.title)
    if cand_tokens:
        for ep in episodes:
            ep_tokens = keyword_set(ep.title)
            if not ep_tokens:
                continue
            overlap = len(cand_tokens & ep_tokens) / max(1, min(len(cand_tokens), len(ep_tokens)))
            if overlap >= 0.65:
                return ep

    # Fallback to normalized subtitle/substring matching (Issue 4)
    def clean_title_for_soft_match(t: str, show_name: str | None = None) -> str:
        t_clean = t.lower()
        if show_name:
            # Strip show name suffix/prefix
            sn = show_name.lower()
            t_clean = re.sub(rf"\b{re.escape(sn)}\b", "", t_clean)
        # Strip common podcast markers & episode numbering patterns
        t_clean = re.sub(r"\b(episode|ep|show)\s*\d+\b", "", t_clean)
        t_clean = re.sub(r"[^\w\s]", " ", t_clean)
        return " ".join(t_clean.split())

    for ep in episodes:
        cand_clean = clean_title_for_soft_match(hit.title, ep.show_name)
        ep_clean = clean_title_for_soft_match(ep.title, ep.show_name)
        if not cand_clean or not ep_clean:
            continue
        # Check substring containment
        if cand_clean in ep_clean or ep_clean in cand_clean:
            return ep
        # Cleaned token overlap
        cand_clean_tokens = set(cand_clean.split())
        ep_clean_tokens = set(ep_clean.split())
        # Filter short/worthless tokens
        cand_clean_tokens = {tok for tok in cand_clean_tokens if len(tok) > 2}
        ep_clean_tokens = {tok for tok in ep_clean_tokens if len(tok) > 2}
        if cand_clean_tokens and ep_clean_tokens:
            overlap = len(cand_clean_tokens & ep_clean_tokens) / max(1, min(len(cand_clean_tokens), len(ep_clean_tokens)))
            if overlap >= 0.75:
                return ep

    return None
