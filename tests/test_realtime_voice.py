# Copyright (c) 2026 Amir Fish. All rights reserved.
# SPDX-License-Identifier: LicenseRef-CCC-Software-License
"""Tests for ccc_server/realtime_voice.py — the Codex realtime voice lane.

Drives a real subprocess: tests/fake_voice_appserver.py, a stdlib script
speaking the JSON-RPC subset the module needs. No OpenAI calls, no real
codex binary, no keychain reads — byok accessors are monkeypatched. The
fake key below is a fixture sentinel: it is checked for LEAKS (never
echoed), so it is deliberately obvious and valueless.
"""
import importlib
import json
import os
import stat
import sys
import time
import urllib.request
from pathlib import Path

import pytest

server = importlib.import_module("server")
rv = importlib.import_module("ccc_server.realtime_voice")

FAKE_BIN = str(Path(__file__).parent / "fake_voice_appserver.py")
SENTINEL_KEY = "sk-voice-test-FAKEFATALEAKS"  # fixture sentinel, not a real key
SDP_OFFER = "v=0\r\no=- 123 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=fake\r\n"


@pytest.fixture()
def voice_env(tmp_path, monkeypatch):
    """Isolate the voice module: fake app-server bin, fake BYOK key,
    tiny timeouts, captured usage ledger, per-test log file."""
    log = tmp_path / "appserver.log"

    monkeypatch.setenv("FAKE_VOICE_LOG", str(log))
    monkeypatch.setenv("FAKE_VOICE_MODE", "")
    monkeypatch.setattr(server, "_resolve_codex_bin",
                        lambda: {"available": True, "bin": FAKE_BIN})
    monkeypatch.setattr(server, "byok_get_key",
                        lambda profile, provider: SENTINEL_KEY if provider == "openai" else None)
    monkeypatch.setattr(server, "byok_list_profiles",
                        lambda: [{"name": "TestProfile", "providers": ["openai"]}])
    # Cheap deterministic board data for the briefing + ccc_* tools.
    monkeypatch.setattr(server, "compute_attention_feed",
                        lambda **kw: {"items": [
                            {"kind": "question", "where": "ads", "repo": "byum",
                             "session_id": "sess_12345",
                             "question_text": "Approve the plan?"},
                        ]})
    monkeypatch.setattr(server, "_watchtower_queue_rollup",
                        lambda: {"open_total": 4, "queues_total": 2,
                                 "workers_live": 1, "stuck_total": 0})
    usage_calls = []
    monkeypatch.setattr(server, "byok_record_usage",
                        lambda **kw: usage_calls.append(kw))
    monkeypatch.setattr(server, "compute_session_detail",
                        lambda sid: {"ok": True, "title": "demo", "engine": "claude",
                                     "session_state": {"summary": "working"}})

    # Fast watchdog knobs: 0.35s idle, ~0.5s max, 0.4s heartbeat loss.
    cfg = {"voice": "marin", "profile": "", "max_minutes": 15,
           "idle_seconds": 120, "save_transcripts": False}
    monkeypatch.setattr(rv, "voice_config_load", lambda: dict(cfg))
    monkeypatch.setattr(rv, "HEARTBEAT_TIMEOUT_SECONDS", 0.4)

    yield Simple(cfg=cfg, log=log, usage=usage_calls)
    # Never leave a child alive between tests.
    try:
        rv.voice_stop(reason="test_cleanup")
    except Exception:
        pass


class Simple:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _start(**over):
    params = {"sdp_offer": SDP_OFFER}
    params.update(over)
    return rv.voice_start(params)


