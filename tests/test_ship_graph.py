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


@pytest.fixture
def mock_multi_repo_env(tmp_path, monkeypatch):
    """Sets up an environment with multiple repositories for testing repo-named and keyword cases."""
    db_path = tmp_path / "multi_ship_graph.sqlite"
    wt_db_path = tmp_path / "multi_queues.db"
    projects_dir = tmp_path / "projects"
    codex_dir = tmp_path / "codex"
    repo_ccc = tmp_path / "claude-command-center"
    repo_bym = tmp_path / "BYM"

    projects_dir.mkdir(parents=True)
    codex_dir.mkdir(parents=True)
    repo_ccc.mkdir(parents=True)
    repo_bym.mkdir(parents=True)

    for r in (repo_ccc, repo_bym):
        subprocess.run(["git", "init", "-b", "main"], cwd=r, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=r, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=r, check=True)

    (repo_ccc / "f.txt").write_text("1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_ccc, check=True)
    subprocess.run(["git", "commit", "-m", "chore: remove Hunch permanently and block its return"], cwd=repo_ccc, check=True)
    sha_ccc_hunch = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_ccc, check=True, capture_output=True, text=True).stdout.strip()

    (repo_bym / "f.txt").write_text("2", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_bym, check=True)
    subprocess.run(["git", "commit", "-m", "chore: remove Hunch permanently in bym"], cwd=repo_bym, check=True)
    sha_bym_hunch = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_bym, check=True, capture_output=True, text=True).stdout.strip()

    (repo_bym / "f2.txt").write_text("3", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_bym, check=True)
    subprocess.run(["git", "commit", "-m", "feat(booking): add partner controls to booking flows"], cwd=repo_bym, check=True)
    sha_bym_flow = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_bym, check=True, capture_output=True, text=True).stdout.strip()

    (repo_ccc / "f_server.txt").write_text("server", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_ccc, check=True)
    subprocess.run(["git", "commit", "-m", "refactor(server): extract group-chat sidecar to server.py"], cwd=repo_ccc, check=True)
    sha_ccc_gc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_ccc, check=True, capture_output=True, text=True).stdout.strip()

    (repo_ccc / "f_log.txt").write_text("log", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_ccc, check=True)
    subprocess.run(["git", "commit", "-m", "feat(logs): add per-event copy button"], cwd=repo_ccc, check=True)
    sha_ccc_log = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_ccc, check=True, capture_output=True, text=True).stdout.strip()

    (repo_ccc / "f_set.txt").write_text("settings", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_ccc, check=True)
    subprocess.run(["git", "commit", "-m", "fix(settings): per-event copy button reference"], cwd=repo_ccc, check=True)
    sha_ccc_set = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_ccc, check=True, capture_output=True, text=True).stdout.strip()

    (repo_ccc / "f_sess.txt").write_text("sess", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_ccc, check=True)
    subprocess.run(["git", "commit", "-m", "fix(sessions): fix button styling in session list"], cwd=repo_ccc, check=True)
    sha_ccc_sess = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_ccc, check=True, capture_output=True, text=True).stdout.strip()

    repo_idx = tmp_path / "indexing"
    repo_idx.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_idx, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_idx, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_idx, check=True)
    (repo_idx / "f_idx.txt").write_text("idx", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_idx, check=True)
    subprocess.run(["git", "commit", "-m", "fix(search): drop self-referential sessions, add exclude-session"], cwd=repo_idx, check=True)
    sha_idx_search = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_idx, check=True, capture_output=True, text=True).stdout.strip()

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
        wt_conn.commit()

    repos_str = f"{repo_ccc}{os.pathsep}{repo_bym}{os.pathsep}{repo_idx}"
    monkeypatch.setenv("CCC_SHIP_GRAPH_DB", str(db_path))
    monkeypatch.setenv("WATCHTOWER_DB", str(wt_db_path))
    monkeypatch.setenv("CCC_PROJECTS_ROOT", str(projects_dir))
    monkeypatch.setenv("CCC_CODEX_SESSIONS_ROOT", str(codex_dir))
    monkeypatch.setenv("CCC_SHIP_GRAPH_DAYS", "45")
    monkeypatch.setenv("CCC_SHIP_GRAPH_REPOS", repos_str)

    if hasattr(ship_graph._tls, "conn") and ship_graph._tls.conn:
        try:
            ship_graph._tls.conn.close()
        except Exception:
            pass
        ship_graph._tls.conn = None
    ship_graph._last_sync_ts = 0.0

    return {
        "repo_ccc": repo_ccc,
        "repo_bym": repo_bym,
        "repo_idx": repo_idx,
        "sha_ccc_hunch": sha_ccc_hunch,
        "sha_bym_hunch": sha_bym_hunch,
        "sha_bym_flow": sha_bym_flow,
        "sha_ccc_gc": sha_ccc_gc,
        "sha_ccc_log": sha_ccc_log,
        "sha_ccc_set": sha_ccc_set,
        "sha_ccc_sess": sha_ccc_sess,
        "sha_idx_search": sha_idx_search,
    }


