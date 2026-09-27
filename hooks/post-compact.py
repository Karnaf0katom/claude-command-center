#!/usr/bin/env python3
"""PostCompact hook — clears the compacting marker pre-compact.py wrote, then
prints a short re-orientation block.

Compaction throws away the transcript that carried a session's implicit
context. This gives a quick anchor back: the WatchTower ticket the session
claimed (if any), the last few things actually asked for, and a pointer to
`ccc recall` / `ccc shipped` for anything deeper. Local only, no subprocess,
no LLM call — everything comes from re-reading the transcript Claude Code
already wrote to disk.

Bounded to a head+tail read of the transcript (not the whole file) so this
stays well under Claude Code's hook timeout even for a huge pre-compaction
transcript. Every step is wrapped so a malformed transcript, a missing file,
or an unexpected payload shape just skips that piece of the block — hooks
must never fail the turn.
"""

import json
import os
import re
import sys

LIVE_STATE_DIR = os.path.expanduser("~/.claude/command-center/live-state")
# Matches WT-48 as well as multi-segment refs like MEMO-FIX-10.
TICKET_REF_RE = re.compile(r"\b([A-Z][A-Z0-9]{0,20}(?:-[A-Z][A-Z0-9]{0,20}){0,3}-\d{1,8})\b")
# Bounds the I/O regardless of total transcript size: HEAD catches the
# session's opening dispatch message (where a WatchTower ref usually first
# appears), TAIL catches the most recent asks and the freshest wt JSON result.
HEAD_BYTES = 320_000
TAIL_BYTES = 400_000
MAX_BLOCK_CHARS = 600
# User-role turns that were injected by tooling, not typed by the user: queue
# notifications, peer-session messages, background-task events.
INJECTED_PREFIXES = (
    "[watchtower]",
    "Another Claude session sent a message",
    "[SYSTEM NOTIFICATION",
)


def _clear_compacting_marker(session_id):
    path = os.path.join(LIVE_STATE_DIR, f"{session_id}_compacting.json")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _read_chunk(path, size, from_end):
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


def _records(text):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _text_blocks(content):
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"]
    return []


def _has_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in content
    )


def _tool_result_texts(content):
    if not isinstance(content, list):
        return
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_result":
            continue
        cc = c.get("content")
        if isinstance(cc, str):
            yield cc
        elif isinstance(cc, list):
            for b in cc:
                if isinstance(b, dict) and b.get("type") == "text":
                    yield b.get("text", "")


def _scan_into(text, state):
    """Update `state` (asks list, ticket_ref, ticket_title) from one chunk of
    transcript text, in file order. Called on the head chunk then the tail
    chunk, so a later match — closer to "now" — always overwrites an earlier
    one; asks accumulate and get trimmed to the last 3 at the end."""
    for rec in _records(text):
        if rec.get("type") != "user":
            continue
        msg = rec.get("message", {})
        if msg.get("role") != "user":
            continue
        content = msg.get("content")

        if not _has_tool_result(content):
            for t in _text_blocks(content):
                t = re.sub(r"\s+", " ", t).strip()
                if not t or t.startswith("<") or t.startswith(INJECTED_PREFIXES):
                    continue
                state["asks"].append(t)
                m = TICKET_REF_RE.search(t)
                if m:
                    state["ticket_ref"] = m.group(1)

        for result_text in _tool_result_texts(content):
            try:
                obj = json.loads(result_text)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("ref") and obj.get("title"):
                state["ticket_ref"] = str(obj["ref"])
                state["ticket_title"] = str(obj["title"])


def _truncate(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _build_block(asks, ticket_ref, ticket_title):
    lines = ["Re-orientation after compaction:"]
    if ticket_ref:
        head = ticket_ref
        if ticket_title:
            head += f" — {_truncate(ticket_title, 60)}"
        lines.append(f"Ticket: {head}")
    if asks:
        lines.append("Last asks:")
        for a in asks:
            lines.append(f"- {_truncate(a, 90)}")
    lines.append("Tools: `ccc recall <query>` / `ccc shipped <topic>`.")
    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:
        block = block[: MAX_BLOCK_CHARS - 1].rstrip() + "…"
    return block


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)

        session_id = data.get("session_id", "")
        if session_id:
            _clear_compacting_marker(session_id)

        transcript_path = data.get("transcript_path", "")
        if not transcript_path or not os.path.isfile(transcript_path):
            return

        state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
        size = os.path.getsize(transcript_path)
        if size <= HEAD_BYTES + TAIL_BYTES:
            # Small enough to read once — a separate head+tail read would
            # cover the same bytes twice.
            _scan_into(_read_chunk(transcript_path, size, from_end=False), state)
        else:
            # size > HEAD_BYTES + TAIL_BYTES here, so the two windows never
            # overlap. Head first (opening dispatch message, usually where a
            # WatchTower ref first appears), then tail (recent asks, freshest
            # wt result) — tail matches overwrite head matches.
            _scan_into(_read_chunk(transcript_path, HEAD_BYTES, from_end=False), state)
            _scan_into(_read_chunk(transcript_path, TAIL_BYTES, from_end=True), state)

        asks = state["asks"][-3:]
        ticket_ref = state["ticket_ref"]
        ticket_title = state["ticket_title"]

        if not asks and not ticket_ref:
            return

        print(_build_block(asks, ticket_ref, ticket_title))

    except Exception:
        pass


if __name__ == "__main__":
    main()
