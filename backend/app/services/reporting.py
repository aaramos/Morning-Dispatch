import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from backend.app.core.config import get_settings
from backend.app.db import database
from backend.agents.librarian.articles import ArticleFetchResult
from backend.agents.discovery.types import DiscoveryResult

logger = logging.getLogger(__name__)

GENERIC_TITLE_VALUES = {
    "",
    "approved gmail newsletter item.",
    "approved gmail newsletter item",
    "candidate item",
    "excluded candidate",
    "filtered candidate",
    "youtube metadata signal.",
    "youtube metadata signal",
    "youtube transcript signal.",
    "youtube transcript signal",
    "web result from tavily.",
    "web result from tavily",
    "web result from brave.",
    "web result from brave",
    "web result from serpapi.",
    "web result from serpapi",
}

SOURCE_ISSUE_ADAPTERS = {
    "collections": "collections",
    "collection": "collections",
    "foreign media": "foreign_media",
    "gmail": "gmail",
    "market data": "markets",
    "markets": "markets",
    "podcast": "podcasts",
    "podcasts": "podcasts",
    "reddit": "reddit",
    "web": "web_search",
    "web search": "web_search",
    "youtube": "youtube",
    "google news": "google_news",
}


def _clean_title_value(value: Any) -> str:
    title = str(value or "").strip()
    if title.lower() in GENERIC_TITLE_VALUES:
        return ""
    return title


def _title_from_url(url: Any) -> str:
    parsed = urlparse(str(url or ""))
    host = parsed.netloc.removeprefix("www.").strip()
    if host:
        return host
    return ""


