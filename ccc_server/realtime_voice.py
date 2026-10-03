# Copyright (c) 2026 Amir Fish. All rights reserved.
# SPDX-License-Identifier: LicenseRef-CCC-Software-License
"""Realtime voice mode for CCC, powered by the Codex app-server
`thread/realtime/*` API (experimental).

One voice session at a time. The browser negotiates WebRTC directly with
the app-server's voice host, so audio never transits CCC: the server
relays only the SDP offer/answer plus JSON-RPC transcript/state events,
fans them out over SSE, and answers `item/tool/call` requests from the
backing Codex thread against a small read-only tool surface. Mutations
are never executed by the model — they become proposals in
ccc_server/assistant_actions.py that the user confirms by click.

The OpenAI API key (BYOK profile, provider "openai") is injected into the
child environment only: never on argv, never logged, never persisted,
never returned from any API.

Stdlib-only, same rule as server.py. Names still living in server.py are
reached via ``_core`` at call time.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from ccc_server import core as _core
from ccc_server.paths import COMMAND_CENTER_STATE_DIR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VOICE_STATE_DIR = COMMAND_CENTER_STATE_DIR / "voice"
VOICE_CONFIG_FILE = VOICE_STATE_DIR / "voice.json"
VOICE_TRANSCRIPT_DIR = VOICE_STATE_DIR / "transcripts"
VOICE_SCRATCH_DIR = VOICE_STATE_DIR / "scratch"

DEFAULT_VOICE = "marin"
DEFAULT_MAX_MINUTES = 15
DEFAULT_IDLE_SECONDS = 120
# Browsers throttle hidden-tab timers to ~1/min, so 45s would kill a voice
# session the moment the user switches tabs mid-conversation. 120s survives
# one throttle cycle; a truly closed tab still dies fast via sendBeacon.
HEARTBEAT_TIMEOUT_SECONDS = 120.0
START_TIMEOUT_SECONDS = 60.0
CALL_TIMEOUT_SECONDS = 30.0
EVENT_BUFFER_MAX = 600
TRANSCRIPT_MAX_ITEMS = 400

# Schema-derived catalog (codex app-server generate-json-schema --experimental,
# codex-cli 0.160.0). Refreshed from live thread/realtime/listVoices on each
# session start; this is the offline fallback for /api/voice/voices.
VOICES_V1 = ["juniper", "maple", "spruce", "ember", "vale", "breeze", "arbor", "sol", "cove"]
VOICES_V2 = ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"]
DEFAULT_V1 = "cove"
DEFAULT_V2 = "marin"
_ALL_VOICES = set(VOICES_V1) | set(VOICES_V2)

# Dynamic tools the backing Codex thread may call. Read-only by contract;
# every mutation goes through ccc_propose_action -> assistant_actions.
TOOL_NAMES = ("ccc_attention", "ccc_session", "ccc_queues", "ccc_propose_action")

_VOICE_PROMPT = (
    "You are the voice of Claude Command Center (CCC), a dashboard for the "
    "user's coding-agent sessions. Answers are spoken aloud: keep them to "
    "one to three short sentences unless the user asks for detail. For "
    "anything about live sessions, queues, or tickets, delegate to your "
    "Codex backend, which has ccc_* tools with the real board state — never "
    "guess or invent session state. For anything that changes state (send "
    "input to a session, spawn a session, file or comment a ticket), the "
    "backend must call ccc_propose_action — tell the user the action is "
    "waiting for their click in the CCC dashboard, and never claim it "
    "happened before that confirmation arrives."
)

_CODEX_INSTRUCTIONS = (
    "You back a CCC voice session. Use only the ccc_* tools for board "
    "state; do not run shell commands or read files. Keep tool results "
    "compact. Anything that mutates CCC must go through ccc_propose_action; "
    "you never execute changes yourself."
)

_APPROVAL_DENY_METHODS = frozenset({
    "execCommandApproval",
    "applyPatchApproval",
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
    "item/tool/requestUserInput",
})

# ---------------------------------------------------------------------------
# Config persistence (voice.json — voice name, BYOK profile, limits)
# ---------------------------------------------------------------------------

_CONFIG_LOCK = threading.Lock()


def _default_config():
    return {
        "voice": DEFAULT_VOICE,
        "profile": "",
        "max_minutes": DEFAULT_MAX_MINUTES,
        "idle_seconds": DEFAULT_IDLE_SECONDS,
        "save_transcripts": False,
    }


def voice_config_load():
    try:
        data = json.loads(VOICE_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    cfg = _default_config()
    if isinstance(data, dict):
        for key in cfg:
            if key in data:
                cfg[key] = data[key]
    return _config_sanitize(cfg)


def _config_sanitize(cfg):
    out = _default_config()
    voice = str(cfg.get("voice") or "").strip().lower()
    out["voice"] = voice if voice in _ALL_VOICES else DEFAULT_VOICE
    out["profile"] = str(cfg.get("profile") or "").strip()[:120]
    try:
        out["max_minutes"] = max(1, min(120, int(cfg.get("max_minutes"))))
    except (TypeError, ValueError):
        out["max_minutes"] = DEFAULT_MAX_MINUTES
    try:
        out["idle_seconds"] = max(15, min(1800, int(cfg.get("idle_seconds"))))
    except (TypeError, ValueError):
        out["idle_seconds"] = DEFAULT_IDLE_SECONDS
    out["save_transcripts"] = bool(cfg.get("save_transcripts"))
    return out


def voice_config_save(patch):
    if not isinstance(patch, dict):
        return {"ok": False, "error": "config patch must be an object"}
    with _CONFIG_LOCK:
        cfg = _config_sanitize({**voice_config_load(), **patch})
        try:
            VOICE_STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = VOICE_CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(VOICE_CONFIG_FILE)
        except OSError as e:
            return {"ok": False, "error": f"could not save voice config: {e}"}
    return {"ok": True, "config": cfg}


# ---------------------------------------------------------------------------
# BYOK OpenAI key resolution + profile helpers
# ---------------------------------------------------------------------------

def _openai_profiles():
    """BYOK profiles that hold an `openai` key (names only, never the key)."""
    try:
        profiles = _core.byok_list_profiles()
    except Exception:
        return []
    return [p["name"] for p in profiles if "openai" in (p.get("providers") or [])]


def _resolve_openai_key(profile):
    """(key, profile_used, error_dict). The key only ever lives in the child env."""
    candidates = []
    if profile:
        candidates = [profile]
    else:
        candidates = _openai_profiles()
    for name in candidates:
        try:
            key = _core.byok_get_key(name, "openai")
        except Exception:
            key = None
        if key:
            return key, name, None
    if profile:
        return None, None, {
            "ok": False, "code": "voice_no_openai_key",
            "error": f"BYOK profile {profile!r} has no OpenAI key. "
                     "Pick another profile or add an OpenAI key in Settings > BYOK.",
        }
    return None, None, {
        "ok": False, "code": "voice_no_openai_key",
        "error": "Voice needs an OpenAI API key. Add one under Settings > BYOK "
                 "(provider: openai), then try again.",
    }


def _redact(text, key):
    if not key:
        return text
    try:
        return str(text).replace(key, "[REDACTED]")
    except Exception:
        return "[redacted]"


# ---------------------------------------------------------------------------
# Voice catalog (offline fallback + live refresh)
# ---------------------------------------------------------------------------

_VOICES_LOCK = threading.Lock()
_VOICES_LIVE = None


def voice_catalog():
    with _VOICES_LOCK:
        live = _VOICES_LIVE
    if live:
        return {"ok": True, "source": "live", "voices": live}
    return {
        "ok": True,
        "source": "fallback",
        "voices": {"v1": list(VOICES_V1), "v2": list(VOICES_V2),
                   "defaultV1": DEFAULT_V1, "defaultV2": DEFAULT_V2},
    }


def _record_live_voices(result):
    global _VOICES_LIVE
    voices = (result or {}).get("voices") or {}
    v1 = [v for v in voices.get("v1") or [] if isinstance(v, str)]
    v2 = [v for v in voices.get("v2") or [] if isinstance(v, str)]
    if not (v1 or v2):
        return
    with _VOICES_LOCK:
        _VOICES_LIVE = {
            "v1": v1 or list(VOICES_V1),
            "v2": v2 or list(VOICES_V2),
            "defaultV1": voices.get("defaultV1") or DEFAULT_V1,
            "defaultV2": voices.get("defaultV2") or DEFAULT_V2,
        }


# ---------------------------------------------------------------------------
# Board briefing (bounded: cached attention feed + queue rollup only)
# ---------------------------------------------------------------------------

def build_briefing(limit=8, feed_fn=None, rollup_fn=None):
    """Compact, size-capped board snapshot for the voice session's seed.

    Bounded work by contract: one cached attention-feed call + one cached
    queue rollup, no per-row scans, no subprocesses. `feed_fn`/`rollup_fn`
    are injectable so the perf test can count calls.
    """
    if feed_fn is None:
        feed_fn = _core.compute_attention_feed
    if rollup_fn is None:
        rollup_fn = _core._watchtower_queue_rollup
    lines = []
    now = datetime.now().strftime("%H:%M")
    try:
        feed = feed_fn(recent_only=True, limit=limit) or {}
    except Exception:
        feed = {}
    items = feed.get("items") or []
    if items:
        lines.append(f"CCC board at {now}: {len(items)} item(s) need attention.")
        for it in items[:limit]:
            label = it.get("where") or it.get("kind") or "item"
            repo = it.get("repo") or it.get("folder_label") or ""
            q = (it.get("question_text") or it.get("title") or "").strip().replace("\n", " ")
            line = f"- {label}"
            if repo:
                line += f" in {repo}"
            if it.get("session_id"):
                line += f" [session {str(it['session_id'])[:8]}]"
            if q:
                line += f": {q[:160]}"
            lines.append(line)
    else:
        lines.append(f"CCC board at {now}: nothing needs attention right now.")
    try:
        roll = rollup_fn() or {}
    except Exception:
        roll = {}
    if roll:
        lines.append(
            "WatchTower: "
            f"{roll.get('open_total', 0)} open ticket(s) across "
            f"{roll.get('queues_total', 0)} queue(s), "
            f"{roll.get('workers_live', 0)} worker(s) live, "
            f"{roll.get('stuck_total', 0)} stuck queue(s)."
        )
    text = "\n".join(lines)
    return text[:3500]


def _fmt_tool_text(feed_or_data):
    return {"contentItems": [{"type": "inputText", "text": feed_or_data}], "success": True}


def _tool_err(msg):
    return {"contentItems": [{"type": "inputText", "text": f"error: {msg}"}], "success": False}


# ---------------------------------------------------------------------------
# The voice session
# ---------------------------------------------------------------------------

class VoiceSession:
    """One realtime voice session = one `codex app-server` child."""

    def __init__(self, session_id, voice, profile):
        self.id = session_id
        self.voice = voice
        self.profile = profile
        self.state = "connecting"  # connecting|live|stopping|closed|error
        self.state_detail = ""
        self.error = None
        self.reason = None
        self.proc = None
        self.thread_id = None
        self.realtime_version = None
        self.started_at = time.time()
        self.closed_at = None
        self.last_activity = time.monotonic()
        self.last_heartbeat = None  # None until the browser checks in once
        self.audio_ms = 0
        self.codex_tokens = 0
        self.transcript = []          # [{role, text, ts}] memory only
        self._tr_open = {}            # role -> partial text in flight
        self.pending_actions = {}     # action_id -> public action dict
        self.seq = 0
        self.events = deque(maxlen=EVENT_BUFFER_MAX)
        self.cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._pending = {}            # rpc id -> (event, result box)
        self._next_id = 0
        self._sdp_event = threading.Event()
        self._sdp_answer = None
        self._stderr_tail = deque(maxlen=40)
        self._closed_once = False
        self._key = None              # held only to redact accidents; never read
        self._watchdog_stop = threading.Event()

    # -- events ---------------------------------------------------------

    def emit(self, etype, data):
        with self.cond:
            self.seq += 1
            self.events.append({"seq": self.seq, "type": etype, "data": data})
            self.cond.notify_all()

    def set_state(self, state, detail=""):
        self.state = state
        self.state_detail = detail
        self.emit("state", {"state": state, "detail": detail})

    def touch(self):
        self.last_activity = time.monotonic()

    def events_since(self, seq):
        with self.cond:
            return [dict(e) for e in self.events if e["seq"] > seq]

    def wait_events(self, seq, timeout=15.0):
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                pending = [dict(e) for e in self.events if e["seq"] > seq]
                if pending:
                    return pending
                left = deadline - time.monotonic()
                if left <= 0 or self.state in ("closed", "error"):
                    # Flush a final state event so waiters see the close.
                    return [dict(e) for e in self.events if e["seq"] > seq]
                self.cond.wait(min(left, 5.0))

    # -- JSON-RPC -------------------------------------------------------

    def _send(self, obj):
        line = json.dumps(obj) + "\n"
        proc = self.proc
        if proc is None or proc.stdin is None:
            return False
        try:
            with self._write_lock:
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def call(self, method, params, timeout=CALL_TIMEOUT_SECONDS):
        with self._write_lock:
            self._next_id += 1
            rid = self._next_id
        ev = threading.Event()
        box = {}
        self._pending[rid] = (ev, box)
        if not self._send({"id": rid, "method": method, "params": params or {}}):
            self._pending.pop(rid, None)
            return None, {"message": "app-server is not running"}
        if not ev.wait(timeout):
            self._pending.pop(rid, None)
            return None, {"message": f"{method} timed out"}
        return box.get("result"), box.get("error")

    def _respond(self, rid, result):
        self._send({"id": rid, "result": result if result is not None else {}})

    # -- child lifecycle ------------------------------------------------

    def spawn(self, codex_bin, key, scratch):
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = key
        self._key = key  # retained for redaction only
        self.proc = subprocess.Popen(
            [codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=scratch,
            env=env,
            start_new_session=True,
        )
        threading.Thread(target=self._reader, name="voice-reader", daemon=True).start()
        threading.Thread(target=self._stderr_pump, name="voice-stderr", daemon=True).start()
        threading.Thread(target=self._watchdog, name="voice-watchdog", daemon=True).start()

    def _stderr_pump(self):
        proc = self.proc
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = _redact(raw.decode("utf-8", "replace").rstrip(), self._key)
                if line:
                    self._stderr_tail.append(line[:500])
        except (OSError, ValueError):
            pass

    def _reader(self):
        proc = self.proc
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.emit("error", {"message": "malformed app-server line"})
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:
                    self._on_server_request(msg)
                elif "id" in msg:
                    slot = self._pending.pop(msg.get("id"), None)
                    if slot:
                        ev, box = slot
                        box["result"] = msg.get("result")
                        box["error"] = msg.get("error")
                        ev.set()
                elif "method" in msg:
                    self._on_notification(msg.get("method"), msg.get("params") or {})
        except (OSError, ValueError):
            pass
        finally:
            self._child_exited()

    def _child_exited(self):
        if self.state in ("closed", "error"):
            return
        tail = "; ".join(list(self._stderr_tail)[-3:])[:300]
        detail = tail or "app-server exited"
        self.error = detail
        self.reason = self.reason or "child_exit"
        self.emit("error", {"message": _redact(detail, self._key)})
        self.close(reason=self.reason or "child_exit")

    # -- notifications --------------------------------------------------

    def _on_notification(self, method, params):
        if not isinstance(params, dict):
            params = {}
        if method == "thread/realtime/started":
            self.realtime_version = params.get("version")
            self.set_state("listening")
            self.touch()
            return
        if method == "thread/realtime/sdp":
            self._sdp_answer = params.get("sdp")
            self._sdp_event.set()
            return
        if method == "thread/realtime/transcript/delta":
            role = str(params.get("role") or "assistant")
            delta = str(params.get("delta") or "")
            self._tr_open[role] = (self._tr_open.get(role) or "") + delta
            self.emit("transcript_delta", {"role": role, "delta": delta})
            self.touch()
            return
        if method == "thread/realtime/transcript/done":
            role = str(params.get("role") or "assistant")
            text = str(params.get("text") or self._tr_open.pop(role, "") or "")
            self._tr_open.pop(role, None)
            entry = {"role": role, "text": text, "ts": time.time()}
            self.transcript.append(entry)
            if len(self.transcript) > TRANSCRIPT_MAX_ITEMS:
                self.transcript = self.transcript[-TRANSCRIPT_MAX_ITEMS:]
            self.emit("transcript", entry)
            if role == "assistant":
                self.set_state("listening")
            self.touch()
            return
        if method == "thread/realtime/outputAudio/delta":
            audio = params.get("audio") or {}
            samples = audio.get("samplesPerChannel") or 0
            rate = audio.get("sampleRate") or 24000
            if rate:
                self.audio_ms += int(1000 * samples / rate)
            if self.state == "listening":
                self.set_state("speaking")
            self.touch()
            return
        if method == "thread/realtime/error":
            msg = _redact(str(params.get("message") or "realtime error")[:400], self._key)
            self.emit("error", {"message": msg})
            self.touch()
            return
        if method == "thread/realtime/closed":
            self.close(reason=str(params.get("reason") or "closed"))
            return
        if method == "thread/realtime/itemAdded":
            self._on_realtime_item(params.get("item"))
            return
        if method in ("item/started", "item/completed"):
            self._on_thread_item(method, params)
            return
        if method == "thread/tokenUsage/updated":
            usage = ((params.get("tokenUsage") or {}).get("total") or {})
            self.codex_tokens = int(usage.get("totalTokens") or self.codex_tokens)
            return
        # Everything else (mcpServer/*, account/*, hook/*, remoteControl/*)
        # is irrelevant to the voice lane.

    def _on_realtime_item(self, item):
        if not isinstance(item, dict):
            return
        if item.get("type") == "dynamicToolCall":
            self.set_state("thinking", str(item.get("tool") or "tool"))
        elif "<realtime_delegation>" in json.dumps(item):
            self.set_state("thinking", "delegating")
        self.touch()

    def _on_thread_item(self, method, params):
        item = params.get("item") or {}
        itype = item.get("type") if isinstance(item, dict) else None
        if itype == "dynamicToolCall" and method == "item/started":
            self.set_state("thinking", str(item.get("tool") or "tool"))
        elif itype == "agentMessage" and method == "item/started":
            self.set_state("thinking", "composing")
        self.touch()

    # -- server -> client requests --------------------------------------

    def _on_server_request(self, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        rid = msg.get("id")
        if method == "item/tool/call":
            result = self._run_tool(params)
            self._respond(rid, result)
            return
        if method == "currentTime/read":
            self._respond(rid, {"currentTimeAt": int(time.time())})
            return
        if method in _APPROVAL_DENY_METHODS:
            # Voice sessions run approvalPolicy=never on a read-only sandbox;
            # an approval request reaching us means the model tried to leave
            # the lane. Deny everything.
            self._respond(rid, self._deny_result(method))
            return
        # Unknown request: empty object is the protocol's null-ish response.
        self._respond(rid, {})

    @staticmethod
    def _deny_result(method):
        if method == "mcpServer/elicitation/request":
            return {"action": "decline"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "item/permissions/requestApproval":
            return {"permissions": None, "scope": "turn"}
        return {"decision": "denied"}

    # -- tools ----------------------------------------------------------

    def _run_tool(self, params):
        tool = str(params.get("tool") or "")
        args = params.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        self.emit("tool", {"tool": tool, "phase": "call"})
        try:
            if tool == "ccc_attention":
                return _fmt_tool_text(self._tool_attention(args))
            if tool == "ccc_session":
                return _fmt_tool_text(self._tool_session(args))
            if tool == "ccc_queues":
                return _fmt_tool_text(self._tool_queues(args))
            if tool == "ccc_propose_action":
                return _fmt_tool_text(self._tool_propose(args))
            return _tool_err(f"unknown tool {tool!r}")
        except Exception as e:
            return _tool_err(_redact(str(e)[:200], self._key))

    def _tool_attention(self, args):
        try:
            limit = max(1, min(20, int(args.get("limit") or 10)))
        except (TypeError, ValueError):
            limit = 10
        feed = _core.compute_attention_feed(recent_only=True, limit=limit) or {}
        items = feed.get("items") or []
        if not items:
            return "Nothing needs the user's attention right now."
        out = [f"{len(items)} item(s) need attention:"]
        for it in items:
            repo = it.get("repo") or it.get("folder_label") or ""
            q = (it.get("question_text") or "").strip().replace("\n", " ")[:180]
            row = f"- [{it.get('kind')}] {it.get('where') or ''} {repo}".strip()
            if it.get("session_id"):
                row += f" session={it['session_id']}"
            if q:
                row += f" | {q}"
            out.append(row)
        return "\n".join(out)[:4000]

    def _tool_session(self, args):
        sid = str(args.get("session_id") or "").strip()
        if not sid:
            return "error: session_id is required"
        detail, _status = _core.compute_session_detail(sid)
        if not isinstance(detail, dict) or not detail.get("ok", True):
            return f"session {sid} not found"
        parts = [f"session {sid}:"]
        state = detail.get("session_state") or {}
        for key in ("title", "engine", "repo_label", "folder_label"):
            if detail.get(key):
                parts.append(f"{key}={detail[key]}")
        if state.get("summary"):
            parts.append(f"summary: {str(state['summary'])[:300]}")
        if state.get("next_step_user"):
            parts.append(f"awaits user: {str(state['next_step_user'])[:300]}")
        if detail.get("last_assistant_text"):
            parts.append(f"last: {str(detail['last_assistant_text'])[:300]}")
        return "\n".join(parts)[:3000]

    def _tool_queues(self, args):
        roll = _core._watchtower_queue_rollup() or {}
        if not roll:
            return "WatchTower queue data unavailable."
        return (
            f"{roll.get('open_total', 0)} open ticket(s) across "
            f"{roll.get('queues_total', 0)} queue(s); "
            f"{roll.get('workers_live', 0)} live worker(s); "
            f"{roll.get('stuck_total', 0)} stuck queue(s)."
        )

    def _tool_propose(self, args):
        kind = str(args.get("kind") or "").strip()
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        reason = str(args.get("reason") or "")[:300]
        from ccc_server import assistant_actions as _aa
        try:
            res = _aa.store().propose(kind, params, reason)
        except _aa.ActionError as e:
            return f"error: {e}"
        aid = res.get("action_id")
        item = _aa.store().get(aid) or {}
        pub = _aa.store().public(item, with_token=True)
        self.pending_actions[aid] = pub
        self.emit("action", pub)
        return (
            f"Proposed action {aid}: {res.get('effect')}. It is now a card in the "
            "CCC dashboard. Tell the user to click Confirm (or Dismiss); the "
            "action will not run until they do. You will be told the outcome."
        )

    # -- action outcome (called by server.py after confirm/dismiss) ------

    def notify_action_result(self, action):
        if not isinstance(action, dict):
            return
        aid = action.get("id")
        # Drop the confirm token from what we retain/forward: the token only
        # ever travels to the browser in the original "action" event.
        clean = {k: v for k, v in action.items() if k != "confirm_token"}
        if aid in self.pending_actions:
            self.pending_actions.pop(aid, None)
        self.emit("action", clean)
        if not self.thread_id or self.state not in ("listening", "thinking", "speaking"):
            return
        status = clean.get("status")
        effect = clean.get("effect") or "the action"
        if status == "done":
            text = f"The user confirmed and it is done: {effect}."
        elif status == "failed":
            err = ((clean.get("result") or {}).get("error") or "it failed")[:160]
            text = f"The user confirmed, but it failed: {err}."
        elif status == "dismissed":
            text = f"The user declined: {effect}."
        else:
            return
        self.call("thread/realtime/appendSpeech",
                  {"threadId": self.thread_id, "text": text}, timeout=15)

    # -- watchdog / timeouts ---------------------------------------------

    def _watchdog(self):
        cfg = voice_config_load()
        idle = float(cfg["idle_seconds"])
        max_len = float(cfg["max_minutes"]) * 60.0
        while not self._watchdog_stop.wait(1.0):
            if self.state in ("closed", "error"):
                return
            now = time.monotonic()
            if now - self.last_activity > idle:
                self.stop(reason="idle_timeout")
                return
            if time.time() - self.started_at > max_len:
                self.stop(reason="max_duration")
                return
            if (self.last_heartbeat is not None
                    and now - self.last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS):
                self.stop(reason="heartbeat_lost")
                return

    def heartbeat(self):
        # Deliberately NOT touch(): heartbeats prove the browser is alive,
        # they are not voice activity — otherwise an idle timer would never
        # fire while the tab is open.
        self.last_heartbeat = time.monotonic()

    # -- close / teardown -------------------------------------------------

    def stop(self, reason="user"):
        """Graceful stop: realtime/stop, then kill the child. Idempotent."""
        if self.state in ("closed", "error"):
            return
        self.reason = self.reason or reason
        self.set_state("stopping", reason)
        if self.thread_id and self.proc and self.proc.poll() is None:
            try:
                self.call("thread/realtime/stop", {"threadId": self.thread_id}, timeout=8)
            except Exception:
                pass
        self._kill_proc()
        self.close(reason=self.reason or reason)

    def _kill_proc(self):
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        proc.kill()
                    proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def close(self, reason="closed"):
        if self._closed_once:
            return
        self._closed_once = True
        self._key = None  # redaction handle ends with the child
        self._watchdog_stop.set()
        self.closed_at = time.time()
        self.reason = self.reason or reason
        self._kill_proc()
        self._finish_bookkeeping()
        if self.state not in ("error",):
            self.set_state("closed", self.reason)
        _manager_clear(self)

    def _finish_bookkeeping(self):
        dur = self.closed_at - self.started_at
        if self._tr_open:
            for role, text in self._tr_open.items():
                if text:
                    self.transcript.append({"role": role, "text": text, "ts": time.time()})
            self._tr_open = {}
        # Optional transcript persistence (default off — privacy).
        cfg = voice_config_load()
        if cfg.get("save_transcripts") and self.transcript:
            try:
                VOICE_TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
                path = VOICE_TRANSCRIPT_DIR / f"{self.id}.jsonl"
                with open(path, "w", encoding="utf-8") as fh:
                    for entry in self.transcript:
                        fh.write(json.dumps(entry) + "\n")
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            except OSError:
                pass
        # Usage ledger: duration + provider openai, zero guessed tokens.
        try:
            _core.byok_record_usage(
                session_id=self.id,
                engine="codex-realtime",
                model="openai/realtime",
                key_profile=self.profile or None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=None,
                provider="openai",
                extra={
                    "duration_s": round(dur, 1),
                    "audio_ms": self.audio_ms,
                    "codex_tokens": self.codex_tokens,
                    "reason": self.reason,
                },
            )
        except Exception:
            pass
        self.emit("closed", {
            "reason": self.reason,
            "duration_s": round(dur, 1),
            "audio_ms": self.audio_ms,
            "codex_tokens": self.codex_tokens,
        })

    def public(self):
        return {
            "id": self.id,
            "state": self.state,
            "state_detail": self.state_detail,
            "voice": self.voice,
            "profile": self.profile,
            "started_at": self.started_at,
            "elapsed_s": round((self.closed_at or time.time()) - self.started_at, 1),
            "audio_ms": self.audio_ms,
            "codex_tokens": self.codex_tokens,
            "error": self.error,
            "reason": self.reason,
            "realtime_version": self.realtime_version,
            "pending_actions": [dict(a) for a in self.pending_actions.values()],
        }


# ---------------------------------------------------------------------------
# Manager: exactly one live session
# ---------------------------------------------------------------------------

_MGR_LOCK = threading.Lock()
_SESSION = None
_LAST_SESSION = None


def voice_status():
    with _MGR_LOCK:
        sess = _SESSION
        last = _LAST_SESSION
    cfg = voice_config_load()
    return {
        "ok": True,
        "active": sess is not None and sess.state not in ("closed", "error"),
        "session": sess.public() if sess else None,
        "last_session": last.public() if last and not sess else None,
        "config": cfg,
        "openai_profiles": _openai_profiles(),
    }


def _session_or_none():
    with _MGR_LOCK:
        return _SESSION


def _manager_clear(sess):
    """Move a self-closed session out of the live slot (idle/max/heartbeat/
    child-exit paths close without going through voice_stop)."""
    global _SESSION, _LAST_SESSION
    with _MGR_LOCK:
        if _SESSION is sess:
            _LAST_SESSION = sess
            _SESSION = None


def _session_by_id(session_id):
    """Live session first, then the most recently closed one (SSE readers
    still need the closed session's event buffer after it leaves _SESSION)."""
    with _MGR_LOCK:
        for cand in (_SESSION, _LAST_SESSION):
            if cand is not None and cand.id == session_id:
                return cand
    return None


def voice_heartbeat(session_id):
    sess = _session_by_id(session_id)
    if not sess or sess.state in ("closed", "error"):
        return {"ok": False, "error": "no such voice session", "code": "voice_no_session"}, 404
    sess.heartbeat()
    return {"ok": True}, 200


def voice_start(params):
    """Validate, spawn, negotiate. Returns (response_dict, http_status)."""
    global _SESSION
    params = params if isinstance(params, dict) else {}
    sdp_offer = params.get("sdp_offer")
    if not isinstance(sdp_offer, str) or "v=0" not in sdp_offer[:200]:
        return {"ok": False, "code": "voice_bad_request",
                "error": "sdp_offer (a WebRTC SDP offer string) is required"}, 400
    if len(sdp_offer) > 65536:
        return {"ok": False, "code": "voice_bad_request", "error": "sdp_offer too large"}, 400

    cfg = voice_config_load()
    voice = str(params.get("voice") or cfg["voice"] or DEFAULT_VOICE).strip().lower()
    if voice not in _ALL_VOICES:
        # A live catalog may know voices the fallback doesn't; keep it loose:
        # reject only obviously malformed values.
        if not voice or len(voice) > 40 or not voice.replace("-", "").isalnum():
            return {"ok": False, "code": "voice_bad_request",
                    "error": "unknown voice"}, 400
    profile = str(params.get("profile") or cfg["profile"] or "").strip()

    codex_info = _core._resolve_codex_bin()
    if not codex_info.get("available") or not codex_info.get("bin"):
        return {"ok": False, "code": "voice_no_codex",
                "error": codex_info.get("reason") or "Codex CLI not found"}, 503

    key, used_profile, err = _resolve_openai_key(profile)
    if err:
        return err, 400

    with _MGR_LOCK:
        if _SESSION is not None and _SESSION.state not in ("closed", "error"):
            return {"ok": False, "code": "voice_busy",
                    "error": "a voice session is already running"}, 409
        sess = VoiceSession("voice_" + secrets.token_hex(4), voice, used_profile)
        _SESSION = sess

    try:
        _bootstrap(sess, codex_info["bin"], key, sdp_offer)
    except Exception as e:
        sess.error = _redact(str(e)[:300], key)
        sess.close(reason="start_failed")
        return {"ok": False, "code": "voice_start_failed",
                "error": sess.error or "voice session failed to start"}, 502

    return {"ok": True, "session_id": sess.id, "state": sess.state,
            "sdp_answer": sess._sdp_answer, "voice": sess.voice,
            "profile": used_profile}, 200


def _bootstrap(sess, codex_bin, key, sdp_offer):
    VOICE_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    sess.spawn(codex_bin, key, str(VOICE_SCRATCH_DIR))

    result, error = sess.call("initialize", {
        "clientInfo": {"name": "claude-command-center", "version": str(_core.__version__)},
        "capabilities": {"experimentalApi": True},
    }, timeout=START_TIMEOUT_SECONDS)
    if error or not isinstance(result, dict):
        raise RuntimeError(f"app-server initialize failed: {(error or {}).get('message', 'no result')}")
    sess._send({"method": "initialized"})

    result, error = sess.call("thread/realtime/listVoices", {}, timeout=15)
    if isinstance(result, dict):
        _record_live_voices(result)

    briefing = build_briefing()
    result, error = sess.call("thread/start", {
        "cwd": str(VOICE_SCRATCH_DIR),
        "ephemeral": True,
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "dynamicTools": _tool_specs(),
    }, timeout=START_TIMEOUT_SECONDS)
    thread = (result or {}).get("thread") or {}
    tid = thread.get("id")
    if error or not tid:
        raise RuntimeError(f"thread/start failed: {(error or {}).get('message', 'no thread id')}")
    sess.thread_id = tid

    sess._sdp_event.clear()
    result, error = sess.call("thread/realtime/start", {
        "threadId": tid,
        "outputModality": "audio",
        "transport": {"type": "webrtc", "sdp": sdp_offer},
        "voice": sess.voice,
        "version": "v3",
        "prompt": _VOICE_PROMPT,
        "realtimeStartInstructions": _CODEX_INSTRUCTIONS,
        "initialItems": [{"role": "developer", "text": briefing}],
        "clientManagedHandoffs": False,
    }, timeout=START_TIMEOUT_SECONDS)
    if error:
        raise RuntimeError(f"realtime/start failed: {(error or {}).get('message', 'error')}")

    if not sess._sdp_event.wait(timeout=25):
        raise RuntimeError("app-server did not return an SDP answer")
    if not sess._sdp_answer:
        raise RuntimeError("empty SDP answer")


def _tool_specs():
    """dynamicTools surface: three read-only + one propose-only mutating tool."""
    return [
        {
            "type": "function",
            "name": "ccc_attention",
            "description": (
                "List what currently needs the user's attention on the CCC "
                "board (sessions waiting on them, blockers). Read-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "max items, default 10"}},
            },
        },
        {
            "type": "function",
            "name": "ccc_session",
            "description": "Summarize one CCC session by session_id. Read-only.",
            "inputSchema": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
        },
        {
            "type": "function",
            "name": "ccc_queues",
            "description": "WatchTower queue rollup: open tickets, live workers, stuck queues. Read-only.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "ccc_propose_action",
            "description": (
                "Propose a MUTATING action for the user to confirm in the CCC "
                "dashboard. Never executes anything. kind is one of "
                "spawn_session, inject, wt_add, wt_comment; params follows the "
                "kind: spawn_session {cwd, prompt, engine?, model?, name?}; "
                "inject {session_id, text}; wt_add {queue, title, text, priority?}; "
                "wt_comment {ref, text}."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["spawn_session", "inject", "wt_add", "wt_comment"]},
                    "params": {"type": "object"},
                    "reason": {"type": "string"},
                },
                "required": ["kind", "params"],
            },
        },
    ]