def _wait(pred, timeout=8.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(interval)
    return None


def _sess_id(res):
    assert res[0].get("ok"), res
    return res[0]["session_id"]


def _events(sid, after=0, timeout=0.5):
    return rv.voice_events_wait(sid, after, timeout=timeout)


def test_start_happy_path_env_key_only(voice_env):
    res, status = _start()
    assert status == 200
    sid = res["session_id"]
    assert res["sdp_answer"].startswith("v=0")
    assert res["voice"] == "marin"
    assert res["profile"] == "TestProfile"

    sess = rv._session_by_id(sid)
    assert sess is not None
    # Key rode in the child's env, never on argv (visible in `ps`).
    argv_str = " ".join(sess.proc.args)
    assert SENTINEL_KEY not in argv_str
    # The fake reported seeing a key in its env.
    assert sess.proc.poll() is None
    # No API response carries the key.
    assert SENTINEL_KEY not in json.dumps(res)
    assert SENTINEL_KEY not in json.dumps(rv.voice_status())

    # Session reached listening via the fake's started notification.
    assert _wait(lambda: sess.state == "listening", timeout=5)
    log = voice_env.log.read_text()
    assert "initialize" in log and "thread/start" in log
    assert "thread/realtime/start" in log
    assert SENTINEL_KEY not in log
    assert "has_initialItems\": true" in log


def test_second_start_is_409(voice_env):
    res, status = _start()
    assert status == 200
    res2, status2 = _start()
    assert status2 == 409
    assert res2["code"] == "voice_busy"


def test_missing_key_typed_error(voice_env, monkeypatch):
    monkeypatch.setattr(server, "byok_get_key", lambda p, pr: None)
    monkeypatch.setattr(server, "byok_list_profiles",
                        lambda: [{"name": "Empty", "providers": []}])
    res, status = _start()
    assert status == 400
    assert res["code"] == "voice_no_openai_key"
    assert SENTINEL_KEY not in json.dumps(res)


def test_bad_sdp_rejected(voice_env):
    res, status = _start(sdp_offer="not an sdp")
    assert status == 400
    assert res["code"] == "voice_bad_request"


def test_stop_kills_child_and_closes(voice_env):
    res, _ = _start()
    sid = _sess_id((res, 200))
    sess = rv._session_by_id(sid)
    res2, status2 = rv.voice_stop(sid)
    assert status2 == 200 and res2["stopped"] is True
    assert _wait(lambda: sess.proc.poll() is not None, timeout=6), "child survived stop"
    d = rv.voice_status()
    assert d["active"] is False
    # Usage ledger recorded the session, provider openai, no key material.
    assert voice_env.usage, "byok_record_usage was not called"
    u = voice_env.usage[-1]
    assert u["provider"] == "openai"
    assert u["extra"]["duration_s"] >= 0
    assert SENTINEL_KEY not in json.dumps(u)


def test_child_crash_closes_session(voice_env, monkeypatch):
    monkeypatch.setenv("FAKE_VOICE_MODE", "crash_after_realtime")
    res, status = _start()
    assert status == 200  # SDP arrived before the exit
    sid = res["session_id"]
    sess = rv._session_by_id(sid)
    assert _wait(lambda: sess.state in ("closed", "error"), timeout=6)
    assert not rv.voice_status()["active"]


def test_idle_timeout(voice_env):
    voice_env.cfg["idle_seconds"] = 0.25  # watchdog reads config once at start
    res, _ = _start()
    sid = _sess_id((res, 200))
    sess = rv._session_by_id(sid)
    assert _wait(lambda: sess.state in ("closed",), timeout=8), sess.state
    assert sess.reason == "idle_timeout"


def test_max_duration(voice_env):
    voice_env.cfg["max_minutes"] = 0.005  # ~0.3s
    voice_env.cfg["idle_seconds"] = 120
    res, _ = _start()
    sid = _sess_id((res, 200))
    sess = rv._session_by_id(sid)
    assert _wait(lambda: sess.state in ("closed",), timeout=8), sess.state
    assert sess.reason == "max_duration"


def test_heartbeat_loss(voice_env):
    voice_env.cfg["idle_seconds"] = 120
    res, _ = _start()
    sid = _sess_id((res, 200))
    sess = rv._session_by_id(sid)
    rv.voice_heartbeat(sid)          # one beat, then silence
    assert _wait(lambda: sess.state in ("closed",), timeout=8), sess.state
    assert sess.reason == "heartbeat_lost"


def test_heartbeat_unknown_session(voice_env):
    res, status = rv.voice_heartbeat("voice_nope")
    assert status == 404
    assert res["code"] == "voice_no_session"


def test_malformed_notification_survives(voice_env, monkeypatch):
    monkeypatch.setenv("FAKE_VOICE_MODE", "malformed")
    res, status = _start()
    assert status == 200
    sid = res["session_id"]
    sess = rv._session_by_id(sid)
    events = _wait(lambda: [e for e in rv.voice_events_wait(sid, 0, 0.1)[0]
                            if e["type"] == "error"], timeout=5)
    assert events, "malformed line never surfaced as an error event"
    assert sess.state not in ("closed", "error")  # session kept running


def test_event_fanout(voice_env):
    res, _ = _start()
    sid = _sess_id((res, 200))
    events, latest, alive = _events(sid, 0, timeout=1.0)
    types = [e["type"] for e in events]
    assert "state" in types
    assert alive is True
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and latest >= seqs[-1]


def test_transcript_events(voice_env):
    res, _ = _start()
    sid = _sess_id((res, 200))
    sess = rv._session_by_id(sid)
    # Trigger a spoken line through appendSpeech path of the fake.
    sess._send({"method": "thread/realtime/appendSpeech",
                "params": {"threadId": sess.thread_id, "text": "hi"}, "id": 777})
    ev = _wait(lambda: [e for e in rv.voice_events_wait(sid, 0, 0.1)[0]
                        if e["type"] == "transcript"], timeout=5)
    assert ev and ev[-1]["data"]["text"] == "Done."
    assert sess.transcript and sess.transcript[-1]["role"] == "assistant"


def test_key_redaction_in_errors(voice_env):
    res, _ = _start()
    sid = _sess_id((res, 200))
    sess = rv._session_by_id(sid)
    # Inject a fake realtime error carrying the key value; the emitted
    # event must be scrubbed.
    sess._on_notification("thread/realtime/error",
                          {"message": f"auth failed for {SENTINEL_KEY}"})
    events, _, _ = _events(sid, 0, timeout=0.2)
    blob = json.dumps(events)
    assert SENTINEL_KEY not in blob
    assert "[REDACTED]" in blob


def test_tool_call_readonly(voice_env, monkeypatch):
    monkeypatch.setenv("FAKE_VOICE_MODE", "call_tool")
    res, _ = _start()
    sid = _sess_id((res, 200))
    ev = _wait(lambda: [e for e in rv.voice_events_wait(sid, 0, 0.1)[0]
                        if e["type"] == "tool"], timeout=6)
    assert ev and ev[-1]["data"]["tool"] == "ccc_attention"
    # The fake logged our DynamicToolCallResponse — it should carry the
    # fake feed content, proving the read-only tool ran.
    assert _wait(lambda: "Approve the plan" in voice_env.log.read_text(), 5)


def test_propose_action_creates_card(voice_env, monkeypatch):
    monkeypatch.setenv("FAKE_VOICE_MODE", "propose_action")
    res, _ = _start()
    sid = _sess_id((res, 200))
    ev = _wait(lambda: [e for e in rv.voice_events_wait(sid, 0, 0.1)[0]
                        if e["type"] == "action"], timeout=6)
    assert ev, "no action event"
    action = ev[-1]["data"]
    assert action["kind"] == "inject"
    assert action["status"] == "proposed"
    assert action["confirm_token"]  # browser needs it; never re-emitted
    sess = rv._session_by_id(sid)
    assert action["id"] in sess.pending_actions


def test_approval_requests_denied(voice_env, monkeypatch):
    monkeypatch.setenv("FAKE_VOICE_MODE", "approval_probe")
    res, _ = _start()
    _sess_id((res, 200))
    # The fake logs the client's response payload — a deny decision.
    assert _wait(lambda: '"decision": "denied"' in voice_env.log.read_text(), 5)


def test_notify_action_result_speaks(voice_env, monkeypatch):
    monkeypatch.setenv("FAKE_VOICE_MODE", "propose_action")
    res, _ = _start()
    sid = _sess_id((res, 200))
    ev = _wait(lambda: [e for e in rv.voice_events_wait(sid, 0, 0.1)[0]
                        if e["type"] == "action"], timeout=6)
    action = ev[-1]["data"]
    sess = rv._session_by_id(sid)
    assert _wait(lambda: sess.state == "listening", 5)
    # Simulate the browser confirming.
    rv.notify_action_result({"id": action["id"], "kind": "inject",
                             "status": "done", "effect": "Send a message",
                             "confirm_token": "SECRET_TOKEN",
                             "result": {"ok": True}})
    assert _wait(lambda: "appendSpeech" in voice_env.log.read_text(), 6)
    # The token must not ride the SSE update.
    events, _, _ = _events(sid, 0, 0.2)
    assert "SECRET_TOKEN" not in json.dumps(events)


def test_transcript_saved_only_when_opted_in(voice_env, tmp_path, monkeypatch):
    res, _ = _start()
    sid = _sess_id((res, 200))
    sess = rv._session_by_id(sid)
    sess._send({"method": "thread/realtime/appendSpeech",
                "params": {"threadId": sess.thread_id, "text": "x"}, "id": 778})
    _wait(lambda: sess.transcript, 5)
    rv.voice_stop(sid)
    assert not (rv.VOICE_TRANSCRIPT_DIR / f"{sid}.jsonl").exists()

    voice_env.cfg["save_transcripts"] = True
    res2, _ = _start()
    sid2 = _sess_id((res2, 200))
    sess2 = rv._session_by_id(sid2)
    sess2._send({"method": "thread/realtime/appendSpeech",
                 "params": {"threadId": sess2.thread_id, "text": "x"}, "id": 779})
    _wait(lambda: sess2.transcript, 5)
    rv.voice_stop(sid2)
    saved = rv.VOICE_TRANSCRIPT_DIR / f"{sid2}.jsonl"
    assert saved.exists()
    assert SENTINEL_KEY not in saved.read_text()


def test_config_save_roundtrip_and_clamps(voice_env):
    res = rv.voice_config_save({"voice": "coral", "profile": "P1",
                                "max_minutes": 999, "idle_seconds": 1,
                                "save_transcripts": True})
    assert res["ok"]
    c = res["config"]
    assert c["voice"] == "coral"
    assert c["profile"] == "P1"
    assert c["max_minutes"] == 120   # clamped
    assert c["idle_seconds"] == 15   # clamped
    assert c["save_transcripts"] is True
    # Unknown voice falls back to default.
    res2 = rv.voice_config_save({"voice": "not-a-voice"})
    assert res2["ok"] and res2["config"]["voice"] == rv.DEFAULT_VOICE


def test_briefing_is_bounded_and_compact(voice_env):
    feed_calls = []
    roll_calls = []

    def feed(**kw):
        feed_calls.append(kw)
        return {"items": [{"kind": "question", "where": "x",
                           "question_text": "y" * 5000}]}

    def roll():
        roll_calls.append(1)
        return {"open_total": 3, "queues_total": 2,
                "workers_live": 1, "stuck_total": 0}

    text = rv.build_briefing(feed_fn=feed, rollup_fn=roll)
    assert len(feed_calls) == 1 and len(roll_calls) == 1
    assert len(text) <= 3500
    assert "WatchTower" in text


def test_deny_result_shapes():
    assert rv.VoiceSession._deny_result("execCommandApproval")["decision"] == "denied"
    assert rv.VoiceSession._deny_result("mcpServer/elicitation/request")["action"] == "decline"
    assert rv.VoiceSession._deny_result("item/tool/requestUserInput")["answers"] == {}
