"""Brief HTML rendering for Morning Dispatch issues.

Extracted verbatim from backend/app/db/database.py (M2 split). Everything here
takes plain payload/result data and returns HTML strings; nothing touches the
database. Callers that have inference metrics pass them in via digest_stats.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from html import escape, unescape
from typing import Any
from urllib.parse import parse_qsl, urlparse

from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.core.config import get_settings


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _build_digest_stats(
    *,
    configured_source_count: int,
    newsletter_count: int,
    link_count: int,
    podcast_episode_count: int = 0,
    article_results: list[ArticleFetchResult],
    duration_seconds: float | None,
    inference_run_id: str | None,
    stage_seconds: dict[str, float] | None,
) -> dict[str, Any]:
    """Pure fallback equivalent of database.build_digest_stats.

    The renderer never has an inference run to summarize (callers with metrics
    pass digest_stats in), so token/model usage fields are zeroed.
    """
    if inference_run_id is not None:  # pragma: no cover - renderer never passes one
        raise ValueError("brief_renderer cannot summarize inference metrics; pass digest_stats from the caller")
    active_results = [result for result in article_results if result.tier != "dropped"]
    included_count = sum(1 for result in active_results if result.fetched)
    unresolved_count = sum(1 for result in active_results if not result.fetched)
    dropped_count = sum(1 for result in article_results if result.tier == "dropped")
    try:
        processing_seconds = None if duration_seconds is None else float(duration_seconds)
    except (TypeError, ValueError):
        processing_seconds = None
    normalized_stage_seconds: dict[str, float] = {}
    if isinstance(stage_seconds, dict):
        for key, value in stage_seconds.items():
            try:
                normalized_stage_seconds[str(key)] = round(max(0.0, float(value)), 3)
            except (TypeError, ValueError):
                continue
    return {
        "source_count": max(0, int(configured_source_count or 0)),
        "newsletter_count": max(0, int(newsletter_count or 0)),
        "link_count": max(0, int(link_count or 0)),
        "podcast_episode_count": max(0, int(podcast_episode_count or 0)),
        "article_candidate_count": len(article_results),
        "included_article_count": included_count,
        "unresolved_count": unresolved_count,
        "dropped_count": dropped_count,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "model_call_count": 0,
        "model_success_count": 0,
        "model_failure_count": 0,
        "completion_unavailable_count": 0,
        "model_usage": [],
        "processing_seconds": processing_seconds,
        "stage_seconds": normalized_stage_seconds,
    }


RAW_URL_RE = re.compile(r"https?://[^\s<>)\"']+", re.IGNORECASE)


MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]{1,180})\]\((https?://[^)\s]+)[^)]*\)", re.IGNORECASE)


IMAGE_PLACEHOLDER_RE = re.compile(r"(?:[-–—]{2,}\s*)?View image:\s*\([^)]*(?:\)|$)\s*(?:Caption:\s*)?", re.IGNORECASE)


HTML_TAG_RE = re.compile(r"<[^>]+>")


ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff\u00ad]+")


REFERENCE_MARK_RE = re.compile(r"\[\d+\]")


SEPARATOR_RE = re.compile(r"(?:[-–—]\s*){3,}")


NEWSLETTER_UTILITY_LABELS = {
    "advertise",
    "archive",
    "click here",
    "follow on x",
    "manage preferences",
    "read online",
    "read it in full online",
    "sign up",
    "signup",
    "subscribe",
    "unsubscribe",
    "view in browser",
    "view online",
    "work with us",
}


NEWSLETTER_BOILERPLATE_PATTERNS = (
    re.compile(r"\bOops!\s*Looks like your email provider is scrambling the email.*?(?=(?:[A-Z][a-z]+[,.:;!?]|\Z))", re.IGNORECASE),
    re.compile(r"\bClick here to read it in full online:?", re.IGNORECASE),
    re.compile(r"\bWe'd hate to see you go,\s*but if you want to unsubscribe.*$", re.IGNORECASE),
    re.compile(r"\bIf you want to unsubscribe,\s*please click here:?.*$", re.IGNORECASE),
    re.compile(r"\bTogether with\s*·?\s*Today's Author\b.*$", re.IGNORECASE),
    re.compile(r"\bToday's Author\b.*$", re.IGNORECASE),
    re.compile(r"\bView Online\s+TLDR\s+TOGETHER WITH\b.*$", re.IGNORECASE),
    re.compile(r"\bTLDR\s+TOGETHER WITH\b.*$", re.IGNORECASE),
    re.compile(r"\bSignup\s*\|\s*Work With Us\s*\|\s*Follow on X\s*\|\s*Archive\b", re.IGNORECASE),
    re.compile(r"\bSign up\s*\|\s*Work With Us\s*\|\s*Follow on X\s*\|\s*Archive\b", re.IGNORECASE),
)


FOLLOW_IMAGE_RE = re.compile(r"Follow image link:\s*(?:\([^)]*\)|\S*)\s*(?:Caption:\s*)?", re.IGNORECASE)


NEWSLETTER_LOW_VALUE_RE = re.compile(
    r"\b(?:"
    r"email provider is scrambling|"
    r"read it in full online|"
    r"unsubscribe|"
    r"manage preferences|"
    r"view in browser|"
    r"view online"
    r")\b",
    re.IGNORECASE,
)


def render_ingested_issue(
    title: str,
    snapshot: str,
    payloads: list[NormalizedPayload],
    article_results: list[ArticleFetchResult] | None,
    lookback_hours: int,
    generated_at: str | None = None,
    issue_id: str | None = None,
    digest_stats: dict[str, Any] | None = None,
    newsletter_payloads: list[NormalizedPayload] | None = None,
    source_selection: dict[str, bool] | None = None,
) -> str:
    article_results = article_results or []
    body_payloads = (
        list(newsletter_payloads)
        if newsletter_payloads is not None
        else [payload for payload in payloads if payload.source_type == "gmail"]
    )
    visible_results = [result for result in article_results if result.tier != "dropped"]
    fetched_articles = [result for result in visible_results if result.fetched]
    headline_fallback_articles = [
        result
        for result in visible_results
        if (
            not result.fetched
            and result.tier == "lower_confidence"
            and not _is_media_result(result)
            and (result.payload.metadata or {}).get("search_provider") == "google_news_rss"
        )
    ]
    market_articles = [result for result in fetched_articles if result.payload.source_type == "market_snapshot"]
    story_articles = [
        result
        for result in fetched_articles
        if not _is_media_result(result) and result.payload.source_type != "market_snapshot"
    ]
    media_articles = [result for result in fetched_articles if _is_media_result(result)]
    lead_article = next((result for result in story_articles if result.tier == "lead"), None)
    if lead_article is None and story_articles:
        lead_article = story_articles[0]
    # Non-lower-confidence story items, lead first. These feed both the cross-source
    # Top Stories section (item 5) and the per-source sections (item 4).
    main_story_articles = [
        result
        for result in story_articles
        if result is not lead_article and result.tier != "lower_confidence"
    ]

    def _top_story_score(result: ArticleFetchResult) -> float:
        return result.relevance_score if result.relevance_score is not None else result.link_score

    # Podcast episodes (subscription model) are eligible for Top Stories when
    # compelling, while otherwise remaining in the Listen lane (item 9).
    compelling_podcasts = sorted(
        [
            result
            for result in media_articles
            if result.payload.source_type == "podcast_episode"
            and (result.tier == "lead" or _top_story_score(result) >= _PODCAST_TOP_STORY_THRESHOLD)
        ],
        key=_top_story_score,
        reverse=True,
    )[:_MAX_PODCAST_TOP_STORIES]

    mixable = main_story_articles + compelling_podcasts
    mixable.sort(key=_top_story_score, reverse=True)
    ordered_main = ([lead_article] if lead_article else []) + mixable
    lower_confidence_articles = [
        result
        for result in [*story_articles, *headline_fallback_articles]
        if result is not lead_article and result.tier == "lower_confidence"
    ]

    # Top Stories (item 5): the best items mixed across every story source.
    top_stories = ordered_main[:_TOP_STORIES_TARGET]
    # A podcast promoted into Top Stories must not also render in the Listen lane.
    promoted_podcast_ids = {
        result.payload.id for result in top_stories if result.payload.source_type == "podcast_episode"
    }
    if promoted_podcast_ids:
        media_articles = [
            result for result in media_articles if result.payload.id not in promoted_podcast_ids
        ]
    # Per-source sections are story-only; podcasts beyond Top Stories stay in Listen.
    per_source_remainder = [
        result
        for result in ordered_main[_TOP_STORIES_TARGET:]
        if not _is_media_result(result)
    ]

    newsletter_items = [_render_newsletter_item(payload) for payload in body_payloads]
    newsletter_html = "\n".join(item for item in newsletter_items if item)
    effective_stats = digest_stats or _build_digest_stats(
        configured_source_count=0,
        newsletter_count=len(body_payloads),
        link_count=sum(1 for payload in payloads if payload.source_type == "gmail_link"),
        podcast_episode_count=sum(1 for payload in payloads if payload.source_type == "podcast_episode"),
        article_results=article_results,
        duration_seconds=None,
        inference_run_id=None,
        stage_seconds=None,
    )
    market_html = _render_market_snapshot_section(market_articles)
    image_strip_html = _render_image_strip([result for result in [*top_stories, *media_articles] if result])

    # Top Stories: lead block for the single best item, story-rows for the rest.
    # Media items promoted into Top Stories (e.g. a compelling podcast, item 9) keep
    # their media-card presentation (player + transcript) instead of a text row.
    def _render_top_story(result: ArticleFetchResult, *, index: int, lead: bool) -> str:
        if _is_media_result(result):
            return _render_media_card(result, issue_id=issue_id)
        return (
            _render_lead_story(result, issue_id=issue_id)
            if lead
            else _render_ranked_story(result, index=index, issue_id=issue_id)
        )

    lead_html = _render_top_story(top_stories[0], index=0, lead=True) if top_stories else ""
    running_index = 1
    top_rows_html = "\n".join(
        _render_top_story(result, index=running_index + offset, lead=False)
        for offset, result in enumerate(top_stories[1:])
    )
    running_index += max(0, len(top_stories) - 1)

    # Per-source sections (item 4): each story source gets its own labeled section
    # for the items beyond Top Stories, in a stable display order.
    per_source_sections_html, running_index = _render_source_sections(
        per_source_remainder, start_index=running_index, issue_id=issue_id
    )

    # Media gets dedicated per-source sections too (Watch for video, Listen for audio).
    media_sections_html = _render_media_sections(media_articles, issue_id=issue_id)

    # Dedicated real estate for EVERY selected source (item: per-source honesty).
    # A selected source that produced zero rendered items still gets a labeled block
    # stating so, instead of silently vanishing from the brief.
    empty_source_html = _render_empty_source_notes(
        source_selection,
        rendered_results=[*story_articles, *headline_fallback_articles, *media_articles, *market_articles],
    )

    lower_html = "\n".join(
        _render_lower_confidence_story(result, index=index, issue_id=issue_id)
        for index, result in enumerate(lower_confidence_articles, start=running_index)
    )
    sidebar_html = _render_brief_sidebar(
        stats=effective_stats,
        market_html=market_html,
        newsletter_html=newsletter_html,
        newsletter_count=len(newsletter_items),
        article_count=len(story_articles) + len(headline_fallback_articles),
        media_count=len(media_articles),
        source_results=[*story_articles, *headline_fallback_articles, *media_articles, *market_articles],
        lookback_hours=lookback_hours,
    )
    empty_state = ""
    if not payloads:
        empty_state = """
        <section class="empty">
          <strong>No source content was found for this brief.</strong>
          Try broadening the interest or adding more sources before rebuilding.
        </section>
        """
    ranked_empty = ""
    if not lead_html and not top_rows_html and not per_source_sections_html and not media_sections_html:
        ranked_empty = '<p class="meta">No stories were ready for this brief.</p>'
    media_section = media_sections_html
    lower_section = ""
    if lower_html:
        lower_section = f"""
        <section class="lower-confidence" aria-labelledby="lower-confidence-heading">
          <div class="section-kicker">Lower confidence</div>
          <h2 id="lower-confidence-heading">Worth a skim</h2>
          <div class="low-conf-list">{lower_html}</div>
        </section>
        """
    generated_value = generated_at or utc_now()
    masthead_meta = _render_masthead_meta(generated_value, lookback_hours, effective_stats)
    dateline_label = escape(_format_brief_dateline(generated_value))
    feedback_script = _render_feedback_script(issue_id)
    podcast_script = _render_podcast_modal_script()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Playfair+Display:ital,wght@0,700;0,800;0,900;1,700&display=swap" rel="stylesheet" />
  <style>
    :root {{
      color: #1a1a1a;
      background: #fafaf9;
      --paper: #ffffff;
      --paper-deep: #fafaf9;
      --ink: #1a1a1a;
      --muted: #6b6b66;
      --line: #eaeae5;
      --accent: #1e3a8a;
      --accent-dark: #172554;
      --sidebar: #f5f5f0;
      --shadow: 0 12px 40px rgba(0, 0, 0, .04);
      --display: 'Playfair Display', Georgia, serif;
      --body: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      --mono: 'JetBrains Mono', monospace;
    }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
    body {{
      margin: 0;
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-family: var(--body);
      color: #1a1a1a;
      color: var(--ink);
      background: #fafaf9;
      background: var(--paper-deep);
      -webkit-font-smoothing: antialiased;
    }}
    .brief-shell {{
      max-width: 1180px;
      width: 100%;
      margin: 0 auto;
      padding: 40px 24px 80px;
    }}
    .brief-masthead {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: center;
      border-bottom: 2px solid #1a1a1a;
      border-bottom: 2px solid var(--ink);
      padding-bottom: 20px;
      margin-bottom: 36px;
    }}
    .masthead-brand {{
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-size: clamp(1.8rem, 4.5vw, 3.2rem);
      font-weight: 800;
      line-height: 1;
      letter-spacing: -0.02em;
    }}
    .masthead-meta, .dateline, .section-kicker, .meta {{
      font: 700 .7rem/1.4 'JetBrains Mono', monospace;
      font: 700 .7rem/1.4 var(--mono);
      color: #6b6b66;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .masthead-meta {{ max-width: 48ch; text-align: right; }}
    .brief-header {{ display: flex; flex-direction: column; gap: 12px; max-width: 920px; margin-bottom: 36px; }}
    h1 {{
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-size: 3rem;
      font-weight: 850;
      line-height: 1.05;
      margin: 0;
      letter-spacing: -0.01em;
    }}
    .brief-header h1 {{ display: -webkit-box; max-height: 12rem; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 4; overflow-wrap: break-word; word-break: normal; hyphens: auto; }}
    h2 {{
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-size: clamp(1.6rem, 3.2vw, 2.4rem);
      line-height: 1.1;
      margin: 0 0 20px;
      letter-spacing: -0.01em;
    }}
    h3 {{
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-size: clamp(1.25rem, 2.5vw, 1.75rem);
      line-height: 1.15;
      margin: 0;
      letter-spacing: -0.01em;
    }}
    h1, h2, h3, h4, p, a, .meta, .story-title, .side-value {{ overflow-wrap: anywhere; }}
    a {{ color: inherit; text-decoration-thickness: 1px; text-underline-offset: 4px; transition: color 0.15s ease; }}
    a:hover {{
      color: #1e3a8a;
      color: var(--accent);
    }}
    img, video, iframe, table {{ max-width: 100%; }}
    .brief-body {{
      display: flex;
      flex-wrap: wrap;
      gap: 40px;
      align-items: start;
    }}
    .story-column, .brief-sidebar, .lead-block, .story-row, .media-card, .low-conf-row, .newsletter {{ min-width: 0; }}
    .story-column {{
      flex: 1 1 600px;
      display: flex;
      flex-direction: column;
      gap: 36px;
    }}
    .brief-sidebar {{
      flex: 0 0 340px;
      min-width: 290px;
      position: sticky;
      top: 24px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}
    .img-strip {{
      display: flex;
      gap: 12px;
    }}
    .strip-frame, .story-thumb, .media-thumb {{
      position: relative;
      overflow: hidden;
      background: #eaeae0;
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .strip-frame {{ flex: 1; aspect-ratio: 4 / 3; }}
    .strip-link {{ display: block; width: 100%; height: 100%; }}
    .strip-frame img, .story-thumb img, .media-thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .fallback-art {{
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100%;
      color: #1e3a8a;
      color: var(--accent);
      background: linear-gradient(135deg, #f5f5f0, #eaeae0);
    }}
    .fallback-art svg {{ width: 30px; height: 30px; }}
    .lead-block {{
      display: flex;
      gap: 20px;
      padding: 28px 0;
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid #eaeae5;
      border-bottom: 1px solid var(--line);
    }}
    .lead-bar {{
      flex: 0 0 6px;
      background: #1e3a8a;
      background: var(--accent);
      border-radius: 3px;
    }}
    .lead-content {{
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .lead-title {{
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-size: clamp(2rem, 4.5vw, 2.8rem);
      line-height: 1.1;
      font-weight: 800;
      letter-spacing: -0.015em;
    }}
    .lead-summary {{ font-size: 1.02rem; line-height: 1.6; margin: 0; color: #333330; }}
    .story-meta, .chip-row, .keywords, .feedback-controls {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    .source-type, .score {{
      display: inline-flex;
      align-items: center;
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 8px;
      font: 700 .62rem/1 'JetBrains Mono', monospace;
      font: 700 .62rem/1 var(--mono);
      color: #6b6b66;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .06em;
      background: rgba(255, 255, 255, 0.8);
    }}
    .source-type.youtube, .source-type.podcast, .source-type.foreign-media, .translation-badge {{
      color: #1e3a8a;
      color: var(--accent);
      border-color: rgba(30, 58, 138, .15);
      background: rgba(30, 58, 138, .04);
    }}
    .translation-badge.low, .translation-badge.unavailable {{ color: #b45309; border-color: rgba(180, 83, 9, .15); background: rgba(180, 83, 9, .04); }}
    .translation-original {{ margin-top: 10px; color: #6b6b66; color: var(--muted); font-size: .84rem; }}
    .translation-original summary {{
      cursor: pointer;
      font-weight: 700;
      color: #1e3a8a;
      color: var(--accent);
    }}
    .translation-original p {{ margin: 8px 0 0; line-height: 1.5; }}
    .keywords {{
      margin-top: 10px;
      font: 500 .68rem/1.5 'JetBrains Mono', monospace;
      font: 500 .68rem/1.5 var(--mono);
      color: #6b6b66;
      color: var(--muted);
    }}
    .keywords span {{
      border-bottom: 1px dotted #eaeae5;
      border-bottom: 1px dotted var(--line);
    }}
    .ranked-section, .top-stories-section, .source-section, .media-section, .lower-confidence {{
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
      padding-top: 24px;
    }}
    .story-list {{ display: flex; flex-direction: column; gap: 0; }}
    .story-row {{
      display: flex;
      gap: 20px;
      padding: 24px 0;
      border-bottom: 1px solid #eaeae5;
      border-bottom: 1px solid var(--line);
      align-items: start;
    }}
    .story-num {{
      font-family: 'JetBrains Mono', monospace;
      font-family: var(--mono);
      font-size: 0.9rem;
      line-height: 1;
      color: #1e3a8a;
      color: var(--accent);
      font-weight: 700;
      background: #f5f5f0;
      background: var(--sidebar);
      width: 32px;
      height: 32px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      flex: 0 0 32px;
    }}
    .story-copy {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }}
    .story-title {{
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-size: clamp(1.3rem, 2.2vw, 1.7rem);
      line-height: 1.15;
      font-weight: 800;
      letter-spacing: -0.01em;
    }}
    .story-summary, .low-conf-row p, .newsletter p, .youtube-summary p, .podcast-summary p, .podcast-transcript p {{ font-size: .95rem; line-height: 1.6; margin: 0; color: #40403d; }}
    .market-snapshot {{
      background: #ffffff;
      background: var(--paper);
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 12px 40px rgba(0, 0, 0, .04);
      box-shadow: var(--shadow);
    }}
    .market-snapshot h2 {{ font-size: 1.25rem; margin-bottom: 14px; letter-spacing: -0.01em; }}
    .market-grid {{ display: flex; flex-direction: column; gap: 0; }}
    .market-card {{
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 12px 0;
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
    }}
    .market-symbol {{ display: flex; flex-direction: column; gap: 3px; flex: 0 0 80px; }}
    .market-symbol strong {{
      font: 700 0.95rem/1 'JetBrains Mono', monospace;
      font: 700 0.95rem/1 var(--mono);
      color: #1a1a1a;
      color: var(--ink);
    }}
    .market-symbol span {{
      color: #6b6b66;
      color: var(--muted);
      font: 700 .54rem/1.2 'JetBrains Mono', monospace;
      font: 700 .54rem/1.2 var(--mono);
      text-transform: uppercase;
      letter-spacing: .06em;
    }}
    .market-performance {{ display: flex; flex-direction: column; gap: 5px; min-width: 0; flex: 1; }}
    .market-row {{ display: flex; align-items: baseline; justify-content: space-between; gap: 8px; flex-wrap: wrap; }}
    .market-price {{
      font: 700 .88rem/1 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 700 .88rem/1 var(--body);
    }}
    .market-change {{
      font: 700 .65rem/1 'JetBrains Mono', monospace;
      font: 700 .65rem/1 var(--mono);
    }}
    .market-change.up {{ color: #166534; }}
    .market-change.down {{ color: #991b1b; }}
    .market-change.flat {{
      color: #6b6b66;
      color: var(--muted);
    }}
    .sparkline {{ width: 100%; height: 26px; display: block; overflow: visible; }}
    .sparkline path {{
      fill: none;
      stroke: #1e3a8a;
      stroke: var(--accent);
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    .sparkline .baseline {{
      stroke: #eaeae5;
      stroke: var(--line);
      stroke-width: 1;
    }}
    .story-thumb {{ flex: 0 0 112px; aspect-ratio: 1; border-radius: 6px; }}
    .media-grid {{ display: flex; flex-wrap: wrap; gap: 20px; }}
    .media-card {{
      flex: 1 1 calc(50% - 10px);
      min-width: 280px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: 20px;
      background: #ffffff;
      background: var(--paper);
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 12px 40px rgba(0, 0, 0, .04);
      box-shadow: var(--shadow);
    }}
    .media-thumb {{ aspect-ratio: 16 / 9; border-radius: 6px; }}
    .media-title {{
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-size: 1.35rem;
      line-height: 1.15;
      font-weight: 800;
      letter-spacing: -0.01em;
    }}
    .media-cta {{
      align-self: flex-start;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid #1e3a8a;
      border: 1px solid var(--accent);
      border-radius: 8px;
      color: #1e3a8a;
      color: var(--accent);
      padding: 8px 14px;
      font: 700 .74rem/1 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 700 .74rem/1 var(--body);
      text-decoration: none;
      transition: all 0.15s ease;
    }}
    .media-cta:hover {{
      background: #1e3a8a;
      background: var(--accent);
      color: #ffffff;
    }}
    .low-conf-list {{ display: flex; flex-direction: column; gap: 0; }}
    .low-conf-row {{
      display: flex;
      gap: 16px;
      padding: 18px 0;
      border-bottom: 1px solid #eaeae5;
      border-bottom: 1px solid var(--line);
      opacity: .85;
    }}
    .low-conf-row > div:last-child {{ flex: 1; min-width: 0; }}
    .low-conf-row .story-num {{
      font-size: 0.85rem;
      color: #6b6b66;
      color: var(--muted);
      background: #fafaf9;
      background: var(--paper-deep);
    }}
    .side-panel {{
      background: #ffffff;
      background: var(--paper);
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 12px 40px rgba(0, 0, 0, .04);
      box-shadow: var(--shadow);
    }}
    .side-panel h2 {{ font-size: 1.25rem; margin-bottom: 16px; letter-spacing: -0.01em; }}
    .side-stats {{ display: flex; flex-wrap: wrap; gap: 14px; }}
    .side-stat {{
      flex: 1 1 calc(50% - 7px);
      min-width: 120px;
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }}
    .side-stat span {{
      display: block;
      font: 700 .62rem/1.3 'JetBrains Mono', monospace;
      font: 700 .62rem/1.3 var(--mono);
      color: #6b6b66;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .side-stat strong {{
      display: block;
      margin-top: 4px;
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-size: 1.35rem;
      line-height: 1;
      font-weight: 800;
    }}
    .source-mix {{
      margin-top: 18px;
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }}
    .source-mix h3 {{
      margin: 0 0 10px;
      font: 700 .68rem/1.3 'JetBrains Mono', monospace;
      font: 700 .68rem/1.3 var(--mono);
      color: #6b6b66;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .source-mix-row {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      padding: 6px 0;
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
    }}
    .source-mix-row:first-of-type {{ border-top: 0; }}
    .source-mix-label {{
      color: #40403d;
      font: 600 .78rem/1.3 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 600 .78rem/1.3 var(--body);
    }}
    .source-mix-count {{
      font: 700 .78rem/1 'JetBrains Mono', monospace;
      font: 700 .78rem/1 var(--mono);
      color: #1a1a1a;
      color: var(--ink);
    }}
    .side-note {{
      margin-top: 18px;
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }}
    .side-note h3 {{
      margin: 0 0 8px;
      font: 700 .68rem/1.3 'JetBrains Mono', monospace;
      font: 700 .68rem/1.3 var(--mono);
      color: #6b6b66;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .side-note p {{ margin: 0; color: #40403d; font-size: .84rem; line-height: 1.5; }}
    .stage-list {{
      margin: 10px 0 0;
      padding-left: 18px;
      color: #6b6b66;
      color: var(--muted);
      font: 600 .74rem/1.6 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 600 .74rem/1.6 var(--body);
    }}
    details.source-notes {{
      margin-top: 20px;
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }}
    details.source-notes summary {{
      cursor: pointer;
      font: 700 .68rem/1.3 'JetBrains Mono', monospace;
      font: 700 .68rem/1.3 var(--mono);
      color: #6b6b66;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
      outline: none;
    }}
    .newsletter {{
      padding: 16px 0;
      border-bottom: 1px solid #eaeae5;
      border-bottom: 1px solid var(--line);
    }}
    .newsletter h3 {{ font-size: 1.05rem; line-height: 1.2; margin-top: 6px; font-weight: 700; }}
    .feedback-controls {{ margin-top: 14px; }}
    .feedback-controls button {{
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      background: var(--paper);
      color: #1e3a8a;
      color: var(--accent);
      padding: 6px 12px;
      font: 700 .72rem/1 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 700 .72rem/1 var(--body);
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .feedback-controls button:hover {{
      background: #f5f5f0;
      background: var(--sidebar);
    }}
    .feedback-controls[data-feedback='sent'] button {{ opacity: .55; }}
    .feedback-state {{
      color: #6b6b66;
      color: var(--muted);
      font: 700 .68rem 'JetBrains Mono', monospace;
      font: 700 .68rem var(--mono);
      text-transform: uppercase;
    }}
    .podcast-modal-link, .youtube-modal-link, .newsletter-modal-link {{ color: inherit; text-decoration: none; }}
    .podcast-modal-link:hover, .youtube-modal-link:hover, .newsletter-modal-link:hover {{ text-decoration: underline; }}
    .podcast-modal {{ position: fixed; inset: 0; z-index: 20; display: none; place-items: center; padding: 24px; background: rgba(0, 0, 0, .4); backdrop-filter: blur(4px); }}
    .podcast-modal:target {{ display: flex; align-items: center; justify-content: center; }}
    .podcast-panel {{
      width: min(920px, 100%);
      max-height: min(86vh, 980px);
      overflow: auto;
      background: #ffffff;
      background: var(--paper);
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 20px 80px rgba(0,0,0,0.12);
      padding: 32px;
    }}
    .podcast-close {{
      float: right;
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #1a1a1a;
      background: var(--ink);
      color: #ffffff;
      padding: 8px 14px;
      font: 700 .72rem/1 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 700 .72rem/1 var(--body);
      cursor: pointer;
      text-decoration: none;
      transition: opacity 0.15s ease;
    }}
    .podcast-close:hover {{ opacity: 0.9; }}
    .podcast-brand {{ display: flex; gap: 20px; align-items: center; margin: 14px 0 24px; }}
    .podcast-art {{ width: 120px; aspect-ratio: 1; object-fit: cover; border-radius: 8px; border: 1px solid #eaeae5; border: 1px solid var(--line); background: #eaeae0; }}
    .podcast-art.fallback {{
      display: flex;
      align-items: center;
      justify-content: center;
      font-family: 'Playfair Display', Georgia, serif;
      font-family: var(--display);
      font-weight: 800;
      font-size: 1.8rem;
      color: #1e3a8a;
      color: var(--accent);
    }}
    .podcast-panel h3 {{ font-size: clamp(1.6rem, 3.5vw, 2.6rem); line-height: 1.1; margin: 0 0 10px; }}
    .podcast-actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin: 12px 0 20px;
      font: 700 .72rem 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 700 .72rem var(--body);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .podcast-actions a {{
      color: #1e3a8a;
      color: var(--accent);
    }}
    .podcast-speed-controls {{ display: flex; gap: 8px; margin: 0 0 20px; align-items: center; flex-wrap: wrap; }}
    .podcast-speed-controls button {{
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #ffffff;
      background: var(--paper);
      padding: 6px 10px;
      font: 700 .7rem 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 700 .7rem var(--body);
      cursor: pointer;
    }}
    .podcast-speed-controls button.active {{
      border-color: #1e3a8a;
      border-color: var(--accent);
      background: #1e3a8a;
      background: var(--accent);
      color: #ffffff;
    }}
    .podcast-player {{ width: 100%; margin: 4px 0 20px; border-radius: 8px; }}
    .youtube-panel {{ width: min(1040px, 100%); }}
    .youtube-player {{
      width: 100%;
      aspect-ratio: 16 / 9;
      border-radius: 12px;
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      background: #000000;
      margin: 8px 0 24px;
      overflow: hidden;
    }}
    .youtube-summary, .podcast-summary, .podcast-transcript, .newsletter-body {{
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
      padding-top: 20px;
      margin-top: 20px;
    }}
    .youtube-summary h4, .podcast-summary h4, .podcast-transcript h4, .newsletter-body h4 {{
      margin: 0 0 12px;
      font: 700 .72rem/1.2 'JetBrains Mono', monospace;
      font: 700 .72rem/1.2 var(--mono);
      color: #6b6b66;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .newsletter-body p {{ margin: 0 0 14px; font-size: .98rem; line-height: 1.65; color: #333330; }}
    .foreign-tabs {{ display: flex; gap: 8px; margin: 16px 0; flex-wrap: wrap; }}
    .foreign-tabs button {{
      border: 1px solid #eaeae5;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      background: var(--paper);
      padding: 8px 12px;
      font: 700 .72rem/1 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 700 .72rem/1 var(--body);
      cursor: pointer;
    }}
    .foreign-tabs button.active {{
      background: #1e3a8a;
      background: var(--accent);
      color: #ffffff;
      border-color: #1e3a8a;
      border-color: var(--accent);
    }}
    .foreign-view[hidden] {{ display: none; }}
    .foreign-view {{
      border-top: 1px solid #eaeae5;
      border-top: 1px solid var(--line);
      padding-top: 20px;
    }}
    .foreign-status, .foreign-notice, .foreign-provenance {{
      color: #6b6b66;
      color: var(--muted);
      font: 600 .8rem/1.5 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 600 .8rem/1.5 var(--body);
    }}
    .foreign-provenance {{ margin: 8px 0 0; }}
    .foreign-body p {{ margin: 0 0 14px; font-size: .98rem; line-height: 1.65; color: #333330; }}
    body.modal-open {{ overflow: hidden; }}
    .empty {{
      margin-top: 36px;
      padding: 24px;
      border: 1px dashed #eaeae5;
      border: 1px dashed var(--line);
      border-radius: 12px;
      font: 1rem 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font: 1rem var(--body);
      background: #ffffff;
      background: var(--paper);
    }}
    @media (max-width: 900px) {{
      .brief-shell {{ padding: 32px 16px 60px; }}
      .brief-header h1 {{ font-size: 2.4rem; line-height: 1.1; max-height: 9.6rem; }}
      .brief-masthead {{ align-items: flex-start; flex-direction: column; }}
      .masthead-meta {{ max-width: none; text-align: left; }}
      .brief-body, .media-grid, .podcast-brand {{ flex-direction: column; }}
      .brief-sidebar {{ position: static; }}
      .market-card {{ flex-direction: column; align-items: flex-start; }}
      .story-row {{ flex-direction: row; }}
      .story-thumb {{ display: none; }}
      .img-strip {{ flex-direction: column; }}
      .strip-frame {{ aspect-ratio: 16 / 9; }}
      .podcast-panel {{ max-height: 90vh; padding: 24px; }}
    }}
    @media (max-width: 480px) {{
      .brief-shell {{ padding-inline: 12px; }}
      .brief-header h1 {{ font-size: 1.85rem; line-height: 1.1; max-height: 7.4rem; }}
      .lead-block {{ gap: 16px; }}
      .side-stats {{ flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <main class="brief-shell">
    <header class="brief-masthead">
      <div class="masthead-brand">Morning Dispatch</div>
      <div class="masthead-meta">{masthead_meta}</div>
    </header>
    <section class="brief-header">
      <div class="dateline">{dateline_label}</div>
      <h1>{escape(title)}</h1>
    </section>
    {empty_state}
    <div class="brief-body">
      <div class="story-column">
        {image_strip_html}
        {lead_html}
        <section class="top-stories-section" aria-labelledby="top-stories-heading">
          <div class="section-kicker">Across all sources</div>
          <h2 id="top-stories-heading">Top stories</h2>
          <div class="story-list">{top_rows_html or ranked_empty or '<p class="meta">No additional stories this run.</p>'}</div>
        </section>
        {per_source_sections_html}
        {media_section}
        {empty_source_html}
        {lower_section}
      </div>
      {sidebar_html}
    </div>
	  </main>
	  {feedback_script}
	  {podcast_script}
	</body>
	</html>"""


