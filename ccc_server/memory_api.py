# Copyright (c) 2026 Amir Fish. All rights reserved.
# SPDX-License-Identifier: LicenseRef-CCC-Software-License
"""API-layer glue for ccc_server.ship_graph (is_shipped, search_sessions).

Both functions already exist and are fully working — until now the only
caller was the ~/rnd/ccc-memory benchmark, so no running agent could reach
them. This module is the thin read-only surface that powers:

  - GET /api/memory/recall?q=&limit=              -> recall()
  - GET /api/memory/shipped?topic=                -> shipped()
  - GET /api/memory/file-history?path=&repo=       -> file_history()
  - GET /api/memory/decisions?topic=              -> decisions()
  - `ccc recall "<query>"` / `ccc shipped "<topic>"` / `ccc history <path>` /
    `ccc decisions "<topic>"` (./ccc CLI)

No new retrieval logic lives here. search_sessions() only returns
session_id, so recall() enriches each hit with title/repo/date/snippet read
from two tables that ship_graph.search_sessions() has already synced in the
same call (ship_graph's own session_meta table for repo/date, session_fts's
sdoc table for title/snippet) — two batched SQL queries total, no per-row
file reads, no subprocess.

file_history() is the one function here that does spawn a subprocess: a
single live `git log --follow` for the one path being asked about (not a
per-row loop over anything), because the cached `commits` table has no
path filter and can't follow renames. Session hits for the same path come
from session_meta.files (already synced), a single LIKE-filtered query.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

from ccc_server import session_fts as _sfts
from ccc_server import ship_graph as _sg


def _session_meta_rows(conn: sqlite3.Connection, sids: list[str]) -> dict[str, dict]:
    placeholders = ",".join("?" for _ in sids)
    out: dict[str, dict] = {}
    try:
        cur = conn.execute(
            f"SELECT sid, repo, start_ts, end_ts FROM session_meta WHERE sid IN ({placeholders})",
            sids,
        )
    except sqlite3.OperationalError:
        return out
    for sid, repo, start_ts, end_ts in cur.fetchall():
        ts = start_ts or end_ts
        out[sid] = {
            "repo": repo or "",
            "date": time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "",
        }
    return out


def _sdoc_rows(sids: list[str]) -> dict[str, dict]:
    placeholders = ",".join("?" for _ in sids)
    out: dict[str, dict] = {}
    try:
        conn = _sfts._get_connection()
        cur = conn.execute(
            f"SELECT sid, title, prompts FROM sdoc WHERE sid IN ({placeholders})",
            sids,
        )
    except sqlite3.OperationalError:
        return out
    for sid, title, prompts in cur.fetchall():
        out[sid] = {
            "title": title or "",
            "snippet": " ".join((prompts or "").split())[:200],
        }
    return out


def recall(query: str, limit: int = 20) -> dict:
    """GET /api/memory/recall — ranked sessions for `query`, each enriched
    with title/repo/date/snippet from already-synced index state.

    Neither search_sessions() nor its underlying index syncs block this call
    for more than a small budget: a cold index warms/catches up on a
    background thread (see session_fts.warm_start / ship_graph.warm_start),
    and a request that lands mid-warm just gets whatever is indexed so far
    plus `indexing: true` rather than waiting tens of seconds.
    """
    hits = _sg.search_sessions(query, limit=limit)
    sids = [h["session_id"] for h in hits if h.get("session_id")]
    session_meta = _session_meta_rows(_sg._get_connection(), sids) if sids else {}
    sdoc = _sdoc_rows(sids) if sids else {}
    results = []
    for sid in sids:
        sm = session_meta.get(sid, {})
        sd = sdoc.get(sid, {})
        results.append({
            "session_id": sid,
            "title": sd.get("title", ""),
            "repo": sm.get("repo", ""),
            "date": sm.get("date", ""),
            "snippet": sd.get("snippet", ""),
        })
    indexing = _sfts.is_indexing() or _sg.is_indexing()
    return {"query": query, "results": results, "indexing": indexing}


def shipped(topic: str) -> dict:
    """GET /api/memory/shipped — is_shipped() contract as-is, with the
    topic echoed back for the CLI/UI to display."""
    result = _sg.is_shipped(topic)
    result["topic"] = topic
    return result


def _resolve_repo_for_path(path: str, repo_hint: str = "") -> tuple[str, str, str]:
    """(repo_name, repo_root, rel_path) for a file path.

    `path` may be absolute or repo-relative. `repo_hint` — a repo name from
    ship_graph.discover_repo_roots(), or that repo's own root path —
    disambiguates a relative path when more than one known repo could
    contain it; without a hint, a relative path is matched against each
    known repo's working tree.
    """
    roots = _sg.discover_repo_roots()
    p = (path or "").strip()

    if repo_hint:
        hint = repo_hint.strip()
        root = roots.get(hint)
        name = hint
        if not root:
            for rname, rpath in roots.items():
                if rpath.rstrip("/") == hint.rstrip("/"):
                    root, name = rpath, rname
                    break
        if root:
            root_s = root.rstrip("/")
            rel = p[len(root_s):].lstrip("/") if p.startswith(root_s) else p.lstrip("/")
            return name, root_s, rel

    if p.startswith("/"):
        best = None
        for name, root in roots.items():
            root_s = root.rstrip("/")
            if p == root_s or p.startswith(root_s + "/"):
                if best is None or len(root_s) > len(best[1]):
                    best = (name, root_s)
        if best:
            name, root_s = best
            return name, root_s, p[len(root_s):].lstrip("/")
        return "", "", p

    for name, root in roots.items():
        if (Path(root) / p).exists():
            return name, root.rstrip("/"), p
    return "", "", p


def _git_file_commits(repo_root: str, rel_path: str, limit: int) -> list[dict]:
    """Live `git log --follow` for one path — the cached `commits` table has
    no path filter and doesn't track renames, so this is a targeted subprocess
    call, not a per-row scan."""
    if not repo_root or not rel_path:
        return []
    try:
        res = subprocess.run(
            ["git", "-C", repo_root, "log", "--follow", f"-{max(1, limit)}",
             "--format=%h\x1f%ct\x1f%s", "--", rel_path],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []
    out = []
    for line in res.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        short_hash, ct, subject = parts[0], parts[1], parts[2]
        ts = float(ct) if ct else 0.0
        out.append({
            "kind": "commit",
            "hash": short_hash,
            "ts": ts,
            "date": time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "",
            "why": subject,
        })
    return out


def _sessions_touching_file(conn: sqlite3.Connection, repo_root: str, rel_path: str,
                             limit: int) -> list[dict]:
    """Sessions whose session_meta.files (already synced) names this path —
    one LIKE-filtered SQL query, then an in-Python exact-match filter over
    just the matched rows (bounded by the LIKE, not by session count)."""
    basename = Path(rel_path).name if rel_path else ""
    if not basename:
        return []
    try:
        cur = conn.execute(
            "SELECT sid, start_ts, end_ts, files FROM session_meta WHERE files LIKE ?",
            (f"%{basename}%",),
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        return []
    abs_candidate = str(Path(repo_root) / rel_path) if repo_root else ""
    out = []
    for sid, start_ts, end_ts, files_json in rows:
        try:
            files = json.loads(files_json) if files_json else []
        except (TypeError, ValueError):
            files = []
        if not any(f == abs_candidate or f == rel_path or f.endswith("/" + rel_path) for f in files):
            continue
        out.append({"sid": sid, "ts": start_ts or end_ts or 0.0})
    out.sort(key=lambda h: h["ts"], reverse=True)
    return out[:limit]


def file_history(path: str, repo: str = "", limit: int = 20) -> dict:
    """GET /api/memory/file-history — sessions and commits that touched
    `path`, newest first, each with a one-line why (commit subject / session
    title)."""
    p = (path or "").strip()
    if not p:
        return {"path": "", "repo": "", "history": []}

    repo_name, repo_root, rel_path = _resolve_repo_for_path(p, repo)
    entries = _git_file_commits(repo_root, rel_path, limit) if repo_root and rel_path else []

    conn = _sg._get_connection()
    _sg._sync_all(conn, force=False)
    sess_hits = _sessions_touching_file(conn, repo_root, rel_path, limit) if rel_path else []
    sids = [h["sid"] for h in sess_hits]
    sdoc = _sdoc_rows(sids) if sids else {}
    for h in sess_hits:
        sd = sdoc.get(h["sid"], {})
        entries.append({
            "kind": "session",
            "session_id": h["sid"],
            "ts": h["ts"],
            "date": time.strftime("%Y-%m-%d", time.localtime(h["ts"])) if h["ts"] else "",
            "why": sd.get("title") or "(untitled session)",
        })

    entries.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return {"path": p, "repo": repo_name, "history": entries[:limit]}


# Words that mark a snippet as recording a DECISION rather than just
# mentioning a topic — "we added X" isn't a decision, "we went with X
# instead of Y" is. Heuristic-only: this is a stand-in backend for
# MEMO-FIX-7's dedicated decision store. _decision_hits() is the seam —
# MEMO-FIX-7 can replace its body with a real store lookup without touching
# decisions()'s signature or the /api/memory/decisions route.
_DECISION_MARKERS = (
    "decided", "decision", "we'll use", "we will use", "going with",
    "went with", "instead of", "rather than", "chose", "chosen",
    "settled on", "agreed to", "opted for", "final call", "let's go with",
)


def _looks_like_decision(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _DECISION_MARKERS)


def _decision_hits(topic: str, limit: int) -> list[dict]:
    """Heuristic decision-shaped filter over search_sessions() hits. See the
    module-level note above _DECISION_MARKERS for the MEMO-FIX-7 hand-off."""
    hits = _sg.search_sessions(topic, limit=max(limit * 3, 20))
    sids = [h["session_id"] for h in hits if h.get("session_id")]
    sdoc = _sdoc_rows(sids) if sids else {}
    out = []
    for sid in sids:
        sd = sdoc.get(sid, {})
        snippet = sd.get("snippet", "")
        if not _looks_like_decision(snippet):
            continue
        out.append({"session_id": sid, "title": sd.get("title", ""), "snippet": snippet})
        if len(out) >= limit:
            break
    return out


def decisions(topic: str, limit: int = 10) -> dict:
    """GET /api/memory/decisions — sessions whose recall snippet reads as a
    decision (`decided`, `instead of`, `went with`, ...) for `topic`.

    Backed by search_sessions() plus a decision-language filter for now;
    MEMO-FIX-7's real decision store is meant to plug into `_decision_hits`
    later without changing this function's contract."""
    t = (topic or "").strip()
    if not t:
        return {"topic": "", "results": []}
    hits = _decision_hits(t, limit)
    sids = [h["session_id"] for h in hits]
    session_meta = _session_meta_rows(_sg._get_connection(), sids) if sids else {}
    results = []
    for h in hits:
        sm = session_meta.get(h["session_id"], {})
        results.append({
            "session_id": h["session_id"],
            "title": h["title"],
            "repo": sm.get("repo", ""),
            "date": sm.get("date", ""),
            "snippet": h["snippet"],
        })
    return {"topic": t, "results": results}
