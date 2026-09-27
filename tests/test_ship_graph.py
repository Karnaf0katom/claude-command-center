"""Tests for ccc_server/ship_graph.py: commit/ticket graph and shipped answers."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import ccc_server.ship_graph as ship_graph


@pytest.fixture
def mock_graph_env(tmp_path, monkeypatch):
    """Sets up an isolated environment for ship_graph testing."""
    db_path = tmp_path / "ship_graph.sqlite"
    wt_db_path = tmp_path / "queues.db"
    projects_dir = tmp_path / "projects"
    codex_dir = tmp_path / "codex"
    repo_dir = tmp_path / "test-repo"

    projects_dir.mkdir(parents=True)
    codex_dir.mkdir(parents=True)
    repo_dir.mkdir(parents=True)

    # Initialize a git repository with sample commits
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    test_file = repo_dir / "app.py"
    test_file.write_text("# initial", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "feat(auth): add biometric webauthn login support"], cwd=repo_dir, check=True)

    commit_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    # Create watchtower queues.db with closed and open tickets in items table
    with sqlite3.connect(wt_db_path) as wt_conn:
        wt_conn.execute("""
            CREATE TABLE items (
                ref TEXT PRIMARY KEY,
                project TEXT,
                number INTEGER,
                status TEXT,
                updated_at TEXT,
                item_json TEXT
            )
        """)
        auth_item = {
            "title": "Add biometric webauthn login support",
            "text": "Closed with commit",
            "resolution": {"commit": commit_sha},
        }
        wt_conn.execute(
            """INSERT INTO items (ref, project, number, status, updated_at, item_json)
               VALUES (?, ?, ?, ?, datetime('now'), ?)""",
            ("AUTH-101", "AUTH", 101, "closed", json.dumps(auth_item)),
        )
        pay_item = {
            "title": "Support cryptocurrency bitcoin payments",
            "text": "Planned for Q4",
        }
        wt_conn.execute(
            """INSERT INTO items (ref, project, number, status, updated_at, item_json)
               VALUES (?, ?, ?, ?, datetime('now'), ?)""",
            ("PAY-202", "PAY", 202, "open", json.dumps(pay_item)),
        )
        wt_conn.commit()

    # Create a transcript that references the commit and session
    repo_sessions_dir = projects_dir / "test-repo"
    repo_sessions_dir.mkdir(parents=True)
    session_file = repo_sessions_dir / "session-001.jsonl"
    lines = [
        {
            "type": "user",
            "cwd": str(repo_dir),
            "timestamp": "2026-09-20T10:00:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": f"[main {commit_sha[:7]}] feat(auth): add biometric webauthn login support\n 1 file changed",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-20T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": f"Commit created [{commit_sha[:8]}] for AUTH-101",
            },
        },
    ]
    session_file.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    monkeypatch.setenv("CCC_SHIP_GRAPH_DB", str(db_path))
    monkeypatch.setenv("WATCHTOWER_DB", str(wt_db_path))
    monkeypatch.setenv("CCC_PROJECTS_ROOT", str(projects_dir))
    monkeypatch.setenv("CCC_CODEX_SESSIONS_ROOT", str(codex_dir))
    monkeypatch.setenv("CCC_SHIP_GRAPH_DAYS", "45")
    monkeypatch.setenv("CCC_SHIP_GRAPH_REPOS", str(repo_dir))

    # Reset thread-local connection and sync timestamp
    if hasattr(ship_graph._tls, "conn") and ship_graph._tls.conn:
        try:
            ship_graph._tls.conn.close()
        except Exception:
            pass
        ship_graph._tls.conn = None
    ship_graph._last_sync_ts = 0.0

    return {
        "repo_dir": repo_dir,
        "commit_sha": commit_sha,
        "db_path": db_path,
        "wt_db_path": wt_db_path,
    }


def test_is_shipped_contract_on_empty_and_unknown(mock_graph_env):
    """Empty or unknown queries return standard contract shape."""
    empty_res = ship_graph.is_shipped("")
    assert empty_res == {"shipped": False, "confidence": 0.0, "evidence": [], "tickets": []}

    unknown_res = ship_graph.is_shipped("quantum flux capacitor teleportation")
    assert "shipped" in unknown_res
    assert "confidence" in unknown_res
    assert "evidence" in unknown_res
    assert "tickets" in unknown_res
    assert unknown_res["shipped"] is False
    assert unknown_res["evidence"] == []


def test_is_shipped_positive_with_evidence(mock_graph_env):
    """A shipped feature returns shipped=True with commit evidence and ticket."""
    env = mock_graph_env
    res = ship_graph.is_shipped("Did we ship biometric webauthn login?")

    assert res["shipped"] is True
    assert res["confidence"] >= 0.80
    assert len(res["evidence"]) > 0

    top_evidence = res["evidence"][0]
    assert top_evidence["commit"] == env["commit_sha"]
    assert top_evidence["repo"] == "test-repo"
    assert "biometric" in top_evidence["subject"]
    assert top_evidence.get("session_id") == "session-001"
    assert "AUTH-101" in res["tickets"]


def test_is_shipped_negative_on_open_ticket(mock_graph_env):
    """An open ticket with no commits returns shipped=False with high confidence."""
    res = ship_graph.is_shipped("Do we support cryptocurrency bitcoin payments?")

    assert res["shipped"] is False
    assert res["confidence"] >= 0.80
    assert res["evidence"] == []
    assert "PAY-202" in res["tickets"]


def test_search_sessions_contract(mock_graph_env):
    """search_sessions returns ranked session results matching schema."""
    res = ship_graph.search_sessions("biometric webauthn login", limit=10)
    assert isinstance(res, list)
    if res:
        assert "session_id" in res[0]
        assert res[0]["session_id"] == "session-001"

    empty_res = ship_graph.search_sessions("", limit=10)
    assert empty_res == []
