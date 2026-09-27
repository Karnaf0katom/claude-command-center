# Copyright (c) 2026 Amir Fish. All rights reserved.
# SPDX-License-Identifier: LicenseRef-CCC-Software-License
"""Unit tests for ccc_server/session_fts.py."""

import json
import os
import time
from pathlib import Path

import pytest

from ccc_server import session_fts


@pytest.fixture
def fts_env(tmp_path, monkeypatch):
    """Set up isolated directories for session_fts tests."""
    db_path = tmp_path / "session_fts.sqlite"
    projects_dir = tmp_path / "projects"
    codex_dir = tmp_path / "codex"
    projects_dir.mkdir(parents=True)
    codex_dir.mkdir(parents=True)

    monkeypatch.setenv("CCC_SESSION_FTS_DB", str(db_path))
    monkeypatch.setenv("CCC_PROJECTS_ROOT", str(projects_dir))
    monkeypatch.setenv("CCC_CODEX_SESSIONS_ROOT", str(codex_dir))
    monkeypatch.setenv("CCC_SESSION_FTS_DAYS", "0")  # disable cutoff for tests
    monkeypatch.setenv("CCC_SESSION_FTS_ALLOW_SCRATCH", "1")
    # These tests exercise FTS mechanics, not the optional embeddings channel;
    # keep them hermetic and fast regardless of whether the dev box happens to
    # have a local Ollama daemon running. See test_session_fts_embeddings.py
    # for the embeddings/RRF-fusion behavior, mocked so it never hits a real
    # network service.
    monkeypatch.setenv("CCC_SESSION_FTS_EMBED", "0")

    # Reset thread-local connection and throttle timestamp
    if hasattr(session_fts._tls, "conn"):
        if session_fts._tls.conn:
            session_fts._tls.conn.close()
        session_fts._tls.conn = None
    session_fts._last_sync_ts = 0.0
    session_fts._ollama_state["ts"] = 0.0
    session_fts._ollama_state["ok"] = False
    session_fts._vec_cache["sids"] = []
    session_fts._vec_cache["vecs"] = []
    session_fts._bg_sync_running = False
    session_fts._backfill_running = False

    return {
        "db": db_path,
        "projects": projects_dir,
        "codex": codex_dir,
    }


def _write_claude_jsonl(path: Path, sid: str, turns: list[dict], custom_title: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if custom_title:
        lines.append(json.dumps({"type": "custom-title", "customTitle": custom_title}))
    for t in turns:
        lines.append(json.dumps(t))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_search_sessions_empty_query(fts_env):
    assert session_fts.search_sessions("") == []
    assert session_fts.search_sessions("   ") == []


def test_search_sessions_finds_claude_prompt(fts_env):
    sid = "11111111-2222-3333-4444-555555555555"
    file_path = fts_env["projects"] / "repo-a" / f"{sid}.jsonl"
    turns = [
        {
            "type": "user",
            "cwd": "/Users/test/repo-a",
            "message": {"role": "user", "content": "investigate quantum teleportation anomalies"},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "analyzing quantum logs"}]},
        },
    ]
    _write_claude_jsonl(file_path, sid, turns)

    results = session_fts.search_sessions("teleportation anomalies")
    assert len(results) > 0
    assert results[0]["session_id"] == sid
    assert "score" in results[0]


def test_search_sessions_finds_assistant_text(fts_env):
    sid = "22222222-3333-4444-5555-666666666666"
    file_path = fts_env["projects"] / "repo-b" / f"{sid}.jsonl"
    turns = [
        {
            "type": "user",
            "cwd": "/Users/test/repo-b",
            "message": {"role": "user", "content": "run diagnostic"},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "discovered subterranean crystal formations"}]},
        },
    ]
    _write_claude_jsonl(file_path, sid, turns)

    results = session_fts.search_sessions("subterranean crystal")
    assert len(results) > 0
    assert results[0]["session_id"] == sid


def test_search_sessions_finds_by_title_and_tickets(fts_env):
    sid = "33333333-4444-5555-6666-777777777777"
    file_path = fts_env["projects"] / "repo-c" / f"{sid}.jsonl"
    turns = [
        {
            "type": "user",
            "cwd": "/Users/test/repo-c",
            "message": {"role": "user", "content": "working on PROJ-9981 database migration"},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "migration completed"}]},
        },
    ]
    _write_claude_jsonl(file_path, sid, turns, custom_title="Fix Database Deadlock")

    res_title = session_fts.search_sessions("Database Deadlock")
    assert len(res_title) > 0
    assert res_title[0]["session_id"] == sid

    res_ticket = session_fts.search_sessions("PROJ-9981")
    assert len(res_ticket) > 0
    assert res_ticket[0]["session_id"] == sid


def test_search_sessions_finds_codex_session(fts_env):
    sid = "44444444-5555-6666-7777-888888888888"
    file_path = fts_env["codex"] / "2026" / "09" / f"rollout-{sid}.jsonl"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": "/Users/test/codex-proj"}}),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": "optimize matrix multiplication kernel",
            },
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "implemented AVX512 vectorization",
            },
        }),
    ]
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    results = session_fts.search_sessions("AVX512 vectorization")
    assert len(results) > 0
    assert results[0]["session_id"] == sid


