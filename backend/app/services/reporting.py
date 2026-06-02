import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from backend.app.core.config import get_settings
from backend.app.db import database
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.discovery.types import DiscoveryResult

logger = logging.getLogger(__name__)


def compile_reporting_data(
    exploration_id: str,
    discovery: DiscoveryResult,
    fetched_articles: List[ArticleFetchResult],
    source_window_issues: List[Dict[str, Any]],
    enriched_articles: List[ArticleFetchResult],
    ranked_articles: List[ArticleFetchResult],
    after_audit: List[ArticleFetchResult],
    after_editorial: List[ArticleFetchResult],
    after_critic: List[ArticleFetchResult],
    final_results: List[ArticleFetchResult],
    progress: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Compiles the complete lifecycle information for all candidates discovered during the brief run."""
    candidates_by_id: Dict[str, Dict[str, Any]] = {}

    # Helper to check if a stage has been filled
    def set_reason_at_stage(cand_id: str, stage: str, reason: str):
        if cand_id in candidates_by_id:
            cand = candidates_by_id[cand_id]
            # Ensure we only set the reason once, and clear other stages to null
            for s in cand["stages"]:
                if s == stage:
                    cand["stages"][s] = reason
                else:
                    cand["stages"][s] = None

    # 1. Register survivors of discovery
    for c in discovery.candidates:
        payload = c.payload
        adapter = c.adapter or payload.source_type or "web_search"
        title = (
            payload.metadata.get("title")
            or payload.metadata.get("subject")
            or payload.metadata.get("link_text")
            or payload.source_name
            or c.reason
            or ""
        )
        url = payload.original_url or ""
        candidates_by_id[payload.id] = {
            "id": payload.id,
            "title": title,
            "url": url,
            "source": adapter,
            "stages": {
                stage: None
                for stage in [
                    "discovery",
                    "screening",
                    "recency",
                    "fetch",
                    "audit",
                    "editorial",
                    "critic",
                    "inclusion",
                ]
            },
        }

    # 2. Register and mark exclusions
    for ex in discovery.exclusions:
        cand_id = ex.get("candidate_id") or ex.get("id")
        if not cand_id:
            continue

        adapter = ex.get("adapter") or ex.get("source_type") or "web_search"
        title = ex.get("title") or ex.get("subject") or "Excluded Candidate"
        url = ex.get("original_url") or ex.get("url") or ""
        reason = ex.get("reason") or "Excluded during discovery."
        excluded_by = ex.get("excluded_by") or []

        if cand_id not in candidates_by_id:
            candidates_by_id[cand_id] = {
                "id": cand_id,
                "title": title,
                "url": url,
                "source": adapter,
                "stages": {
                    stage: None
                    for stage in [
                        "discovery",
                        "screening",
                        "recency",
                        "fetch",
                        "audit",
                        "editorial",
                        "critic",
                        "inclusion",
                    ]
                },
            }

        if "agentic_screening" in excluded_by:
            set_reason_at_stage(cand_id, "screening", reason)
        else:
            set_reason_at_stage(cand_id, "discovery", reason)

    # 3. Add recency window exclusions
    url_to_id = {cand["url"]: cand_id for cand_id, cand in candidates_by_id.items() if cand["url"]}
    title_to_id = {cand["title"].lower().strip(): cand_id for cand_id, cand in candidates_by_id.items() if cand["title"]}

    for issue in source_window_issues:
        url = issue.get("item_url") or issue.get("url")
        reason = issue.get("reason") or "Published outside the requested source window."
        cand_id = None
        if url:
            cand_id = url_to_id.get(url)
        if not cand_id and issue.get("source_name"):
            cand_id = title_to_id.get(issue["source_name"].lower().strip())
        if not cand_id and issue.get("item"):
            cand_id = title_to_id.get(issue["item"].lower().strip())

        if cand_id:
            set_reason_at_stage(cand_id, "recency", reason)

    # 4. Add fetch failures
    for result in fetched_articles:
        cand_id = result.payload.id
        if cand_id in candidates_by_id:
            cand = candidates_by_id[cand_id]
            # Verify if already dropped
            if any(cand["stages"].values()):
                continue
            if result.status != "fetched":
                reason = result.error or f"Failed to fetch content ({result.status})."
                set_reason_at_stage(cand_id, "fetch", reason)

    # 5. Add pre-ranking quality audit drops
    enriched_ids = {r.payload.id for r in enriched_articles}
    ranked_ids = {r.payload.id for r in ranked_articles}

    for result in fetched_articles:
        cand_id = result.payload.id
        if cand_id not in candidates_by_id:
            continue
        cand = candidates_by_id[cand_id]
        if any(cand["stages"].values()):
            continue

        if cand_id not in enriched_ids:
            set_reason_at_stage(
                cand_id, "audit", "Failed pre-ranking quality audit (noisy title, domain, or body text)."
            )
        elif cand_id not in ranked_ids:
            set_reason_at_stage(
                cand_id,
                "audit",
                f"Filtered out due to low relevance score ({result.relevance_score or 0.0:.2f} is below target threshold).",
            )

    # 6. Add source audit drops
    after_audit_ids = {r.payload.id for r in after_audit if r.tier != "dropped"}
    audit_issues = progress.get("source_audit_issues") or []
    audit_reason = "Flagged by source audit (failed freshness, fit, or source quality checks)."
    if audit_issues and isinstance(audit_issues, list):
        audit_reason = f"Flagged by source audit: {audit_issues[0].get('reason', audit_reason)}"

    for result in ranked_articles:
        cand_id = result.payload.id
        if cand_id not in candidates_by_id:
            continue
        cand = candidates_by_id[cand_id]
        if any(cand["stages"].values()):
            continue

        if cand_id not in after_audit_ids:
            set_reason_at_stage(cand_id, "audit", audit_reason)

    # 7. Add editorial drops
    editorial_reasons = {}
    editorial_str = (progress.get("reasoning") or {}).get("editorial") or ""
    if editorial_str:
        decisions = re.findall(
            r'\"index\"\s*:\s*(\d+)[^}]+?\"decision\"\s*:\s*\"([^\"]+)\"[^}]+?\"reason\"\s*:\s*\"([^\"]+)\"',
            editorial_str,
            re.DOTALL,
        )
        for idx_str, decision, reason in decisions:
            try:
                idx = int(idx_str)
                if 0 <= idx < len(after_audit):
                    editorial_reasons[after_audit[idx].payload.id] = reason
            except Exception:
                pass

    after_editorial_ids = {r.payload.id for r in after_editorial if r.tier != "dropped"}
    for result in after_audit:
        if result.tier == "dropped":
            continue
        cand_id = result.payload.id
        if cand_id not in candidates_by_id:
            continue
        cand = candidates_by_id[cand_id]
        if any(cand["stages"].values()):
            continue

        if cand_id not in after_editorial_ids:
            reason = editorial_reasons.get(cand_id) or "Excluded by editor (not aligned with core topic priorities)."
            set_reason_at_stage(cand_id, "editorial", reason)

    # 8. Add critic drops
    after_critic_ids = {r.payload.id for r in after_critic if r.tier != "dropped"}
    for result in after_editorial:
        if result.tier == "dropped":
            continue
        cand_id = result.payload.id
        if cand_id not in candidates_by_id:
            continue
        cand = candidates_by_id[cand_id]
        if any(cand["stages"].values()):
            continue

        if cand_id not in after_critic_ids:
            set_reason_at_stage(
                cand_id,
                "critic",
                "Dropped during critic review (identified as redundant, duplicate, or low-value content).",
            )

    # 9. Add inclusion limits drops
    final_ids = {r.payload.id for r in final_results if r.tier != "dropped"}
    for result in after_critic:
        if result.tier == "dropped":
            continue
        cand_id = result.payload.id
        if cand_id not in candidates_by_id:
            continue
        cand = candidates_by_id[cand_id]
        if any(cand["stages"].values()):
            continue

        if cand_id not in final_ids:
            set_reason_at_stage(
                cand_id,
                "inclusion",
                "Exceeded source-specific capacity limit (YouTube/Podcast capped at 20; Gmail/Markets/Web/Foreign capped at 40).",
            )

    return list(candidates_by_id.values())


def save_reporting_log(exploration_id: str, data: List[Dict[str, Any]]) -> str:
    settings = get_settings()
    output_dir = settings.data_dir / "digest-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"exploration-{exploration_id}-reporting.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def get_or_build_reporting_log(exploration_id: str) -> List[Dict[str, Any]]:
    settings = get_settings()
    path = settings.data_dir / "digest-output" / f"exploration-{exploration_id}-reporting.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read reporting json for exploration %s: %s", exploration_id, exc)

    logger.info("Reconstructing reporting log on-demand for exploration %s", exploration_id)
    return reconstruct_reporting_data(exploration_id)


def reconstruct_reporting_data(exploration_id: str) -> List[Dict[str, Any]]:
    exploration = database.get_exploration(exploration_id)
    if not exploration:
        return []

    progress = exploration.get("progress") or {}
    brief_ref = exploration.get("brief_ref")
    html_content = None
    if brief_ref and Path(brief_ref).exists():
        try:
            html_content = Path(brief_ref).read_text(encoding="utf-8")
        except Exception:
            pass

    candidates: Dict[str, Dict[str, Any]] = {}

    # 1. Parse included items from HTML
    if html_content:
        # Extract story titles and links
        stories = re.findall(r'class="story-title"\s*>\s*<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', html_content)
        for url, title in stories:
            url = url.strip()
            title = title.strip()
            source = "web_search"
            if "mail.google.com" in url:
                source = "gmail"
            elif "youtube.com" in url or "youtu.be" in url:
                source = "youtube"
            elif "markets" in url:
                source = "markets"
            candidates[url] = {
                "title": title,
                "url": url,
                "source": source,
                "stages": {
                    stage: None
                    for stage in [
                        "discovery",
                        "screening",
                        "recency",
                        "fetch",
                        "audit",
                        "editorial",
                        "critic",
                        "inclusion",
                    ]
                },
            }

        # Extract media titles and links
        media = re.findall(r'class="media-title"\s*>\s*<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', html_content)
        for url, title in media:
            url = url.strip()
            title = title.strip()
            source = "youtube" if "youtube.com" in url or "youtu.be" in url else "podcasts"
            candidates[url] = {
                "title": title,
                "url": url,
                "source": source,
                "stages": {
                    stage: None
                    for stage in [
                        "discovery",
                        "screening",
                        "recency",
                        "fetch",
                        "audit",
                        "editorial",
                        "critic",
                        "inclusion",
                    ]
                },
            }

    # 2. Add exclusions from progress
    exclusions = progress.get("exclusions") or []
    for ex in exclusions:
        cand_id = ex.get("candidate_id") or ex.get("id") or ex.get("original_url") or ex.get("url")
        if not cand_id:
            continue
        url = ex.get("original_url") or ex.get("url") or ""
        title = ex.get("title") or "Excluded Candidate"
        adapter = ex.get("adapter") or ex.get("source_type") or "web_search"
        reason = ex.get("reason") or "Excluded during discovery."
        excluded_by = ex.get("excluded_by") or []

        stages = {
            stage: None
            for stage in [
                "discovery",
                "screening",
                "recency",
                "fetch",
                "audit",
                "editorial",
                "critic",
                "inclusion",
            ]
        }
        if "agentic_screening" in excluded_by:
            stages["screening"] = reason
        else:
            stages["discovery"] = reason

        key = url if url else cand_id
        candidates[key] = {"title": title, "url": url, "source": adapter, "stages": stages}

    # 3. Add recency window exclusions
    filter_notes = progress.get("source_filter_notes") or []
    for note in filter_notes:
        url = note.get("item_url") or note.get("url") or ""
        title = note.get("item") or note.get("source_name") or "Filtered Candidate"
        source = note.get("source") or "web_search"
        reason = note.get("reason") or "Published outside the requested source window."

        stages = {
            stage: None
            for stage in [
                "discovery",
                "screening",
                "recency",
                "fetch",
                "audit",
                "editorial",
                "critic",
                "inclusion",
            ]
        }
        stages["recency"] = reason

        key = url if url else title
        if key in candidates:
            # Shift the dropped stage to recency
            for s in candidates[key]["stages"]:
                if s == "recency":
                    candidates[key]["stages"][s] = reason
                else:
                    candidates[key]["stages"][s] = None
        else:
            candidates[key] = {"title": title, "url": url, "source": source, "stages": stages}

    # 4. Add editorial exclusions placeholder if reasoning is found
    reasoning = progress.get("reasoning") or {}
    editorial_str = reasoning.get("editorial") or ""
    if editorial_str:
        decisions = re.findall(
            r'\"index\"\s*:\s*(\d+)[^}]+?\"decision\"\s*:\s*\"([^\"]+)\"[^}]+?\"reason\"\s*:\s*\"([^\"]+)\"',
            editorial_str,
            re.DOTALL,
        )
        for idx_str, decision, reason in decisions:
            if decision in ("exclude", "demote"):
                key = f"editorial-exclusion-{idx_str}"
                candidates[key] = {
                    "title": f"Editorial Excluded Candidate (Index {idx_str})",
                    "url": "",
                    "source": "editorial_agent",
                    "stages": {
                        "discovery": None,
                        "screening": None,
                        "recency": None,
                        "fetch": None,
                        "audit": None,
                        "editorial": reason if decision == "exclude" else None,
                        "critic": None,
                        "inclusion": None,
                    },
                }

    return list(candidates.values())
