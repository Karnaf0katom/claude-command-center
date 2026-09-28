# Copyright (c) 2026 Amir Fish. All rights reserved.
# SPDX-License-Identifier: LicenseRef-CCC-Software-License
"""MEMO-FIX-lineage: `ccc spawn --continue-from` and `ccc send
--new-if-large-and-stale`, plus the one delivery-time hook (`forward_target`)
that lets every continuation entry point (this module, the F2 "Continue in a
new session" button, unattended usage-limit auto-resume) share the same
"old sid -> successor sid" forwarding without a new data store.

Resuming an old session reloads its whole transcript into context (a real
case re-loaded 430k tokens to do a few merges). The fix is never to resume:
spawn a fresh session that starts from a short, curated brief instead. This
module is the one place that knows how to do that spawn, decide when a
`ccc send` should prefer it over a plain resume, and keep report-to routing
(CCC-1202) pointed at whichever session in a continuation chain is newest.

No new lineage data structure: recording a continuation is just embedding
the literal "Origin session id: <sid>" marker in the new session's first
prompt (via usage_limit.continuation_retrieval_block) -- ship_graph's
existing background transcript parser does the rest
(ccc_server/ship_graph.py's CONTINUATION_ORIGIN_RE).
"""

from __future__ import annotations

import time

from ccc_server import core as _core
from ccc_server import lineage as _lineage
from ccc_server import report_routes as _report_routes
from ccc_server import ship_graph as _sg
from ccc_server import usage_limit as _usage_limit
from ccc_server.session_brief import _transcript_path, brief as _brief

DEFAULT_LARGE_TOKENS = 150_000
DEFAULT_STALE_SECONDS = 60 * 60

_SUPPORTED_ENGINES = ("claude", "codex", "kimi")


def session_context(query):
    """Resolve `query` (sid, unique prefix, or fuzzy search) to everything a
    continuation spawn needs, already pointed at the LATEST successor in its
    continuation chain: title, cwd, engine, model/effort, transcript path,
    current context size, idle time, and the session's own report-to parent
    (so a spawned continuation can inherit it, not just the sid it resolved
    from). Returns None if `query` matches nothing at all."""
    info = _brief(query)
    if not info.get("found"):
        return None
    sid = info["session_id"]
    latest = info.get("latest") or sid
    if latest != sid:
        latest_info = _brief(latest)
        if latest_info.get("found"):
            info = latest_info
    conn = _sg._get_connection()
    path = _transcript_path(conn, latest)
    engine = info.get("engine") or "claude"
    row = _usage_limit._usage_limit_row_for_session(latest, engine=engine) or {}
    context_tokens = _usage_limit._usage_limit_context_tokens(engine, path, row)
    mtime = row.get("mtime")
    idle_seconds = max(0.0, time.time() - mtime) if mtime else 0.0
    report_to = _lineage.report_to_of(sid) or _lineage.report_to_of(latest)
    return {
        "session_id": sid,
        "latest": latest,
        "title": info.get("title") or "",
        "cwd": info.get("cwd") or "",
        "repo": info.get("repo") or "",
        "engine": engine,
        "transcript_path": path,
        "context_tokens": context_tokens,
        "idle_seconds": idle_seconds,
        "model": row.get("model") or None,
        "effort": row.get("reasoning_effort") or None,
        "report_to": report_to,
    }


def build_continuation_prompt(user_prompt, ctx):
    """user_prompt plus the generated preamble, sharing one retrieval-block
    template (usage_limit.continuation_retrieval_block) with the F2 button
    and unattended usage-limit auto-resume so the three never drift out of
    wording with each other."""
    block, _label = _usage_limit.continuation_retrieval_block(
        ctx["engine"], ctx["latest"], ctx["context_tokens"],
    )
    title = ctx["title"] or ctx["latest"]
    preamble = (
        f"You continue the work of session {ctx['latest']} ({title}).\n"
        f"Previous owner transcript: {ctx['transcript_path'] or '(not indexed yet)'}\n\n"
        f"{block}"
    )
    task = user_prompt.strip() if user_prompt else "Continue the work from where it left off."
    return f"{preamble}\n\nTask: {task}"


def rebind_chain_to(old_sid, new_sid):
    """Every child that reports to ANY member of `old_sid`'s continuation
    chain now reports to `new_sid` instead -- the successor picks up exactly
    the callbacks the old chain would have received. Returns the route ids
    moved."""
    conn = _sg._get_connection()
    members = _lineage.continuation_chain_members(conn, old_sid) or [old_sid]
    rebound = []
    for member in members:
        if member == new_sid:
            continue
        try:
            moved = _report_routes.rebind(new_sid, from_report_to=member)
        except ValueError:
            continue
        rebound.extend(moved)
    return rebound


