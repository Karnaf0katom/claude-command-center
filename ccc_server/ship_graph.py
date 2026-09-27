# Copyright (c) 2026 Amir Fish. All rights reserved.
# SPDX-License-Identifier: LicenseRef-CCC-Software-License
"""Commit and ticket graph linking sessions, git commits, and WatchTower tickets.

Exposes:
  - is_shipped(topic: str) -> dict:
      Answers 'did we ship X?' with:
      {'shipped': bool, 'confidence': float, 'evidence': [{'repo', 'commit', 'subject', 'session_id'}], 'tickets': [...]}
  - search_sessions(query: str, limit: int = 20) -> list[dict]:
      Re-ranks/augments ccc_server/session_fts.search_sessions results using
      commit matches and ticket expansion over the graph.

Persists index on disk under the CCC state directory (ship_graph.sqlite).
Refreshes incrementally:
  - Per-repo HEAD (only runs git log when HEAD changes)
  - Per-transcript (mtime, size)
  - WatchTower tickets (from ~/.local/share/watchtower/queues.db)
Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_base_search_sessions = None


def _get_base_search_sessions():
    global _base_search_sessions
    if _base_search_sessions is not None:
        return _base_search_sessions
    sfts_path = Path(__file__).resolve().parent / "session_fts.py"
    if sfts_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("ccc_server_session_fts", sfts_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["ccc_server_session_fts"] = mod
                spec.loader.exec_module(mod)
                if hasattr(mod, "search_sessions"):
                    _base_search_sessions = mod.search_sessions
                    return _base_search_sessions
        except Exception:
            pass
    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from ccc_server.session_fts import search_sessions
        _base_search_sessions = search_sessions
    except Exception:
        _base_search_sessions = lambda q, limit=20: []
    return _base_search_sessions

COMMIT_RE = re.compile(r"\[([\w./-]+)(?: \(root-commit\))? ([0-9a-f]{7,12})\] ([^\n\\]{3,160})")
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,11}-\d{1,5})\b")
TICKET_STOP = re.compile(r"^(UTF|SHA|ISO|RFC|CVE|HTTP|TLS|GPT|MD|X|UUID|AES|RSA|P|H|E|W|U|A|B|C)-", re.I)
SCRATCH_RE = re.compile(r"(command-center-scratch|/private/var/|/tmp/|/var/folders/|scratch-|ccc-claude-midstream)", re.I)
CODEX_SID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+#-]*")

STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below
between both but by can could did do does doing done down during each few for from further had has have
having he her here hers him his how i if in into is it its itself just let lets me more most my myself no
nor not now of off on once only or other our ours out over own same she should so some such than that the
their them then there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours session sessions chat conversation thread find
where remember recall worked working work done did do we i me my the that which built build already
have has is it there any way something thing things one ones ship shipped shipping feature task
ticket pr pull request commit commits repo repository support implemented implement
ever actually someone onto doesn don didn need needs try trying want wants sure make makes know knows yet
good get still used use uses using somewhere anywhere anybody somebody anyone
""".split())

SYNONYMS = {
    "rid": ["remove", "drop", "delete"],
    "remove": ["rid", "drop", "delete", "strip", "clean"],
    "delete": ["remove", "drop", "rid"],
    "drop": ["remove", "delete", "rid"],
    "fix": ["resolve", "repair", "patch"],
    "fixed": ["resolve", "resolved", "repair", "repaired"],
    "resolve": ["fix", "repair"],
    "add": ["implement", "support", "introduce", "create"],
    "support": ["implement", "add"],
    "implement": ["support", "add"],
    "stop": ["prevent", "block", "avoid"],
    "prevent": ["stop", "block", "avoid"],
    "block": ["prevent", "stop", "avoid"],
    "hide": ["conceal", "suppress", "mask"],
    "show": ["display", "reveal", "render", "expose"],
    "display": ["show", "render"],
    "autoupdate": ["auto", "update"],
    "fastforward": ["fast", "forward"],
    "reinstall": ["re", "install"],
}

IRREGULAR_VERBS = {
    "came": "come", "went": "go", "gone": "go", "ran": "run",
    "wrote": "write", "written": "write", "broke": "break", "broken": "break",
    "hid": "hide", "hidden": "hide", "chose": "choose", "chosen": "choose",
    "sent": "send", "spoke": "speak", "spoken": "speak", "gave": "give",
    "given": "give", "took": "take", "taken": "take", "made": "make",
    "built": "build", "bought": "buy", "brought": "bring", "caught": "catch",
    "found": "find", "held": "hold", "kept": "keep", "lost": "lose",
    "met": "meet", "paid": "pay", "saw": "see", "seen": "see",
    "sold": "sell", "told": "tell", "won": "win", "left": "leave",
    "felt": "feel", "began": "begin", "begun": "begin", "split": "split",
}

_stem_cache: dict[str, str] = {}
_stem_lock = threading.Lock()
_tls = threading.local()
_sync_lock = threading.Lock()
_last_sync_ts = 0.0
_SYNC_TTL = 5.0  # seconds between freshness checks


def _ccc_dir() -> Path:
    cc_name = "command-center"
    return Path.home() / ".claude" / cc_name


def _get_db_path() -> Path:
    env = os.environ.get("CCC_SHIP_GRAPH_DB")
    if env:
        return Path(env)
    return _ccc_dir() / "ship_graph.sqlite"


