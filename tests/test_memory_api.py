"""Tests for ccc_server/memory_api.py: the API-layer glue over ship_graph's
is_shipped/search_sessions that powers GET /api/memory/recall,
GET /api/memory/shipped, and the `ccc recall` / `ccc shipped` CLI verbs."""

import json
import subprocess

import pytest

import ccc_server.memory_api as memory_api
import ccc_server.session_fts as session_fts
import ccc_server.ship_graph as ship_graph


@pytest.fixture
def mock_memory_env(tmp_path, monkeypatch):
    """A real transcript in a real git repo, indexed by both ship_graph
    (session_meta: repo/date) and session_fts (sdoc: title/snippet) — the
    two already-synced tables memory_api.recall() joins across."""
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
    subprocess.run(
        ["git", "commit", "-m", "feat(widgets): add confetti animation"],
        cwd=repo_dir, check=True,
    )
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True,
    ).stdout.strip()

    session_dir = projects_dir / "widget-repo"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session-abc.jsonl"
    lines = [
        {
            "type": "user",
            "cwd": str(repo_dir),
            "timestamp": "2026-09-20T10:00:00Z",
            "message": {"role": "user", "content": "add a confetti animation on save"},
        },
        {
            "type": "assistant",
            "cwd": str(repo_dir),
            "timestamp": "2026-09-20T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": f"Done — [main {commit_sha[:7]}] feat(widgets): add confetti animation",
            },
        },
    ]
    session_file.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    monkeypatch.setenv("CCC_SHIP_GRAPH_DB", str(ship_db))
    monkeypatch.setenv("WATCHTOWER_DB", str(wt_db_path))
    monkeypatch.setenv("CCC_PROJECTS_ROOT", str(projects_dir))
    monkeypatch.setenv("CCC_CODEX_SESSIONS_ROOT", str(codex_dir))
    monkeypatch.setenv("CCC_SHIP_GRAPH_DAYS", "0")  # disable cutoff for tests
    monkeypatch.setenv("CCC_SHIP_GRAPH_REPOS", str(repo_dir))
    monkeypatch.setenv("CCC_SESSION_FTS_DB", str(fts_db))
    monkeypatch.setenv("CCC_SESSION_FTS_DAYS", "0")  # disable cutoff for tests
    monkeypatch.setenv("CCC_SESSION_FTS_ALLOW_SCRATCH", "1")  # tmp_path looks scratch-y
    # These tests exercise the FTS/graph enrichment path, not the optional
    # embeddings channel; keep them hermetic regardless of whether the dev
    # box happens to have a local Ollama daemon running (see
    # test_session_fts.py's fts_env fixture for the same guard). Without
    # this, _vec_cache -- a process-wide global -- can carry a previous
    # test's session vector into this test's (unrelated) query and produce
    # a false-positive RRF-fused hit with score 0.0.
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
    # ship_graph.search_sessions() re-ranks a separate dynamically-loaded copy
    # of session_fts with its own TTL clock (see _get_base_search_sessions);
    # force a fresh copy so it isn't skipped as still-warm from a prior test.
    ship_graph._base_search_sessions = None

    return {"repo_dir": repo_dir, "commit_sha": commit_sha}


def test_recall_enriches_hits_with_title_repo_date_snippet(mock_memory_env):
    res = memory_api.recall("confetti animation", limit=10)
    assert res["query"] == "confetti animation"
    results = res["results"]
    assert results, "expected at least one hit"
    row = results[0]
    assert row["session_id"] == "session-abc"
    assert row["repo"] == "widget-repo"
    assert row["date"] == "2026-09-20"
    assert row["snippet"]
    assert isinstance(row["title"], str)
    # Every result carries exactly the ticket's four enrichment fields.
    assert set(row) == {"session_id", "title", "repo", "date", "snippet"}


def test_recall_empty_query_returns_no_results(mock_memory_env):
    assert memory_api.recall("", limit=10) == {"query": "", "results": [], "indexing": False}


def test_recall_unmatched_query_returns_empty_results(mock_memory_env):
    res = memory_api.recall("quantum flux capacitor teleportation", limit=10)
    assert res["results"] == []


def test_shipped_contract_passthrough_with_topic_echo(mock_memory_env):
    res = memory_api.shipped("confetti animation")
    assert res["topic"] == "confetti animation"
    assert res["shipped"] is True
    assert "confidence" in res and "evidence" in res and "tickets" in res


def test_shipped_unknown_topic_not_shipped(mock_memory_env):
    res = memory_api.shipped("quantum flux capacitor teleportation")
    assert res["shipped"] is False
    assert res["topic"] == "quantum flux capacitor teleportation"


def test_shipped_empty_topic_matches_is_shipped_contract(mock_memory_env):
    res = memory_api.shipped("")
    assert res == {"shipped": False, "confidence": 0.0, "evidence": [], "tickets": [], "topic": ""}


def test_file_history_returns_commit_and_session_newest_first(mock_memory_env):
    res = memory_api.file_history(str(mock_memory_env["repo_dir"] / "app.py"))
    assert res["path"] == str(mock_memory_env["repo_dir"] / "app.py")
    assert res["repo"] == "widget-repo"
    kinds = {e["kind"] for e in res["history"]}
    assert "commit" in kinds
    commit_entries = [e for e in res["history"] if e["kind"] == "commit"]
    assert commit_entries[0]["why"] == "feat(widgets): add confetti animation"
    assert commit_entries[0]["hash"]
    # Every entry carries a one-line "why" and is newest-first by date.
    dates = [e["date"] for e in res["history"] if e.get("date")]
    assert dates == sorted(dates, reverse=True)


def test_file_history_repo_relative_path_resolves_against_known_repos(mock_memory_env):
    res = memory_api.file_history("app.py", repo="widget-repo")
    assert res["repo"] == "widget-repo"
    assert any(e["kind"] == "commit" for e in res["history"])


def test_file_history_untracked_path_returns_empty_history(mock_memory_env):
    res = memory_api.file_history(str(mock_memory_env["repo_dir"] / "nope.py"))
    assert res["history"] == []


def test_file_history_empty_path_returns_empty_history(mock_memory_env):
    assert memory_api.file_history("") == {"path": "", "repo": "", "history": []}


def test_decisions_filters_to_decision_shaped_snippets(mock_memory_env):
    res = memory_api.decisions("confetti animation")
    assert res["topic"] == "confetti animation"
    # The seeded transcript's snippet ("add a confetti animation on save")
    # carries no decision language, so nothing should pass the filter.
    assert res["results"] == []


def test_decisions_empty_topic_returns_empty_results(mock_memory_env):
    assert memory_api.decisions("") == {"topic": "", "results": []}


def test_decisions_matches_decision_language_in_snippet(mock_memory_env, monkeypatch):
    monkeypatch.setattr(
        memory_api, "_sdoc_rows",
        lambda sids: {sid: {"title": "Pick a queue engine",
                             "snippet": "we decided to go with sqlite instead of postgres"}
                      for sid in sids})
    monkeypatch.setattr(memory_api._sg, "search_sessions",
                        lambda q, limit=20: [{"session_id": "session-abc"}])
    res = memory_api.decisions("queue engine")
    assert res["topic"] == "queue engine"
    assert len(res["results"]) == 1
    assert res["results"][0]["session_id"] == "session-abc"
    assert "instead of" in res["results"][0]["snippet"]
