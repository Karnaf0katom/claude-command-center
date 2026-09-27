"""Tests for ccc_server/ship_graph.py: commit/ticket graph and shipped answers."""

import json
import os
import sqlite3
import subprocess
import time
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
def mock_trap_env(tmp_path, monkeypatch):
    """Synthetic two-repo trap environment: locative scopes, keyword lookalikes,
    a hidden-dir mirror clone, and a same-subject branch/main duplicate."""
    db_path = tmp_path / "trap_ship_graph.sqlite"
    wt_db_path = tmp_path / "trap_queues.db"
    projects_dir = tmp_path / "projects"
    codex_dir = tmp_path / "codex"
    repo_alpha = tmp_path / "alpha-app"
    repo_beta = tmp_path / "beta-site"

    projects_dir.mkdir(parents=True)
    codex_dir.mkdir(parents=True)

    def init_repo(path):
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)

    def commit(path, filename, content, subject, ts=None):
        (path / filename).write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", filename], cwd=path, check=True, capture_output=True)
        env = None
        if ts is not None:
            env = {**os.environ,
                   "GIT_AUTHOR_DATE": f"@{int(ts)} +0000",
                   "GIT_COMMITTER_DATE": f"@{int(ts)} +0000"}
        subprocess.run(["git", "commit", "-m", subject], cwd=path, check=True,
                       capture_output=True, env=env)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                              capture_output=True, text=True).stdout.strip()

    init_repo(repo_alpha)
    init_repo(repo_beta)

    sha_a1 = commit(repo_alpha, "a1.txt", "1",
                    "feat(chat): add step-back button to huddle replay controls")
    sha_a2 = commit(repo_alpha, "a2.txt", "2",
                    "feat(canvas): huddle replay controls inside the canvas board")
    sha_a3 = commit(repo_alpha, "a3.txt", "3",
                    "fix(queue): resolve ticket detail lantern button when claim is unbackfilled")
    sha_a4 = commit(repo_alpha, "a4.txt", "4",
                    "feat(prefs): add toggle to hide the memory banner in preferences")
    sha_a5 = commit(repo_alpha, "a5.txt", "5",
                    "feat(chat): add emoji reaction picker to huddle replay")
    sha_a6 = commit(repo_alpha, "a6.txt", "6",
                    "feat(history): ticket graph for lantern search and ledger")
    sha_a7 = commit(repo_alpha, "a7.txt", "7",
                    "chore: update star chart [skip ci]")

    sha_b1 = commit(repo_beta, "b1.txt", "1",
                    "feat(site): add partner controls to checkout flows")
    sha_b2 = commit(repo_beta, "b2.txt", "2",
                    "fix(site): ignore .build-tmp so the stylesheet scanner skips build output")

    now = time.time()
    subprocess.run(["git", "checkout", "-b", "next"], cwd=repo_beta, check=True, capture_output=True)
    sha_b3 = commit(repo_beta, "b3.txt", "3",
                    "fix(site): visitor SMS skips crawler visits", ts=now - 3600)
    subprocess.run(["git", "checkout", "main"], cwd=repo_beta, check=True, capture_output=True)
    sha_b4 = commit(repo_beta, "b4.txt", "4",
                    "fix(site): visitor SMS skips crawler visits", ts=now)

    # Hidden-dir mirror clone of beta-site: must never be indexed
    mirror_dir = tmp_path / ".mirror"
    mirror_dir.mkdir(parents=True)
    subprocess.run(["git", "clone", str(repo_beta), str(mirror_dir / "beta-site")],
                   check=True, capture_output=True)

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
        zephyr1 = {
            "title": "Zephyr queue triage sweep",
            "text": "",
            "resolution": {"commit": sha_a2},
        }
        zephyr2 = {
            "title": "ZEPHYR-1b: lantern evidence precision",
            "text": "",
        }
        wt_conn.execute(
            """INSERT INTO items (ref, project, number, status, updated_at, item_json)
               VALUES (?, ?, ?, ?, datetime('now'), ?)""",
            ("ZEPHYR-1", "ZEPHYR", 1, "closed", json.dumps(zephyr1)),
        )
        wt_conn.execute(
            """INSERT INTO items (ref, project, number, status, updated_at, item_json)
               VALUES (?, ?, ?, ?, datetime('now'), ?)""",
            ("ZEPHYR-2", "ZEPHYR", 2, "open", json.dumps(zephyr2)),
        )
        wt_conn.commit()

    repos_str = os.pathsep.join([
        str(repo_alpha), str(repo_beta), str(mirror_dir / "beta-site"),
    ])
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
        "repo_alpha": repo_alpha,
        "repo_beta": repo_beta,
        "mirror": mirror_dir / "beta-site",
        "db_path": db_path,
        "sha_a1": sha_a1, "sha_a2": sha_a2, "sha_a3": sha_a3, "sha_a4": sha_a4,
        "sha_a5": sha_a5, "sha_a6": sha_a6, "sha_a7": sha_a7,
        "sha_b1": sha_b1, "sha_b2": sha_b2, "sha_b3": sha_b3, "sha_b4": sha_b4,
    }


