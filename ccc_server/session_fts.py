# Copyright (c) 2026 Amir Fish. All rights reserved.
# SPDX-License-Identifier: LicenseRef-CCC-Software-License
"""Session-level FTS5 retrieval over Claude Code and Codex transcripts.

Contract:
  search_sessions(query: str, limit: int = 20) -> list of dicts with 'session_id', best first.

Builds and incrementally updates an SQLite FTS5 index persisted on disk
under ~/.claude/command-center/session_fts.sqlite, keyed by transcript
(mtime, size). Never re-parses unchanged files. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Query sanitization reuse from history_search
try:
    from ccc_server.history_search import (
        extract_history_terms,
        _is_explicit_history_fts_query,
        _rewrite_history_query,
    )
except ImportError:
    if "bench_history_search" in sys.modules:
        _bhs = sys.modules["bench_history_search"]
        extract_history_terms = _bhs.extract_history_terms
        _is_explicit_history_fts_query = _bhs._is_explicit_history_fts_query
        _rewrite_history_query = _bhs._rewrite_history_query
    else:
        import importlib.util
        _hs_path = Path(__file__).resolve().parent / "history_search.py"
        _spec = importlib.util.spec_from_file_location("ccc_server_history_search", _hs_path)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        extract_history_terms = _mod.extract_history_terms
        _is_explicit_history_fts_query = _mod._is_explicit_history_fts_query
        _rewrite_history_query = _mod._rewrite_history_query

# Regular expressions matching transcript cataloging
COMMIT_RE = re.compile(r"\[([\w./-]+)(?: \(root-commit\))? ([0-9a-f]{7,12})\] ([^\n\\]{3,160})")
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,11}-\d{1,5})\b")
TICKET_STOP = re.compile(r"^(UTF|SHA|ISO|RFC|CVE|HTTP|TLS|GPT|MD|X|UUID|AES|RSA|P|H|E|W|U|A|B|C)-", re.I)
SCRATCH_RE = re.compile(r"(command-center-scratch|/private/var/|/tmp/|/var/folders/|scratch-|ccc-claude-midstream)", re.I)
CODEX_SID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)

MAX_USER = 30000
MAX_ASSIST = 30000

# BM25 column weights for sdoc(sid, title, prompts, report, body, meta, summary)
# sid is UNINDEXED (col 0 = 0 weight)
BM25_WEIGHTS = (0, 15.0, 2.0, 1.0, 1.0, 4.0, 0.0)
BM25_WEIGHTS_STR = ", ".join(str(w) for w in BM25_WEIGHTS)

_tls = threading.local()
_sync_lock = threading.Lock()
_last_sync_ts = 0.0
_SYNC_TTL = 5.0  # seconds between directory scans


def _get_db_path() -> Path:
    env = os.environ.get("CCC_SESSION_FTS_DB")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "command-center" / "session_fts.sqlite"


def _get_projects_dir() -> Path:
    env = os.environ.get("CCC_PROJECTS_ROOT")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "projects"


def _get_codex_dir() -> Path:
    env = os.environ.get("CCC_CODEX_SESSIONS_ROOT")
    if env:
        return Path(env)
    return Path.home() / ".codex" / "sessions"


def _repo_of(cwd: str) -> str:
    if not cwd:
        return ""
    p = cwd.rstrip("/")
    m = re.search(r"/([^/]+?)(?:-wt-[^/]+)?/\.(?:worktrees|claude/worktrees)/[^/]+", p)
    if m:
        return m.group(1)
    name = Path(p).name
    name = re.sub(r"-wt-.*$", "", name)
    return name


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("text", "input_text", "output_text"):
                    out.append(c.get("text") or "")
    return "\n".join(out)


def _tool_blob(content) -> str:
    out = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                inner = c.get("content")
                out.append(inner if isinstance(inner, str) else _text_of(inner))
    return "\n".join(out)


def _is_real_prompt(t: str) -> bool:
    s = t.lstrip()
    if not s:
        return False
    if s.startswith("<") or s.startswith("Caveat:") or s.startswith("[Request interrupted"):
        return False
    if s.startswith("This session is being continued from a previous conversation"):
        return False
    if s.startswith("# AGENTS.md instructions") or s.startswith("# Global agent guidance"):
        return False
    return True


def _ts(s) -> float | None:
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _is_scratch(path: str, cwd: str) -> int:
    if os.environ.get("CCC_SESSION_FTS_ALLOW_SCRATCH") == "1":
        return 0
    return 1 if (SCRATCH_RE.search(path) or SCRATCH_RE.search(cwd or "")) else 0


def _finish(sid, engine, path, cwd, title, users, assists, tools, files, ts0, ts1):
    first = users[0] if users else ""
    user_text = "\n---\n".join(u[:4000] for u in users)[:MAX_USER]
    joined = "\n---\n".join(a[:3000] for a in assists)
    assistant_text = (
        joined if len(joined) <= MAX_ASSIST
        else joined[: MAX_ASSIST // 3] + "\n…\n" + joined[-2 * MAX_ASSIST // 3:]
    )
    final = "\n---\n".join(assists[-3:])[:12000]
    blob = "\n".join(tools)
    commits = {}
    for m in COMMIT_RE.finditer(blob):
        commits[m.group(2)] = {"branch": m.group(1), "subject": m.group(3).strip()}
    alltext = user_text + "\n" + assistant_text
    tickets = sorted({t for t in TICKET_RE.findall(alltext) if not TICKET_STOP.match(t)})
    st = os.stat(path)

    meta_parts = []
    repo = _repo_of(cwd)
    if repo:
        meta_parts.append(repo)
    if tickets:
        meta_parts.append(" ".join(tickets))
    if commits:
        meta_parts.append(" ".join(v["subject"] for v in commits.values()))
    if files:
        meta_parts.append(" ".join(Path(f).name for f in sorted(files)[:60]))
    meta = " ".join(meta_parts)

    t = (title or "").strip()
    if t.lower().startswith("prewarm-") or len(t) < 4:
        t = " ".join(first.split())[:200]

    is_scratch = _is_scratch(path, cwd)

    return {
        "sid": sid,
        "engine": engine,
        "path": path,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "n_user": len(users),
        "scratch": is_scratch,
        "title": t,
        "user_text": user_text,
        "final_text": final,
        "assistant_text": assistant_text,
        "meta": meta,
        "summary": "",
    }


def parse_claude(path: str) -> dict | None:
    sid = Path(path).stem
    cwd = ""
    title_custom = title_ai = ""
    users, assists, tools = [], [], []
    files = set()
    ts0 = ts1 = None
    with open(path, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            if t == "custom-title":
                title_custom = d.get("customTitle") or title_custom
                continue
            if t == "ai-title":
                title_ai = d.get("aiTitle") or title_ai
                continue
            if t not in ("user", "assistant"):
                continue
            if d.get("isSidechain"):
                continue
            cwd = cwd or d.get("cwd") or ""
            ts = _ts(d.get("timestamp"))
            if ts:
                ts0 = ts if ts0 is None else min(ts0, ts)
                ts1 = ts if ts1 is None else max(ts1, ts)
            msg = d.get("message") or {}
            content = msg.get("content")
            if t == "user":
                if d.get("isMeta"):
                    continue
                txt = _text_of(content)
                if txt and _is_real_prompt(txt):
                    users.append(txt)
                tb = _tool_blob(content)
                if tb:
                    tools.append(tb[:4000])
            else:
                txt = _text_of(content)
                if txt:
                    assists.append(txt)
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            inp = c.get("input") or {}
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            if fp and c.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                                files.add(fp)
                            cmd = inp.get("command")
                            if isinstance(cmd, str) and "git commit" in cmd:
                                tools.append(cmd[:1500])
    return _finish(sid, "claude", path, cwd, title_custom or title_ai, users, assists, tools, files, ts0, ts1)


def parse_codex(path: str) -> dict | None:
    sid = ""
    m = CODEX_SID_RE.search(path)
    if m:
        sid = m.group(1)
    cwd = ""
    users, assists, tools, finals = [], [], [], []
    files = set()
    ts0 = ts1 = None
    with open(path, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            p = d.get("payload") or {}
            ts = _ts(d.get("timestamp"))
            if ts:
                ts0 = ts if ts0 is None else min(ts0, ts)
                ts1 = ts if ts1 is None else max(ts1, ts)
            if t == "session_meta":
                sid = p.get("id") or p.get("session_id") or sid
                cwd = p.get("cwd") or cwd
                continue
            if t == "turn_context" and not cwd:
                cwd = p.get("cwd") or cwd
            pt = p.get("type")
            if t == "response_item" and pt == "message":
                txt = _text_of(p.get("content"))
                role = p.get("role")
                if role == "user" and _is_real_prompt(txt):
                    users.append(txt)
                elif role == "assistant" and txt:
                    assists.append(txt)
            elif t == "response_item" and pt in ("function_call_output", "custom_tool_call_output"):
                out = p.get("output")
                tb = out if isinstance(out, str) else _text_of(out)
                if tb and ("git" in tb or "] " in tb):
                    tools.append(tb[:4000])
            elif t == "response_item" and pt in ("function_call", "custom_tool_call"):
                arg = p.get("arguments") or p.get("input") or ""
                if isinstance(arg, str) and "git commit" in arg:
                    tools.append(arg[:1500])
                for fm in re.finditer(r"\*\*\* (?:Update|Add) File: ([^\n\\]+)", arg if isinstance(arg, str) else ""):
                    files.add(fm.group(1).strip())
            elif t == "event_msg" and pt == "task_complete":
                if p.get("last_agent_message"):
                    finals.append(p["last_agent_message"])
    if not sid:
        return None
    r = _finish(sid, "codex", path, cwd, "", users, assists, tools, files, ts0, ts1)
    if r and finals:
        r["final_text"] = "\n---\n".join(finals[-3:])[:12000]
    return r


def _parse_file(args: tuple[str, str]) -> dict | None:
    engine, path = args
    try:
        return parse_claude(path) if engine == "claude" else parse_codex(path)
    except Exception:
        return None


def _candidate_files(days: float | None = None) -> list[tuple[str, str, float, int]]:
    """Enumerate candidate session transcript files with their mtime and size."""
    if days is None:
        try:
            days = float(os.environ.get("CCC_SESSION_FTS_DAYS", os.environ.get("BENCH_DAYS", "45")))
        except ValueError:
            days = 45.0
    cutoff = (time.time() - days * 86400) if days and days > 0 else 0.0

    out = []
    projects_dir = _get_projects_dir()
    if projects_dir.exists():
        for p in projects_dir.glob("*/*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > 0 and st.st_mtime >= cutoff:
                out.append(("claude", str(p), st.st_mtime, st.st_size))

    codex_dir = _get_codex_dir()
    if codex_dir.exists():
        for p in codex_dir.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > 0 and st.st_mtime >= cutoff:
                out.append(("codex", str(p), st.st_mtime, st.st_size))
    return out


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS sdoc USING fts5(
            sid UNINDEXED,
            title,
            prompts,
            report,
            body,
            meta,
            summary,
            tokenize='porter unicode61'
        );
        CREATE TABLE IF NOT EXISTS file_cache (
            path TEXT PRIMARY KEY,
            sid TEXT,
            mtime REAL,
            size INTEGER,
            indexed INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_file_cache_sid ON file_cache(sid);
    """)
    conn.commit()


