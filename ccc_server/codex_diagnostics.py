"""Per-session Codex diagnostics for the Metadata tab.

Answers, in plain language, the questions a user asks when a Codex session
looks stuck: how CCC is driving it (managed app-server vs one-shot
``codex exec`` fallback), why it fell back, whether the run is ephemeral
(no native rollout / no state-DB row, so invisible to Codex desktop and
non-resumable), whether Codex desktop is competing for the shared state DB
or the rollout itself, process liveness, and recent telemetry.

Every collector is best-effort: one failure must never blank the payload,
and ``build_codex_session_diagnostics`` never raises.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ccc_server import core as _core

_TELEMETRY_TAIL_BYTES = 512 * 1024
_TELEMETRY_KEEP = 12
_COORD_EVENTS_KEEP = 8

_EPHEMERAL_NOTE = (
    "This run was ephemeral: Codex wrote no native thread (no rollout file, "
    "no state DB row). It exists only in CCC's capture log — it will not "
    "appear in Codex desktop and cannot be resumed natively."
)


def _age_s(mtime, now):
    try:
        return round(now - float(mtime), 1)
    except (TypeError, ValueError, OSError):
        return None


def _numeric_pid(pid):
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def _find_spawn_entry(sid):
    """Best spawn-registry entry for `sid`: live in-process, then live on
    disk, then any historical registry row (dead spawns included — the
    diagnostics panel wants history)."""
    entry = _core._find_live_spawn_entry_for_session(sid)
    if entry:
        return entry, "live"
    entry = _core._disk_spawn_entry_for_session(sid)
    if entry:
        return entry, "live"
    try:
        for candidate in _core._load_spawn_registry() or []:
            if not isinstance(candidate, dict):
                continue
            if sid in (candidate.get("session_id"), candidate.get("resumed_sid")):
                return candidate, "dead"
    except Exception:
        pass
    try:
        reg = _core._codex_thread_registry_entry(sid)
        if isinstance(reg, dict):
            shaped = _core._codex_thread_registry_spawn_shape(reg)
            if shaped.get("transport_owner") or shaped.get("log") or shaped.get("pid"):
                return shaped, "registry"
    except Exception:
        pass
    return None, "none"


def _entry_alive(entry, source):
    if not isinstance(entry, dict):
        return False
    if source == "live" and entry.get("_live_probed"):
        return True
    pid = entry.get("pid")
    numeric = _numeric_pid(pid)
    if numeric is None:
        # App-server spawns carry symbolic pids like "codex-app-…"; they are
        # threads inside a shared daemon, not processes CCC can poll.
        return False
    if source == "live":
        try:
            return _core._poll_spawn_entry(entry) is None
        except Exception:
            pass
    try:
        return bool(_core._is_pid_alive(numeric))
    except Exception:
        return False


def _transport_kind(entry, sid, has_native):
    """Classify how this thread is (or was) driven."""
    entry = entry or {}
    pid = entry.get("pid")
    app_server_spawn = bool(entry.get("app_server_spawn"))
    owner = str(entry.get("transport_owner") or "")
    if app_server_spawn or owner == "ccc-managed-app-server" or str(pid or "").startswith("codex-app-"):
        kind = _core._codex_app_server_transport_kind()
        return "app-server-managed" if kind == "managed" else "app-server-stdio"
    if entry.get("resumed_sid"):
        return "exec-resume"
    log = str(entry.get("log") or "")
    if _numeric_pid(pid) is not None and "spawn-codex-" in os.path.basename(log):
        return "exec-fallback"
    if not entry and has_native:
        return "external"
    if not entry:
        return "unknown"
    if _numeric_pid(pid) is not None:
        return "exec-fallback"
    return "unknown"


_TRANSPORT_LABELS = {
    "app-server-managed": "Managed app-server",
    "app-server-stdio": "Private app-server",
    "exec-fallback": "One-shot codex exec (fallback)",
    "exec-resume": "Resume via codex exec",
    "external": "External / native",
    "unknown": "Unknown",
}

_TRANSPORT_DETAILS = {
    "app-server-managed": "CCC drives this thread through the shared managed Codex app-server daemon.",
    "app-server-stdio": "CCC drives this thread through its own private app-server subprocess.",
    "exec-fallback": "CCC could not use an app-server, so it ran a one-shot `codex exec` process and captured its output.",
    "exec-resume": "CCC resumed this thread with a one-shot `codex exec resume` process.",
    "external": "This thread was started outside CCC (terminal or Codex desktop).",
    "unknown": "CCC has no spawn record for this thread.",
}


def _log_facts(log_path, alive, now):
    facts = {"log_path": None, "log_size": 0, "log_mtime_age_s": None,
             "turn_outcome": None, "turn_failed_error": None}
    if not log_path:
        return facts
    path = str(log_path)
    facts["log_path"] = path
    try:
        st = Path(path).stat()
        facts["log_size"] = st.st_size
        facts["log_mtime_age_s"] = _age_s(st.st_mtime, now)
    except OSError:
        pass
    try:
        err = _core._codex_turn_failed_error_from_log(path)
    except Exception:
        err = None
    if err:
        facts["turn_outcome"] = "failed"
        facts["turn_failed_error"] = str(err)[:400]
        return facts
    completed = False
    try:
        for line in _core._tail_read_lines(path, max_bytes=131072):
            if '"turn.completed"' in line:
                completed = True
    except Exception:
        pass
    if completed:
        facts["turn_outcome"] = "completed"
    elif alive:
        facts["turn_outcome"] = "running"
    return facts


def _storage_facts(sid, entry, now):
    facts = {
        "native_rollout": {"present": False, "path": None, "size": 0, "mtime_age_s": None},
        "sqlite_row": False,
        "capture_log": {"present": False, "path": None},
        "ephemeral": False,
        "note": "",
    }
    try:
        rollout = _core._resolve_codex_rollout_path(sid)
    except Exception:
        rollout = None
    if rollout:
        try:
            st = Path(rollout).stat()
            facts["native_rollout"] = {
                "present": True, "path": str(rollout), "size": st.st_size,
                "mtime_age_s": _age_s(st.st_mtime, now),
            }
        except OSError:
            facts["native_rollout"] = {"present": True, "path": str(rollout),
                                       "size": 0, "mtime_age_s": None}
    try:
        facts["sqlite_row"] = bool(_core._codex_thread_row(sid))
    except Exception:
        pass
    capture = None
    try:
        row = _core._codex_capture_thread_row(sid)
        if isinstance(row, dict):
            capture = row.get("_ccc_capture")
    except Exception:
        pass
    if not capture:
        try:
            log = (entry or {}).get("log")
            if log and _core._extract_codex_thread_id_from_log(log) == sid:
                capture = log
        except Exception:
            pass
    if capture:
        facts["capture_log"] = {"present": True, "path": str(capture)}
    if not facts["native_rollout"]["present"] and not facts["sqlite_row"] and facts["capture_log"]["present"]:
        facts["ephemeral"] = True
        facts["note"] = _EPHEMERAL_NOTE
    return facts


def _competition_facts(sid):
    facts = {"desktop_running": False, "desktop_app_server_pids": [],
             "shared_state_holders": [], "conflict_message": None,
             "desktop_attached_to_rollout": False, "external_writer_active": False,
             "writer": None, "rollout_mtime_age_s": None}
    try:
        facts["desktop_running"] = bool(_core._codex_desktop_app_is_running())
    except Exception:
        pass
    try:
        for proc in _core._codex_desktop_app_server_procs() or []:
            pid = _numeric_pid((proc or {}).get("pid"))
            if pid is not None:
                facts["desktop_app_server_pids"].append(pid)
    except Exception:
        pass
    try:
        conflict = _core._codex_shared_state_conflict()
        if isinstance(conflict, dict):
            facts["shared_state_holders"] = [
                {"pid": h.get("pid"), "command": h.get("command")}
                for h in conflict.get("holders") or [] if isinstance(h, dict)
            ]
            facts["conflict_message"] = conflict.get("message")
    except Exception:
        pass
    try:
        snap = _core._codex_thread_writer_snapshot(sid) or {}
        facts["writer"] = snap.get("writer")
        facts["desktop_attached_to_rollout"] = bool(snap.get("desktop_attached"))
        facts["external_writer_active"] = bool(snap.get("external_active"))
        facts["rollout_mtime_age_s"] = snap.get("mtime_age_s")
    except Exception:
        pass
    return facts


def _app_server_facts(sid, now):
    facts = {"live": False, "transport_kind": None, "thread_known": False,
             "status": None, "active_turn_id": None, "active_writer": None,
             "ccc_turn_start_pending": False, "last_activity_age_s": None,
             "needs_approval": False, "approval_message": ""}
    try:
        facts["live"] = bool(_core._codex_app_server_is_live())
        facts["transport_kind"] = _core._codex_app_server_transport_kind()
    except Exception:
        pass
    state = None
    try:
        state = _core._codex_app_server_thread_state(sid)
    except Exception:
        state = None
    if isinstance(state, dict) and state:
        facts["thread_known"] = True
        facts["status"] = state.get("status")
        facts["active_turn_id"] = state.get("active_turn_id")
        facts["active_writer"] = state.get("active_writer")
        facts["ccc_turn_start_pending"] = bool(state.get("ccc_turn_start_pending"))
        try:
            last = float(state.get("last_activity_at") or state.get("last_event_at") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last:
            facts["last_activity_age_s"] = round(now - last, 1)
    try:
        activity = _core._codex_app_server_activity_fields(sid) or {}
        facts["needs_approval"] = bool(activity.get("needs_approval"))
        facts["approval_message"] = str(activity.get("needs_approval_message") or "")
    except Exception:
        pass
    return facts


def _coordination_events(sid):
    try:
        _core._codex_load_coordination_state()
        with _core._CODEX_APP_SERVER_LOCK:
            state = _core._CODEX_APP_SERVER_THREAD_STATE.get(sid) or {}
            events = list(state.get("coordination_events") or [])
    except Exception:
        return []
    texts = {}
    try:
        texts = _core._CODEX_COORD_EVENT_TEXT or {}
    except Exception:
        texts = {}
    out = []
    for ev in events[-_COORD_EVENTS_KEEP:]:
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        out.append({
            "ts": ev.get("ts"),
            "kind": kind,
            "text": ev.get("detail") or texts.get(kind) or kind.replace("_", " "),
            "writer": ev.get("writer"),
            "detail": ev.get("detail"),
        })
    return out


def _pid_str_equal(a, b):
    na, nb = _numeric_pid(a), _numeric_pid(b)
    return na is not None and nb is not None and na == nb


def _telemetry_rows(sid, entry):
    path = _core.CODEX_TELEMETRY_FILE
    try:
        size = path.stat().st_size
    except OSError:
        return [], None, None
    rows = []
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - _TELEMETRY_TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
        for line in data.splitlines()[1:] if size > _TELEMETRY_TAIL_BYTES else data.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return [], None, None

    spawn_pid = (entry or {}).get("pid")
    entry_cwd = str((entry or {}).get("cwd") or "")

    # Find the codex_spawn ok row carrying our pid; a cwd-matched fallback row
    # within 15s of it is then attributed to this session even though the
    # fallback row itself predates the thread id.
    exec_ok_ts = None
    for row in rows:
        if (row.get("event") == "codex_spawn" and row.get("ok")
                and row.get("via") == "codex-spawn"
                and _pid_str_equal(row.get("pid"), spawn_pid)):
            try:
                exec_ok_ts = float(row.get("ts"))
            except (TypeError, ValueError):
                exec_ok_ts = None

    matched = []
    fallback_row = None
    for row in rows:
        keep = False
        if row.get("thread_id") == sid:
            keep = True
        elif _pid_str_equal(row.get("pid"), spawn_pid):
            keep = True
        elif (row.get("event") == "codex_spawn" and row.get("fallback")
                and exec_ok_ts is not None and entry_cwd
                and str(row.get("cwd") or "") == entry_cwd):
            try:
                if abs(float(row.get("ts")) - exec_ok_ts) <= 15:
                    keep = True
                    fallback_row = row
            except (TypeError, ValueError):
                pass
        if keep:
            matched.append({
                "ts": row.get("ts"),
                "event": row.get("event"),
                "via": row.get("via"),
                "stage": row.get("stage"),
                "fallback": row.get("fallback"),
                "fallback_reason": row.get("fallback_reason"),
                "error": row.get("error"),
                "ok": row.get("ok"),
                "pid": row.get("pid"),
                "thread_id": row.get("thread_id"),
            })
    return matched[-_TELEMETRY_KEEP:], fallback_row


def _verdict(payload):
    v = {"state": "idle", "severity": "ok", "headline": "Idle",
         "detail": "No active turn detected."}
    app = payload["app_server"]
    comp = payload["competition"]
    proc = payload["process"]
    transport = payload["transport"]
    storage = payload["storage"]

    if app.get("needs_approval"):
        v = {"state": "needs_approval", "severity": "warn",
             "headline": "Waiting for your approval",
             "detail": app.get("approval_message") or "Codex is waiting for approval"}
    elif comp.get("external_writer_active"):
        writer = comp.get("writer")
        headline = ("Codex desktop is driving this thread right now" if writer == "desktop"
                    else "Another app is driving this thread right now")
        v = {"state": "external_turn", "severity": "warn", "headline": headline,
             "detail": "CCC will queue anything you send until that turn goes quiet."}
    elif proc.get("alive") and str(transport.get("kind") or "").startswith("exec"):
        pid = proc.get("pid")
        detail = "Output streams from CCC's capture log."
        if transport.get("fallback_reason"):
            why = "CCC could not use its app-server (" + str(transport["fallback_reason"]) + ") and fell back to a plain CLI run."
            if transport.get("fallback_error"):
                why += " " + str(transport["fallback_error"])[:200]
            detail = why + " " + detail
        if storage.get("ephemeral"):
            detail += " This run is ephemeral — see Storage."
        v = {"state": "running_exec", "severity": "info",
             "headline": "Running as a one-shot `codex exec` process (pid %s)" % (pid if pid is not None else "?"),
             "detail": detail}
    elif app.get("active_turn_id") and (app.get("active_writer") == "ccc" or app.get("ccc_turn_start_pending")):
        v = {"state": "running_app_server", "severity": "info",
             "headline": "CCC is running a turn via the app-server",
             "detail": "Codex is working; events stream in as the turn progresses."}
    elif proc.get("turn_outcome") == "failed":
        v = {"state": "failed", "severity": "error",
             "headline": "The run failed",
             "detail": proc.get("turn_failed_error") or ""}
    elif proc.get("turn_outcome") == "completed" and not proc.get("alive"):
        detail = "The one-shot process exited after completing its turn."
        if storage.get("ephemeral"):
            detail += " This run is ephemeral — see Storage."
        v = {"state": "completed", "severity": "ok", "headline": "Run finished", "detail": detail}
    elif storage.get("ephemeral") and not proc.get("alive"):
        v = {"state": "ended_invisible", "severity": "warn",
             "headline": "Finished, but only CCC can see it",
             "detail": storage.get("note") or _EPHEMERAL_NOTE}
    elif comp.get("conflict_message") and transport.get("kind") in ("unknown", "external"):
        v = {"state": "blocked_shared_state", "severity": "warn",
             "headline": "Codex desktop holds the shared state DB",
             "detail": comp["conflict_message"]}
    payload["verdict"] = v


def build_codex_session_diagnostics(session_id):
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "error": "missing session_id"}
    now = time.time()
    payload = {
        "ok": True, "session_id": sid, "engine": "codex", "generated_at": now,
        "verdict": {}, "transport": {}, "process": {}, "storage": {},
        "competition": {}, "app_server": {},
        "coordination_events": [], "telemetry": [],
    }
    try:
        entry, entry_source = _find_spawn_entry(sid)
    except Exception:
        entry, entry_source = None, "none"
    entry = entry or {}

    try:
        alive = _entry_alive(entry, entry_source)
    except Exception:
        alive = False

    # Storage first: transport classification needs has_native.
    storage = _storage_facts(sid, entry, now)
    payload["storage"] = storage
    has_native = storage["native_rollout"]["present"] or storage["sqlite_row"]
    entry_is_codex = str(entry.get("engine") or "codex") == "codex" and bool(entry)
    if not entry_is_codex and not has_native and not storage["capture_log"]["present"]:
        # No spawn record, no native thread, no capture: not a Codex session
        # CCC knows anything about. Callers hide the panel on ok=False.
        return {"ok": False, "error": "not a known Codex session", "session_id": sid}

    try:
        kind = _transport_kind(entry, sid, has_native)
    except Exception:
        kind = "unknown"
    payload["transport"] = {
        "kind": kind,
        "label": _TRANSPORT_LABELS.get(kind, "Unknown"),
        "detail": _TRANSPORT_DETAILS.get(kind, ""),
        "fallback_reason": None,
        "fallback_error": None,
        "spawned_via": str(entry.get("spawned_via") or ""),
        "model": str(entry.get("model") or ""),
        "reasoning_effort": str(entry.get("reasoning_effort") or entry.get("effort") or ""),
        "spawned_at": str(entry.get("spawned_at") or entry.get("started") or ""),
        "cwd": str(entry.get("cwd") or ""),
    }

    try:
        proc = _log_facts(entry.get("log"), alive, now)
        proc["pid"] = entry.get("pid")
        proc["alive"] = alive
    except Exception:
        proc = {"pid": entry.get("pid"), "alive": alive, "log_path": None,
                "log_size": 0, "log_mtime_age_s": None,
                "turn_outcome": None, "turn_failed_error": None}
    payload["process"] = proc

    try:
        payload["competition"] = _competition_facts(sid)
    except Exception:
        pass
    try:
        payload["app_server"] = _app_server_facts(sid, now)
    except Exception:
        pass
    try:
        payload["coordination_events"] = _coordination_events(sid)
    except Exception:
        pass
    try:
        rows, fallback_row = _telemetry_rows(sid, entry)
        payload["telemetry"] = rows
        if fallback_row:
            if fallback_row.get("fallback_reason"):
                payload["transport"]["fallback_reason"] = str(fallback_row["fallback_reason"])
            if fallback_row.get("error"):
                payload["transport"]["fallback_error"] = str(fallback_row["error"])[:400]
    except Exception:
        pass

    try:
        _verdict(payload)
    except Exception:
        payload["verdict"] = {"state": "idle", "severity": "ok",
                              "headline": "Idle", "detail": "No active turn detected."}
    return payload