def render_placeholder_issue(title: str, snapshot: str, generated_at: str | None = None) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&family=Playfair+Display:wght@700;800&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
    body {{ margin: 0; font-family: 'Plus Jakarta Sans', sans-serif; color: #1a1a1a; background: #fafaf9; -webkit-font-smoothing: antialiased; }}
    main {{ width: min(800px, 100%); margin: 0 auto; padding: 48px 24px; }}
    header {{ border-bottom: 2px solid #1a1a1a; padding-bottom: 18px; margin-bottom: 28px; }}
    h1 {{ font-family: 'Playfair Display', serif; font-size: 2.6rem; line-height: 1.1; margin: 0; letter-spacing: -0.01em; display: -webkit-box; max-height: 10.4rem; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 4; overflow-wrap: break-word; word-break: normal; hyphens: auto; }}
    h1, p {{ overflow-wrap: anywhere; }}
    .date {{ margin-top: 12px; font: 700 0.72rem 'Plus Jakarta Sans', sans-serif; text-transform: uppercase; color: #6b6b66; letter-spacing: 0.08em; }}
    .snapshot {{ font-size: 1.25rem; line-height: 1.55; max-width: 720px; color: #333330; }}
    .empty {{ margin-top: 32px; padding-top: 24px; border-top: 1px solid #eaeae5; font: 0.95rem 'Plus Jakarta Sans', sans-serif; }}
    @media (max-width: 640px) {{
      h1 {{ font-size: 1.95rem; line-height: 1.1; max-height: 7.8rem; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{title}</h1>
      <div class="date">Building your brief</div>
    </header>
    <p class="snapshot">{snapshot}</p>
    <section class="empty">
      <strong>Your brief is on its way.</strong>
      Stories will appear here once sources have been searched and ranked. Check back in a moment.
    </section>
  </main>
</body>
</html>"""


def _nullable_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _render_newsletter_item(payload: NormalizedPayload) -> str:
    subject = _title_for_payload(payload)
    sender = payload.source_name or "Gmail"
    snippet = _summary_for_payload(payload, max_chars=700)
    if _weak_newsletter_snippet(snippet):
        return ""
    published = _format_issue_date(payload.published_at)
    return f"""
      <article class="newsletter">
        <div class="meta">{escape(sender)} · {escape(published)}</div>
        <h3>{escape(subject)}</h3>
        <p>{escape(snippet)}</p>
      </article>
    """


def _is_media_result(result: ArticleFetchResult) -> bool:
    return result.payload.source_type in {"podcast_episode", "youtube_video"} or result.content_type in {"podcast", "video"}


def _result_metadata(result: ArticleFetchResult) -> dict[str, Any]:
    return {**(result.payload.metadata or {}), **(result.metadata or {})}


def _result_url(result: ArticleFetchResult) -> str:
    return result.final_url or result.original_url or result.canonical_url or "#"


def _reader_url(result: ArticleFetchResult) -> str:
    for value in (result.canonical_url, result.final_url, result.original_url, result.payload.original_url):
        url = _safe_web_url(value)
        if url and not _is_gmail_message_url(url):
            return url
    for value in (result.canonical_url, result.final_url, result.original_url, result.payload.original_url):
        url = _safe_web_url(value)
        if url:
            return url
    return "#"


def _is_embedded_newsletter_result(result: ArticleFetchResult) -> bool:
    if result.payload.source_type != "gmail":
        return False
    return _is_gmail_message_url(_reader_url(result)) or _reader_url(result) == "#"


def _is_gmail_message_url(value: str | None) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.netloc.lower() == "mail.google.com"


def _result_image_url(result: ArticleFetchResult) -> str | None:
    metadata = _result_metadata(result)
    for key in ("image_url", "thumbnail_url"):
        image_url = _safe_web_url(metadata.get(key))
        if image_url:
            return image_url
    if result.payload.source_type == "youtube_video":
        video_id = _youtube_video_id(metadata.get("video_id"), _result_url(result))
        if video_id:
            return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    return None


def _youtube_video_id(raw_video_id: Any, youtube_url: str) -> str:
    video_id = str(raw_video_id or "").strip()
    if not video_id and youtube_url:
        parsed = urlparse(youtube_url)
        hostname = parsed.hostname or ""
        if "youtu.be" in hostname:
            video_id = parsed.path.strip("/")
        elif "youtube" in hostname:
            query = dict(parse_qsl(parsed.query, keep_blank_values=False))
            video_id = str(query.get("v") or "").strip()
    return video_id if re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id) else ""


def _source_label(result: ArticleFetchResult) -> str:
    source_type = result.payload.source_type
    if source_type == "youtube_video":
        return "YouTube"
    if source_type == "podcast_episode":
        return "Podcast"
    if source_type == "reddit_thread":
        return "Legacy Discussion"
    if source_type == "gmail_link":
        metadata = result.payload.metadata or {}
        if metadata.get("search_provider") == "google_news_rss":
            return "News"
        if metadata.get("search_query") or metadata.get("search_provider"):
            return "Web Search"
        if _translation_metadata(result).get("translated"):
            # A translated newsletter/web link is a web article, not a Gmail item.
            return "Web"
        return "Gmail"
    if source_type == "collection_chunk":
        return "Collection"
    if source_type == "market_snapshot":
        return "Markets"
    if source_type == "foreign_web":
        return "Foreign Media"
    return "Web"


def _source_class(result: ArticleFetchResult) -> str:
    return _source_label(result).lower().replace(" ", "-")


def _meta_line_for_result(result: ArticleFetchResult) -> str:
    url = _result_url(result)
    domain = result.domain or _domain(url) or _source_label(result).lower()
    source = result.payload.source_name or _source_label(result)
    translation = _translation_metadata(result)
    if translation.get("translated"):
        source = _story_title(result) or source
    published = _format_article_date(result.payload.published_at)
    parts = [domain]
    if published:
        parts.append(published)
    elif _served_once_note(result):
        parts.append(_served_once_note(result))
    parts.append(f"via {source}")
    return " · ".join(part for part in parts if part)


def _served_once_note(result: ArticleFetchResult) -> str:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    if metadata.get("served_once") is True:
        return str(metadata.get("served_once_note") or "Date unknown; shown once.")
    return ""


def _score_badge(result: ArticleFetchResult) -> str:
    if result.relevance_score is None:
        return ""
    return f'<span class="score">{int(result.relevance_score * 100)}%</span>'


def _source_badge(result: ArticleFetchResult) -> str:
    label = _source_label(result)
    return f'<span class="source-type {_source_class(result)}">{escape(label)}</span>{_translation_badge_html(result)}'


def _translation_metadata(result: ArticleFetchResult) -> dict[str, Any]:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    payload_metadata = result.payload.metadata if isinstance(result.payload.metadata, dict) else {}
    translation = metadata.get("translation") or payload_metadata.get("translation")
    return dict(translation) if isinstance(translation, dict) else {}


def _translation_badge_html(result: ArticleFetchResult) -> str:
    translation = _translation_metadata(result)
    source_language = str(translation.get("source_language") or (result.payload.metadata or {}).get("source_language") or "").strip()
    if not source_language:
        return ""
    source_language_name = str(translation.get("source_language_name") or (result.payload.metadata or {}).get("source_language_name") or source_language.upper()).strip()
    quality = _translation_quality_label(translation)
    mode = str(translation.get("mode") or "").strip()
    translator = str(translation.get("translator") or "").strip()
    if translation and not translation.get("translated"):
        label = f"{source_language.upper()} translation unavailable"
        class_name = "source-type translation-badge unavailable"
    else:
        label = f"{source_language.upper()} -> EN"
        if quality:
            label = f"{label} · {quality.upper()}"
        class_name = "source-type translation-badge"
        if quality == "low":
            class_name = f"{class_name} low"
    title_parts = [f"Translated from {source_language_name}"]
    if quality:
        title_parts.append(f"{quality} confidence")
    if translator:
        title_parts.append(f"model: {translator}")
    if mode:
        title_parts.append(f"mode: {mode}")
    title = "; ".join(title_parts)
    return f'<span class="{class_name}" title="{escape(title, quote=True)}">{escape(label)}</span>'


def _translation_original_html(result: ArticleFetchResult) -> str:
    translation = _translation_metadata(result)
    original_title = _clean_newsletter_text(
        str(translation.get("original_title") or (result.payload.metadata or {}).get("original_search_title") or "")
    )
    original_summary = _clean_newsletter_text(
        str(translation.get("original_summary") or (result.payload.metadata or {}).get("original_search_summary") or "")
    )
    if not original_title and not original_summary:
        return ""
    source_language_name = str(translation.get("source_language_name") or (result.payload.metadata or {}).get("source_language_name") or "original").strip()
    context = _translation_context_label(translation)
    summary_label = f"Original {source_language_name} text"
    if context:
        summary_label = f"{summary_label} · {context}"
    title_html = f"<p><strong>Title:</strong> {escape(original_title)}</p>" if original_title else ""
    summary_html = f"<p><strong>Summary:</strong> {escape(original_summary)}</p>" if original_summary else ""
    return (
        f'<details class="translation-original">'
        f"<summary>{escape(summary_label)}</summary>"
        f"{title_html}{summary_html}"
        f"</details>"
    )


def _translation_quality_label(translation: dict[str, Any]) -> str:
    quality = str(
        translation.get("quality") or translation.get("translation_quality") or translation.get("confidence") or ""
    ).strip().lower()
    return quality if quality in {"high", "medium", "low"} else ""


def _translation_context_label(translation: dict[str, Any]) -> str:
    parts: list[str] = []
    quality = _translation_quality_label(translation)
    translator = str(translation.get("translator") or "").strip()
    mode = str(translation.get("mode") or "").strip()
    if quality:
        parts.append(f"{quality} confidence")
    if translator:
        parts.append(translator)
    if mode and mode != "fast":
        parts.append(mode.replace("_", " "))
    return " · ".join(parts)


def _keyword_html(result: ArticleFetchResult) -> str:
    keywords = [keyword for keyword in result.keywords[:5] if keyword]
    if not keywords:
        return ""
    return '<div class="keywords">' + " ".join(f"<span>{escape(keyword)}</span>" for keyword in keywords) + "</div>"


def _story_summary(result: ArticleFetchResult) -> str:
    return _clean_newsletter_text(result.editor_summary or result.excerpt or result.text)


def _story_title(result: ArticleFetchResult) -> str:
    if result.payload.source_type == "podcast_episode":
        return _podcast_story_title(result)
    return _clean_newsletter_text(result.title) or result.title or _result_url(result)


def _podcast_story_title(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    show_name = str(metadata.get("podcast_title") or result.payload.source_name or "").strip()
    episode_title = str(metadata.get("title") or result.title or "").strip()
    if show_name and episode_title:
        return f"{_clean_newsletter_text(show_name)}: {_clean_newsletter_text(episode_title)}"
    if show_name:
        return _clean_newsletter_text(show_name)
    if episode_title:
        return _clean_newsletter_text(episode_title)
    return _clean_newsletter_text(result.title) or result.title or _result_url(result)


def _story_link_parts(result: ArticleFetchResult, *, issue_id: str | None) -> tuple[str, str, str, str, str]:
    url = _reader_url(result)
    if _is_embedded_newsletter_result(result):
        modal_id = _newsletter_modal_id(result)
        attributes = f' data-newsletter-modal-target="{escape(modal_id, quote=True)}"'
        return f"#{modal_id}", "", ' class="newsletter-modal-link"', attributes, _render_newsletter_modal(result, modal_id)
    if not _supports_foreign_article_modal(result, issue_id=issue_id):
        target = ' target="_blank" rel="noreferrer"' if _safe_web_url(url) else ""
        return url, target, "", "", ""
    modal_id = _foreign_article_modal_id(result)
    attributes = _foreign_article_attributes(result, modal_id=modal_id)
    return f"#{modal_id}", "", ' class="foreign-article-link"', attributes, _render_foreign_article_modal(result, modal_id, issue_id)


def _supports_foreign_article_modal(result: ArticleFetchResult, *, issue_id: str | None) -> bool:
    if not issue_id:
        return False
    translation = _translation_metadata(result)
    payload_metadata = result.payload.metadata or {}
    source_language = str(translation.get("source_language") or payload_metadata.get("source_language") or "").strip()
    return bool(source_language and _result_url(result).startswith(("http://", "https://")))


def _foreign_article_attributes(result: ArticleFetchResult, *, modal_id: str) -> str:
    translation = _translation_metadata(result)
    payload_metadata = result.payload.metadata or {}
    original_title = _clean_newsletter_text(
        str(translation.get("original_title") or payload_metadata.get("original_search_title") or "")
    )
    original_summary = _clean_newsletter_text(
        str(translation.get("original_summary") or payload_metadata.get("original_search_summary") or "")
    )
    values = {
        "foreign-article-target": modal_id,
        "foreign-url": _result_url(result),
        "foreign-title": _story_title(result),
        "foreign-summary": _story_summary(result),
        "foreign-source-language": str(translation.get("source_language") or payload_metadata.get("source_language") or ""),
        "foreign-source-language-name": str(translation.get("source_language_name") or payload_metadata.get("source_language_name") or ""),
        "foreign-original-title": original_title,
        "foreign-original-summary": original_summary,
        "foreign-translation-quality": _translation_quality_label(translation),
        "foreign-translation-mode": str(translation.get("mode") or ""),
        "foreign-translator": str(translation.get("translator") or ""),
    }
    return "".join(
        f' data-{escape(key, quote=True)}="{escape(value, quote=True)}"'
        for key, value in values.items()
        if value
    )


def _render_image_strip(results: list[ArticleFetchResult]) -> str:
    frames = []
    for result in results:
        image_url = _result_image_url(result)
        if not image_url:
            continue
        link_url = _reader_url(result)
        image_html = f'<img src="{escape(image_url, quote=True)}" alt="{escape(_story_title(result), quote=True)}" loading="lazy" />'
        if link_url and link_url != "#":
            image_html = f'<a class="strip-link" href="{escape(link_url, quote=True)}">{image_html}</a>'
        frames.append(
            f"""
            <figure class="strip-frame">
              {image_html}
            </figure>
            """
        )
        if len(frames) == 3:
            break
    if not frames:
        return ""
    return f'<section class="img-strip" aria-label="Story images">{"".join(frames)}</section>'


def _render_thumbnail(result: ArticleFetchResult, class_name: str) -> str:
    image_url = _result_image_url(result)
    if image_url:
        return (
            f'<figure class="{class_name}">'
            f'<img src="{escape(image_url, quote=True)}" alt="{escape(_story_title(result), quote=True)}" loading="lazy" />'
            f'</figure>'
        )
    return f'<figure class="{class_name}">{_render_fallback_art(result)}</figure>'


def _render_fallback_art(result: ArticleFetchResult) -> str:
    return f'<div class="fallback-art" aria-hidden="true">{_source_icon_svg(result)}</div>'


def _source_icon_svg(result: ArticleFetchResult) -> str:
    label = _source_label(result)
    if label == "YouTube":
        path = '<path d="M9 7.5v9l8-4.5-8-4.5Z" fill="currentColor"/><rect x="3" y="5" width="18" height="14" rx="4" fill="none" stroke="currentColor" stroke-width="1.8"/>'
    elif label == "Podcast":
        path = '<path d="M12 4a4 4 0 0 1 4 4v4a4 4 0 0 1-8 0V8a4 4 0 0 1 4-4Z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M6 11v1a6 6 0 0 0 12 0v-1M12 18v3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'
    else:
        path = '<path d="M4 18 9.5 9l4 5 2.5-3 4 7H4Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><circle cx="16" cy="7" r="2" fill="currentColor"/><rect x="3" y="4" width="18" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/>'
    return f'<svg viewBox="0 0 24 24" role="img" aria-label="{escape(label)}">{path}</svg>'


def _render_market_snapshot_section(results: list[ArticleFetchResult]) -> str:
    cards = [_render_market_card(result) for result in results[:10]]
    cards = [card for card in cards if card]
    if not cards:
        return ""
    return f"""
      <section class="market-snapshot" aria-labelledby="market-snapshot-heading">
        <div class="section-kicker">Markets</div>
        <h2 id="market-snapshot-heading">Ticker performance</h2>
        <div class="market-grid">{''.join(cards)}</div>
      </section>
    """


def _render_market_card(result: ArticleFetchResult) -> str:
    metadata = _result_metadata(result)
    ticker = str(metadata.get("ticker") or "").strip().upper()
    if not ticker:
        return ""
    company_name = str(metadata.get("company_name") or result.title or ticker).strip()
    price = _market_float(metadata.get("current_price"))
    currency = str(metadata.get("currency") or "").strip()
    change_1d = _market_float(metadata.get("change_1d_pct"))
    change_3m = _market_float(metadata.get("change_3m_pct"))
    if change_3m is None:
        change_3m = _market_float(metadata.get("change_30d_pct"))
    history = _market_price_history(metadata.get("price_history"))
    sparkline = _render_sparkline(history)
    return f"""
      <article class="market-card">
        <div class="market-symbol">
          <strong>{escape(ticker)}</strong>
          <span>{escape(_compact_company_name(company_name, ticker))}</span>
        </div>
        <div class="market-performance">
          <div class="market-row">
            <span class="market-price">{escape(_format_market_price(price, currency))}</span>
            <span class="market-change {_change_class(change_1d)}">{escape(_format_pct(change_1d, "today"))}</span>
            <span class="market-change {_change_class(change_3m)}">{escape(_format_pct(change_3m, "3M"))}</span>
          </div>
          {sparkline}
        </div>
      </article>
    """


def _market_price_history(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    prices: list[float] = []
    for item in value[-90:]:
        raw = item.get("close") if isinstance(item, dict) else item
        number = _market_float(raw)
        if number is not None:
            prices.append(number)
    return prices


def _render_sparkline(prices: list[float]) -> str:
    if len(prices) < 2:
        return '<div class="meta">No 3-month price history available.</div>'
    width = 220
    height = 46
    padding = 3
    low = min(prices)
    high = max(prices)
    span = high - low
    coordinates: list[str] = []
    for index, price in enumerate(prices):
        x = padding + (index / max(1, len(prices) - 1)) * (width - padding * 2)
        y = height / 2 if span == 0 else padding + ((high - price) / span) * (height - padding * 2)
        coordinates.append(f"{x:.1f},{y:.1f}")
    path = "M " + " L ".join(coordinates)
    return (
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Trailing 3-month price sparkline">'
        f'<path class="baseline" d="M {padding} {height / 2:.1f} H {width - padding}" />'
        f'<path d="{escape(path, quote=True)}" /></svg>'
    )


def _market_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _format_market_price(value: float | None, currency: str) -> str:
    if value is None:
        return "Price n/a"
    symbol = "$" if not currency or currency.upper() == "USD" else f"{currency.upper()} "
    return f"{symbol}{value:,.2f}"


def _format_pct(value: float | None, label: str) -> str:
    if value is None:
        return f"n/a {label}"
    return f"{value:+.1f}% {label}"


def _change_class(value: float | None) -> str:
    if value is None or abs(value) < 0.05:
        return "flat"
    return "up" if value > 0 else "down"


def _compact_company_name(company_name: str, ticker: str) -> str:
    cleaned = company_name.replace(f"({ticker})", "").replace(ticker, "").strip(" -")
    return cleaned[:70] or ticker


# Number of cross-source items featured in the Top Stories section (item 5).
_TOP_STORIES_TARGET = 5


# A subscribed podcast episode may be promoted into Top Stories when compelling
# (relevance/link score at or above this), capped so podcasts never dominate.
_PODCAST_TOP_STORY_THRESHOLD = 0.7


_MAX_PODCAST_TOP_STORIES = 2


# Stable display order for per-source brief sections (item 4). Labels not listed
# fall back to alphabetical order after these.
_SOURCE_SECTION_ORDER = (
    "Gmail",
    "News",
    "Web",
    "Foreign Media",
    "Reddit",
    "Collections",
    "Markets",
    "SEC filings",
    "Macro",
)


def _render_source_sections(
    results: list[ArticleFetchResult],
    *,
    start_index: int,
    issue_id: str | None = None,
) -> tuple[str, int]:
    """Render one dedicated section per source (item 4) for non-top-story items.

    Returns the combined HTML and the next running story index so lower-confidence
    numbering continues uninterrupted.
    """
    grouped: dict[str, list[ArticleFetchResult]] = {}
    for result in results:
        label = _origin_source_label(result)
        grouped.setdefault(label, []).append(result)

    ordered_labels = [label for label in _SOURCE_SECTION_ORDER if label in grouped]
    ordered_labels.extend(sorted(label for label in grouped if label not in _SOURCE_SECTION_ORDER))

    sections: list[str] = []
    index = start_index
    for label in ordered_labels:
        rows: list[str] = []
        for result in grouped[label]:
            rows.append(_render_ranked_story(result, index=index, issue_id=issue_id))
            index += 1
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "source"
        sections.append(
            f"""
        <section class="source-section" aria-labelledby="source-{slug}-heading">
          <div class="section-kicker">Source</div>
          <h2 id="source-{slug}-heading">{escape(label)}</h2>
          <div class="story-list">{''.join(rows)}</div>
        </section>
        """
        )
    return "\n".join(sections), index


_SELECTED_SOURCE_LABELS: dict[str, str] = {
    "gmail": "Gmail",
    "google_news": "News",
    "web_search": "Web",
    "foreign_media": "Foreign Media",
    "reddit": "Reddit",
    "youtube": "YouTube",
    "podcasts": "Podcast",
    "markets": "Markets",
    "collections": "Collections",
}


def _render_empty_source_notes(
    source_selection: dict[str, bool] | None,
    *,
    rendered_results: list[ArticleFetchResult],
) -> str:
    """One honest block per selected source that produced nothing this run.

    Only emitted when the caller passes an explicit source_selection (the live
    pipeline), so existing renders without it are unchanged.
    """
    if not source_selection:
        return ""
    rendered_labels = {_origin_source_label(result) for result in rendered_results}
    notes: list[str] = []
    for adapter, label in _SELECTED_SOURCE_LABELS.items():
        if source_selection.get(adapter) is not True:
            continue
        if label in rendered_labels:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "source"
        notes.append(
            f"""
        <section class="source-section source-section-empty" aria-labelledby="source-{slug}-empty-heading">
          <div class="section-kicker">Source</div>
          <h2 id="source-{slug}-empty-heading">{escape(label)}</h2>
          <p class="meta">No usable content found in this source for this run.</p>
        </section>
        """
        )
    return "\n".join(notes)


def _media_section_bucket(result: ArticleFetchResult) -> str:
    if result.payload.source_type == "youtube_video" or result.content_type == "video":
        return "Watch"
    if result.payload.source_type == "podcast_episode" or result.content_type == "podcast":
        return "Listen"
    return "Watch & listen"


def _render_media_sections(
    media_articles: list[ArticleFetchResult],
    *,
    issue_id: str | None = None,
) -> str:
    """Dedicated per-source media sections (item 4): Watch for video, Listen for audio."""
    if not media_articles:
        return ""
    grouped: dict[str, list[ArticleFetchResult]] = {}
    for result in media_articles:
        grouped.setdefault(_media_section_bucket(result), []).append(result)

    sections: list[str] = []
    for heading in ("Watch", "Listen", "Watch & listen"):
        items = grouped.get(heading)
        if not items:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "media"
        cards = "\n".join(_render_media_card(result, issue_id=issue_id) for result in items)
        sections.append(
            f"""
        <section class="media-section" aria-labelledby="media-{slug}-heading">
          <div class="section-kicker">Media</div>
          <h2 id="media-{slug}-heading">{escape(heading)}</h2>
          <div class="media-grid">{cards}</div>
        </section>
        """
        )
    return "\n".join(sections)


def _render_lead_story(result: ArticleFetchResult, *, issue_id: str | None = None) -> str:
    url = _reader_url(result)
    link_url, link_target, link_class, link_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    feedback_html = _render_feedback_controls(issue_id, url) if result.fetched else ""
    return f"""
      <article class="lead-block">
        <div class="lead-bar" aria-hidden="true"></div>
        <div class="lead-content">
          <div class="story-meta">{_source_badge(result)}{_score_badge(result)}<span class="meta">{escape(_meta_line_for_result(result))}</span></div>
          <h2 class="lead-title"><a href="{escape(link_url, quote=True)}"{link_target}{link_class}{link_attributes}>{escape(_story_title(result))}</a></h2>
          <p class="lead-summary">{escape(_story_summary(result))}</p>
          {_translation_original_html(result)}
          {_keyword_html(result)}
          {feedback_html}
          {modal_html}
        </div>
      </article>
    """


def _render_ranked_story(
    result: ArticleFetchResult,
    *,
    index: int,
    issue_id: str | None = None,
) -> str:
    url = _reader_url(result)
    link_url, link_target, link_class, link_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    feedback_html = _render_feedback_controls(issue_id, url) if result.fetched else ""
    return f"""
      <article class="story-row">
        <div class="story-num">{index:02d}</div>
        <div class="story-copy">
          <div class="story-meta">{_source_badge(result)}{_score_badge(result)}<span class="meta">{escape(_meta_line_for_result(result))}</span></div>
          <h3 class="story-title"><a href="{escape(link_url, quote=True)}"{link_target}{link_class}{link_attributes}>{escape(_story_title(result))}</a></h3>
          <p class="story-summary">{escape(_story_summary(result))}</p>
          {_translation_original_html(result)}
          {_keyword_html(result)}
          {feedback_html}
          {modal_html}
        </div>
        {_render_thumbnail(result, "story-thumb")}
      </article>
    """


def _render_lower_confidence_story(
    result: ArticleFetchResult,
    *,
    index: int,
    issue_id: str | None = None,
) -> str:
    url = _reader_url(result)
    link_url, link_target, link_class, link_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    feedback_html = _render_feedback_controls(issue_id, url) if result.fetched else ""
    return f"""
      <article class="low-conf-row">
        <div class="story-num">{index:02d}</div>
        <div>
          <div class="story-meta">{_source_badge(result)}{_score_badge(result)}<span class="meta">{escape(_meta_line_for_result(result))}</span></div>
          <h3 class="story-title"><a href="{escape(link_url, quote=True)}"{link_target}{link_class}{link_attributes}>{escape(_story_title(result))}</a></h3>
          <p>{escape(_story_summary(result))}</p>
          {_translation_original_html(result)}
          {_keyword_html(result)}
          {feedback_html}
          {modal_html}
        </div>
      </article>
    """


def _render_media_card(result: ArticleFetchResult, *, issue_id: str | None = None) -> str:
    url = _result_url(result)
    title_attributes = ""
    title_class = ""
    title_target = ' target="_blank" rel="noreferrer"'
    modal_html = ""
    cta_copy = "Open"
    if result.payload.source_type == "podcast_episode":
        podcast_url = _podcast_external_url(result)
        audio_url = _podcast_audio_url(result)
        if audio_url:
            modal_id = _podcast_modal_id(result)
            url = f"#{modal_id}"
            title_attributes = f' data-podcast-modal-target="{escape(modal_id, quote=True)}"'
            if podcast_url:
                title_attributes += f' data-podcast-url="{escape(podcast_url, quote=True)}"'
            title_class = ' class="podcast-modal-link"'
            title_target = ""
            modal_html = _render_podcast_modal(result, modal_id)
            cta_copy = "Listen"
        else:
            url = podcast_url or _result_url(result)
            cta_copy = "Open podcast"
    elif result.payload.source_type == "youtube_video":
        modal_id = _youtube_modal_id(result)
        url = f"#{modal_id}"
        yt_metadata = result.payload.metadata or {}
        yt_watch_url = yt_metadata.get("youtube_url") or _result_url(result) or ""
        title_attributes = f' data-youtube-modal-target="{escape(modal_id, quote=True)}" data-youtube-url="{escape(yt_watch_url, quote=True)}"'
        title_class = ' class="youtube-modal-link"'
        title_target = ""
        modal_html = _render_youtube_modal(result, modal_id)
        cta_copy = "Watch"
    feedback_html = _render_feedback_controls(issue_id, _result_url(result)) if result.fetched else ""
    return f"""
      <article class="media-card">
        {_render_thumbnail(result, "media-thumb")}
        <div class="story-meta">{_source_badge(result)}{_score_badge(result)}<span class="meta">{escape(_meta_line_for_result(result))}</span></div>
        <h3 class="media-title"><a href="{escape(url, quote=True)}"{title_target}{title_class}{title_attributes}>{escape(_story_title(result))}</a></h3>
        <p class="story-summary">{escape(_story_summary(result))}</p>
        {_translation_original_html(result)}
        {_keyword_html(result)}
        <a class="media-cta" href="{escape(url, quote=True)}"{title_target}{title_attributes}>{escape(cta_copy)}</a>
        {feedback_html}
        {modal_html}
      </article>
    """


def _foreign_article_modal_id(result: ArticleFetchResult) -> str:
    raw_key = _result_url(result) or result.title or result.payload.id
    return f"foreign-{hashlib.sha1(raw_key.encode('utf-8')).hexdigest()[:12]}"


def _newsletter_modal_id(result: ArticleFetchResult) -> str:
    raw_key = str(result.payload.id or result.original_url or result.title or result.payload.source_name)
    return f"newsletter-{hashlib.sha1(raw_key.encode('utf-8')).hexdigest()[:12]}"


def _render_newsletter_modal(result: ArticleFetchResult, modal_id: str) -> str:
    metadata = result.payload.metadata or {}
    sender = str(metadata.get("sender_email") or result.payload.source_name or "Gmail newsletter").strip()
    subject = str(metadata.get("subject") or result.title or "Newsletter item").strip()
    published = _format_issue_date(result.payload.published_at or result.published_at)
    body = _newsletter_body_html(result)
    return f"""
        <div class="podcast-modal newsletter-modal" id="{escape(modal_id, quote=True)}" role="dialog" aria-modal="true" aria-labelledby="{escape(modal_id, quote=True)}-title">
          <div class="podcast-panel newsletter-panel">
            <a href="#" class="podcast-close" data-newsletter-close>Close</a>
            <div class="section-kicker">{escape(sender)} · {escape(published)}</div>
            <h3 id="{escape(modal_id, quote=True)}-title">{escape(_clean_newsletter_text(subject))}</h3>
            <section class="newsletter-body">
              <h4>Newsletter content</h4>
              {body}
            </section>
          </div>
        </div>
    """


def _newsletter_body_html(result: ArticleFetchResult) -> str:
    text = _clean_newsletter_text(result.text or result.payload.raw_text or result.excerpt or result.editor_summary)
    if not text:
        return "<p>No newsletter body text is available for this item.</p>"
    paragraphs = [
        part.strip()
        for part in re.split(r"\n{2,}", text)
        if part.strip()
    ]
    if not paragraphs:
        paragraphs = [text]
    return "\n".join(f"<p>{escape(part)}</p>" for part in paragraphs[:24])


def _render_foreign_article_modal(result: ArticleFetchResult, modal_id: str, issue_id: str) -> str:
    url = _result_url(result)
    translation = _translation_metadata(result)
    payload_metadata = result.payload.metadata or {}
    source_language_name = str(translation.get("source_language_name") or payload_metadata.get("source_language_name") or "Original").strip()
    original_title = _clean_newsletter_text(
        str(translation.get("original_title") or payload_metadata.get("original_search_title") or _story_title(result))
    )
    original_summary = _clean_newsletter_text(
        str(translation.get("original_summary") or payload_metadata.get("original_search_summary") or "")
    )
    original_body = str(translation.get("original_body") or "").strip()
    original_seed = "\n\n".join(part for part in (original_title, original_body or original_summary) if part)
    original_html = _render_transcript_paragraphs(original_seed)
    translation_context = _translation_context_label(translation)
    is_full_translation = str(translation.get("mode") or "").strip() == "assess_and_translate"
    provenance_prefix = "Machine translated" if is_full_translation else "Metadata translated"
    provenance = f"{provenance_prefix} from {source_language_name}"
    if translation_context:
        provenance = f"{provenance} · {translation_context}"
    # When the full body was translated during the build, bake it straight into the
    # modal and mark it loaded so the reader gets the entire article immediately —
    # no on-open round trip to re-translate.
    full_translated_body = result.text if is_full_translation and (result.text or "").strip() else ""
    if full_translated_body:
        translated_body_html = _render_transcript_paragraphs(full_translated_body)
        status_text = "Full article translated to English during brief build."
        loaded_attr = ' data-foreign-loaded="true"'
    else:
        translated_body_html = f"<p>{escape(_story_summary(result))}</p>"
        status_text = "Open this article to translate the full body. The current card uses translated metadata."
        loaded_attr = ""
    return f"""
        <div class="podcast-modal foreign-modal" id="{escape(modal_id, quote=True)}" data-foreign-exploration-id="{escape(issue_id, quote=True)}"{loaded_attr} role="dialog" aria-modal="true" aria-labelledby="{escape(modal_id, quote=True)}-title">
          <div class="podcast-panel youtube-panel">
            <a class="podcast-close" data-foreign-close href="#">Close</a>
            <div class="section-kicker">Machine translated from {escape(source_language_name)}</div>
            <h3 id="{escape(modal_id, quote=True)}-title">{escape(_story_title(result))}</h3>
            <div class="podcast-actions">
              <a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" data-external-source>View original source</a>
            </div>
            <p class="foreign-provenance" data-foreign-provenance>{escape(provenance)}</p>
            <p class="foreign-status" aria-live="polite">{escape(status_text)}</p>
            <div class="foreign-tabs" role="tablist" aria-label="Article language view">
              <button type="button" class="active" data-foreign-tab="translated">Translated</button>
              <button type="button" data-foreign-tab="original">Original {escape(source_language_name)}</button>
            </div>
            <section class="foreign-view" data-foreign-view="translated">
              <div class="foreign-notice"></div>
              <div class="foreign-body" data-foreign-translated-body>
                {translated_body_html}
              </div>
            </section>
            <section class="foreign-view" data-foreign-view="original" hidden>
              <div class="foreign-body" data-foreign-original-body>{original_html}</div>
            </section>
          </div>
        </div>
    """


def _render_brief_sidebar(
    *,
    stats: dict[str, Any],
    market_html: str,
    newsletter_html: str,
    newsletter_count: int,
    article_count: int,
    media_count: int,
    source_results: list[ArticleFetchResult],
    lookback_hours: int,
) -> str:
    total_model_calls = int(stats.get("model_call_count") or 0)
    successful_model_calls = int(stats.get("model_success_count") or 0)
    failed_model_calls = int(stats.get("model_failure_count") or 0)
    ai_call_value = (
        f"{_format_int(successful_model_calls)}/{_format_int(total_model_calls)} ok"
        if total_model_calls and failed_model_calls
        else _format_int(total_model_calls)
    )
    side_stats = [
        ("Articles", _format_int(article_count)),
        ("Media", _format_int(media_count)),
        ("Sources searched", _format_int(stats.get("source_count"))),
        ("Newsletters", _format_int(newsletter_count)),
        ("Links", _format_int(stats.get("link_count"))),
        ("AI tokens", _format_int(stats.get("total_tokens"))),
        ("AI calls", ai_call_value),
        ("Processing", _format_duration(stats.get("processing_seconds"))),
        ("Recency", _format_recency_value(lookback_hours)),
    ]
    stat_html = "\n".join(
        f'<div class="side-stat"><span>{escape(label)}</span><strong class="side-value">{escape(value)}</strong></div>'
        for label, value in side_stats
    )
    source_mix_html = _render_source_mix(source_results)
    stage_seconds = stats.get("stage_seconds") if isinstance(stats.get("stage_seconds"), dict) else {}
    stage_html = ""
    if stage_seconds:
        stage_labels = {
            "ingestion": "Ingestion",
            "fetching": "Fetching",
            "classification": "Classification",
            "editorial": "Editorial + review",
            "publishing": "Publishing",
        }
        stage_items = "\n".join(
            f"<li>{escape(stage_labels.get(str(key), str(key).replace('_', ' ').title()))}: {escape(_format_stage_duration(value))}</li>"
            for key, value in stage_seconds.items()
        )
        stage_html = f'<ul class="stage-list">{stage_items}</ul>'
    token_detail = _render_token_detail(stats)
    source_notes_html = ""
    if newsletter_html:
        source_notes_html = f"""
          <details class="source-notes" open>
            <summary>Source notes</summary>
            {newsletter_html}
          </details>
        """
    strategy_html = _render_sidebar_note("Search strategy", _search_strategy_text(stats))
    model_usage_html = _render_sidebar_note("AI used", _model_usage_text(stats))
    return f"""
      <aside class="brief-sidebar" aria-label="Brief sources and process">
        {market_html}
        <section class="side-panel provenance">
          <div class="section-kicker">Sources & process</div>
          <h2>About this brief</h2>
          <div class="side-stats">{stat_html}</div>
          {source_mix_html}
          {strategy_html}
          {model_usage_html}
          {stage_html}
          {token_detail}
          {source_notes_html}
        </section>
      </aside>
    """


def _render_source_mix(results: list[ArticleFetchResult]) -> str:
    counts: dict[str, int] = {}
    for result in results:
        if result.tier == "dropped":
            continue
        label = _origin_source_label(result)
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    ordered_labels = [
        "Gmail",
        "News",
        "Web",
        "Foreign Media",
        "YouTube",
        "Podcast",
        "Markets",
        "Collections",
        "SEC filings",
        "Macro",
    ]
    rows: list[tuple[str, int]] = [(label, counts.pop(label)) for label in ordered_labels if label in counts]
    rows.extend(sorted(counts.items(), key=lambda item: item[0].lower()))
    row_html = "\n".join(
        f'<div class="source-mix-row"><span class="source-mix-label">{escape(label)}</span>'
        f'<strong class="source-mix-count">{escape(_format_int(count))}</strong></div>'
        for label, count in rows
    )
    return f"""
          <div class="source-mix" aria-label="Included items by source">
            <h3>Source mix</h3>
            {row_html}
          </div>
    """


def _origin_source_label(result: ArticleFetchResult) -> str:
    """Origin-based section/count label (item 8).

    A Gmail newsletter link to a web article counts as Gmail; a web-search hit or a
    translated foreign link counts as Web; a native-language result counts as
    Foreign Media. This keeps the brief's section grouping and source-mix honest
    about where content actually came from, independent of article type.
    """
    source_type = result.payload.source_type
    metadata = result.payload.metadata or {}
    if source_type == "gmail_link":
        if metadata.get("search_provider") == "google_news_rss":
            return "News"
        if metadata.get("search_query") or metadata.get("search_provider"):
            return "Web"
        if _translation_metadata(result).get("translated"):
            return "Web"
        return "Gmail"
    if source_type == "gmail":
        return "Gmail"
    if source_type == "foreign_web":
        return "Foreign Media"
    if source_type == "youtube_video":
        return "YouTube"
    if source_type == "podcast_episode":
        return "Podcast"
    if source_type == "market_snapshot":
        return "Markets"
    if source_type == "collection_chunk":
        return "Collections"
    if source_type in {"reddit_post", "reddit_thread"}:
        return "Reddit"
    if source_type == "sec_filing":
        return "SEC filings"
    if source_type == "fred_series":
        return "Macro"
    return "Web"


def _render_sidebar_note(title: str, text: str | None) -> str:
    body = str(text or "").strip()
    if not body:
        return ""
    return f"""
          <div class="side-note">
            <h3>{escape(title)}</h3>
            <p>{escape(body)}</p>
          </div>
    """


def _search_strategy_text(stats: dict[str, Any]) -> str:
    strategy = stats.get("search_strategy") if isinstance(stats.get("search_strategy"), dict) else {}
    summary = str(strategy.get("summary") or "").strip()
    if summary:
        axes = _string_values(strategy.get("strategy_axes") if isinstance(strategy, dict) else None, limit=5)
        if axes:
            return summary.rstrip(".") + ". Strategy axes: " + "; ".join(axes) + "."
        return summary
    queries = _string_values(strategy.get("queries") if isinstance(strategy, dict) else None, limit=2)
    source_names = _string_values(strategy.get("sources") if isinstance(strategy, dict) else None, limit=5)
    scope = str(strategy.get("source_scope") or stats.get("source_scope_label") or "").strip()
    pieces: list[str] = []
    if source_names:
        pieces.append("Looked across " + ", ".join(source_names))
    if queries:
        pieces.append("Query examples: " + "; ".join(queries))
    if scope:
        pieces.append("Source scope: " + scope)
    return ". ".join(pieces).strip()


def _model_usage_text(stats: dict[str, Any]) -> str:
    usage = stats.get("model_usage") if isinstance(stats.get("model_usage"), list) else []
    if usage:
        model_names: list[str] = []
        total_calls = 0
        successful = 0
        failed = 0
        modes: set[str] = set()
        for row in usage:
            if not isinstance(row, dict):
                continue
            model = str(row.get("model") or "").strip()
            if model and model not in model_names:
                model_names.append(model)
            mode = str(row.get("mode") or "").strip()
            if mode:
                modes.add(_model_mode_label(mode))
            total_calls += int(row.get("call_count") or 0)
            successful += int(row.get("success_count") or 0)
            failed += int(row.get("failure_count") or 0)
        if model_names:
            model_part = ", ".join(model_names[:3])
            if len(model_names) > 3:
                model_part += f" +{len(model_names) - 3} more"
            task_part = ", ".join(sorted(modes)) if modes else "brief generation"
            call_part = f"{successful}/{total_calls} calls completed" if failed else f"{total_calls} calls"
            return f"{model_part} supported {task_part}; {call_part}."
    fallback = str(stats.get("model_usage_summary") or "").strip()
    if fallback:
        return fallback
    total_calls = int(stats.get("model_call_count") or 0)
    if total_calls:
        successful = int(stats.get("model_success_count") or 0)
        failed = int(stats.get("model_failure_count") or 0)
        return f"AI assisted the brief generation; {successful}/{total_calls} calls completed." if failed else f"AI assisted the brief generation across {total_calls} calls."
    return ""


def _model_mode_label(mode: str) -> str:
    labels = {
        "single": "article summaries",
        "source_audit": "source audit",
        "editorial": "ranking",
        "critic": "review",
        "refinement": "interest refinement",
    }
    return labels.get(mode, mode.replace("_", " "))


def _string_values(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _render_article_sections(results: list[ArticleFetchResult], *, issue_id: str | None = None) -> str:
    grouped: dict[str, list[ArticleFetchResult]] = {}
    for result in results:
        grouped.setdefault(result.section or "Noteworthy", []).append(result)

    sections: list[str] = []
    for section, section_results in grouped.items():
        cards = "\n".join(
            _render_article_card(result, variant="compact", issue_id=issue_id)
            for result in section_results
        )
        sections.append(
            f"""
            <section class="article-section">
              <h2>{escape(section)}</h2>
              <div class="article-grid">{cards}</div>
            </section>
            """
        )
    return "\n".join(sections)


def _render_article_card(
    result: ArticleFetchResult | None,
    *,
    variant: str = "compact",
    issue_id: str | None = None,
) -> str:
    if result is None:
        return ""
    url = _reader_url(result)
    domain = result.domain or _domain(url) or "article"
    source = result.payload.source_name or "Gmail"
    published = _format_article_date(result.payload.published_at)
    meta_parts = [domain]
    if published:
        meta_parts.append(published)
    elif _served_once_note(result):
        meta_parts.append(_served_once_note(result))
    meta_parts.append(f"via {source}")
    meta = " · ".join(escape(part) for part in meta_parts)
    score = f'<span class="score">{int((result.relevance_score or 0) * 100)}%</span>' if result.relevance_score else ""
    keywords = ", ".join(result.keywords[:5])
    keyword_html = f'<div class="keywords">{escape(keywords)}</div>' if keywords else ""
    card_class = "article-card lead" if variant == "lead" else "article-card"
    summary = _clean_newsletter_text(result.editor_summary or result.excerpt)
    title = _clean_newsletter_text(result.title) or result.title
    feedback_html = _render_feedback_controls(issue_id, url) if result.fetched else ""
    podcast_modal_id = _podcast_modal_id(result) if result.payload.source_type == "podcast_episode" and _podcast_audio_url(result) else ""
    youtube_modal_id = _youtube_modal_id(result) if result.payload.source_type == "youtube_video" else ""
    title_attributes = ""
    title_class = ""
    title_target = ' target="_blank" rel="noreferrer"'
    modal_html = ""
    if podcast_modal_id:
        url = f"#{podcast_modal_id}"
        title_attributes = f' data-podcast-modal-target="{escape(podcast_modal_id, quote=True)}"'
        title_class = ' class="podcast-modal-link"'
        title_target = ""
        modal_html = _render_podcast_modal(result, podcast_modal_id)
    elif youtube_modal_id:
        url = f"#{youtube_modal_id}"
        title_attributes = f' data-youtube-modal-target="{escape(youtube_modal_id, quote=True)}"'
        title_class = ' class="youtube-modal-link"'
        title_target = ""
        modal_html = _render_youtube_modal(result, youtube_modal_id)
    elif _supports_foreign_article_modal(result, issue_id=issue_id):
        url, title_target, title_class, title_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    elif _is_embedded_newsletter_result(result):
        url, title_target, title_class, title_attributes, modal_html = _story_link_parts(result, issue_id=issue_id)
    return f"""
      <article class="{card_class}">
        <div class="meta">{meta}{score}</div>
        <h3><a href="{escape(url, quote=True)}"{title_target}{title_class}{title_attributes}>{escape(title)}</a></h3>
        <p>{escape(summary)}</p>
        {keyword_html}
        {feedback_html}
        {modal_html}
      </article>
    """


def _podcast_modal_id(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    raw_key = str(metadata.get("podcast_episode_id") or result.original_url or result.title or result.payload.id)
    return f"podcast-{hashlib.sha1(raw_key.encode('utf-8')).hexdigest()[:12]}"


def _podcast_audio_url(result: ArticleFetchResult) -> str:
    return _safe_web_url((result.payload.metadata or {}).get("audio_url")) or ""


def _podcast_external_url(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    return (
        _safe_web_url(metadata.get("episode_url"))
        or _safe_web_url(metadata.get("apple_podcasts_url"))
        or _safe_web_url(_result_url(result))
        or ""
    )


def _render_podcast_modal(result: ArticleFetchResult, modal_id: str) -> str:
    metadata = result.payload.metadata or {}
    show_name = str(metadata.get("podcast_title") or result.payload.source_name or "Podcast")
    episode_title = _clean_newsletter_text(str(metadata.get("title") or result.title or "Podcast episode"))
    image_url = _safe_web_url(metadata.get("image_url"))
    audio_url = _podcast_audio_url(result)
    apple_url = _safe_web_url(metadata.get("apple_podcasts_url"))
    episode_url = _safe_web_url(metadata.get("episode_url"))
    transcript_source = str(metadata.get("transcript_source") or "show_notes")
    transcript_label = "Transcript" if transcript_source in {"transcript", "transcript_cache"} else "Show Notes"
    transcript_html = _render_transcript_paragraphs(_podcast_transcript_text(result))
    duration = _format_duration(metadata.get("duration_seconds"))
    summary = _clean_newsletter_text(result.editor_summary or result.excerpt)
    summary_html = (
        f"""
        <section class="podcast-summary">
          <h4>Summary</h4>
          <p>{escape(summary)}</p>
        </section>
        """
        if summary
        else ""
    )
    meta_parts = [show_name]
    if duration:
        meta_parts.append(duration)
    if result.payload.published_at:
        meta_parts.append(_format_article_date(result.payload.published_at))
    brand_html = (
        f'<img class="podcast-art" src="{escape(image_url, quote=True)}" alt="{escape(show_name, quote=True)} artwork" loading="lazy" />'
        if image_url
        else f'<div class="podcast-art fallback" aria-hidden="true">{escape(_podcast_initials(show_name))}</div>'
    )
    player_html = f'<audio class="podcast-player" data-podcast-player controls preload="none" src="{escape(audio_url, quote=True)}"></audio>'
    action_links = []
    if apple_url:
        action_links.append(f'<a href="{escape(apple_url, quote=True)}" target="_blank" rel="noreferrer">Apple Podcasts</a>')
    if episode_url and episode_url != apple_url:
        action_links.append(f'<a href="{escape(episode_url, quote=True)}" target="_blank" rel="noreferrer">Listen</a>')
    actions_html = f'<div class="podcast-actions">{" ".join(action_links)}</div>' if action_links else ""
    speed_controls_html = (
        """
          <div class="podcast-speed-controls">
            <button type="button" data-podcast-speed="0.75">Slow</button>
            <button type="button" data-podcast-speed="1" class="active">Normal</button>
            <button type="button" data-podcast-speed="1.25">Speed up</button>
          </div>
        """
        if audio_url
        else ""
    )
    return f"""
        <div class="podcast-modal" id="{escape(modal_id, quote=True)}" role="dialog" aria-modal="true" aria-labelledby="{escape(modal_id, quote=True)}-title">
          <div class="podcast-panel">
            <a class="podcast-close" data-podcast-close href="#">Close</a>
            <div class="podcast-brand">
              {brand_html}
              <div>
                <div class="meta">{escape(" · ".join(part for part in meta_parts if part))}</div>
                <h3 id="{escape(modal_id, quote=True)}-title">{escape(episode_title)}</h3>
                {actions_html}
              </div>
            </div>
            {summary_html}
            {player_html}
            {speed_controls_html}
            <section class="podcast-transcript">
              <h4>{escape(transcript_label)}</h4>
              {transcript_html}
            </section>
          </div>
        </div>
    """


def _youtube_modal_id(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    raw_key = str(metadata.get("video_id") or result.original_url or result.title or result.payload.id)
    return f"youtube-{hashlib.sha1(raw_key.encode('utf-8')).hexdigest()[:12]}"


def _render_youtube_modal(result: ArticleFetchResult, modal_id: str) -> str:
    metadata = result.payload.metadata or {}
    channel_name = str(metadata.get("channel_name") or result.payload.source_name or "YouTube")
    video_title = _clean_newsletter_text(str(metadata.get("youtube_title") or metadata.get("title") or result.title or "YouTube video"))
    youtube_url = _safe_web_url(metadata.get("youtube_url")) or _safe_web_url(result.final_url or result.original_url) or ""
    embed_url = _youtube_embed_url(metadata.get("video_id"), youtube_url)
    image_url = _result_image_url(result)
    summary = _clean_newsletter_text(result.editor_summary or result.excerpt)
    transcript_html = _render_transcript_paragraphs(_youtube_transcript_text(result))
    duration = _format_duration(metadata.get("duration_seconds"))
    meta_parts = [channel_name]
    if duration:
        meta_parts.append(duration)
    if result.payload.published_at:
        meta_parts.append(_format_article_date(result.payload.published_at))
    player_html = (
        f'<iframe class="youtube-player" data-youtube-src="{escape(embed_url, quote=True)}" title="{escape(video_title, quote=True)}" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen loading="lazy"></iframe>'
        if embed_url
        else '<p class="meta">Video playback is not available for this item.</p>'
    )
    action_links = []
    if youtube_url:
        action_links.append(f'<a href="{escape(youtube_url, quote=True)}" target="_blank" rel="noreferrer">Watch on YouTube</a>')
    actions_html = f'<div class="podcast-actions">{" ".join(action_links)}</div>' if action_links else ""
    brand_art = (
        f'<img class="podcast-art" src="{escape(image_url, quote=True)}" alt="{escape(channel_name, quote=True)} thumbnail" loading="lazy" />'
        if image_url
        else '<div class="podcast-art fallback" aria-hidden="true">YT</div>'
    )
    return f"""
        <div class="podcast-modal youtube-modal" id="{escape(modal_id, quote=True)}" role="dialog" aria-modal="true" aria-labelledby="{escape(modal_id, quote=True)}-title">
          <div class="podcast-panel youtube-panel">
            <a class="podcast-close" data-youtube-close href="#">Close</a>
            <div class="podcast-brand">
              {brand_art}
              <div>
                <div class="meta">{escape(" · ".join(part for part in meta_parts if part))}</div>
                <h3 id="{escape(modal_id, quote=True)}-title">{escape(video_title)}</h3>
                {actions_html}
              </div>
            </div>
            {player_html}
            <section class="youtube-summary">
              <h4>Summary</h4>
              <p>{escape(summary)}</p>
            </section>
            <section class="podcast-transcript">
              <h4>Transcript</h4>
              {transcript_html}
            </section>
          </div>
        </div>
    """


def _youtube_embed_url(raw_video_id: Any, youtube_url: str) -> str:
    video_id = str(raw_video_id or "").strip()
    if not video_id and youtube_url:
        parsed = urlparse(youtube_url)
        if parsed.hostname and "youtu.be" in parsed.hostname:
            video_id = parsed.path.strip("/")
        elif parsed.hostname and "youtube" in parsed.hostname:
            query = dict(parse_qsl(parsed.query, keep_blank_values=False))
            video_id = str(query.get("v") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
        return ""
    return f"https://www.youtube-nocookie.com/embed/{video_id}?rel=0"


def _youtube_transcript_text(result: ArticleFetchResult) -> str:
    return " ".join((result.text or result.payload.raw_text or result.excerpt or "").split())


def _safe_web_url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith(("http://", "https://")) else None


def _podcast_initials(value: str) -> str:
    parts = [part[:1].upper() for part in re.findall(r"[A-Za-z0-9]+", value)[:3]]
    return "".join(parts) or "P"


def _format_duration(value: Any) -> str:
    seconds = _nullable_int(value)
    if not seconds:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, _seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _podcast_transcript_text(result: ArticleFetchResult) -> str:
    text = " ".join((result.text or result.payload.raw_text or result.excerpt or "").split())
    match = re.search(r"(?:Transcript|Show notes):\s*(.+)", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _render_transcript_paragraphs(text: str) -> str:
    paragraphs = _transcript_paragraphs(text)
    if not paragraphs:
        return '<p>No transcript text is available yet.</p>'
    return "\n".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)


def _transcript_paragraphs(text: str) -> list[str]:
    cleaned = _clean_newsletter_text(text)
    parts = [part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip()]
    if len(parts) > 1:
        return parts
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", cleaned) if sentence.strip()]
    if not sentences:
        return [cleaned] if cleaned else []
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > 760:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _render_feedback_controls(issue_id: str | None, url: str | None) -> str:
    if not issue_id or not url:
        return ""
    return f"""
        <div class="feedback-controls" data-feedback-url="{escape(url, quote=True)}">
          <button type="button" data-feedback-signal="up" title="Save a positive signal for future ranking">Useful</button>
          <button type="button" data-feedback-signal="down" title="Save a negative signal for future ranking">Not useful</button>
          <span class="feedback-state" aria-live="polite"></span>
        </div>
    """


def _render_feedback_script(issue_id: str | None) -> str:
    if not issue_id:
        return ""
    return f"""
  <script>
    (() => {{
      const issueId = {json.dumps(issue_id)};
      document.addEventListener("click", async (event) => {{
        const button = event.target.closest("[data-feedback-signal]");
        if (!button) return;
        const controls = button.closest(".feedback-controls");
        const state = controls ? controls.querySelector(".feedback-state") : null;
        if (!controls) return;
        const url = controls.getAttribute("data-feedback-url");
        const signal = button.getAttribute("data-feedback-signal");
        if (!url || !signal) return;
        controls.querySelectorAll("button").forEach((item) => item.disabled = true);
        if (state) state.textContent = "Saving";
        try {{
          const response = await fetch("/api/feedback", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ issue_id: issueId, url, signal }}),
          }});
          if (!response.ok) throw new Error("Feedback failed");
          controls.setAttribute("data-feedback", "sent");
          if (state) state.textContent = signal === "up" ? "Saved for future ranking" : "Downrank signal saved";
        }} catch (_error) {{
          controls.querySelectorAll("button").forEach((item) => item.disabled = false);
          if (state) state.textContent = "Try again";
        }}
      }});
    }})();
  </script>
    """


def _render_podcast_modal_script() -> str:
    return """
  <script>
    (() => {
      const activeModal = () => {
        if (!window.location.hash) return null;
        const id = window.location.hash.slice(1);
        return document.getElementById(id);
      };

      const syncModalState = () => {
        const modal = activeModal();
        document.body.classList.toggle("modal-open", Boolean(modal && modal.classList.contains("podcast-modal")));
        if (modal && modal.classList.contains("podcast-modal")) {
          const player = modal.querySelector(".podcast-player");
          const activeSpeed = modal.querySelector(".podcast-speed-controls .active[data-podcast-speed]");
          if (player && activeSpeed) {
            const rate = Number(activeSpeed.getAttribute("data-podcast-speed"));
            if (Number.isFinite(rate) && rate > 0) {
              player.playbackRate = rate;
            }
          }
        }
        document.querySelectorAll(".podcast-modal audio").forEach((player) => {
          if (!modal || !modal.contains(player)) player.pause();
        });
        document.querySelectorAll(".youtube-modal iframe[data-youtube-src]").forEach((player) => {
          if (modal && modal.contains(player)) {
            if (!player.getAttribute("src")) player.setAttribute("src", player.getAttribute("data-youtube-src"));
          } else {
            player.removeAttribute("src");
          }
        });
      };

      const closeModal = () => {
        document.querySelectorAll(".podcast-modal audio").forEach((player) => player.pause());
        document.querySelectorAll(".youtube-modal iframe[data-youtube-src]").forEach((player) => player.removeAttribute("src"));
        document.body.classList.remove("modal-open");
        if (window.location.hash) history.pushState("", document.title, window.location.pathname + window.location.search);
      };

      const paragraphs = (value) => {
        const text = String(value || "").trim();
        if (!text) return "<p>No article text is available.</p>";
        return text
          .split(/\\n{2,}/)
          .map((part) => part.trim())
          .filter(Boolean)
          .map((part) => `<p>${part.replace(/[&<>"']/g, (char) => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;"
          })[char])}</p>`)
          .join("");
      };

      const setForeignView = (modal, viewName) => {
        modal.querySelectorAll("[data-foreign-view]").forEach((view) => {
          view.hidden = view.getAttribute("data-foreign-view") !== viewName;
        });
        modal.querySelectorAll("[data-foreign-tab]").forEach((button) => {
          button.classList.toggle("active", button.getAttribute("data-foreign-tab") === viewName);
        });
      };

      const setPodcastSpeed = (modal, speedValue) => {
        const player = modal ? modal.querySelector("audio[data-podcast-player]") : null;
        const rate = Number(speedValue);
        if (!player || !Number.isFinite(rate) || rate <= 0) return;
        player.playbackRate = rate;
        modal.querySelectorAll("[data-podcast-speed]").forEach((button) => {
          const value = Number(button.getAttribute("data-podcast-speed"));
          const selected = Number.isFinite(value) && value === rate;
          if (selected) {
            button.classList.add("active");
          } else {
            button.classList.remove("active");
          }
        });
      };

      const loadForeignArticle = async (trigger, modal) => {
        if (!modal || modal.getAttribute("data-foreign-loaded") === "true") return;
        const status = modal.querySelector(".foreign-status");
        const notice = modal.querySelector(".foreign-notice");
        const provenance = modal.querySelector("[data-foreign-provenance]");
        const translatedBody = modal.querySelector("[data-foreign-translated-body]");
        const originalBody = modal.querySelector("[data-foreign-original-body]");
        const explorationId = modal.getAttribute("data-foreign-exploration-id");
        if (!explorationId) return;
        modal.setAttribute("data-foreign-loaded", "loading");
        if (status) status.textContent = "Fetching and translating the full article...";
        try {
          const response = await fetch(`/api/explore/explorations/${encodeURIComponent(explorationId)}/foreign-article/translation`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              url: trigger.getAttribute("data-foreign-url"),
              title: trigger.getAttribute("data-foreign-title"),
              summary: trigger.getAttribute("data-foreign-summary"),
              source_language: trigger.getAttribute("data-foreign-source-language"),
              source_language_name: trigger.getAttribute("data-foreign-source-language-name"),
              original_title: trigger.getAttribute("data-foreign-original-title"),
              original_summary: trigger.getAttribute("data-foreign-original-summary")
            })
          });
          if (!response.ok) throw new Error("Translation request failed");
          const data = await response.json();
          if (translatedBody) translatedBody.innerHTML = paragraphs(data.translated_body || data.translated_summary);
          if (originalBody) originalBody.innerHTML = paragraphs([data.original_title, data.original_body].filter(Boolean).join("\\n\\n"));
          if (notice) notice.textContent = data.notice || "";
          const quality = data.translation_quality || data.quality || trigger.getAttribute("data-foreign-translation-quality") || "";
          const translator = data.translator || trigger.getAttribute("data-foreign-translator") || "";
          const mode = data.mode || trigger.getAttribute("data-foreign-translation-mode") || "";
          if (provenance) {
            provenance.textContent = [
              data.source_language_name ? `Full translation from ${data.source_language_name}` : "Full translation",
              quality ? `${quality} confidence` : "",
              translator ? `model: ${translator}` : "",
              mode ? `mode: ${mode}` : ""
            ].filter(Boolean).join(" · ");
          }
          if (status) {
            if (data.status === "translated_summary_only") {
              status.textContent = data.cached ? "Loaded summary translation from cache." : "Translated available text; full body was not available.";
            } else if (data.status === "translation_unavailable" || data.status === "translation_failed") {
              status.textContent = "Full translation is unavailable. Open the original source for details.";
            } else {
              status.textContent = data.cached ? "Loaded from translation cache." : "Translated article ready.";
            }
          }
          modal.setAttribute("data-foreign-loaded", "true");
        } catch (_error) {
          modal.removeAttribute("data-foreign-loaded");
          if (status) status.textContent = "Could not translate this article. Try again or open the original source.";
        }
      };

      document.addEventListener("click", (event) => {
        const externalSource = event.target.closest("[data-external-source]");
        if (externalSource) {
          const url = externalSource.getAttribute("href");
          if (url) {
            event.preventDefault();
            const opened = window.open(url, "_blank", "noopener,noreferrer");
            if (!opened) window.location.href = url;
          }
          return;
        }

        const tab = event.target.closest("[data-foreign-tab]");
        if (tab) {
          const modal = tab.closest(".foreign-modal");
          if (modal) setForeignView(modal, tab.getAttribute("data-foreign-tab"));
          return;
        }

        const speedButton = event.target.closest("[data-podcast-speed]");
        if (speedButton) {
          const modal = speedButton.closest(".podcast-modal");
          if (modal) {
            event.preventDefault();
            setPodcastSpeed(modal, speedButton.getAttribute("data-podcast-speed"));
          }
          return;
        }

        const trigger = event.target.closest("[data-podcast-modal-target], [data-youtube-modal-target], [data-foreign-article-target], [data-newsletter-modal-target]");
        if (trigger) {
          const modalId = trigger.getAttribute("data-podcast-modal-target") || trigger.getAttribute("data-youtube-modal-target") || trigger.getAttribute("data-foreign-article-target") || trigger.getAttribute("data-newsletter-modal-target");
          if (modalId) {
            event.preventDefault();
            window.location.hash = modalId;
            syncModalState();
            const modal = document.getElementById(modalId);
            if (trigger.hasAttribute("data-foreign-article-target")) loadForeignArticle(trigger, modal);
          }
          return;
        }

        const closeButton = event.target.closest("[data-podcast-close], [data-youtube-close], [data-foreign-close], [data-newsletter-close]");
        if (closeButton) {
          event.preventDefault();
          closeModal();
          return;
        }

        if (event.target.classList && event.target.classList.contains("podcast-modal")) {
          closeModal();
        }
      });

      document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        closeModal();
      });

      window.addEventListener("hashchange", syncModalState);
      syncModalState();
    })();
  </script>
    """


def _render_digest_stats(stats: dict[str, Any]) -> str:
    stage_seconds = stats.get("stage_seconds") if isinstance(stats.get("stage_seconds"), dict) else {}
    stage_html = ""
    if stage_seconds:
        stage_labels = {
            "ingestion": "Ingestion",
            "fetching": "Article fetching",
            "classification": "AI classification",
            "editorial": "Editorial + review",
            "publishing": "Publishing",
        }
        stage_items = "\n".join(
            f"<li>{escape(stage_labels.get(str(key), str(key).replace('_', ' ').title()))}: "
            f"{escape(_format_stage_duration(value))}</li>"
            for key, value in stage_seconds.items()
        )
        stage_html = f'<div class="digest-stat"><span>Stage timing</span><ul class="stage-list">{stage_items}</ul></div>'

    total_model_calls = int(stats.get("model_call_count") or 0)
    successful_model_calls = int(stats.get("model_success_count") or 0)
    failed_model_calls = int(stats.get("model_failure_count") or 0)
    ai_call_value = (
        f"{_format_int(successful_model_calls)}/{_format_int(total_model_calls)} ok"
        if total_model_calls and failed_model_calls
        else _format_int(total_model_calls)
    )
    stat_items = [
        ("Sources", _format_int(stats.get("source_count"))),
        ("Newsletters", _format_int(stats.get("newsletter_count"))),
        ("Links extracted", _format_int(stats.get("link_count"))),
        ("Podcast episodes", _format_int(stats.get("podcast_episode_count"))),
        ("Articles included", _format_int(stats.get("included_article_count"))),
        ("Items filtered", _format_int(int(stats.get("dropped_count") or 0) + int(stats.get("unresolved_count") or 0))),
        ("AI tokens", _format_int(stats.get("total_tokens"))),
        ("AI calls", ai_call_value),
        ("Processing time", _format_duration(stats.get("processing_seconds"))),
    ]
    stat_html = "\n".join(
        f'<div class="digest-stat"><span>{escape(label)}</span><strong>{escape(value)}</strong></div>'
        for label, value in stat_items
    )
    token_detail = _render_token_detail(stats)
    return f"""
      <div class="digest-stats">
        {stat_html}
        {stage_html}
      </div>
      {token_detail}
    """


def _format_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def _render_token_detail(stats: dict[str, Any]) -> str:
    if not int(stats.get("total_tokens") or 0):
        return ""
    prompt = _format_int(stats.get("prompt_tokens"))
    completion = int(stats.get("completion_tokens") or 0)
    completion_display = _format_int(completion)
    unavailable_count = int(stats.get("completion_unavailable_count") or 0)
    failed_count = int(stats.get("model_failure_count") or 0)
    if unavailable_count and failed_count and completion == 0:
        return f'<p class="meta">Token detail: {prompt} prompt tokens recorded; completion tokens unavailable.</p>'
    if unavailable_count and failed_count:
        return (
            f'<p class="meta">Token detail: {prompt} prompt + {completion_display} completion recorded; '
            "some completion tokens unavailable.</p>"
        )
    return f'<p class="meta">Token detail: {prompt} prompt + {completion_display} completion.</p>'


def _format_duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if seconds < 1:
        return f"{round(seconds * 1000):,} ms"
    minutes, remaining = divmod(round(seconds), 60)
    if minutes:
        return f"{minutes}m {remaining:02d}s"
    return f"{remaining}s"


def _format_stage_duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "not measured"
    if seconds <= 0:
        return "not measured"
    return _format_duration(seconds)


def _title_for_payload(payload: NormalizedPayload) -> str:
    metadata = payload.metadata or {}
    link_text = metadata.get("link_text")
    if link_text:
        return str(link_text)
    reddit_title = metadata.get("title")
    if reddit_title:
        return str(reddit_title)
    subject = metadata.get("subject") or metadata.get("parent_subject")
    if subject:
        return str(subject)
    if payload.original_url:
        parsed = urlparse(payload.original_url)
        path = parsed.path.strip("/").replace("-", " ").replace("_", " ")
        return path[:120] or parsed.netloc
    return payload.source_name or "Gmail item"


def _summary_for_payload(payload: NormalizedPayload, max_chars: int = 320) -> str:
    text = _clean_newsletter_text(payload.raw_text)
    if not text and payload.original_url:
        text = payload.original_url
    if not text:
        text = _title_for_payload(payload)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}..."


def clean_issue_html_for_display(html: str) -> str:
    """Apply display cleanup to issues generated before the newsletter scrubber existed."""
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for article in soup.select("article.newsletter"):
        meta = article.select_one(".meta")
        if meta is not None:
            sender, _, published = meta.get_text(" ", strip=True).partition("·")
            date = _format_issue_date(published.strip())
            meta.string = f"{sender.strip()} · {date}"
            changed = True

        paragraph = article.find("p")
        if paragraph is not None:
            cleaned = _truncate_text(_clean_newsletter_text(paragraph.get_text(" ", strip=True)), 700)
            if _weak_newsletter_snippet(cleaned):
                article.decompose()
                changed = True
                continue
            paragraph.string = cleaned
            changed = True
    for details in soup.select("details.source-notes"):
        if not details.has_attr("open"):
            details["open"] = ""
            changed = True
    for paragraph in soup.select("article.article-card p"):
        cleaned = _clean_newsletter_text(paragraph.get_text(" ", strip=True))
        if cleaned != paragraph.get_text(" ", strip=True):
            paragraph.string = cleaned
            changed = True
    return str(soup) if changed else html


def ensure_generated_footer(html: str, generated_at: str | None) -> str:
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("footer.issue-footer"):
        return html

    target = soup.find("main") or soup.body
    if target is None:
        return html

    _ensure_generated_footer_style(soup)
    footer = soup.new_tag("footer", attrs={"class": "issue-footer"})
    footer.string = f"Generated {_format_generated_timestamp(generated_at)}"
    target.append(footer)
    return str(soup)


def _ensure_generated_footer_style(soup: BeautifulSoup) -> None:
    if ".issue-footer" in soup.get_text(" ", strip=True):
        return
    head = soup.find("head")
    if head is None:
        return
    existing_style = "".join(style.get_text() for style in soup.find_all("style"))
    if ".issue-footer" in existing_style:
        return
    style = soup.new_tag("style", id="morning-dispatch-generated-footer-style")
    style.string = (
        ".issue-footer { margin-top: 36px; padding-top: 16px; border-top: 1px solid #d4cbbd; "
        "font: 700 .76rem Arial, sans-serif; color: #5f675f; text-transform: uppercase; }"
    )
    head.append(style)


def _clean_newsletter_text(value: str | None) -> str:
    text = unescape(_repair_text_encoding(value or ""))
    text = ZERO_WIDTH_RE.sub(" ", text)
    text = IMAGE_PLACEHOLDER_RE.sub(" ", text)
    text = FOLLOW_IMAGE_RE.sub(" ", text)
    text = MARKDOWN_LINK_RE.sub(_newsletter_markdown_label, text)
    for pattern in NEWSLETTER_BOILERPLATE_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(
        r"\b(?:read online|sign\s*up|signup|work with us|advertise|follow on x|archive|subscribe|unsubscribe|view online|view in browser)\b"
        r"(?:\s*\|\s*\b(?:read online|sign\s*up|signup|work with us|advertise|follow on x|archive|subscribe|unsubscribe|view online|view in browser)\b)+",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = RAW_URL_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = REFERENCE_MARK_RE.sub(" ", text)
    text = SEPARATOR_RE.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ").replace("^^", " ").replace("^", " ").replace("`", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" -|")
    if re.fullmatch(r"(?:online|read online|click here)[:.!?]?", text, flags=re.IGNORECASE):
        return ""
    return text


def _repair_text_encoding(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for encoding in ("latin1", "cp1252"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != text:
            return repaired
    return text


def _newsletter_markdown_label(match: re.Match[str]) -> str:
    label = _clean_markdown_label(match.group(1))
    if _is_newsletter_utility_label(label):
        return " "
    return f" {label} "


def _clean_markdown_label(label: str) -> str:
    text = ZERO_WIDTH_RE.sub(" ", unescape(label or ""))
    text = HTML_TAG_RE.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ").replace("`", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -|")


def _is_newsletter_utility_label(label: str) -> bool:
    normalized = re.sub(r"\s+", " ", label.lower()).strip(" -|")
    return normalized in NEWSLETTER_UTILITY_LABELS


def _weak_newsletter_snippet(snippet: str) -> bool:
    text = re.sub(r"\s+", " ", snippet or "").strip()
    if not text:
        return True
    words = re.findall(r"[A-Za-z0-9]+", text)
    if len(words) <= 3 and re.fullmatch(r"(?:online|read online|click here)[:.!?]?", text, flags=re.IGNORECASE):
        return True
    if len(words) < 8 and NEWSLETTER_LOW_VALUE_RE.search(text):
        return True
    return not words


def _format_issue_date(value: str | None) -> str:
    if not value:
        return "Unknown date"
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return f"{parsed:%b} {parsed.day}, {parsed.year}"
    except ValueError:
        pass
    if "T" in text:
        return text.split("T", 1)[0]
    if "," in text:
        return text
    if " " in text:
        return text.split(" ", 1)[0]
    return text


def _format_article_date(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year:04d}"
        except ValueError:
            pass
    for pattern in ("%b %d, %Y", "%B %d, %Y"):
        try:
            parsed = datetime.strptime(text, pattern)
            return f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year:04d}"
        except ValueError:
            pass
    if "T" in text:
        date_part = text.split("T", 1)[0]
        try:
            parsed = datetime.strptime(date_part, "%Y-%m-%d")
            return f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year:04d}"
        except ValueError:
            return date_part
    return text


def _render_generated_footer(generated_at: str | None) -> str:
    return f'<footer class="issue-footer">Morning Dispatch · {escape(_format_generated_timestamp(generated_at))}</footer>'


def _render_masthead_meta(generated_at: str | None, lookback_hours: int, stats: dict[str, Any] | None = None) -> str:
    source_scope = str((stats or {}).get("source_scope_label") or "").strip()
    if not source_scope:
        source_scope = _format_source_scope(lookback_hours)
    return escape(f"Generated {_format_generated_timestamp(generated_at)} · Source scope: {source_scope}")


def _format_source_scope(lookback_hours: int) -> str:
    try:
        hours = max(1, int(lookback_hours))
    except (TypeError, ValueError):
        hours = 24
    if hours % 24 == 0:
        days = hours // 24
        if days == 1:
            return "last 24 hours"
        return f"last {days} days"
    if hours == 1:
        return "last hour"
    return f"last {hours} hours"


def _format_recency_value(lookback_hours: int) -> str:
    try:
        hours = max(1, int(lookback_hours))
    except (TypeError, ValueError):
        hours = 24
    if hours < 24:
        return "1 hour" if hours == 1 else f"{hours} hours"
    if hours % 24:
        return f"{hours} hours"
    days = hours // 24
    if days < 14:
        return "1 day" if days == 1 else f"{days} days"
    if days >= 60:
        months = max(1, round(days / 30))
        return "1 month" if months == 1 else f"{months} months"
    if days % 7 == 0:
        weeks = days // 7
        return "1 week" if weeks == 1 else f"{weeks} weeks"
    return f"{days} days"


def _format_brief_dateline(value: str | None) -> str:
    if not value:
        value = utc_now()
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return "Your brief"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        local_zone = ZoneInfo(get_settings().scheduler_timezone)
    except ZoneInfoNotFoundError:
        local_zone = UTC
    parsed = parsed.astimezone(local_zone)
    return parsed.strftime("%A, %B %-d, %Y")


def _format_generated_timestamp(value: str | None) -> str:
    if not value:
        value = utc_now()
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        local_zone = ZoneInfo(get_settings().scheduler_timezone)
    except ZoneInfoNotFoundError:
        local_zone = UTC
    parsed = parsed.astimezone(local_zone)
    hour = parsed.strftime("%I").lstrip("0") or "0"
    zone_label = parsed.tzname() or "UTC"
    return f"{parsed:%m/%d/%Y} {hour}:{parsed:%M} {parsed:%p} {zone_label}"


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}..."


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc.removeprefix("www.") or None
