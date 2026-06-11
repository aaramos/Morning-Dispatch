from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.core.config import ensure_runtime_dirs, get_settings
from backend.app.db import database
from backend.app.main import create_app


def configure_runtime(monkeypatch, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MORNING_DISPATCH_HOME", str(runtime))
    monkeypatch.setenv("MORNING_DISPATCH_DATA_DIR", str(runtime / "data"))
    monkeypatch.setenv("MORNING_DISPATCH_SECRETS_DIR", str(runtime / "secrets"))
    monkeypatch.setenv(
        "MORNING_DISPATCH_DB_PATH",
        str(runtime / "data" / "db" / "morning_dispatch.sqlite3"),
    )
    ensure_runtime_dirs(get_settings())


def _create_exploration() -> dict:
    profile = database.upsert_topic_profile(
        {
            "statement": "Track database performance work",
            "scope": "DB perf",
            "source_selection": {"web_search": True},
        }
    )
    return database.create_exploration(
        topic_id=profile["topic_id"],
        mode="show_now",
        source_selection={"web_search": True},
        status="running",
    )


FULL_PROGRESS = {
    "queue": {"status": "running", "message": "Building now.", "action": "build"},
    "pipeline": {"discovery": "complete", "fetch": "running"},
    "sources": {"web_search": {"status": "complete", "candidate_count": 4}},
    "candidate_count": 4,
    "source_audit": {"status": "complete", "summary": "Sources match."},
    "source_audit_issues": [{"source_name": "Source Audit", "reason": "audit note"}],
    "source_filter_notes": [{"source_name": "Web", "reason": "too old"}],
    "requested_source_issues": [{"source_name": "Pod", "reason": "unresolved"}],
    "model_health": {"status": "ok"},
    "built_with_issues": False,
    "error": None,
    "brief": {
        "title": "DB Perf Brief",
        "html_path": "/api/explore/explorations/x/brief/html",
        "snapshot": "a long snapshot " * 50,
        "stats": {"model_call_count": 3, "model_success_count": 3},
        "candidate_count": 4,
    },
    # Heavy diagnostics that must be trimmed from the list view.
    "reasoning": {"editorial": "x" * 2000, "critic": "y" * 2000},
    "exclusions": [{"url": f"https://example.com/{i}"} for i in range(50)],
    "_intermediates": {"big": "z" * 5000},
    "source_date_review": {"items": ["a"] * 30},
    "must_have": {"anchors": ["term"] * 10},
}


def test_connect_sets_busy_timeout_and_synchronous(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    with database.connect() as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        # NORMAL == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_runs_ensure_runtime_dirs_once_per_data_dir(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    calls: list[object] = []
    original = database.ensure_runtime_dirs
    monkeypatch.setattr(
        database, "ensure_runtime_dirs", lambda settings: calls.append(settings) or original(settings)
    )
    database._RUNTIME_DIRS_READY.clear()
    with database.connect():
        pass
    with database.connect():
        pass
    assert len(calls) == 1


def test_update_exploration_progress_returns_bool(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    exploration = _create_exploration()

    assert database.update_exploration_progress(
        exploration["exploration_id"], progress={"queue": {"status": "running"}}
    ) is True
    assert database.update_exploration_progress("missing-id", progress={}) is False

    stored = database.get_exploration(exploration["exploration_id"])
    assert stored is not None
    assert stored["progress"] == {"queue": {"status": "running"}}


def test_list_explorations_summary_only_trims_heavy_progress(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    database.init_database()
    exploration = _create_exploration()
    database.update_exploration_progress(exploration["exploration_id"], progress=FULL_PROGRESS)

    [full] = database.list_explorations(limit=5)
    assert "reasoning" in full["progress"]
    assert "snapshot" in full["progress"]["brief"]

    [summary] = database.list_explorations(limit=5, summary_only=True)
    progress = summary["progress"]
    for key in (
        "queue",
        "pipeline",
        "sources",
        "candidate_count",
        "source_audit",
        "source_audit_issues",
        "source_filter_notes",
        "requested_source_issues",
        "model_health",
        "built_with_issues",
        "error",
    ):
        assert progress[key] == FULL_PROGRESS[key]
    for key in ("reasoning", "exclusions", "_intermediates", "source_date_review", "must_have"):
        assert key not in progress
    assert progress["brief"] == {
        "title": "DB Perf Brief",
        "html_path": "/api/explore/explorations/x/brief/html",
        "stats": {"model_call_count": 3, "model_success_count": 3},
        "candidate_count": 4,
    }


def test_explorations_list_route_returns_summary(monkeypatch, tmp_path) -> None:
    configure_runtime(monkeypatch, tmp_path)
    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        exploration = _create_exploration()
        database.update_exploration_progress(exploration["exploration_id"], progress=FULL_PROGRESS)

        listed = client.get("/api/explore/explorations?limit=5")
        assert listed.status_code == 200
        [row] = [item for item in listed.json() if item["exploration_id"] == exploration["exploration_id"]]
        assert "reasoning" not in row["progress"]
        assert row["progress"]["queue"]["status"] == "running"
        assert row["progress"]["brief"]["title"] == "DB Perf Brief"
        assert "snapshot" not in row["progress"]["brief"]

        detail = client.get(f"/api/explore/explorations/{exploration['exploration_id']}")
        if detail.status_code == 200:
            assert "reasoning" in detail.json()["progress"]
