from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from backend.agents.digestor.base import NormalizedPayload
from backend.agents.discovery.adapters import CollectionsSourceAdapter
from backend.agents.discovery.collections_source import collections_status, search_collections, sync_collections
from backend.agents.discovery.types import SourceAdapterContext, TopicProfile
from backend.agents.librarian.articles import direct_article_results
from backend.app.db import database
from backend.app.main import create_app


def _runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    collections_root = runtime / "Collections"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv("MORNING_DISPATCH_COLLECTIONS_ROOT", str(collections_root))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    monkeypatch.setenv("MORNING_DISPATCH_SHARED_SEARCH_ENV_PATH", str(runtime / "missing-hermes.env"))
    return runtime, collections_root


def test_collections_setup_creates_root_and_status_reports_collections(monkeypatch, tmp_path) -> None:
    _runtime_path, collections_root = _runtime(monkeypatch, tmp_path)

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        before = client.get("/api/explore/source-status")
        assert before.status_code == 200
        assert before.json()["sources"]["collections"]["enabled"] is False

        created = client.post("/api/admin/collections/setup")
        assert created.status_code == 200
        assert collections_root.exists()

        (collections_root / "AI Course").mkdir()
        after = client.get("/api/explore/source-status")
        assert after.status_code == 200
        assert after.json()["sources"]["collections"]["enabled"] is True
        assert after.json()["sources"]["collections"]["collection_count"] == 1


def test_collections_sync_indexes_text_files_and_searches(monkeypatch, tmp_path) -> None:
    _runtime_path, collections_root = _runtime(monkeypatch, tmp_path)
    database.init_database()
    course = collections_root / "AI Course"
    course.mkdir(parents=True)
    (course / "notes.md").write_text(
        "Local AI evaluation playbooks should compare sources, citations, and deployment risk. " * 8,
        encoding="utf-8",
    )
    (course / "slides.pdf").write_text("not really a pdf", encoding="utf-8")

    summary = sync_collections(collections_root, max_file_bytes=1_000_000)
    matches = search_collections("local AI evaluation sources", limit=5)

    assert summary["collection_count"] == 1
    assert summary["indexed_count"] == 1
    assert summary["unsupported_count"] == 1
    assert len(matches) == 1
    assert matches[0].collection_name == "AI Course"
    assert "evaluation" in matches[0].matched_terms


def test_collections_adapter_returns_brief_candidates(monkeypatch, tmp_path) -> None:
    _runtime_path, collections_root = _runtime(monkeypatch, tmp_path)
    database.init_database()
    course = collections_root / "AI Course"
    course.mkdir(parents=True)
    (course / "notes.md").write_text(
        "Agentic local AI systems need source-aware evaluation and operational review. " * 8,
        encoding="utf-8",
    )

    candidates = asyncio.run(
        CollectionsSourceAdapter().query(
            TopicProfile.from_dict(
                {
                    "statement": "local AI source-aware evaluation",
                    "scope": "source-aware evaluation for local AI systems",
                    "source_selection": {"collections": True},
                }
            ),
            SourceAdapterContext(exploration_id="explore-1", candidate_limit=5),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].adapter == "collections"
    assert candidates[0].payload.source_type == "collection_chunk"
    assert candidates[0].payload.source_name == "AI Course"
    assert candidates[0].payload.metadata["relative_path"] == "AI Course/notes.md"


def test_collections_payloads_are_direct_brief_inputs() -> None:
    payload = NormalizedPayload(
        source_type="collection_chunk",
        source_name="AI Course",
        raw_text="Collection note about source-aware local AI evaluation. " * 8,
        original_url="file:///tmp/Collections/AI%20Course/notes.md",
        metadata={
            "collection_quality_score": 0.9,
            "collection_name": "AI Course",
            "title": "AI Course: notes.md",
        },
    )

    results = direct_article_results([payload])

    assert len(results) == 1
    assert results[0].section == "Collections"
    assert results[0].content_type == "collection"
    assert results[0].link_score == 0.9


def test_collections_status_without_root(monkeypatch, tmp_path) -> None:
    _runtime_path, collections_root = _runtime(monkeypatch, tmp_path)
    database.init_database()

    status = collections_status(collections_root)

    assert status["root_exists"] is False
    assert status["collection_count"] == 0
