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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _reorient_shared import (  # noqa: E402
    TICKET_REF_RE,
    HEAD_BYTES,
    TAIL_BYTES,
    MAX_BLOCK_CHARS,
    INJECTED_PREFIXES,
    read_chunk as _read_chunk,
    records as _records,
    consider_ask,
    scan_transcript,
    truncate as _truncate,
    build_block as _build_block,
)

LIVE_STATE_DIR = os.path.expanduser("~/.claude/command-center/live-state")


def _clear_compacting_marker(session_id):
    path = os.path.join(LIVE_STATE_DIR, f"{session_id}_compacting.json")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


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
                consider_ask(t, state)

        for result_text in _tool_result_texts(content):
            try:
                obj = json.loads(result_text)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("ref") and obj.get("title"):
                state["ticket_ref"] = str(obj["ref"])
                state["ticket_title"] = str(obj["title"])


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

        state = scan_transcript(transcript_path, _scan_into)

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