def test_is_shipped_repo_named_preference(mock_multi_repo_env):
    """Repo named in question restricts/prefers evidence to that repository."""
    env = mock_multi_repo_env

    # 1. 'removed Hunch from CCC' should pick CCC commit, not BYM commit
    res_ccc = ship_graph.is_shipped("Did we remove Hunch from CCC?")
    assert res_ccc["shipped"] is True
    assert res_ccc["confidence"] >= 0.90
    assert len(res_ccc["evidence"]) > 0
    assert res_ccc["evidence"][0]["repo"] == "claude-command-center"
    assert res_ccc["evidence"][0]["commit"] == env["sha_ccc_hunch"]

    # 2. 'Did BYM ship booking flows?' matches BYM
    res_bym = ship_graph.is_shipped("Did BYM ship booking flows?")
    assert res_bym["shipped"] is True
    assert res_bym["confidence"] >= 0.90
    assert len(res_bym["evidence"]) > 0
    assert res_bym["evidence"][0]["repo"] == "BYM"
    assert res_bym["evidence"][0]["commit"] == env["sha_bym_flow"]

    # 3. 'Did CCC ship booking flows?' should be False since it was only in BYM
    res_ccc_flow = ship_graph.is_shipped("Did CCC ship booking flows?")
    assert res_ccc_flow["shipped"] is False
    assert res_ccc_flow["evidence"] == []


def test_is_shipped_single_keyword_rejected(mock_multi_repo_env):
    """A single shared keyword is not enough to call something shipped."""
    # 'flow' alone matching 'booking flows' must be rejected
    res = ship_graph.is_shipped("Did we ship flows?")
    assert res["shipped"] is False
    assert res["confidence"] < 0.90
    assert res["evidence"] == []

    # 'partner' alone must be rejected
    res2 = ship_graph.is_shipped("Did we ship partner?")
    assert res2["shipped"] is False
    assert res2["confidence"] < 0.90
    assert res2["evidence"] == []


def test_is_shipped_common_product_word_and_noun_phrase_traps(mock_multi_repo_env):
    """Trap tests: common product words alone or partial noun phrases must not trigger shipped."""
    # 1. 'group chat in Flow':
    # BYM commit has 'booking flows' (only 'flow' matches).
    # CCC commit has 'extract group-chat sidecar' (matches 'group' & 'chat', but missing 'flow').
    # Neither should qualify as shipped for 'group chat in Flow'!
    res_flow = ship_graph.is_shipped("Did we ship group chat in Flow?")
    assert res_flow["shipped"] is False
    assert res_flow["evidence"] == []
    assert res_flow["confidence"] <= 0.65

    # 2. 'bulk export button for sessions':
    # CCC has 'fix(sessions): fix button styling in session list'
    # Matching 'button' and 'sessions' without 'bulk export' is a keyword trap!
    res_bulk = ship_graph.is_shipped("Did we add a bulk export button for sessions?")
    assert res_bulk["shipped"] is False
    assert res_bulk["evidence"] == []
    assert res_bulk["confidence"] <= 0.65

    # 3. 'MEMO-FIX session search':
    # indexing has 'fix(search): drop self-referential sessions'
    # Matches 'fix', 'search', 'sessions' - but MEMO-FIX project is not matched!
    res_memo = ship_graph.is_shipped("Did we ship MEMO-FIX session search?")
    assert res_memo["shipped"] is False
    assert res_memo["evidence"] == []
    assert res_memo["confidence"] <= 0.65


def test_is_shipped_scope_preference(mock_multi_repo_env):
    """Conventional-commit scope matching the question subject should be preferred."""
    env = mock_multi_repo_env
    # 'log view copy button' should prefer feat(logs): ... over fix(settings): ...
    res = ship_graph.is_shipped("Did the log view get a per-event copy button?")
    assert res["shipped"] is True
    assert len(res["evidence"]) > 0
    assert res["evidence"][0]["commit"] == env["sha_ccc_log"]


