#!/usr/bin/env python3
"""Shared re-orientation-after-compaction logic (MEMO-FIX-16).

Both `post-compact.py` (Claude Code's PostCompact hook) and
`post-compact-codex.py` (Codex's PostCompact hook) print the same style
<=600-char block after their engine compacts a session: the WatchTower
ticket the session claimed (if any), the last few things actually asked
for, and a pointer to `ccc recall` / `ccc shipped`. This module holds the
engine-agnostic half of that logic (bounded transcript reads, the "is this
a real user ask" filter, block formatting); each engine script supplies its
own transcript-record shape.
"""

import json
import os
import re

# Matches WT-48 as well as multi-segment refs like MEMO-FIX-10.
TICKET_REF_RE = re.compile(r"\b([A-Z][A-Z0-9]{0,20}(?:-[A-Z][A-Z0-9]{0,20}){0,3}-\d{1,8})\b")
# Bounds the I/O regardless of total transcript size: HEAD catches the
# session's opening dispatch message (where a WatchTower ref usually first
# appears), TAIL catches the most recent asks and the freshest wt JSON result.
HEAD_BYTES = 320_000
TAIL_BYTES = 400_000
MAX_BLOCK_CHARS = 600
# User-role turns that were injected by tooling, not typed by the user: queue
# notifications, peer-session messages, background-task events. Engine-
# agnostic — CCC formats these the same way regardless of which engine the
# session is running.
INJECTED_PREFIXES = (
    "[watchtower]",
    "Another Claude session sent a message",
    "[SYSTEM NOTIFICATION",
)
# MEMO-FIX-lineage: "Continue in a new session" / usage-limit auto-resume
# stamps this line into the successor's own first user turn (see
# usage_limit.py's _usage_limit_retrieval_prompt and ccc_server/ship_graph.py's
# CONTINUATION_ORIGIN_RE, which this mirrors). Since it always lands within
# HEAD_BYTES of a continuation's transcript, the same bounded head scan that
# already looks for a ticket ref picks it up for free.
CONTINUATION_ORIGIN_RE = re.compile(r"Origin session id: ([A-Za-z0-9][A-Za-z0-9_.-]{7,127})")


def read_chunk(path, size, from_end):
    with open(path, "rb") as f:
        if from_end:
            total = os.fstat(f.fileno()).st_size
            if total > size:
                f.seek(total - size)
                f.readline()  # drop the partial line the seek landed inside
            data = f.read()
        else:
            data = f.read(size)
    return data.decode("utf-8", errors="replace")


def records(text):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def consider_ask(text, state):
    """Record one candidate user-ask string into `state["asks"]`, skipping
    empty/tag-wrapped/injected text, and update `state["ticket_ref"]` if the
    text names a WatchTower ticket. Also updates `state["continued_from"]`
    when the text carries an "Origin session id:" marker -- that marker is
    synthetic (auto-resume tooling wrote it, not the user), so it's excluded
    from `asks` the same way an injected queue notification would be."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return
    m_origin = CONTINUATION_ORIGIN_RE.search(t)
    if m_origin:
        state["continued_from"] = m_origin.group(1).strip()
        return
    if t.startswith("<") or t.startswith(INJECTED_PREFIXES):
        return
    state["asks"].append(t)
    m = TICKET_REF_RE.search(t)
    if m:
        state["ticket_ref"] = m.group(1)


def scan_transcript(path, scan_into):
    """Bounded head+tail scan of `path`, calling `scan_into(text, state)` on
    each chunk in file order (so tail matches overwrite head matches).
    Returns the resulting state dict."""
    state = {"asks": [], "ticket_ref": "", "ticket_title": "", "continued_from": ""}
    size = os.path.getsize(path)
    if size <= HEAD_BYTES + TAIL_BYTES:
        # Small enough to read once — a separate head+tail read would cover
        # the same bytes twice.
        scan_into(read_chunk(path, size, from_end=False), state)
    else:
        # size > HEAD_BYTES + TAIL_BYTES here, so the two windows never
        # overlap. Head first (opening dispatch message, usually where a
        # WatchTower ref first appears), then tail (recent asks, freshest wt
        # result) — tail matches overwrite head matches.
        scan_into(read_chunk(path, HEAD_BYTES, from_end=False), state)
        scan_into(read_chunk(path, TAIL_BYTES, from_end=True), state)
    return state


def truncate(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_block(asks, ticket_ref, ticket_title, continued_from=""):
    lines = ["Re-orientation after compaction:"]
    if continued_from:
        lines.append(f"Continued from: {continued_from}")
    if ticket_ref:
        head = ticket_ref
        if ticket_title:
            head += f" — {truncate(ticket_title, 60)}"
        lines.append(f"Ticket: {head}")
    if asks:
        lines.append("Last asks:")
        for a in asks:
            lines.append(f"- {truncate(a, 90)}")
    lines.append("Tools: `ccc recall <query>` / `ccc shipped <topic>`.")
    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:
        block = block[: MAX_BLOCK_CHARS - 1].rstrip() + "…"
    return block
