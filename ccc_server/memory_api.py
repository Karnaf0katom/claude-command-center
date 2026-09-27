# Copyright (c) 2026 Amir Fish. All rights reserved.
# SPDX-License-Identifier: LicenseRef-CCC-Software-License
"""API-layer glue for ccc_server.ship_graph (is_shipped, search_sessions).

Both functions already exist and are fully working — until now the only
caller was the ~/rnd/ccc-memory benchmark, so no running agent could reach
them. This module is the thin read-only surface that powers:

  - GET /api/memory/recall?q=&limit=   -> recall()
  - GET /api/memory/shipped?topic=     -> shipped()
  - `ccc recall "<query>"` / `ccc shipped "<topic>"` (./ccc CLI)

No new retrieval logic lives here. search_sessions() only returns
session_id, so recall() enriches each hit with title/repo/date/snippet read
from two tables that ship_graph.search_sessions() has already synced in the
same call (ship_graph's own session_meta table for repo/date, session_fts's
sdoc table for title/snippet) — two batched SQL queries total, no per-row
file reads, no subprocess.
"""

from __future__ import annotations

import sqlite3
import time

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
    with title/repo/date/snippet from already-synced index state."""
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
    return {"query": query, "results": results}


def shipped(topic: str) -> dict:
    """GET /api/memory/shipped — is_shipped() contract as-is, with the
    topic echoed back for the CLI/UI to display."""
    result = _sg.is_shipped(topic)
    result["topic"] = topic
    return result