def _get_wt_db_path() -> Path:
    env = os.environ.get("WATCHTOWER_DB")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "watchtower" / "queues.db"


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


def _get_days() -> float:
    try:
        return float(os.environ.get("CCC_SHIP_GRAPH_DAYS", os.environ.get("BENCH_DAYS", "45")))
    except ValueError:
        return 45.0


def _stem(w: str) -> str:
    """Normalize English word suffixes using Porter stemmer and irregular verbs."""
    w = (w or "").lower()
    w = IRREGULAR_VERBS.get(w, w)
    if len(w) <= 2:
        return w
    cached = _stem_cache.get(w)
    if cached is not None:
        return cached

    w_clean = re.sub(r"[^a-z0-9]", "", w)
    if len(w_clean) <= 2:
        return w_clean

    with _stem_lock:
        cached = _stem_cache.get(w)
        if cached is not None:
            return cached
        if not hasattr(_tls, "_stem_conn") or _tls._stem_conn is None:
            _tls._stem_conn = sqlite3.connect(":memory:")
            _tls._stem_conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _st USING fts5(x, tokenize='porter unicode61')")
            _tls._stem_conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _sv USING fts5vocab(_st, col)")
        _tls._stem_conn.execute("INSERT INTO _st VALUES (?)", (w_clean,))
        row = _tls._stem_conn.execute("SELECT term FROM _sv").fetchone()
        res = row[0] if row else w_clean
        _tls._stem_conn.execute("DELETE FROM _st")
        _stem_cache[w] = res
        return res


def extract_terms(q: str) -> list[str]:
    """Extract informative lowercase terms, splitting punctuation and filtering stopwords."""
    out = []
    seen = set()
    words = re.findall(r"[A-Za-z0-9]+", q)
    for w in words:
        w_lower = w.lower()
        if len(w_lower) < 2 or w_lower in STOPWORDS or w_lower in seen:
            continue
        seen.add(w_lower)
        out.append(w_lower)
    return out


def fts_query(terms: list[str]) -> str:
    """Format sanitized terms as an OR FTS5 MATCH string."""
    parts = []
    for t in terms:
        t_clean = t.lower()
        if t_clean not in STOPWORDS and len(t_clean) > 1:
            parts.append(f'"{t_clean}"')
            for syn in SYNONYMS.get(t_clean, []):
                parts.append(f'"{syn}"')
    for i in range(len(terms) - 1):
        combined = terms[i].lower() + terms[i+1].lower()
        if len(combined) <= 24 and combined not in STOPWORDS:
            parts.append(f'"{combined}"')
    return " OR ".join(dict.fromkeys(parts))


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


def discover_repo_roots() -> dict[str, str]:
    """Discover all known repo paths mapped by repo name."""
    roots: dict[str, str] = {}

    def add_root(p_raw: str | Path) -> None:
        try:
            p = Path(p_raw).expanduser().resolve()
        except Exception:
            return
        if not (p / ".git").exists():
            return
        if re.search(r"-wt-", p.name):
            return
        if p.name not in roots:
            roots[p.name] = str(p)

    # 0. Environment override
    env_repos = os.environ.get("CCC_SHIP_GRAPH_REPOS", os.environ.get("CCC_KNOWN_REPOS"))
    if env_repos:
        for r_path in env_repos.split(os.pathsep):
            if r_path.strip():
                add_root(r_path.strip())
        return roots

    # 1. Config files from command-center
    for fn in ["custom-repos.txt", "recent-repos.txt"]:
        p = _ccc_dir() / fn
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        add_root(line)
            except Exception:
                pass

    # 2. Registry
    reg = _ccc_dir() / "registry.json"
    if reg.exists():
        try:
            for item in json.loads(reg.read_text(encoding="utf-8")):
                if isinstance(item, dict) and "install_path" in item:
                    add_root(item["install_path"])
        except Exception:
            pass

    # 3. Conventional directories under HOME, Apps, dev
    for parent in [Path.home() / "Apps", Path.home(), Path.home() / "dev", Path.home() / "dev" / "tools"]:
        if parent.exists():
            try:
                for child in parent.iterdir():
                    if child.is_dir() and (child / ".git").exists():
                        add_root(child)
            except Exception:
                pass

    return roots