def test_locative_chunk_required(mock_trap_env):
    """A locative scope named in the question selects the right commit and rejects wrong scopes."""
    env = mock_trap_env

    res = ship_graph.is_shipped("Did we ship huddle replay in the canvas board?")
    assert res["shipped"] is True
    assert res["evidence"][0]["commit"] == env["sha_a2"]

    res_side = ship_graph.is_shipped("Did we ship huddle replay in the sidebar?")
    assert res_side["shipped"] is False
    assert res_side["evidence"] == []
    assert res_side["confidence"] <= 0.85


def test_locative_escape_hatch_for_specific_subject(mock_trap_env):
    """A subject covering 3+ non-locative distinguishing terms outweighs an
    unmatched location — deliberate tradeoff, but never at high confidence."""
    env = mock_trap_env

    res = ship_graph.is_shipped("Did we ship the emoji reaction picker in the sidebar?")
    assert res["shipped"] is True
    assert res["evidence"][0]["commit"] == env["sha_a5"]
    assert res["confidence"] <= 0.85

    # Only 2 non-locative distinguishing terms: the escape hatch must NOT apply
    res_side = ship_graph.is_shipped("Did we ship huddle replay in the sidebar?")
    assert res_side["shipped"] is False
    assert res_side["evidence"] == []


def test_short_question_requires_all_distinguishing_terms(mock_trap_env):
    """Short questions require every distinguishing term in subject or body."""
    res1 = ship_graph.is_shipped("Did we add lantern search for the queue?")
    assert res1["shipped"] is False
    assert res1["evidence"] == []
    assert res1["confidence"] <= 0.85

    res2 = ship_graph.is_shipped("Did we ship the dusk theme on the preferences page?")
    assert res2["shipped"] is False
    assert res2["evidence"] == []
    assert res2["confidence"] <= 0.85

    res3 = ship_graph.is_shipped("Did we ship the checkout flow overhaul?")
    assert res3["shipped"] is False
    assert res3["evidence"] == []
    assert res3["confidence"] <= 0.85


def test_repo_named_lookalike_in_other_repo(mock_trap_env):
    """Naming a repo restricts evidence to it; a lookalike term elsewhere stays False."""
    res_beta = ship_graph.is_shipped("Did beta-site ship huddle replay controls?")
    assert res_beta["shipped"] is False
    assert res_beta["evidence"] == []

    res_alpha = ship_graph.is_shipped("Did alpha-app ship huddle replay controls?")
    assert res_alpha["shipped"] is True
    assert res_alpha["evidence"][0]["repo"] == "alpha-app"