def voice_stop(session_id=None, reason="user"):
    sess = _session_or_none()
    if not sess:
        return {"ok": True, "stopped": False, "note": "no active voice session"}, 200
    if session_id and sess.id != session_id:
        return {"ok": False, "error": "session id mismatch",
                "code": "voice_no_session"}, 409
    sess.stop(reason=reason)
    _manager_clear(sess)
    return {"ok": True, "stopped": True}, 200


def notify_action_result(action):
    """server.py calls this after an assistant-actions confirm/dismiss so the
    voice session learns (and speaks) the outcome."""
    sess = _session_or_none()
    if sess:
        try:
            sess.notify_action_result(action)
        except Exception:
            pass


def voice_events_wait(session_id, after_seq, timeout=15.0):
    """SSE pump helper: returns (events, latest_seq, still_active)."""
    sess = _session_by_id(session_id)
    if not sess:
        return [], after_seq, False
    events = sess.wait_events(after_seq, timeout=timeout)
    latest = events[-1]["seq"] if events else after_seq
    with sess.cond:
        latest = max(latest, sess.seq)
    return events, latest, sess.state not in ("closed", "error")


def voice_shutdown():
    """Server exit hook: no orphan app-server children."""
    sess = _session_or_none()
    if sess:
        try:
            sess.stop(reason="server_shutdown")
        except Exception:
            pass