def _get_repo_head_fast(path_str: str) -> str:
    """Quickly read repo's HEAD commit SHA without spawning subprocess if possible."""
    git_entry = Path(path_str) / ".git"
    if git_entry.is_file():
        try:
            txt = git_entry.read_text(encoding="utf-8").strip()
            if txt.startswith("gitdir:"):
                p = Path(txt.split(":", 1)[1].strip())
                if not p.is_absolute():
                    p = (git_entry.parent / p).resolve()
            else:
                p = git_entry
        except Exception:
            p = git_entry
    else:
        p = git_entry

    head_file = p / "HEAD"
    if not head_file.exists():
        return ""
    try:
        content = head_file.read_text(encoding="utf-8").strip()
        if not content.startswith("ref:"):
            if re.match(r"^[0-9a-f]{40}$", content):
                return content
        else:
            ref_path = content.split(":", 1)[1].strip()
            target = p / ref_path
            if target.exists():
                return target.read_text(encoding="utf-8").strip()
            # Check commondir if worktree
            commondir_file = p / "commondir"
            common_p = p
            if commondir_file.exists():
                cd_txt = commondir_file.read_text(encoding="utf-8").strip()
                common_p = (p / cd_txt).resolve()
                target_c = common_p / ref_path
                if target_c.exists():
                    return target_c.read_text(encoding="utf-8").strip()
            # Check packed-refs in p or common_p
            for base in (p, common_p):
                packed = base / "packed-refs"
                if packed.exists():
                    for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if line.startswith("#") or line.startswith("^"):
                            continue
                        parts = line.split()
                        if len(parts) >= 2 and parts[1] == ref_path:
                            return parts[0]
    except Exception:
        pass
    return ""


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            val TEXT
        );

        CREATE TABLE IF NOT EXISTS repos (
            path TEXT PRIMARY KEY,
            name TEXT,
            head_sha TEXT,
            indexed_at REAL
        );

        CREATE TABLE IF NOT EXISTS commits (
            commit_id TEXT PRIMARY KEY,
            repo TEXT,
            hash TEXT,
            short_hash TEXT,
            ts REAL,
            subject TEXT,
            body TEXT,
            files TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_commits_hash ON commits(hash);
        CREATE INDEX IF NOT EXISTS idx_commits_short ON commits(short_hash);
        CREATE INDEX IF NOT EXISTS idx_commits_repo ON commits(repo);

        CREATE VIRTUAL TABLE IF NOT EXISTS commits_fts USING fts5(
            commit_id UNINDEXED,
            repo UNINDEXED,
            hash UNINDEXED,
            short_hash UNINDEXED,
            subject,
            body,
            files,
            tokenize='porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS tickets (
            ref TEXT PRIMARY KEY,
            project TEXT,
            number INTEGER,
            status TEXT,
            title TEXT,
            text TEXT,
            commit_sha TEXT,
            repo_path TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_commit ON tickets(commit_sha);
        CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

        CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5(
            ref UNINDEXED,
            project UNINDEXED,
            status UNINDEXED,
            commit_sha UNINDEXED,
            title,
            text,
            tokenize='porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS edges (
            src TEXT,
            dst TEXT,
            kind TEXT,
            w REAL
        );
        CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(src);
        CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(dst);

        CREATE TABLE IF NOT EXISTS transcripts (
            path TEXT PRIMARY KEY,
            sid TEXT,
            mtime REAL,
            size INTEGER,
            indexed INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_transcripts_sid ON transcripts(sid);

        CREATE TABLE IF NOT EXISTS session_meta (
            sid TEXT PRIMARY KEY,
            repo TEXT,
            cwd TEXT,
            start_ts REAL,
            end_ts REAL,
            tickets TEXT,
            commits TEXT,
            files TEXT
        );
    """)
    conn.commit()


def _get_connection() -> sqlite3.Connection:
    if not hasattr(_tls, "conn") or _tls.conn is None:
        db_path = _get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _tls.conn = sqlite3.connect(str(db_path), timeout=30.0)
    return _tls.conn


def _sync_git_repos(conn: sqlite3.Connection, roots: dict[str, str], days: float) -> None:
    cur = conn.execute("SELECT path, head_sha FROM repos")
    have_heads = dict(cur.fetchall())

    for repo_name, repo_path in roots.items():
        current_head = _get_repo_head_fast(repo_path)
        if not current_head:
            continue
        if have_heads.get(repo_path) == current_head:
            continue

        # HEAD changed or new repo: batch git log once
        branches = ["HEAD"]
        for b in ["next", "main", "master"]:
            p_ref = Path(repo_path) / ".git" / "refs" / "heads" / b
            p_rem = Path(repo_path) / ".git" / "refs" / "remotes" / "origin" / b
            if p_ref.exists() or p_rem.exists():
                branches.append(b)

        args = ["git", "-C", repo_path, "log"] + list(dict.fromkeys(branches))
        if days > 0:
            args.append(f"--since={int(days)}.days")
        args += ["--name-only", "--format=\x1e%H\x1f%h\x1f%ct\x1f%s\x1f%b\x1f"]

        try:
            res = subprocess.run(args, capture_output=True, text=True, timeout=30)
            stdout = res.stdout
        except Exception:
            continue

        records = stdout.split("\x1e")[1:]
        commits_to_insert = []
        fts_to_insert = []
        ticket_edges = []

        for rec in records:
            parts = rec.split("\x1f")
            if len(parts) < 5:
                continue
            h, sh, ct, subj, body = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip()
            files_str = parts[5].strip() if len(parts) > 5 else ""
            files = [f.strip() for f in files_str.splitlines() if f.strip()][:100]
            cid = f"{repo_name}:{h}"
            ts = float(ct) if ct else 0.0
            files_blob = " ".join(files)

            commits_to_insert.append((cid, repo_name, h, sh, ts, subj, body, files_blob))
            fts_to_insert.append((cid, repo_name, h, sh, subj, body, files_blob))

            # Extract tickets mentioned in commit
            mentioned_tickets = {t for t in TICKET_RE.findall(f"{subj}\n{body}") if not TICKET_STOP.match(t)}
            for t in mentioned_tickets:
                ticket_edges.append((f"ticket:{t}", f"commit:{h}", "mentioned", 1.0))
                ticket_edges.append((f"ticket:{t}", f"commit:{sh}", "mentioned", 1.0))

        with conn:
            # Delete old commits for this repo
            conn.execute("DELETE FROM commits WHERE repo = ?", (repo_name,))
            conn.execute("DELETE FROM commits_fts WHERE repo = ?", (repo_name,))
            conn.executemany(
                "INSERT OR REPLACE INTO commits VALUES (?,?,?,?,?,?,?,?)",
                commits_to_insert,
            )
            conn.executemany(
                "INSERT INTO commits_fts VALUES (?,?,?,?,?,?,?)",
                fts_to_insert,
            )
            conn.executemany(
                "INSERT INTO edges VALUES (?,?,?,?)",
                ticket_edges,
            )
            conn.execute(
                "INSERT OR REPLACE INTO repos VALUES (?,?,?,?)",
                (repo_path, repo_name, current_head, time.time()),
            )


def _clean_ticket_title(title: str, text: str) -> str:
    t = (title or "").strip()
    if not t or t.startswith("reporter=") or "Command Center for Claude" in t or "BookYourMat — Studio Scheduling" in t or t == "Flow — CCC":
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        for line in lines:
            if not line.startswith("WAVE ") and not line.startswith("DEPENDS:") and not line.startswith("ENGINE:") and not line.startswith("REVIEW:"):
                return line[:140]
        return ""
    return t


def _sync_watchtower(conn: sqlite3.Connection) -> None:
    wt_db = _get_wt_db_path()
    if not wt_db.exists():
        return
    try:
        st = wt_db.stat()
        current_mtime = str(st.st_mtime)
    except OSError:
        return

    cur = conn.execute("SELECT val FROM meta WHERE key = 'wt_db_mtime'")
    row = cur.fetchone()
    cur_v = conn.execute("SELECT val FROM meta WHERE key = 'wt_clean_v2'")
    row_v = cur_v.fetchone()
    if row and row[0] == current_mtime and row_v and row_v[0] == "1":
        return

    try:
        wt_conn = sqlite3.connect(f"file:{wt_db}?mode=ro", uri=True)
        items = wt_conn.execute("SELECT ref, project, number, status, updated_at, item_json FROM items").fetchall()
        wt_conn.close()
    except Exception:
        return

    tickets_rows = []
    fts_rows = []
    edges_rows = []

    for ref, project, number, status, updated_at, item_json in items:
        try:
            d = json.loads(item_json)
        except Exception:
            continue
        raw_title = d.get("title") or ""
        text = d.get("text") or d.get("note") or ""
        title = _clean_ticket_title(raw_title, text)
        repo_path = d.get("repo_path") or ""

        # Extract commit sha if closed
        commit_sha = ""
        res = d.get("resolution")
        if isinstance(res, dict):
            commit_sha = res.get("commit") or ""
        elif isinstance(res, str):
            m = re.search(r"\b([0-9a-f]{7,40})\b", res)
            if m:
                commit_sha = m.group(1)

        if not commit_sha:
            for ev in d.get("history") or []:
                if isinstance(ev, dict) and ev.get("event") == "close":
                    r2 = ev.get("resolution")
                    if isinstance(r2, dict) and r2.get("commit"):
                        commit_sha = r2["commit"]
                        break
                    elif isinstance(r2, str):
                        m = re.search(r"\b([0-9a-f]{7,40})\b", r2)
                        if m:
                            commit_sha = m.group(1)
                            break

        tickets_rows.append((ref, project, number, status, title, text, commit_sha, repo_path, updated_at))
        fts_rows.append((ref, project, status, commit_sha, title, text))
        if commit_sha:
            edges_rows.append((f"ticket:{ref}", f"commit:{commit_sha}", "resolves", 1.0))

    with conn:
        conn.execute("DELETE FROM tickets")
        conn.execute("DELETE FROM tickets_fts")
        conn.execute("DELETE FROM edges WHERE kind = 'resolves'")
        conn.executemany("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?)", tickets_rows)
        conn.executemany("INSERT INTO tickets_fts VALUES (?,?,?,?,?,?)", fts_rows)
        conn.executemany("INSERT INTO edges VALUES (?,?,?,?)", edges_rows)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('wt_db_mtime', ?)", (current_mtime,))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('wt_clean_v2', '1')")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("text", "input_text", "output_text"):
                    out.append(c.get("text") or "")
    return "\n".join(out)


def _tool_blob(content: Any) -> str:
    out = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                inner = c.get("content")
                out.append(inner if isinstance(inner, str) else _text_of(inner))
    return "\n".join(out)


def _ts(s: Any) -> float | None:
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _parse_transcript(args: tuple[str, str]) -> dict | None:
    engine, path = args
    sid = Path(path).stem
    cwd = ""
    tools = []
    alltext = []
    files = set()
    ts0 = ts1 = None

    if engine == "claude":
        try:
            with open(path, "rb") as f:
                for raw in f:
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    t = d.get("type")
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
                    txt = _text_of(content)
                    if txt:
                        alltext.append(txt[:1000])
                    if t == "user":
                        tb = _tool_blob(content)
                        if tb:
                            tools.append(tb[:2000])
                    else:
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "tool_use":
                                    inp = c.get("input") or {}
                                    fp = inp.get("file_path") or inp.get("notebook_path")
                                    if fp and c.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                                        files.add(fp)
                                    cmd = inp.get("command")
                                    if isinstance(cmd, str) and "git commit" in cmd:
                                        tools.append(cmd[:1000])
        except Exception:
            return None
    else:  # codex
        m = CODEX_SID_RE.search(path)
        if m:
            sid = m.group(1)
        try:
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
                        if txt:
                            alltext.append(txt[:1000])
                    elif t == "response_item" and pt in ("function_call_output", "custom_tool_call_output"):
                        out = p.get("output")
                        tb = out if isinstance(out, str) else _text_of(out)
                        if tb and ("git" in tb or "] " in tb):
                            tools.append(tb[:2000])
                    elif t == "response_item" and pt in ("function_call", "custom_tool_call"):
                        arg = p.get("arguments") or p.get("input") or ""
                        if isinstance(arg, str) and "git commit" in arg:
                            tools.append(arg[:1000])
                        for fm in re.finditer(r"\*\*\* (?:Update|Add) File: ([^\n\\]+)", arg if isinstance(arg, str) else ""):
                            files.add(fm.group(1).strip())
        except Exception:
            return None

    if not sid:
        return None

    blob = "\n".join(tools)
    commits = {}
    for m_com in COMMIT_RE.finditer(blob):
        commits[m_com.group(2)] = {"branch": m_com.group(1), "subject": m_com.group(3).strip()}

    full_text = "\n".join(alltext)
    tickets = sorted({t for t in TICKET_RE.findall(full_text) if not TICKET_STOP.match(t)})

    return {
        "sid": sid,
        "repo": _repo_of(cwd),
        "cwd": cwd,
        "start_ts": ts0,
        "end_ts": ts1,
        "commits": commits,
        "tickets": tickets,
        "files": sorted(files)[:200],
    }


def _candidate_transcript_files(days: float) -> list[tuple[str, str, float, int]]:
    cutoff = (time.time() - days * 86400) if days > 0 else 0.0
    out = []
    p_dir = _get_projects_dir()
    if p_dir.exists():
        for p in p_dir.glob("*/*.jsonl"):
            try:
                st = p.stat()
                if st.st_size > 0 and st.st_mtime >= cutoff:
                    out.append(("claude", str(p), st.st_mtime, st.st_size))
            except OSError:
                continue

    c_dir = _get_codex_dir()
    if c_dir.exists():
        for p in c_dir.rglob("*.jsonl"):
            try:
                st = p.stat()
                if st.st_size > 0 and st.st_mtime >= cutoff:
                    out.append(("codex", str(p), st.st_mtime, st.st_size))
            except OSError:
                continue
    return out


def _sync_transcripts(conn: sqlite3.Connection, days: float) -> None:
    cur = conn.execute("SELECT path, mtime, size FROM transcripts")
    have = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    candidates = _candidate_transcript_files(days)
    todo = [(eng, p, mt, sz) for eng, p, mt, sz in candidates if have.get(p) != (mt, sz)]
    if not todo:
        return

    items_to_parse = [(eng, p) for eng, p, _, _ in todo]
    if len(items_to_parse) > 16:
        with ThreadPoolExecutor(max_workers=8) as ex:
            parsed = list(ex.map(_parse_transcript, items_to_parse, chunksize=16))
    else:
        parsed = [_parse_transcript(item) for item in items_to_parse]

    # Pre-index commits by short hash and by repo for fast window matching
    cur_cm = conn.execute("SELECT hash, short_hash, repo, ts, files FROM commits")
    by_short = defaultdict(list)
    by_repo = defaultdict(list)
    for h, sh, rep, ts, fl in cur_cm.fetchall():
        f_set = {Path(f).name for f in fl.split()} if fl else set()
        row_cm = {"hash": h, "short": sh, "repo": rep, "ts": ts, "files": f_set}
        by_short[sh[:7]].append(row_cm)
        by_repo[rep].append(row_cm)

    session_rows = []
    edge_rows = []
    trans_rows = []

    for (eng, path, mt, sz), r in zip(todo, parsed):
        if not r or not r.get("sid"):
            trans_rows.append((path, "", mt, sz, 0))
            continue
        sid = r["sid"]
        session_rows.append((
            sid,
            r["repo"],
            r["cwd"],
            r["start_ts"],
            r["end_ts"],
            json.dumps(r["tickets"]),
            json.dumps(r["commits"]),
            json.dumps(r["files"]),
        ))
        trans_rows.append((path, sid, mt, sz, 1))

        # Commit edges
        for h in r["commits"]:
            matches = by_short.get(h[:7], [])
            if matches:
                for cm in matches:
                    edge_rows.append((sid, f"commit:{cm['hash']}", "made", 1.0))
            else:
                edge_rows.append((sid, f"commit:{h}", "made", 1.0))

        # Time window edges
        files_names = {Path(f).name for f in r["files"]}
        if files_names and r["start_ts"] and r["end_ts"] and r["repo"]:
            for cm in by_repo.get(r["repo"], []):
                if (r["start_ts"] - 60 <= cm["ts"] <= r["end_ts"] + 600) and (files_names & cm["files"]):
                    edge_rows.append((sid, f"commit:{cm['hash']}", "window", 0.6))

        # Ticket edges
        for t in r["tickets"]:
            edge_rows.append((sid, f"ticket:{t}", "ref", 1.0))

    with conn:
        conn.executemany("INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?)", trans_rows)
        conn.executemany("INSERT OR REPLACE INTO session_meta VALUES (?,?,?,?,?,?,?,?)", session_rows)
        conn.executemany("INSERT INTO edges VALUES (?,?,?,?)", edge_rows)


def _sync_all(conn: sqlite3.Connection, force: bool = False) -> None:
    global _last_sync_ts
    now = time.time()
    if not force and (now - _last_sync_ts < _SYNC_TTL):
        return

    with _sync_lock:
        if not force and (time.time() - _last_sync_ts < _SYNC_TTL):
            return

        _init_db(conn)
        days = _get_days()
        roots = discover_repo_roots()
        _sync_git_repos(conn, roots, days)
        _sync_watchtower(conn)
        _sync_transcripts(conn, days)
        _last_sync_ts = time.time()


def rrf(lists: list[list[str]], k: int = 60, weights: list[float] | None = None) -> list[str]:
    """Reciprocal Rank Fusion."""
    sc: dict[str, float] = defaultdict(float)
    for i, lst in enumerate(lists):
        w = weights[i] if weights and i < len(weights) else 1.0
        for r, sid in enumerate(lst, 1):
            sc[sid] += w / (k + r)
    return [s for s, _ in sorted(sc.items(), key=lambda kv: -kv[1])]


def search_sessions(query: str, limit: int = 20, force_refresh: bool = False) -> list[dict]:
    """Search sessions re-ranking session_fts results with commit/ticket graph.

    Must NOT lower retrieval vs session_fts alone.
    Returns: [{'session_id': ...}, ...]
    """
    q = (query or "").strip()
    if not q:
        return []

    conn = _get_connection()
    _sync_all(conn, force=force_refresh)

    # 1. Base results from session_fts
    try:
        base_search = _get_base_search_sessions()
        base_hits = base_search(q, limit=max(limit * 2, 50))
        base_sids = [r["session_id"] for r in base_hits if isinstance(r, dict) and "session_id" in r]
    except Exception:
        base_sids = []

    terms = extract_terms(q)
    if not terms:
        return [{"session_id": sid} for sid in base_sids[:limit]]

    match_str = fts_query(terms)
    commit_sids: list[str] = []
    if match_str:
        try:
            cur = conn.execute(
                """SELECT hash, short_hash FROM commits_fts
                   WHERE commits_fts MATCH ?
                   ORDER BY bm25(commits_fts, 0, 0, 0, 0, 5.0, 1.5, 1.0)
                   LIMIT 25""",
                (match_str,),
            )
            commit_hits = cur.fetchall()
            sc_commit: dict[str, float] = defaultdict(float)
            for rank, (h, sh) in enumerate(commit_hits, 1):
                cur_edges = conn.execute(
                    "SELECT src, w FROM edges WHERE dst IN (?, ?, ?, ?) AND kind IN ('made', 'window')",
                    (f"commit:{h}", f"commit:{sh}", h, sh),
                )
                for sid, w in cur_edges.fetchall():
                    sc_commit[sid] += w / (10 + rank)
            commit_sids = [s for s, _ in sorted(sc_commit.items(), key=lambda kv: -kv[1])]
        except sqlite3.OperationalError:
            pass

    # Ticket expansion from base seed sessions
    ticket_sids: list[str] = []
    if base_sids:
        sc_ticket: dict[str, float] = defaultdict(float)
        for rank, sid in enumerate(base_sids[:5], 1):
            cur_t = conn.execute("SELECT dst FROM edges WHERE src = ? AND dst LIKE 'ticket:%'", (sid,))
            for (dst,) in cur_t.fetchall():
                cur_sibs = conn.execute("SELECT src FROM edges WHERE dst = ? AND src != ?", (dst, sid)).fetchall()
                if 0 < len(cur_sibs) <= 12:
                    for (s2,) in cur_sibs:
                        sc_ticket[s2] += 1.0 / (rank * len(cur_sibs))
        ticket_sids = [s for s, _ in sorted(sc_ticket.items(), key=lambda kv: -kv[1])]

    if not commit_sids and not ticket_sids:
        return [{"session_id": sid} for sid in base_sids[:limit]]

    # Fuse with RRF: strong base weight guarantees no retrieval drop
    fused = rrf([base_sids, commit_sids, ticket_sids], k=60, weights=[1.0, 0.35, 0.2])
    return [{"session_id": sid} for sid in fused[:limit]]


def is_shipped(topic: str) -> dict:
    """Determine whether a topic has been shipped.

    Contract:
      {'shipped': bool, 'confidence': float, 'evidence': [{'repo','commit','subject','session_id'}], 'tickets': [...]}
    """
    t = (topic or "").strip()
    if not t:
        return {"shipped": False, "confidence": 0.0, "evidence": [], "tickets": []}

    conn = _get_connection()
    _sync_all(conn, force=False)

    terms = extract_terms(t)
    if not terms:
        return {"shipped": False, "confidence": 0.0, "evidence": [], "tickets": []}

    match_str = fts_query(terms)
    if not match_str:
        return {"shipped": False, "confidence": 0.0, "evidence": [], "tickets": []}

    stemmed_terms = {_stem(w) for w in terms}

    query_asks_docs = any(w in terms for w in ("doc", "docs", "document", "documentation", "spec", "specs", "readme", "runbook"))

    # 1. Search WatchTower tickets
    candidate_tickets: list[dict] = []
    open_tickets: list[dict] = []
    closed_tickets: list[dict] = []

    try:
        cur_t = conn.execute(
            """SELECT ref, project, status, commit_sha, title, text,
                      bm25(tickets_fts, 0, 0, 0, 0, 6.0, 1.0) as rank
               FROM tickets_fts
               WHERE tickets_fts MATCH ?
               ORDER BY rank LIMIT 150""",
            (match_str,),
        )
        for ref, proj, status, commit_sha, title, text, rank in cur_t.fetchall():
            if '/Users/' in (title or '') or '.png' in (title or ''):
                continue
            title_tokens = re.findall(r"[a-z0-9]+", (title or "").lower())
            title_stems = {_stem(w) for w in title_tokens if w not in STOPWORDS}
            text_tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
            text_stems = {_stem(w) for w in text_tokens if w not in STOPWORDS}

            # Check compound pairs in title and text
            for i in range(len(terms) - 1):
                pair = terms[i].lower() + terms[i+1].lower()
                pair_stem = _stem(pair)
                if pair in title_tokens or pair_stem in title_stems:
                    title_stems.add(_stem(terms[i]))
                    title_stems.add(_stem(terms[i+1]))
                if pair in text_tokens or pair_stem in text_stems:
                    text_stems.add(_stem(terms[i]))
                    text_stems.add(_stem(terms[i+1]))

            # Check synonyms in title and text
            for orig_term, syn_list in SYNONYMS.items():
                orig_stem = _stem(orig_term)
                if orig_stem in stemmed_terms:
                    for syn in syn_list:
                        syn_stem = _stem(syn)
                        if syn_stem in title_stems:
                            title_stems.add(orig_stem)
                        if syn_stem in text_stems:
                            text_stems.add(orig_stem)

            m_title = stemmed_terms & title_stems
            title_ratio = len(m_title) / len(stemmed_terms) if stemmed_terms else 0
            m_text = stemmed_terms & text_stems
            text_ratio = len(m_text) / len(stemmed_terms) if stemmed_terms else 0

            title_spaced = " " + " ".join(title_tokens) + " "
            has_phrase_title = any(f" {terms[i]} {terms[i+1]} " in title_spaced for i in range(len(terms) - 1)) if len(terms) >= 2 else False

            is_relevant = (
                title_ratio >= 0.40
                or (len(m_title) >= 2 and (has_phrase_title or len(stemmed_terms) <= 4))
                or (text_ratio >= 0.60 and len(m_title) >= 1)
                or (len(stemmed_terms) == 1 and len(m_title) >= 1)
            )

            if is_relevant:
                t_info = {
                    "ref": ref, "status": status, "commit_sha": commit_sha or "",
                    "title": title, "title_ratio": title_ratio,
                    "m_title": m_title, "has_phrase_title": has_phrase_title,
                }
                candidate_tickets.append(t_info)
                if status in ("open", "in_progress", "blocked", "todo"):
                    open_tickets.append(t_info)
                elif status == "closed" and commit_sha:
                    closed_tickets.append(t_info)
    except sqlite3.OperationalError:
        pass

    # 2. Search commits via FTS5
    candidate_commits: list[dict] = []
    try:
        cur_c = conn.execute(
            """SELECT commit_id, repo, hash, short_hash, subject, body, files,
                      bm25(commits_fts, 0, 0, 0, 0, 8.0, 2.0, 0.5) as rank
               FROM commits_fts
               WHERE commits_fts MATCH ?
               ORDER BY rank LIMIT 80""",
            (match_str,),
        )
        for cid, repo, h, sh, subj, body, files, rank in cur_c.fetchall():
            subj_lower = (subj or "").lower()
            body_lower = (body or "").lower()

            if subj_lower.startswith("merge "):
                continue

            is_doc_commit = (
                subj_lower.startswith("docs:")
                or subj_lower.startswith("docs(")
                or subj_lower.startswith("doc:")
                or "document " in subj_lower
            )
            if is_doc_commit and not query_asks_docs:
                continue

            subj_tokens = re.findall(r"[a-z0-9]+", subj_lower)
            subj_stems = {_stem(w) for w in subj_tokens if w not in STOPWORDS}
            body_tokens = re.findall(r"[a-z0-9]+", body_lower)
            body_stems = {_stem(w) for w in body_tokens if w not in STOPWORDS}

            # Check compound pairs
            for i in range(len(terms) - 1):
                pair = terms[i].lower() + terms[i+1].lower()
                pair_stem = _stem(pair)
                if pair in subj_tokens or pair_stem in subj_stems:
                    subj_stems.add(_stem(terms[i]))
                    subj_stems.add(_stem(terms[i+1]))
                if pair in body_tokens or pair_stem in body_stems:
                    body_stems.add(_stem(terms[i]))
                    body_stems.add(_stem(terms[i+1]))

            # Check synonyms
            for orig_term, syn_list in SYNONYMS.items():
                orig_stem = _stem(orig_term)
                if orig_stem in stemmed_terms:
                    for syn in syn_list:
                        syn_stem = _stem(syn)
                        if syn_stem in subj_stems:
                            subj_stems.add(orig_stem)
                        if syn_stem in body_stems:
                            body_stems.add(orig_stem)

            matched_subj = stemmed_terms & subj_stems
            matched_all = stemmed_terms & (subj_stems | body_stems)

            subj_ratio = len(matched_subj) / len(stemmed_terms) if stemmed_terms else 0
            all_ratio = len(matched_all) / len(stemmed_terms) if stemmed_terms else 0

            subj_stemmed_spaced = " " + " ".join([_stem(w) for w in subj_tokens]) + " "
            body_stemmed_spaced = " " + " ".join([_stem(w) for w in body_tokens]) + " "
            has_phrase_subj = any(f" {_stem(terms[i])} {_stem(terms[i+1])} " in subj_stemmed_spaced for i in range(len(terms) - 1)) if len(terms) >= 2 else False
            has_phrase_body = any(f" {_stem(terms[i])} {_stem(terms[i+1])} " in body_stemmed_spaced for i in range(len(terms) - 1)) if len(terms) >= 2 else False

            candidate_commits.append({
                "commit_id": cid, "repo": repo, "hash": h, "short_hash": sh,
                "subject": subj, "subj_ratio": subj_ratio, "all_ratio": all_ratio,
                "n_matched_subj": len(matched_subj),
                "n_matched_all": len(matched_all),
                "has_phrase": has_phrase_subj or has_phrase_body,
                "has_phrase_subj": has_phrase_subj,
                "has_phrase_body": has_phrase_body,
                "rank": rank,
            })
    except sqlite3.OperationalError:
        pass

    n_stems = len(stemmed_terms)

    # 3. Check for direct commits resolved by closed tickets
    for ct in closed_tickets:
        t_sha = ct.get("commit_sha", "")
        t_ref = ct.get("ref", "")
        if not t_sha:
            continue
        for c in candidate_commits:
            if c["hash"].startswith(t_sha) or t_sha.startswith(c["hash"]):
                if (ct.get("has_phrase_title") or c.get("has_phrase_subj") or ct["title_ratio"] >= 0.60 or c["subj_ratio"] >= 0.50 or n_stems <= 2):
                    c["ticket_boost"] = 15.0
                    c["ticket_ref"] = t_ref
                break

    # 4. Evaluate qualifying commits
    qualifying: list[dict] = []
    for c in candidate_commits:
        ticket_boost = c.get("ticket_boost", 0.0)
        is_strong = False
        n_m_subj = c["n_matched_subj"]
        n_m_all = c["n_matched_all"]
        subj_ratio = c["subj_ratio"]
        all_ratio = c["all_ratio"]
        has_phrase_subj = c.get("has_phrase_subj", False)
        has_phrase_body = c.get("has_phrase_body", False)

        if ticket_boost > 0 and (subj_ratio >= 0.40 or has_phrase_subj or n_m_subj >= 2):
            is_strong = True
        elif n_stems == 1:
            is_strong = (n_m_subj >= 1)
        elif n_stems == 2:
            is_strong = (n_m_subj >= 2) or (n_m_subj >= 1 and has_phrase_subj)
        else:
            if has_phrase_subj and n_m_subj >= 3:
                is_strong = True
            elif subj_ratio >= 0.50:
                is_strong = True
            elif (has_phrase_subj or has_phrase_body) and n_m_all >= 4 and all_ratio >= 0.75:
                is_strong = True

        if is_strong:
            cur_s = conn.execute(
                "SELECT src FROM edges WHERE dst IN (?, ?) AND kind IN ('made', 'window') LIMIT 1",
                (f"commit:{c['hash']}", f"commit:{c['short_hash']}"),
            )
            s_row = cur_s.fetchone()
            c["session_id"] = s_row[0] if s_row else ""
            score = (
                ticket_boost
                + subj_ratio * 25.0
                + n_m_subj * 10.0
                + (12.0 if has_phrase_subj else 0.0)
                + n_m_all * 3.0
                - (c["rank"] * 0.1)
            )
            c["final_score"] = score
            qualifying.append(c)

    qualifying.sort(key=lambda x: -x["final_score"])

    # 5. Decision logic
    all_tickets = list(dict.fromkeys(
        [t["ref"] for t in candidate_tickets] + [c.get("ticket_ref") for c in qualifying if c.get("ticket_ref")]
    ))

    if qualifying:
        top_commit = qualifying[0]
        # Check if an open ticket overrides the commit
        if open_tickets:
            best_ot = max(open_tickets, key=lambda x: x["title_ratio"])
            if best_ot["title_ratio"] > top_commit["subj_ratio"] or (
                best_ot["title_ratio"] >= top_commit["subj_ratio"] and not top_commit.get("has_phrase_subj")
            ) or (
                best_ot["title_ratio"] >= 0.60 and top_commit["subj_ratio"] < 0.60 and not top_commit.get("has_phrase_subj")
            ):
                return {
                    "shipped": False,
                    "confidence": 0.85,
                    "evidence": [],
                    "tickets": all_tickets,
                }

        evidence = [
            {
                "repo": c["repo"],
                "commit": c["hash"],
                "subject": c["subject"],
                **({"session_id": c["session_id"]} if c.get("session_id") else {}),
            }
            for c in qualifying[:5]
        ]
        return {
            "shipped": True,
            "confidence": 0.98,
            "evidence": evidence,
            "tickets": all_tickets,
        }

    # Not shipped: check if there were planned/open tickets
    if open_tickets:
        return {
            "shipped": False,
            "confidence": 0.85,
            "evidence": [],
            "tickets": all_tickets,
        }

    return {
        "shipped": False,
        "confidence": 0.50,
        "evidence": [],
        "tickets": all_tickets,
    }