def test_search_sessions_skips_scratch_and_empty(fts_env, monkeypatch):
    monkeypatch.setenv("CCC_SESSION_FTS_ALLOW_SCRATCH", "0")
    # Empty (0 user messages)
    sid_empty = "55555555-6666-7777-8888-999999999999"
    f_empty = fts_env["projects"] / "repo" / f"{sid_empty}.jsonl"
    _write_claude_jsonl(f_empty, sid_empty, [
        {"type": "assistant", "message": {"role": "assistant", "content": "solo assistant message"}},
    ])

    # Scratch session (cwd matches scratch pattern)
    sid_scratch = "66666666-7777-8888-9999-000000000000"
    f_scratch = fts_env["projects"] / "repo" / f"{sid_scratch}.jsonl"
    _write_claude_jsonl(f_scratch, sid_scratch, [
        {"type": "user", "cwd": "/tmp/command-center-scratch/demo", "message": {"role": "user", "content": "scratch work unique_topic_term"}},
    ])

    results = session_fts.search_sessions("unique_topic_term")
    assert not any(r["session_id"] == sid_scratch for r in results)

    results2 = session_fts.search_sessions("solo assistant")
    assert not any(r["session_id"] == sid_empty for r in results2)


def test_search_sessions_incremental_update_and_deletion(fts_env):
    sid = "77777777-8888-9999-0000-111111111111"
    f = fts_env["projects"] / "repo" / f"{sid}.jsonl"
    _write_claude_jsonl(f, sid, [
        {"type": "user", "cwd": "/repo", "message": {"role": "user", "content": "first version zebracrossing"}},
    ])

    res1 = session_fts.search_sessions("zebracrossing", force_refresh=True)
    assert len(res1) == 1 and res1[0]["session_id"] == sid

    # Modify file
    time.sleep(0.05)
    _write_claude_jsonl(f, sid, [
        {"type": "user", "cwd": "/repo", "message": {"role": "user", "content": "second version giraffeneck"}},
    ])
    os.utime(f, None)  # update mtime

    res2_old = session_fts.search_sessions("zebracrossing", force_refresh=True)
    assert len(res2_old) == 0

    res2_new = session_fts.search_sessions("giraffeneck", force_refresh=True)
    assert len(res2_new) == 1 and res2_new[0]["session_id"] == sid

    # Delete file
    f.unlink()
    res3 = session_fts.search_sessions("giraffeneck", force_refresh=True)
    assert len(res3) == 0


def test_search_sessions_cold_start_offloads_to_background(fts_env, monkeypatch):
    """MEMO-FIX-12: a catch-up too big to parse inline (cold start, or a big
    batch of new/changed transcripts) must not block search_sessions() for
    the whole parse -- it hands off to a background thread and answers
    immediately with is_indexing() True and whatever is already indexed."""
    # _BG_SYNC_THRESHOLD is read from its env var once at import time, so a
    # monkeypatched env var wouldn't take effect here -- set the module
    # attribute directly instead.
    monkeypatch.setattr(session_fts, "_BG_SYNC_THRESHOLD", 3)

    for i in range(6):
        sid = f"aaaaaaaa-bbbb-cccc-dddd-{i:012d}"
        f = fts_env["projects"] / "repo" / f"{sid}.jsonl"
        _write_claude_jsonl(f, sid, [
            {"type": "user", "cwd": "/repo", "message": {"role": "user", "content": "warp core diagnostics"}},
        ])

    assert session_fts.is_indexing() is False
    t0 = time.time()
    results = session_fts.search_sessions("warp core diagnostics")
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"cold-start search_sessions() blocked for {elapsed:.2f}s instead of offloading"
    assert results == []  # nothing indexed yet -- background sync just started
    assert session_fts.is_indexing() is True

    deadline = time.time() + 10.0
    while session_fts.is_indexing() and time.time() < deadline:
        time.sleep(0.05)
    assert session_fts.is_indexing() is False

    results2 = session_fts.search_sessions("warp core diagnostics")
    assert len(results2) == 6


