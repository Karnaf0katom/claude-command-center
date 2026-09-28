"""Session waste: where one session's money went, from the external
``throughput analyze`` tool (github.com/amirfish1/agent-throughput).

Optional integration. When the tool is not installed the endpoint says so
and how to install it; nothing else in CCC depends on it.

The analysis re-reads changed session logs and simulates fixes on every
model call of the session, so it takes seconds (about a minute on the tool's
first run). It therefore runs only when asked for one session, never per row;
results are cached in memory and ``cached_only`` reads never start a process,
which is what a per-row badge may use.

Name clash: CCC ships its own ``scripts/throughput`` (the in-repo usage DB,
``ccc_server/usage_db``). It has no ``analyze``, so the binary is resolved
explicitly and anything inside this repo is rejected.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

SESSION_WASTE_INSTALL_HINT = "pipx install git+https://github.com/amirfish1/agent-throughput"
_SESSION_WASTE_TTL_S = 600
_SESSION_WASTE_TIMEOUT_S = 180
_SESSION_WASTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")
_SESSION_WASTE_REPO = Path(__file__).resolve().parents[1]

_session_waste_cache: dict = {}  # session id -> (monotonic time, payload)
_session_waste_locks: dict = {}  # session id -> Lock: one analysis per session at a time
_session_waste_guard = threading.Lock()


def _session_waste_bin():
    """Path of agent-throughput's ``throughput``, or None. ``CCC_SESSION_WASTE_BIN`` overrides."""
    env = os.environ.get("CCC_SESSION_WASTE_BIN", "").strip()
    cands = [env] if env else [str(Path.home() / ".local" / "bin" / "throughput"), shutil.which("throughput")]
    for cand in cands:
        if not cand or not os.access(cand, os.X_OK):
            continue
        real = Path(os.path.realpath(cand))
        if _SESSION_WASTE_REPO in real.parents:
            continue  # CCC's own scripts/throughput
        return cand
    return None


def _session_waste_slim(r: dict) -> dict:
    """The fields the dashboard shows, in the order it shows them."""
    s = r.get("session") or {}
    money = lambda d: {k: d.get(k) for k in ("list_usd", "real_usd", "share_pct")}  # noqa: E731
    return {
        "ok": True,
        "session_id": s.get("source_session_id"),
        "engine": s.get("engine"),
        "model": s.get("model_label") or s.get("model_id"),
        "score": r.get("score"),
        "list_usd": r.get("list_usd"),
        "real_usd": r.get("real_usd"),
        "list_to_real": r.get("list_to_real"),
        "ratio_source": r.get("ratio_source"),
        "unpriced_calls": r.get("unpriced_calls") or 0,
        "findings": [dict(title=f.get("title"), evidence=f.get("evidence"), fix=f.get("fix"), **money(f))
                     for f in r.get("findings") or []],
        "minor_findings": [{"title": f.get("title"), "share_pct": f.get("share_pct")}
                           for f in r.get("minor_findings") or []],
        "activities": [dict(activity=a.get("activity"), **money(a)) for a in (r.get("activities") or [])[:5]],
    }


def _session_waste_run(binary: str, session_id: str) -> dict:
    try:
        proc = subprocess.run([binary, "analyze", session_id, "--json"], capture_output=True, text=True,
                              timeout=_SESSION_WASTE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"analysis took over {_SESSION_WASTE_TIMEOUT_S}s"}
    except OSError as exc:
        return {"ok": False, "error": f"could not run {binary}: {exc}"}
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {"ok": False, "error": lines[-1][:300] if lines else f"exit {proc.returncode}"}
    try:
        return _session_waste_slim(json.loads(proc.stdout))
    except (ValueError, AttributeError, TypeError):
        return {"ok": False, "error": "unreadable output from throughput analyze"}


def session_waste(session_id: str, *, cached_only: bool = False) -> dict:
    """Waste analysis for one session: cached, or computed now unless ``cached_only``."""
    sid = str(session_id or "").strip()
    if not _SESSION_WASTE_ID_RE.match(sid):
        return {"ok": False, "error": "bad session_id"}
    hit = _session_waste_cache.get(sid)
    if hit and time.monotonic() - hit[0] < _SESSION_WASTE_TTL_S:
        return dict(hit[1], cached=True, age_s=round(time.monotonic() - hit[0]))
    if cached_only:
        return {"ok": False, "cached": False, "error": "not analyzed yet"}
    binary = _session_waste_bin()
    if not binary:
        return {"ok": False, "installed": False, "error": "agent-throughput is not installed",
                "install": SESSION_WASTE_INSTALL_HINT}
    with _session_waste_guard:
        lock = _session_waste_locks.setdefault(sid, threading.Lock())
    with lock:
        hit = _session_waste_cache.get(sid)  # a concurrent request may have just filled it
        if hit and time.monotonic() - hit[0] < _SESSION_WASTE_TTL_S:
            return dict(hit[1], cached=True, age_s=round(time.monotonic() - hit[0]))
        payload = _session_waste_run(binary, sid)
        if payload.get("ok"):
            _session_waste_cache[sid] = (time.monotonic(), payload)
        return dict(payload, cached=False)