def _get_connection() -> sqlite3.Connection:
    if not hasattr(_tls, "conn") or _tls.conn is None:
        db_path = _get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _tls.conn = sqlite3.connect(str(db_path), timeout=30.0)
    return _tls.conn


def _sync_index(conn: sqlite3.Connection, force: bool = False) -> None:
    global _last_sync_ts
    now = time.time()
    if not force and (now - _last_sync_ts < _SYNC_TTL):
        return

    with _sync_lock:
        if not force and (time.time() - _last_sync_ts < _SYNC_TTL):
            return

        _init_db(conn)

        cur = conn.execute("SELECT path, sid, mtime, size, indexed FROM file_cache")
        have = {r[0]: (r[1], r[2], r[3], r[4]) for r in cur.fetchall()}

        files = _candidate_files()
        current_paths = {p for _, p, _, _ in files}

        todo = []
        for eng, path, mt, sz in files:
            cached = have.get(path)
            if cached is None or cached[1] != mt or cached[2] != sz:
                todo.append((eng, path, mt, sz))

        deleted_paths = [p for p in have if p not in current_paths]

        if not todo and not deleted_paths:
            _last_sync_ts = time.time()
            return

        parsed_results = []
        if todo:
            items_to_parse = [(eng, path) for eng, path, _, _ in todo]
            if len(items_to_parse) > 16:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    parsed_results = list(ex.map(_parse_file, items_to_parse, chunksize=16))
            else:
                parsed_results = [_parse_file(item) for item in items_to_parse]

        with conn:
            for (eng, path, mt, sz), r in zip(todo, parsed_results):
                old_cached = have.get(path)
                old_sid = old_cached[0] if old_cached else None

                if old_sid:
                    conn.execute("DELETE FROM sdoc WHERE sid = ?", (old_sid,))

                if r and r.get("sid"):
                    sid = r["sid"]
                    conn.execute("DELETE FROM sdoc WHERE sid = ?", (sid,))
                    if r.get("scratch", 0) == 0 and r.get("n_user", 0) > 0:
                        conn.execute(
                            "INSERT INTO sdoc VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                sid,
                                r["title"],
                                r["user_text"],
                                r["final_text"],
                                r["assistant_text"],
                                r["meta"],
                                r["summary"],
                            ),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO file_cache (path, sid, mtime, size, indexed) VALUES (?, ?, ?, ?, 1)",
                            (path, sid, mt, sz),
                        )
                    else:
                        conn.execute(
                            "INSERT OR REPLACE INTO file_cache (path, sid, mtime, size, indexed) VALUES (?, ?, ?, ?, 0)",
                            (path, sid, mt, sz),
                        )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO file_cache (path, sid, mtime, size, indexed) VALUES (?, ?, ?, ?, 0)",
                        (path, "", mt, sz),
                    )

            for p in deleted_paths:
                old_sid = have[p][0]
                if old_sid:
                    conn.execute("DELETE FROM sdoc WHERE sid = ?", (old_sid,))
                conn.execute("DELETE FROM file_cache WHERE path = ?", (p,))

        _last_sync_ts = time.time()


