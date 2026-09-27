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

    # Reset thread-local connection and throttle timestamp
    if hasattr(session_fts._tls, "conn"):
        if session_fts._tls.conn:
            session_fts._tls.conn.close()
        session_fts._tls.conn = None
    session_fts._last_sync_ts = 0.0

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
