"""Unit coverage for hooks/post-compact.py's re-orientation block (MEMO-FIX-10).

Fast, standalone — imports the hook module directly, no server, no
subprocess. Only test_prints_via_stdin exercises the real process so the
"never fail the turn" and timing behavior get checked end to end.
"""

import importlib.util
import json
import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "post-compact.py"

spec = importlib.util.spec_from_file_location("ccc_post_compact_hook", str(HOOK_PATH))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def _user_text(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text},
    ]}}


def _tool_result_json(obj):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": [
            {"type": "text", "text": json.dumps(obj)},
        ]},
    ]}}


def _jsonl(records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def test_ticket_ref_multi_segment():
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(
        _jsonl([_user_text("You are the worker for WatchTower ticket MEMO-FIX-10.")]),
        state,
    )
    assert state["ticket_ref"] == "MEMO-FIX-10"


def test_ticket_ref_two_segment():
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(_jsonl([_user_text("claim WT-48 please")]), state)
    assert state["ticket_ref"] == "WT-48"


def test_tool_result_json_supplies_title_and_ref():
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(
        _jsonl([_tool_result_json({"ref": "OPS-1246", "title": "Fix the thing"})]),
        state,
    )
    assert state["ticket_ref"] == "OPS-1246"
    assert state["ticket_title"] == "Fix the thing"


def test_tool_result_wins_over_earlier_text_ref():
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(
        _jsonl([
            _user_text("ticket MEMO-FIX-10 please"),
            _tool_result_json({"ref": "MEMO-FIX-10", "title": "Lean PostCompact hook"}),
        ]),
        state,
    )
    assert state["ticket_ref"] == "MEMO-FIX-10"
    assert state["ticket_title"] == "Lean PostCompact hook"


def test_tool_result_without_title_is_ignored():
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(_jsonl([_tool_result_json({"ref": "OPS-1246"})]), state)
    assert state["ticket_ref"] == ""


def test_asks_collects_only_real_text_not_tool_results():
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(
        _jsonl([
            _user_text("first ask"),
            _tool_result_json({"ok": True}),
            _user_text("second ask"),
        ]),
        state,
    )
    assert state["asks"] == ["first ask", "second ask"]


def test_asks_skip_tag_wrapped_meta_content():
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(_jsonl([_user_text("<system-reminder>ignore me</system-reminder>")]), state)
    assert state["asks"] == []


def test_asks_skip_injected_notifications():
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(_jsonl([
        _user_text("fix the flaky test"),
        _user_text("[watchtower] WT-9 claimed"),
        _user_text("Another Claude session sent a message: <cross-session-message>hi</cross-session-message>"),
        _user_text("[SYSTEM NOTIFICATION - NOT USER INPUT] task done"),
    ]), state)
    assert state["asks"] == ["fix the flaky test"]
    assert state["ticket_ref"] == ""


def test_continuation_origin_marker_is_captured_and_excluded_from_asks():
    state = {"asks": [], "ticket_ref": "", "ticket_title": "", "continued_from": ""}
    hook._scan_into(_jsonl([
        _user_text(
            "You are continuing a task from an earlier Claude Code session, "
            "which ran long.\n\nOrigin session id: 93580c29-29f9-4ce3-8077-db00ea0a920f\n"
            "Task: Continue the work from where it left off."
        ),
        _user_text("keep going on the relaunch"),
    ]), state)
    assert state["continued_from"] == "93580c29-29f9-4ce3-8077-db00ea0a920f"
    assert state["asks"] == ["keep going on the relaunch"]


def test_build_block_includes_continued_from_line():
    block = hook._build_block(["do the thing"], "", "", "abc12345")
    assert "Continued from: abc12345" in block


def test_asks_keeps_true_last_three_including_repeats():
    """Head/tail windows never overlap (see main()'s size check), so a
    literal repeat ("continue" asked twice) must survive, not collapse."""
    state = {"asks": [], "ticket_ref": "", "ticket_title": ""}
    hook._scan_into(
        _jsonl([_user_text(f"ask {i}") for i in range(5)] + [_user_text("ask 2")]),
        state,
    )
    assert state["asks"][-3:] == ["ask 3", "ask 4", "ask 2"]


def test_build_block_stays_under_600_chars():
    asks = ["x" * 500, "y" * 500, "z" * 500]
    block = hook._build_block(asks, "MEMO-FIX-10", "a very long title " * 10)
    assert len(block) <= hook.MAX_BLOCK_CHARS


def test_build_block_omits_ticket_line_when_no_ref():
    block = hook._build_block(["do the thing"], "", "")
    assert "Ticket:" not in block
    assert "ccc recall" in block


def test_read_chunk_tail_drops_partial_first_line(tmp_path):
    path = tmp_path / "t.jsonl"
    records = [_user_text(f"ask {i}") for i in range(50)]
    path.write_text(_jsonl(records))
    chunk = hook._read_chunk(str(path), 200, from_end=True)
    # every line that survived must be independently valid JSON
    for line in chunk.splitlines():
        if line.strip():
            json.loads(line)


def test_prints_via_stdin(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_jsonl([
        _user_text("You are the worker for WatchTower ticket MEMO-FIX-10."),
        _tool_result_json({"ref": "MEMO-FIX-10", "title": "Lean PostCompact hook"}),
    ]))
    payload = json.dumps({"session_id": "abc123", "transcript_path": str(transcript)})

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload, capture_output=True, text=True, timeout=5,
    )
    elapsed = time.time() - t0

    assert proc.returncode == 0
    assert elapsed < 1.0
    assert "MEMO-FIX-10" in proc.stdout
    assert "Lean PostCompact hook" in proc.stdout
    assert "ccc recall" in proc.stdout
    assert len(proc.stdout) <= hook.MAX_BLOCK_CHARS + 1  # trailing newline from print()


def test_prints_continued_from_via_stdin(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_jsonl([
        _user_text(
            "You are continuing a task from an earlier session, which ran long.\n\n"
            "Origin session id: 93580c29-29f9-4ce3-8077-db00ea0a920f\n"
            "Task: Continue the work from where it left off."
        ),
        _user_text("finish the relaunch runbook"),
    ]))
    payload = json.dumps({"session_id": "abc123", "transcript_path": str(transcript)})

    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload, capture_output=True, text=True, timeout=5,
    )

    assert proc.returncode == 0
    assert "Continued from: 93580c29-29f9-4ce3-8077-db00ea0a920f" in proc.stdout


def test_never_fails_the_turn_on_bad_input():
    for bad_input in ("", "not json", "{}", '{"session_id": "x"}',
                      json.dumps({"session_id": "x", "transcript_path": "/no/such/file.jsonl"})):
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=bad_input, capture_output=True, text=True, timeout=5,
        )
        assert proc.returncode == 0, bad_input
        assert proc.stdout == ""


def test_clears_compacting_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "LIVE_STATE_DIR", str(tmp_path))
    marker = tmp_path / "sess1_compacting.json"
    marker.write_text("{}")

    hook._clear_compacting_marker("sess1")

    assert not marker.exists()


def test_clear_compacting_marker_missing_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "LIVE_STATE_DIR", str(tmp_path))
    hook._clear_compacting_marker("no-such-session")  # must not raise