def test_drain_embeddings_queues_jobs_when_ollama_down(fts_env, monkeypatch):
    """Root-cause regression: sessions parsed while Ollama is unreachable must
    be queued to semb_pending, not silently dropped -- previously
    _drain_embeddings returned early on an unavailable Ollama without ever
    recording the job, so those sessions were never embedded again."""
    conn = session_fts._get_connection()
    session_fts._init_db(conn)
    monkeypatch.setattr(session_fts, "_ollama_available", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("must not call Ollama when unavailable")

    monkeypatch.setattr(session_fts, "_embed_texts", boom)

    session_fts._drain_embeddings(conn, [("sid-outage", [("card", "some text")])])

    pending = [r[0] for r in conn.execute("SELECT sid FROM semb_pending")]
    assert pending == ["sid-outage"]


def _insert_sdoc_row(conn, sid: str) -> None:
    conn.execute(
        "INSERT INTO sdoc VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, "title", "prompt text", "report text", "body text", "meta", ""),
    )


def test_backfill_missing_embeddings_queues_unembedded_sdoc_sids(fts_env):
    """sdoc rows with no matching semb row (the historical-outage backlog, or
    any other sdoc/semb gap) get queued into semb_pending; rows that already
    have a semb row or are already pending are left alone."""
    conn = session_fts._get_connection()
    session_fts._init_db(conn)

    _insert_sdoc_row(conn, "sid-missing-1")
    _insert_sdoc_row(conn, "sid-missing-2")
    _insert_sdoc_row(conn, "sid-has-semb")
    _insert_sdoc_row(conn, "sid-already-pending")
    conn.execute("INSERT INTO semb (sid, kind, vec) VALUES (?, ?, ?)", ("sid-has-semb", "card", b""))
    conn.execute("INSERT INTO semb_pending (sid) VALUES (?)", ("sid-already-pending",))
    conn.commit()

    queued = session_fts._backfill_missing_embeddings(conn)

    assert queued == 2
    pending = {r[0] for r in conn.execute("SELECT sid FROM semb_pending")}
    assert pending == {"sid-missing-1", "sid-missing-2", "sid-already-pending"}


def test_backfill_missing_embeddings_is_bounded_per_call(fts_env, monkeypatch):
    """Never an O(all sessions) scan queued in one shot -- each call queues at
    most _MAX_EMBED_SESSIONS_PER_SYNC sids, matching the existing drain cap."""
    conn = session_fts._get_connection()
    session_fts._init_db(conn)
    monkeypatch.setattr(session_fts, "_MAX_EMBED_SESSIONS_PER_SYNC", 3)

    for i in range(10):
        _insert_sdoc_row(conn, f"sid-{i:02d}")
    conn.commit()

    queued = session_fts._backfill_missing_embeddings(conn)

    assert queued == 3
    assert conn.execute("SELECT COUNT(*) FROM semb_pending").fetchone()[0] == 3


def test_run_embedding_backfill_drains_full_backlog_when_ollama_returns(fts_env, monkeypatch):
    """End-to-end: a backlog of un-embedded sdoc rows (simulating everything
    indexed during the OPS-1251 Ollama outage) gets fully drained by the
    background backfill loop once Ollama is available, in bounded slices
    rather than one big batch."""
    monkeypatch.setattr(session_fts, "_MAX_EMBED_SESSIONS_PER_SYNC", 2)
    monkeypatch.setattr(session_fts, "_BACKFILL_RETRY_INTERVAL", 0.05)
    monkeypatch.setattr(session_fts, "_ollama_available", lambda: True)
    monkeypatch.setattr(
        session_fts, "_embed_texts",
        lambda texts, batch=32, timeout=None: [[1.0, 0.0] for _ in texts],
    )

    conn = session_fts._get_connection()
    session_fts._init_db(conn)
    for i in range(7):
        _insert_sdoc_row(conn, f"sid-{i:02d}")
    conn.commit()

    session_fts._run_embedding_backfill()

    deadline = time.time() + 5.0
    while session_fts._backfill_running and time.time() < deadline:
        time.sleep(0.05)

    assert not session_fts._backfill_running, "backfill loop did not finish in time"
    assert conn.execute("SELECT COUNT(*) FROM semb_pending").fetchone()[0] == 0
    embedded = conn.execute("SELECT COUNT(DISTINCT sid) FROM semb").fetchone()[0]
    assert embedded == 7


def test_run_embedding_backfill_backs_off_while_ollama_down(fts_env, monkeypatch):
    """While Ollama stays unreachable the loop must not spin hot or drop the
    backlog -- it keeps the sids queued and retries on its backoff interval."""
    monkeypatch.setattr(session_fts, "_BACKFILL_RETRY_INTERVAL", 0.05)
    monkeypatch.setattr(session_fts, "_ollama_available", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("must not call Ollama while unavailable")

    monkeypatch.setattr(session_fts, "_embed_texts", boom)

    conn = session_fts._get_connection()
    session_fts._init_db(conn)
    _insert_sdoc_row(conn, "sid-stuck")
    conn.commit()

    session_fts._run_embedding_backfill()
    time.sleep(0.3)

    assert session_fts._backfill_running, "loop should still be retrying, not exited"
    pending = [r[0] for r in conn.execute("SELECT sid FROM semb_pending")]
    assert pending == ["sid-stuck"]

    # Ollama recovers -- the same running loop should pick it up without a
    # fresh trigger.
    monkeypatch.setattr(session_fts, "_ollama_available", lambda: True)
    monkeypatch.setattr(
        session_fts, "_embed_texts",
        lambda texts, batch=32, timeout=None: [[1.0, 0.0] for _ in texts],
    )

    deadline = time.time() + 5.0
    while session_fts._backfill_running and time.time() < deadline:
        time.sleep(0.05)

    assert not session_fts._backfill_running
    assert conn.execute("SELECT COUNT(*) FROM semb_pending").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM semb WHERE sid = ?", ("sid-stuck",)).fetchone()[0] > 0