def spawn_continuation(query, prompt="", model=None, effort=None, report_to=None,
                        dry_run=False, rebind_chain=True):
    """Resolve `query` to its latest successor, spawn a fresh session that
    continues it, and (unless `dry_run`) rebind the old chain's report routes
    to the new session. `model`/`effort`/`report_to` override the old
    session's own values when given."""
    ctx = session_context(query)
    if ctx is None:
        return {"ok": False, "error": f"no session found for {query!r}"}
    engine = ctx["engine"]
    if engine not in _SUPPORTED_ENGINES:
        return {
            "ok": False,
            "error": f"continuation spawn not supported for engine {engine!r}",
            "supported_engines": list(_SUPPORTED_ENGINES),
        }
    full_prompt = build_continuation_prompt(prompt, ctx)
    final_model = model if model is not None else ctx["model"]
    final_effort = effort if effort is not None else ctx["effort"]
    final_report_to = report_to if report_to is not None else ctx["report_to"]
    name = ("Continue " + (ctx["title"] or ctx["latest"]))[:60]

    preview = {
        "ok": True,
        "dry_run": True,
        "continue_from": ctx["session_id"],
        "latest_session_id": ctx["latest"],
        "engine": engine,
        "cwd": ctx["cwd"],
        "model": final_model,
        "effort": final_effort,
        "report_to": final_report_to,
        "context_tokens": ctx["context_tokens"],
        "idle_seconds": ctx["idle_seconds"],
        "prompt": full_prompt,
    }
    if dry_run:
        return preview

    report_route = None
    if final_report_to:
        try:
            report_route = _report_routes.create(final_report_to)
        except Exception:
            report_route = None
    wrapped_prompt = full_prompt
    if final_report_to:
        wrapped_prompt = _core._wrap_prompt_with_return_address(
            full_prompt, final_report_to, engine=engine, route_id=report_route,
        )

    kwargs = dict(
        name=name, cwd=ctx["cwd"], repo_path=ctx["cwd"],
        model=final_model, parent_session_id=ctx["latest"],
    )
    if engine == "codex":
        spawn_result = _core.spawn_session_codex(
            wrapped_prompt, reasoning_effort=final_effort or "", **kwargs,
        )
    elif engine == "kimi":
        spawn_result = _core.spawn_session_kimi(wrapped_prompt, effort=final_effort, **kwargs)
    else:
        spawn_result = _core.spawn_session(
            wrapped_prompt, reasoning_effort=final_effort or "", **kwargs,
        )

    new_sid = (spawn_result or {}).get("session_id")
    if report_route and new_sid:
        try:
            _report_routes.set_child(report_route, new_sid)
        except Exception:
            pass
    rebound = []
    if rebind_chain and new_sid:
        rebound = rebind_chain_to(ctx["session_id"], new_sid)

    return {
        "ok": bool(new_sid),
        "dry_run": False,
        "continue_from": ctx["session_id"],
        "latest_session_id": ctx["latest"],
        "new_session_id": new_sid,
        "engine": engine,
        "cwd": ctx["cwd"],
        "model": final_model,
        "effort": final_effort,
        "report_to": final_report_to,
        "report_route": report_route,
        "rebound": rebound,
        "spawn_result": spawn_result,
    }


def decide_send_path(query, large_threshold=DEFAULT_LARGE_TOKENS,
                      stale_seconds=DEFAULT_STALE_SECONDS):
    """Should `ccc send` deliver into `query` normally, or spawn a
    continuation instead? "new" only when BOTH large (context tokens at or
    above `large_threshold`) AND stale (idle at or above `stale_seconds`, the
    prompt-cache TTL -- so a resume would be a full cache miss anyway)."""
    ctx = session_context(query)
    if ctx is None:
        return {
            "path": "normal", "reason": f"no session found for {query!r}",
            "context_tokens": 0, "idle_seconds": 0,
            "resolved_session_id": None, "latest_session_id": None,
        }
    large = ctx["context_tokens"] >= large_threshold
    stale = ctx["idle_seconds"] >= stale_seconds
    if large and stale:
        path = "new"
        reason = (
            f"{ctx['context_tokens']:,} tokens >= {large_threshold:,} and idle "
            f"{int(ctx['idle_seconds'])}s >= {stale_seconds}s (cache miss on resume)"
        )
    else:
        path = "normal"
        if not large:
            reason = f"{ctx['context_tokens']:,} tokens < {large_threshold:,} threshold"
        else:
            reason = f"idle {int(ctx['idle_seconds'])}s < {stale_seconds}s (still warm)"
    return {
        "path": path,
        "reason": reason,
        "context_tokens": ctx["context_tokens"],
        "idle_seconds": ctx["idle_seconds"],
        "resolved_session_id": ctx["session_id"],
        "latest_session_id": ctx["latest"],
    }


def forward_target(sid):
    """Delivery-time lineage forward (MEMO-FIX-lineage): once `sid` has a
    recorded continuation successor, messages addressed to it -- a child's
    report, a WatchTower ticket notice (they share this same inject-input
    delivery path), a peer message -- land on the successor instead, with no
    rewrite of whatever already has the old sid written down. Chains resolve
    (A->B->C). Skipped while `sid` is still actively working its own turn: a
    forward must never race a reply that is already in flight."""
    if not sid:
        return sid
    try:
        conn = _sg._get_connection()
        _sg._sync_all(conn, force=False)
        target = _lineage.latest_successor(conn, sid)
    except Exception:
        return sid
    if not target or target == sid:
        return sid
    try:
        state = (_core._sessions_state_snapshot() or {}).get(sid, {}).get("state")
    except Exception:
        state = None
    if state == "working":
        return sid
    return target