def search_sessions(query: str, limit: int = 20, force_refresh: bool = False) -> list[dict]:
    """Search indexed sessions with BM25.

    Returns a list of dicts with 'session_id', ranked best first.
    """
    q = (query or "").strip()
    if not q:
        return []

    conn = _get_connection()
    _sync_index(conn, force=force_refresh)

    if _is_explicit_history_fts_query(q):
        try:
            cur = conn.execute(
                f"SELECT sid, bm25(sdoc, {BM25_WEIGHTS_STR}) FROM sdoc WHERE sdoc MATCH ? ORDER BY bm25(sdoc, {BM25_WEIGHTS_STR}) LIMIT ?",
                (q, limit),
            )
            return [{"session_id": r[0], "score": r[1]} for r in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

    terms = extract_history_terms(q, max_terms=25)
    if not terms:
        return []

    and_q = " AND ".join(f'"{t}"' for t in terms)
    or_q = " OR ".join(f'"{t}"' for t in terms)

    scores: dict[str, float] = {}
    try:
        cur = conn.execute(
            f"SELECT sid, bm25(sdoc, {BM25_WEIGHTS_STR}) FROM sdoc WHERE sdoc MATCH ? ORDER BY bm25(sdoc, {BM25_WEIGHTS_STR}) LIMIT ?",
            (or_q, max(limit * 2, 50)),
        )
        for sid, score in cur.fetchall():
            scores[sid] = score
    except sqlite3.OperationalError:
        pass

    if and_q and and_q != or_q and len(terms) > 1:
        try:
            cur = conn.execute(
                f"SELECT sid, bm25(sdoc, {BM25_WEIGHTS_STR}) FROM sdoc WHERE sdoc MATCH ? ORDER BY bm25(sdoc, {BM25_WEIGHTS_STR}) LIMIT ?",
                (and_q, max(limit * 2, 50)),
            )
            for sid, score in cur.fetchall():
                if sid in scores:
                    scores[sid] = min(scores[sid], score)
                else:
                    scores[sid] = score
        except sqlite3.OperationalError:
            pass

    sorted_sids = sorted(scores, key=lambda s: scores[s])
    return [{"session_id": sid, "score": scores[sid]} for sid in sorted_sids[:limit]]