def _title_from_metadata(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    for key in (
        "title",
        "link_text",
        "youtube_title",
        "podcast_title",
        "original_search_title",
        "subject",
        "parent_subject",
        "section_title",
        "company_name",
    ):
        title = _clean_title_value(metadata.get(key))
        if title:
            return title
    return ""


def _candidate_title_from_payload(payload: Any, *, reason: Any = "", fallback: str = "Source item") -> str:
    metadata = getattr(payload, "metadata", None) or {}
    title = _title_from_metadata(metadata)
    if title:
        return title
    for value in (
        getattr(payload, "source_name", ""),
        reason,
        _title_from_url(getattr(payload, "original_url", "")),
        fallback,
    ):
        title = _clean_title_value(value)
        if title:
            return title
    return fallback


def _candidate_title_from_mapping(item: Dict[str, Any], *, fallback: str = "Source item") -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    title = _title_from_metadata(metadata) or _title_from_metadata(payload_metadata)
    if title:
        return title
    for value in (
        item.get("title"),
        item.get("subject"),
        item.get("item"),
        item.get("source_name"),
        item.get("reason"),
        payload.get("source_name"),
        _title_from_url(item.get("original_url") or item.get("url") or payload.get("original_url")),
        fallback,
    ):
        title = _clean_title_value(value)
        if title:
            return title
    return fallback


def _article_title(result: ArticleFetchResult, *, fallback: str = "Source item") -> str:
    title = _clean_title_value(result.title)
    if title:
        return title
    return _candidate_title_from_payload(result.payload, fallback=fallback)


def _must_have_rejection_reason(result: ArticleFetchResult) -> str:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return str(metadata.get("must_have_rejection_reason") or "").strip()


def _must_have_reporting_stage(result: ArticleFetchResult, *, fallback: str) -> str:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    stage = str(metadata.get("must_have_rejection_stage") or "").strip()
    if stage == "post_fetch":
        return "fetch"
    if stage:
        return "inclusion"
    return fallback


def _post_fetch_gate_rejection_reason(result: ArticleFetchResult) -> str:
    """Reason for an item dropped by a post-fetch gate (must-have or topic relevance).

    Prefers the must-have reason, then the post-fetch topic-relevance reason, so the
    lifecycle log shows the real cause instead of the generic quality-audit fallback.
    """
    must_have = _must_have_rejection_reason(result)
    if must_have:
        return must_have
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return str(metadata.get("topic_relevance_rejection_reason") or "").strip()


def _source_issue_adapter(source_name: Any) -> str:
    normalized = re.sub(r"\s+", " ", str(source_name or "").strip().lower())
    return SOURCE_ISSUE_ADAPTERS.get(normalized, normalized.replace(" ", "_"))


def _row_has_terminal_reason(row: Dict[str, Any]) -> bool:
    stages = row.get("stages") if isinstance(row.get("stages"), dict) else {}
    return any(bool(reason) for reason in stages.values())


def _repair_unresolved_reporting_rows(
    report_data: List[Dict[str, Any]],
    progress: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Backfill terminal reasons into older reports with blank-pass rows.

    Some saved reports predate explicit not-fetched reporting and can show
    candidates as passing every lifecycle stage even when the run-level source
    issue says the entire source failed to contribute final brief content.
    """
    issues = progress.get("requested_source_issues") if isinstance(progress, dict) else []
    if not isinstance(issues, list):
        return report_data

    source_reasons: Dict[str, str] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        reason = str(issue.get("reason") or "").strip()
        if "none survived" not in reason.lower():
            continue
        adapter = _source_issue_adapter(issue.get("source_name"))
        if adapter:
            source_reasons[adapter] = reason

    if not source_reasons:
        return report_data

    repaired: List[Dict[str, Any]] = []
    for row in report_data:
        if not isinstance(row, dict):
            repaired.append(row)
            continue
        source = str(row.get("source") or "").strip()
        reason = source_reasons.get(source)
        if not reason or _row_has_terminal_reason(row):
            repaired.append(row)
            continue
        stages = row.get("stages") if isinstance(row.get("stages"), dict) else {}
        next_row = dict(row)
        next_stages = dict(stages)
        next_stages["inclusion"] = reason
        next_row["stages"] = next_stages
        repaired.append(next_row)
    return repaired


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

    # Items rejected by the recency window can still be revived from the reserve
    # into the final brief (P0). When that happens, "included" must win over
    # "recency" so the lifecycle log matches what actually rendered.
    final_included_ids = {
        result.payload.id for result in final_results if result.tier != "dropped"
    }

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

        if cand_id and cand_id not in final_included_ids:
            set_reason_at_stage(cand_id, "recency", reason)

    # 4. Add candidates that never entered fetch/extract. Discovery can produce
    # many more candidates than the article fetch budget; without an explicit
    # reason these rows look like they passed every stage in the UI.
    processed_ids = {
        result.payload.id
        for result in [
            *fetched_articles,
            *enriched_articles,
            *ranked_articles,
            *after_audit,
            *after_editorial,
            *after_critic,
            *final_results,
        ]
    }
    for cand_id, cand in candidates_by_id.items():
        if any(cand["stages"].values()):
            continue
        if cand_id in processed_ids:
            continue
        set_reason_at_stage(
            cand_id,
            "fetch",
            "Discovery candidate was not fetched or extracted before the article pipeline moved on, usually because the run produced more candidates than the fetch budget.",
        )

    # 5. Add fetch failures
    for result in fetched_articles:
        cand_id = result.payload.id
        if cand_id in candidates_by_id:
            cand = candidates_by_id[cand_id]
            # Verify if already dropped
            if any(cand["stages"].values()):
                continue
            if result.status != "fetched":
                reason = _post_fetch_gate_rejection_reason(result) or result.error or f"Failed to fetch content ({result.status})."
                set_reason_at_stage(cand_id, _must_have_reporting_stage(result, fallback="fetch"), reason)

    # 6. Add pre-ranking quality audit drops
    enriched_ids = {r.payload.id for r in enriched_articles}
    ranked_ids = {r.payload.id for r in ranked_articles}

    for result in fetched_articles:
        cand_id = result.payload.id
        if cand_id not in candidates_by_id:
            continue
        cand = candidates_by_id[cand_id]
        if any(cand["stages"].values()):
            continue

        must_have_reason = _post_fetch_gate_rejection_reason(result)
        if must_have_reason:
            set_reason_at_stage(cand_id, _must_have_reporting_stage(result, fallback="fetch"), must_have_reason)
        elif cand_id not in enriched_ids:
            set_reason_at_stage(
                cand_id, "audit", "Failed pre-ranking quality audit (noisy title, domain, or body text)."
            )
        elif cand_id not in ranked_ids:
            set_reason_at_stage(
                cand_id,
                "audit",
                f"Filtered out due to low relevance score ({result.relevance_score or 0.0:.2f} is below target threshold).",
            )

    # 7. Add source audit drops
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

    editorial_reasons = {}
    editorial_str = (progress.get("reasoning") or {}).get("editorial") or ""
    if editorial_str:
        try:
            editorial_data = json.loads(editorial_str)
            decisions_list = editorial_data.get("decisions") or []
            for dec in decisions_list:
                idx = dec.get("index")
                reason = dec.get("reason")
                if idx is not None and 0 <= idx < len(after_audit):
                    editorial_reasons[after_audit[idx].payload.id] = reason
        except Exception:
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
    final_by_id = {r.payload.id: r for r in final_results}
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
            final_result = final_by_id.get(cand_id)
            must_have_reason = _must_have_rejection_reason(final_result) if final_result is not None else ""
            set_reason_at_stage(
                cand_id,
                _must_have_reporting_stage(final_result, fallback="inclusion") if final_result is not None else "inclusion",
                must_have_reason
                or "Exceeded source-specific capacity limit (YouTube/Podcast capped at 20; Gmail/Markets/Web/Foreign capped at 40).",
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
    exploration = database.get_exploration(exploration_id)
    progress = exploration.get("progress") if exploration else {}
    if not isinstance(progress, dict):
        progress = {}
    if path.exists():
        try:
            report_data = json.loads(path.read_text(encoding="utf-8"))
            return _repair_unresolved_reporting_rows(report_data, progress)
        except Exception as exc:
            logger.warning("Failed to read reporting json for exploration %s: %s", exploration_id, exc)

    if exploration:
        if "candidate_reporting_data" in progress:
            return _repair_unresolved_reporting_rows(progress["candidate_reporting_data"], progress)

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
            source = "youtube" if "youtube.com" in url or "youtu.be" in url or url.startswith("#youtube") else "podcasts"
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
        title = _candidate_title_from_mapping(ex, fallback="Excluded Candidate")
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
        title = _candidate_title_from_mapping(note, fallback="Filtered Candidate")
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
        try:
            editorial_data = json.loads(editorial_str)
            decisions_list = editorial_data.get("decisions") or []
            for dec in decisions_list:
                idx = dec.get("index")
                decision = dec.get("decision")
                reason = dec.get("reason")
                if idx is not None and decision in ("exclude", "demote"):
                    key = f"editorial-exclusion-{idx}"
                    candidates[key] = {
                        "title": f"Editorial Excluded Candidate (Index {idx})",
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
        except Exception:
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