def test_hidden_root_ignored_and_stale_repo_pruned(mock_trap_env):
    """Hidden-path roots are rejected at discovery, and stale repos are pruned on sync."""
    env = mock_trap_env

    roots = ship_graph.discover_repo_roots()
    assert roots.get("beta-site") == str(env["repo_beta"].resolve())
    assert all("/.mirror/" not in p for p in roots.values())

    res = ship_graph.is_shipped("Did we make the stylesheet scanner skip build output?")
    assert res["shipped"] is True
    assert res["evidence"][0]["repo"] == "beta-site"
    assert res["evidence"][0]["commit"] == env["sha_b2"]

    db_path = env["db_path"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO repos (path, name, head_sha, indexed_at) VALUES (?, ?, ?, ?)",
            ("/nonexistent/ghost-repo", "ghost-repo", "x", 0),
        )
        conn.execute(
            "INSERT INTO commits (commit_id, repo, hash, short_hash, ts, subject, body, files) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("ghost-repo:deadbeef", "ghost-repo", "deadbeef" * 5, "deadbee",
             0.0, "chore: phantom commit", "", ""),
        )

    ship_graph._last_sync_ts = 0.0
    ship_graph.is_shipped("anything")

    with sqlite3.connect(db_path) as conn:
        n_commits = conn.execute(
            "SELECT count(*) FROM commits WHERE repo = 'ghost-repo'").fetchone()[0]
        n_repos = conn.execute(
            "SELECT count(*) FROM repos WHERE name = 'ghost-repo'").fetchone()[0]
    assert n_commits == 0
    assert n_repos == 0


def test_identical_subject_prefers_main_commit(mock_trap_env):
    """Identical-subject duplicates prefer the commit reachable from the main ref."""
    env = mock_trap_env
    res = ship_graph.is_shipped("Did we make visitor SMS skip crawler visits?")
    assert res["shipped"] is True
    assert res["evidence"][0]["commit"] == env["sha_b4"]


def test_ticket_project_identifier_not_treated_as_content(mock_trap_env):
    """A real WatchTower project/ref token is a routing hint, not content —
    it must not break matching or let an open project ticket override."""
    env = mock_trap_env

    res = ship_graph.is_shipped("Did we ship ZEPHYR lantern search?")
    assert res["shipped"] is True
    assert res["evidence"][0]["commit"] == env["sha_a6"]

    res2 = ship_graph.is_shipped("Did we ship the lantern search for ZEPHYR-2?")
    assert res2["shipped"] is True
    assert res2["evidence"][0]["commit"] == env["sha_a6"]


def test_ci_bot_commits_ignored(mock_trap_env):
    """Automated CI-marker commits are not shipping evidence."""
    res = ship_graph.is_shipped("Did we ship the star chart widget?")
    assert res["shipped"] is False
    assert res["evidence"] == []


def test_detect_named_repo_prefers_exact_root_over_alias():
    """An exact discovered root name beats a shorter alias match."""
    roots = {"claude-command-center": "/x/claude-command-center",
             "ccc-memory": "/x/ccc-memory"}
    assert ship_graph.detect_named_repo(
        "Did we ship the accept gate in ccc-memory?", roots)[0] == "ccc-memory"
    assert ship_graph.detect_named_repo(
        "Did CCC add the gate?", roots)[0] == "claude-command-center"


def test_question_structure_helper():
    """_question_structure extracts locative chunks from the first clause only."""
    s = ship_graph._question_structure
    stem = ship_graph._stem

    res = s("Did we ship huddle replay in the canvas board so it works?", set())
    assert res["locative_chunks"] == [{stem("canvas"), stem("board")}]

    res_no_prep = s("Did we ship huddle replay controls?", set())
    assert res_no_prep["locative_chunks"] == []

    res_after_break = s("Did we ship huddle replay so it lands in the sidebar?", set())
    assert res_after_break["locative_chunks"] == []



