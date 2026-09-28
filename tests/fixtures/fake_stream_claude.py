#!/usr/bin/env python3
"""Stand-in for `claude -p --input-format stream-json` in warm-pool tests.

Answers every user line with init + one text delta + result, keeping its own
conversation history so tests can see whether /clear isolated requests.
Special inputs: "/clear" resets (unless FAKE_NO_RESET=1), "CRASH" exits,
"HANG" never answers, "TOOL..." emits one tool_use first. Arguments are ignored.
"""
import json
import os
import sys
import time
import uuid


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


history = []
sid = str(uuid.uuid4())
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    text = str(((msg.get("message") or {}).get("content")) or "")
    if text == "/clear":
        if os.environ.get("FAKE_NO_RESET") != "1":
            history = []
            sid = str(uuid.uuid4())
            emit({"type": "conversation_reset", "new_conversation_id": sid})
        emit({"type": "system", "subtype": "init", "session_id": sid})
        emit({"type": "result", "subtype": "success", "result": "", "session_id": sid, "num_turns": 0})
        continue
    if text == "CRASH":
        sys.exit(3)
    if text == "HANG":
        time.sleep(3600)
    emit({"type": "system", "subtype": "init", "session_id": sid})
    if text.startswith("TOOL"):
        # One tool round, emitted twice as partial messages repeat blocks.
        block = {"type": "tool_use", "id": "tu_1", "name": "mcp__claude-index__search_sessions",
                 "input": {"query": "mazkir chips"}}
        emit({"type": "assistant", "message": {"content": [block]}})
        emit({"type": "assistant", "message": {"content": [block]}})
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": "n"}}})
    answer = f"n={len(history)} pid={os.getpid()}"
    history.append(text)
    emit({"type": "result", "subtype": "success", "result": answer, "session_id": sid,
          "num_turns": 1, "total_cost_usd": 0.001, "is_error": False})
