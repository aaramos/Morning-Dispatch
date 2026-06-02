import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.agents.discovery.types import DiscoveryResult, Candidate, NormalizedPayload
from backend.agents.librarian.articles import ArticleFetchResult
from backend.app.services.reporting import (
    compile_reporting_data,
    save_reporting_log,
    get_or_build_reporting_log,
    reconstruct_reporting_data,
)


@pytest.fixture
def mock_candidate_and_fetch_results():
    payload1 = NormalizedPayload(
        id="cand_1",
        source_type="web_search",
        source_name="Google",
        original_url="https://google.com/1",
        raw_text="This is candidate 1 text.",
        published_at="2026-06-01T12:00:00Z",
        fetched_at="2026-06-01T12:05:00Z",
        metadata={},
    )
    c1 = Candidate(payload=payload1, score=0.8, reason="Interest overlap", adapter="web_search")

    payload2 = NormalizedPayload(
        id="cand_2",
        source_type="gmail",
        source_name="Newsletter",
        original_url="https://newsletter.com/2",
        raw_text="This is candidate 2 text.",
        published_at="2026-06-01T12:00:00Z",
        fetched_at="2026-06-01T12:05:00Z",
        metadata={},
    )
    c2 = Candidate(payload=payload2, score=0.7, reason="Good newsletters", adapter="gmail")

    payload3 = NormalizedPayload(
        id="cand_3",
        source_type="youtube",
        source_name="YouTube",
        original_url="https://youtube.com/3",
        raw_text="This is candidate 3 text.",
        published_at="2026-06-01T12:00:00Z",
        fetched_at="2026-06-01T12:05:00Z",
        metadata={},
    )
    c3 = Candidate(payload=payload3, score=0.9, reason="Video update", adapter="youtube")

    f1 = ArticleFetchResult(
        payload=payload1,
        original_url="https://google.com/1",
        final_url="https://google.com/1",
        title="Title 1",
        text="This is candidate 1 text.",
        excerpt="excerpt 1",
        domain="google.com",
        status="fetched",
        tier="main",
    )
    f2 = ArticleFetchResult(
        payload=payload2,
        original_url="https://newsletter.com/2",
        final_url="https://newsletter.com/2",
        title="Title 2",
        text="",
        excerpt="",
        domain="newsletter.com",
        status="failed",
        error="404 Page Not Found",
        tier="dropped",
    )
    f3 = ArticleFetchResult(
        payload=payload3,
        original_url="https://youtube.com/3",
        final_url="https://youtube.com/3",
        title="Title 3",
        text="Video transcript",
        excerpt="video excerpt",
        domain="youtube.com",
        status="fetched",
        tier="main",
    )

    return [c1, c2, c3], [f1, f2, f3]


def test_compile_reporting_data(mock_candidate_and_fetch_results):
    candidates, fetch_results = mock_candidate_and_fetch_results
    discovery = DiscoveryResult(
        profile=MagicMock(),
        candidates=tuple([candidates[0], candidates[2]]),  # cand_1 and cand_3 survived discovery
        statuses=tuple([]),
        exclusions=tuple([
            {
                "candidate_id": "cand_2",
                "adapter": "gmail",
                "original_url": "https://newsletter.com/2",
                "title": "Title 2",
                "excluded_by": ["agentic_screening"],
                "reason": "Filtered by agentic screening (spam, promotion, or off-topic).",
            }
        ]),
    )

    source_window_issues = []
    enriched_articles = [fetch_results[0], fetch_results[2]]
    ranked_articles = [fetch_results[0], fetch_results[2]]
    after_audit = [fetch_results[0], fetch_results[2]]
    after_editorial = [fetch_results[0], fetch_results[2]]
    after_critic = [fetch_results[0], fetch_results[2]]
    final_results = [fetch_results[0]]  # cand_3 dropped by inclusion limits

    progress = {}

    report = compile_reporting_data(
        exploration_id="test_exp_123",
        discovery=discovery,
        fetched_articles=fetch_results,
        source_window_issues=source_window_issues,
        enriched_articles=enriched_articles,
        ranked_articles=ranked_articles,
        after_audit=after_audit,
        after_editorial=after_editorial,
        after_critic=after_critic,
        final_results=final_results,
        progress=progress,
    )

    assert len(report) == 3
    cand_reports = {item["id"]: item for item in report}

    # Verify cand_1 (included)
    assert cand_reports["cand_1"]["stages"]["inclusion"] is None
    assert all(val is None for val in cand_reports["cand_1"]["stages"].values())

    # Verify cand_2 (screening drop)
    assert cand_reports["cand_2"]["stages"]["screening"] == "Filtered by agentic screening (spam, promotion, or off-topic)."
    assert cand_reports["cand_2"]["stages"]["discovery"] is None

    # Verify cand_3 (inclusion limits drop)
    assert cand_reports["cand_3"]["stages"]["inclusion"] == "Exceeded source-specific capacity limit (YouTube/Podcast capped at 20; Gmail/Markets/Web/Foreign capped at 40)."


def test_save_and_retrieve_reporting_log(tmp_path):
    report_data = [{"id": "test_1", "title": "Test Title", "stages": {}}]
    
    with patch("backend.app.services.reporting.get_settings") as mock_settings:
        mock_set = MagicMock()
        mock_set.data_dir = tmp_path
        mock_settings.return_value = mock_set
        
        path_str = save_reporting_log("test_id", report_data)
        assert Path(path_str).exists()
        
        # Test retrieval
        retrieved = get_or_build_reporting_log("test_id")
        assert len(retrieved) == 1
        assert retrieved[0]["id"] == "test_1"


def test_reconstruct_reporting_data_fallback(tmp_path):
    # Setup mock exploration and brief HTML
    exploration = {
        "exploration_id": "legacy_exp",
        "brief_ref": str(tmp_path / "exploration-legacy_exp.html"),
        "progress": {
            "exclusions": [
                {
                    "candidate_id": "ex_1",
                    "adapter": "web_search",
                    "title": "Excluded 1",
                    "original_url": "https://excluded.com/1",
                    "excluded_by": ["keyword"],
                    "reason": "Exclusions keyword check.",
                }
            ],
            "source_filter_notes": [
                {
                    "item_url": "https://recency.com/1",
                    "item": "Recency title",
                    "source": "web_search",
                    "reason": "Outside lookback window.",
                }
            ],
        },
    }

    brief_html = """
    <html>
      <body>
        <h3 class="story-title"><a href="https://included.com/1">Included Story</a></h3>
        <h3 class="media-title"><a href="https://youtube.com/v1">YouTube Video</a></h3>
      </body>
    </html>
    """
    
    # Write mock brief HTML
    Path(exploration["brief_ref"]).write_text(brief_html, encoding="utf-8")

    with patch("backend.app.services.reporting.database.get_exploration") as mock_get:
        mock_get.return_value = exploration
        
        report = reconstruct_reporting_data("legacy_exp")
        assert len(report) >= 3
        
        cand_map = {item["url"]: item for item in report if item["url"]}
        assert "https://included.com/1" in cand_map
        assert cand_map["https://included.com/1"]["title"] == "Included Story"
        
        assert "https://excluded.com/1" in cand_map
        assert cand_map["https://excluded.com/1"]["stages"]["discovery"] == "Exclusions keyword check."
        
        assert "https://recency.com/1" in cand_map
        assert cand_map["https://recency.com/1"]["stages"]["recency"] == "Outside lookback window."
