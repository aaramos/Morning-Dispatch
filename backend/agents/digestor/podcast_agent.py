import os
import time
import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Tuple
import httpx

from backend.app.core.config import get_settings
from backend.app.services import model_routing

logger = logging.getLogger(__name__)

USER_AGENT = "MorningDispatch/0.1.0"
REQUEST_TIMEOUT_SECONDS = 15.0

class PodcastIndexAgent:
    """An agent that intercepts narrow user search queries, broadens them to search the PodcastIndex.org API 
    for relevant feeds, harvests recent episodes, and filters them locally using LLM-derived keywords 
    and playability checks.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.api_key = self.settings.podcastindex_api_key
        self.api_secret = self.settings.podcastindex_api_secret
        
    def _get_auth_headers(self) -> Dict[str, str]:
        if not self.api_key or not self.api_secret:
            raise ValueError("Podcast Index API credentials are not configured in settings.")
            
        auth_date = str(int(time.time()))
        # PodcastIndex requires SHA1 hash of Key + Secret + X-Auth-Date
        hash_input = f"{self.api_key}{self.api_secret}{auth_date}".encode("utf-8")
        authorization = hashlib.sha1(hash_input).hexdigest()
        
        return {
            "User-Agent": USER_AGENT,
            "X-Auth-Date": auth_date,
            "X-Auth-Key": self.api_key,
            "Authorization": authorization,
        }

    # =========================================================================
    # Step 1: LLM Query Reformulation Module
    # =========================================================================
    async def reformulate_query(self, user_query: str) -> Dict[str, Any]:
        """Deconstructs a narrow user query into broad show queries and episode keywords."""
        resolution = model_routing.client_for_agent("refinement", settings=self.settings)
        client = resolution.client
        if client is None:
            logger.warning("No LLM client configured for refinement. Using simple rule-based reformulation.")
            return self._fallback_reformulation(user_query)

        system_prompt = (
            "You are an expert search refinement agent specialized in indexing and directory search query optimization.\n"
            "Your task is to take a narrow, specific user search query and deconstruct it into two parts:\n"
            "1. 'broad_show_queries': An array of 2-3 generalized search terms (maximum 3 words each) targeting podcast show titles/genres (e.g. searching for a podcast channel/feed). These must be broad enough to match show directories.\n"
            "2. 'episode_filter_keywords': An array of 5-8 specific, granular keywords, phrases, or themes to match against individual episode titles and descriptions locally.\n\n"
            "Examples:\n"
            'User: "breakthroughs in Chinese GPU manufacturing and HBM memory"\n'
            'Output: {\n'
            '  "broad_show_queries": ["Chinese AI", "semiconductor", "silicon valley"],\n'
            '  "episode_filter_keywords": ["GPU", "HBM", "SMIC", "Huawei Ascend", "semiconductor packaging", "fabrication"]\n'
            '}\n\n'
            'User: "public companies benefiting from AI infrastructure spending"\n'
            'Output: {\n'
            '  "broad_show_queries": ["AI infrastructure", "AI spending", "tech investing"],\n'
            '  "episode_filter_keywords": ["data center", "NVIDIA", "CapEx", "liquid cooling", "ASML", "public companies", "revenue"]\n'
            '}\n\n'
            "Return only a JSON object matching this structure."
        )

        try:
            prompt_str = f"User query: \"{user_query}\""
            payload = await client.complete_json(
                system=system_prompt,
                prompt=prompt_str,
                max_tokens=300,
            )
            
            broad_show_queries = payload.get("broad_show_queries")
            episode_filter_keywords = payload.get("episode_filter_keywords")
            
            if isinstance(broad_show_queries, list) and isinstance(episode_filter_keywords, list):
                return {
                    "broad_show_queries": [str(q).strip() for q in broad_show_queries if str(q).strip()],
                    "episode_filter_keywords": [str(k).strip() for k in episode_filter_keywords if str(k).strip()]
                }
        except Exception as exc:
            logger.warning("Failed to reformulate query with LLM: %s", exc)
            
        return self._fallback_reformulation(user_query)

    def _fallback_reformulation(self, user_query: str) -> Dict[str, Any]:
        """Simple rule-based reformulation if LLM fails or is not configured."""
        words = re.findall(r"\w+", user_query)
        # Extract first 3 words as broad query
        broad = " ".join(words[:3]) if len(words) >= 3 else user_query
        return {
            "broad_show_queries": [broad, "technology", "artificial intelligence"],
            "episode_filter_keywords": words
        }

    # =========================================================================
    # Step 2: Broad API Show Search
    # =========================================================================
    async def search_shows(self, broad_queries: List[str]) -> List[Dict[str, Any]]:
        """Queries Podcast Index show database with broad queries and filters out dead feeds."""
        headers = self._get_auth_headers()
        discovered_feeds = {}
        
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
            for query in broad_queries:
                try:
                    params = {"q": query, "max": 15}
                    response = await client.get("https://api.podcastindex.org/api/1.0/search/byterm", params=params)
                    if response.status_code != 200:
                        continue
                        
                    data = response.json()
                    feeds = data.get("feeds") or []
                    for feed in feeds:
                        feed_id = feed.get("id")
                        feed_url = str(feed.get("url") or "").strip()
                        dead_status = feed.get("dead", 0)
                        
                        # Only keep healthy, non-duplicate feeds
                        if feed_id and feed_url and dead_status == 0:
                            discovered_feeds[feed_id] = {
                                "id": feed_id,
                                "title": feed.get("title"),
                                "url": feed_url,
                                "author": feed.get("author"),
                                "description": feed.get("description"),
                            }
                except Exception as exc:
                    logger.warning("Show search failed for query '%s': %s", query, exc)
                    
        return list(discovered_feeds.values())

    # =========================================================================
    # Step 3: Recent Episode Harvesting
    # =========================================================================
    async def harvest_episodes(self, feeds: List[Dict[str, Any]], lookback_hours: int = 72) -> List[Dict[str, Any]]:
        """Fetches the 10-15 most recent episodes from the top 10-12 healthy feeds."""
        headers = self._get_auth_headers()
        candidate_episodes = []
        
        # Limit to top 12 feeds to prevent rate limits or slow performance
        target_feeds = feeds[:12]
        
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
            for feed in target_feeds:
                try:
                    feed_id = feed["id"]
                    params = {"id": feed_id, "max": 15}
                    response = await client.get("https://api.podcastindex.org/api/1.0/episodes/byfeedid", params=params)
                    if response.status_code != 200:
                        continue
                        
                    data = response.json()
                    items = data.get("items") or []
                    now = time.time()
                    
                    for item in items:
                        # Extract timestamps
                        pub_date = item.get("datePublished", item.get("date_published", 0))
                        
                        # Only harvest within requested lookback window
                        if lookback_hours and (now - pub_date) > (lookback_hours * 3600):
                            continue
                            
                        candidate_episodes.append({
                            "id": item.get("id"),
                            "feed_id": feed_id,
                            "show_name": feed.get("title"),
                            "title": item.get("title"),
                            "description": item.get("description"),
                            "audio_url": item.get("enclosureUrl", item.get("enclosure_url")),
                            "audio_type": item.get("enclosureType", item.get("enclosure_type")),
                            "duration": item.get("duration"),
                            "published_at": pub_date,
                        })
                except Exception as exc:
                    logger.warning("Episode harvesting failed for feed ID %s: %s", feed.get("id"), exc)
                    
        return candidate_episodes

    # =========================================================================
    # Step 4: Local Content Filtering & Playability Verification
    # =========================================================================
    def filter_and_rank_episodes(
        self, 
        episodes: List[Dict[str, Any]], 
        filter_keywords: List[str], 
        original_query: str
    ) -> List[Dict[str, Any]]:
        """Filters, scores, verifies playability, and ranks the candidates locally."""
        ranked_candidates = []
        
        keyword_sets = [set(k.lower().split()) for k in filter_keywords]
        query_set = set(original_query.lower().split())
        
        for ep in episodes:
            # 1. Playability check: Must have a valid audio URL and type must be audio
            audio_url = ep.get("audio_url")
            audio_type = ep.get("audio_type") or ""
            
            if not audio_url or not audio_url.startswith("http"):
                continue
                
            # Verify audio enclosure type (mpeg, mp3, mp4, x-m4a, ogg, wav, etc.)
            if audio_type and "audio" not in audio_type.lower() and not any(ext in audio_url.lower() for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
                continue
                
            # 2. Relevancy Scoring
            title = (ep.get("title") or "").lower()
            desc = (ep.get("description") or "").lower()
            show = (ep.get("show_name") or "").lower()
            combined_text = f"{title} {desc} {show}"
            
            # Simple keyword matching score
            kw_matches = 0
            for kw_set in keyword_sets:
                if any(word in combined_text for word in kw_set):
                    kw_matches += 1
                    
            # Boost score if keywords match the title directly
            title_matches = 0
            for kw_set in keyword_sets:
                if any(word in title for word in kw_set):
                    title_matches += 1
                    
            # Compute a score normalized between 0.0 and 1.0
            kw_score = (kw_matches / len(keyword_sets)) if keyword_sets else 0.5
            title_score = (title_matches / len(keyword_sets)) if keyword_sets else 0.5
            
            final_score = (0.5 * kw_score) + (0.5 * title_score)
            
            # Add strict minimum relevance filter (e.g. must match at least 1 keyword)
            if kw_matches == 0 and not any(word in combined_text for word in query_set):
                continue
                
            ranked_candidates.append((final_score, ep))
            
        # Rank by score descending and return the top 5
        ranked_candidates.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in ranked_candidates[:5]]

    # =========================================================================
    # Orchestrator Entry Point
    # =========================================================================
    async def run(self, user_query: str, lookback_hours: int = 72) -> List[Dict[str, Any]]:
        """Coordinates LLM reformulation, directory search, episode harvest, and filtering."""
        logger.info("Starting podcast agent pipeline for query: '%s'", user_query)
        
        # Step 1: LLM Query Reformulation
        reform = await self.reformulate_query(user_query)
        broad_queries = reform["broad_show_queries"]
        filter_keywords = reform["episode_filter_keywords"]
        
        logger.info("Broadened queries: %s", broad_queries)
        logger.info("Episode filter keywords: %s", filter_keywords)
        
        # Step 2: Broad API Show Search
        feeds = await self.search_shows(broad_queries)
        logger.info("Discovered %d healthy show feeds.", len(feeds))
        if not feeds:
            return []
            
        # Step 3: Recent Episode Harvesting
        all_episodes = await self.harvest_episodes(feeds, lookback_hours=lookback_hours)
        logger.info("Harvested %d candidate episodes within source window.", len(all_episodes))
        if not all_episodes:
            return []
            
        # Step 4: Local Content Filtering & Playability Verification
        final_episodes = self.filter_and_rank_episodes(
            episodes=all_episodes,
            filter_keywords=filter_keywords,
            original_query=user_query
        )
        logger.info("Selected %d highly relevant, playable episodes.", len(final_episodes))
        
        return final_episodes
