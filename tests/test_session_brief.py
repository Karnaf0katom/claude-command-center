"""Tests for ccc_server/session_brief.py: `ccc brief <session>` /
GET /api/memory/brief/<session> — "where did this session leave off and how
do I resume it?" for one session, sourced entirely from state ship_graph and
session_fts already sync in the background (no new transcript scanning)."""

import json
import sqlite3
import subprocess

import pytest

import ccc_server.session_brief as session_brief
import ccc_server.session_fts as session_fts
import ccc_server.ship_graph as ship_graph


@pytest.fixture
def mock_brief_env(tmp_path, monkeypatch):
    ship_db = tmp_path / "ship_graph.sqlite"
    fts_db = tmp_path / "session_fts.sqlite"
    wt_db_path = tmp_path / "queues.db"
    projects_dir = tmp_path / "projects"
    codex_dir = tmp_path / "codex"
    repo_dir = tmp_path / "widget-repo"

    projects_dir.mkdir(parents=True)
    codex_dir.mkdir(parents=True)
    repo_dir.mkdir(parents=True)

    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    (repo_dir / "app.py").write_text("# initial", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "feat(auth): add login endpoint"],
                    cwd=repo_dir, check=True)
    sha_pushed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir,
                                 check=True, capture_output=True, text=True).stdout.strip()
    # Fake a remote-tracking ref at this commit -- no real network fetch needed
    # to prove "on origin", just a refs/remotes/origin/main pointing at it.
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", sha_pushed],
                    cwd=repo_dir, check=True)

    (repo_dir / "app.py").write_text("# tweak", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: local tweak"], cwd=repo_dir, check=True)
    sha_local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir,
                                check=True, capture_output=True, text=True).stdout.strip()

    with sqlite3.connect(wt_db_path) as wt_conn:
        wt_conn.execute("""
            CREATE TABLE items (
                ref TEXT PRIMARY KEY, project TEXT, number INTEGER,
                status TEXT, updated_at TEXT, item_json TEXT
            )
        """)
        wt_conn.execute(
            """INSERT INTO items (ref, project, number, status, updated_at, item_json)
               VALUES (?, ?, ?, ?, datetime('now'), ?)""",
            ("AUTH-101", "AUTH", 101, "in_progress",
             json.dumps({"title": "Add login endpoint", "text": "in flight"})),
        )
        wt_conn.commit()

    session_dir = projects_dir / "widget-repo"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session-brief-1.jsonl"
    scratch_artifact = "/Users/testuser/dev/scratch/cost-report/report.html"
    lines = [
        {
            "type": "user", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:00:00Z",
            "message": {"role": "user", "content": "AUTH-101: add a login endpoint"},
        },
        {
            "type": "assistant", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:01:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": str(repo_dir / "app.py")}},
            ]},
        },
        {
            "type": "user", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:02:00Z",
            "message": {"role": "user", "content": "[watchtower] AUTH-101 claimed by worker-7"},
        },
        {
            "type": "user", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:03:00Z",
            "message": {"role": "user", "content": "now write the cost report to scratch"},
        },
        {
            "type": "assistant", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:04:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Writing the report and committing the login endpoint."},
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": scratch_artifact}},
            ]},
        },
        {
            "type": "user", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:05:00Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result",
                 "content": f"[main {sha_pushed[:7]}] feat(auth): add login endpoint"},
            ]},
        },
        {
            "type": "user", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:06:00Z",
            "message": {"role": "user", "content": "also commit the follow-up tweak"},
        },
        {
            "type": "user", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:07:00Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "content": f"[main {sha_local[:7]}] chore: local tweak"},
            ]},
        },
        {
            "type": "assistant", "cwd": str(repo_dir), "timestamp": "2026-09-20T10:08:00Z",
            "message": {"role": "assistant", "content": (
                f"Done — report written to {scratch_artifact}. Committed both changes; "
                "the tweak is local-only, not yet pushed. AUTH-101 is in progress."
            )},
        },
    ]
    session_file.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    monkeypatch.setenv("CCC_SHIP_GRAPH_DB", str(ship_db))
    monkeypatch.setenv("WATCHTOWER_DB", str(wt_db_path))
    monkeypatch.setenv("CCC_PROJECTS_ROOT", str(projects_dir))
    monkeypatch.setenv("CCC_CODEX_SESSIONS_ROOT", str(codex_dir))
    # Empty (non-real) roots for the other engines -- without these,
    # session_fts's corpus scan picks up this dev machine's real ~/.kimi-code,
    # ~/.gemini/tmp, ~/.cursor/projects sessions, which can push the pending
    # count over _BG_SYNC_THRESHOLD and defer indexing to a background thread
    # that hasn't finished by the time the test reads sdoc.
    monkeypatch.setenv("CCC_KIMI_SESSIONS_ROOT", str(tmp_path / "kimi-empty"))
    monkeypatch.setenv("CCC_GEMINI_TMP_ROOT", str(tmp_path / "gemini-empty"))
    monkeypatch.setenv("CCC_CURSOR_PROJECTS_ROOT", str(tmp_path / "cursor-empty"))
    monkeypatch.setenv("CCC_SHIP_GRAPH_DAYS", "0")
    monkeypatch.setenv("CCC_SHIP_GRAPH_REPOS", str(repo_dir))
    monkeypatch.setenv("CCC_SESSION_FTS_DB", str(fts_db))
    monkeypatch.setenv("CCC_SESSION_FTS_DAYS", "0")
    monkeypatch.setenv("CCC_SESSION_FTS_ALLOW_SCRATCH", "1")
    monkeypatch.setenv("CCC_SESSION_FTS_EMBED", "0")

    for mod in (ship_graph, session_fts):
        if hasattr(mod._tls, "conn") and mod._tls.conn:
            try:
                mod._tls.conn.close()
            except Exception:
                pass
            mod._tls.conn = None
    ship_graph._last_sync_ts = 0.0
    session_fts._last_sync_ts = 0.0
    ship_graph._bg_sync_running = False
    session_fts._bg_sync_running = False
    session_fts._ollama_state["ts"] = 0.0
    session_fts._ollama_state["ok"] = False
    session_fts._vec_cache["sids"] = []
    session_fts._vec_cache["vecs"] = []
    ship_graph._base_search_sessions = None

    return {
        "repo_dir": repo_dir,
        "sha_pushed": sha_pushed,
        "sha_local": sha_local,
        "scratch_artifact": scratch_artifact,
        "sid": "session-brief-1",
    }


