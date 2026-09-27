#!/usr/bin/env python3
"""Codex PostCompact hook — the Codex equivalent of post-compact.py (MEMO-FIX-16).

Codex's own hook subsystem (codex-rs `hooks` crate, `protocol::HookEventName`)
defines a native `PostCompact` event, matching Claude Code's hook of the same
name field-for-field closely enough to share the same JSON contract: stdin is
a JSON object with (at least) `session_id` and a nullable `transcript_path`
pointing at the rollout `.jsonl` Codex just finished compacting. This is a
direct native trigger, not a fallback — no SessionStart/"resume source"
heuristic or manual rollout-marker scan was needed, because PostCompact
already exists and fires exactly where Claude's does: right after a
compaction completes, before the next turn.

One thing PostCompact hooks do NOT need is `pre-compact.py`'s "compacting…"
marker file — that convention exists purely for Claude Code's live dashboard
badge. CCC tracks Codex's own compaction lifecycle separately (see
`compaction_recovery` state in ccc_server/codex.py), so there is nothing for
this hook to clear.

Reuses hooks/_reorient_shared.py for the bounded transcript read, the "is
this a real ask" filter, and the <=600-char block formatting — only the
Codex rollout record shapes below are new. Local only, no subprocess, no LLM
call, must never fail the turn.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _reorient_shared import (  # noqa: E402
    MAX_BLOCK_CHARS,
    consider_ask,
    scan_transcript,
    build_block,
    records as _reorient_records,
)


def _user_message_text(payload):
    """Classic Codex rollout shape: event_msg/user_message with a flat
    `message` string."""
    return str(payload.get("message") or "")


def _item_completed_user_text(payload):
    """Newer (multi-agent-capable) Codex rollout shape: event_msg/item_completed
    wrapping a `UserMessage` item with a list of {"type": "text", "text": ...}
    content blocks."""
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    if item.get("type") != "UserMessage":
        return None
    parts = [
        c.get("text", "") for c in (item.get("content") or [])
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
    ]
    return "\n\n".join(p.strip() for p in parts if p.strip()).strip()


def _tool_output_texts(payload):
    """function_call_output / custom_tool_call_output `output` field: a plain
    string, or a list of strings/{"text": ...} blocks (screenshots etc. are
    skipped — only text parts can carry a wt ticket ref/title JSON)."""
    output = payload.get("output")
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        yield "\n".join(parts)
    elif isinstance(output, str):
        yield output


def _scan_into(text, state):
    """Codex equivalent of the Claude hook's `_scan_into`: update `state`
    (asks list, ticket_ref, ticket_title) from one chunk of rollout JSONL
    text, in file order."""
    for rec in _reorient_records(text):
        rec_type = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        ptype = payload.get("type")

        if rec_type == "event_msg" and ptype == "user_message":
            consider_ask(_user_message_text(payload), state)
        elif rec_type == "event_msg" and ptype == "item_completed":
            text_ = _item_completed_user_text(payload)
            if text_ is not None:
                consider_ask(text_, state)
        elif rec_type == "response_item" and ptype in (
            "function_call_output", "custom_tool_call_output",
        ):
            for result_text in _tool_output_texts(payload):
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

        transcript_path = data.get("transcript_path") or ""
        if not transcript_path or not os.path.isfile(transcript_path):
            return

        state = scan_transcript(transcript_path, _scan_into)

        asks = state["asks"][-3:]
        ticket_ref = state["ticket_ref"]
        ticket_title = state["ticket_title"]
        continued_from = state["continued_from"]

        if not asks and not ticket_ref and not continued_from:
            return

        print(build_block(asks, ticket_ref, ticket_title, continued_from))

    except Exception:
        pass


if __name__ == "__main__":
    main()