def test_brief_resolves_by_exact_session_id(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    assert res["found"] is True
    assert res["session_id"] == mock_brief_env["sid"]
    assert res["alternates"] == []


def test_brief_resolves_by_unambiguous_id_prefix(mock_brief_env):
    res = session_brief.brief("session-brief")
    assert res["found"] is True
    assert res["session_id"] == mock_brief_env["sid"]


def test_brief_resolves_by_recall_query_with_alternates(mock_brief_env):
    res = session_brief.brief("login endpoint cost report")
    assert res["found"] is True
    assert res["session_id"] == mock_brief_env["sid"]


def test_brief_no_match_reports_not_found(mock_brief_env):
    res = session_brief.brief("quantum flux capacitor teleportation")
    assert res == {"query": "quantum flux capacitor teleportation",
                    "session_id": None, "alternates": [], "found": False}


def test_brief_metadata_and_engine(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    assert res["repo"] == "widget-repo"
    assert res["cwd"] == str(mock_brief_env["repo_dir"])
    assert res["engine"] == "claude"
    assert res["start_date"] and res["end_date"]


def test_brief_ticket_status_from_watchtower(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    tickets = {t["ref"]: t for t in res["tickets"]}
    assert "AUTH-101" in tickets
    assert tickets["AUTH-101"]["status"] == "in_progress"
    assert tickets["AUTH-101"]["title"]


def test_brief_last_user_asks_excludes_injected_messages(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    asks = res["last_user_asks"]
    assert asks == [
        "AUTH-101: add a login endpoint",
        "now write the cost report to scratch",
        "also commit the follow-up tweak",
    ]
    assert not any("watchtower" in a.lower() for a in asks)


def test_brief_last_assistant_reply_is_the_final_message(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    reply = res["last_assistant_reply"]
    assert "local-only, not yet pushed" in reply
    assert mock_brief_env["scratch_artifact"] in reply


def test_brief_commits_report_sha_and_origin_status(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    commits = {c["sha"]: c for c in res["commits"]}
    pushed_short = mock_brief_env["sha_pushed"][:7]
    local_short = mock_brief_env["sha_local"][:7]
    assert commits[pushed_short]["on_origin"] is True
    assert commits[local_short]["on_origin"] is False
    assert commits[pushed_short]["subject"] == "feat(auth): add login endpoint"


def test_brief_files_touched_includes_in_repo_edits(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    assert str(mock_brief_env["repo_dir"] / "app.py") in res["files_touched"]


def test_brief_artifacts_outside_repos_names_the_scratch_file(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    assert res["artifacts_outside_repos"] == [mock_brief_env["scratch_artifact"]]
    assert str(mock_brief_env["repo_dir"] / "app.py") not in res["artifacts_outside_repos"]


def test_brief_resume_command_for_claude_engine(mock_brief_env):
    res = session_brief.brief(mock_brief_env["sid"])
    assert res["resume_command"] == (
        f"cd {mock_brief_env['repo_dir']} && claude --resume {mock_brief_env['sid']}"
    )


def test_resolve_session_empty_query(mock_brief_env):
    assert session_brief.resolve_session("") == {"session_id": None, "alternates": []}
