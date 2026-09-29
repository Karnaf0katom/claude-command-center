import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class CodexLogRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.native = Path(self.tmp.name, "rollout.jsonl")
        self.log = Path(self.tmp.name, "spawn.log")
        self.native.write_text(json.dumps({"type": "event_msg", "timestamp": "2026-09-29T19:23:06Z",
                                          "payload": {"type": "task_started", "turn_id": "turn-1"}}) + "\n")
        self.rows = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed", "item": {"id": "msg-1", "type": "agent_message", "text": "Checking."}},
            {"type": "item.started", "item": {"id": "cmd-1", "type": "command_execution", "command": "pwd", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "cmd-1", "type": "command_execution", "command": "pwd", "status": "completed", "aggregated_output": "example", "exit_code": 0}},
            {"type": "item.completed", "item": {"id": "msg-2", "type": "agent_message", "text": "Recovered answer."}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5, "cached_input_tokens": 10}},
        ]
        self.write_log()
        patches = [
            mock.patch.object(server, "_detect_session_engine", return_value="codex"),
            mock.patch.object(server, "_resolve_codex_rollout_path", return_value=self.native),
            mock.patch.object(server, "_resolve_conversation_reader", side_effect=lambda *args, **kwargs: (self.native, server._parse_codex_event) if server._codex_native_recovery_metadata(self.native) is None else (self.log, server._parse_codex_exec_log_event)),
            mock.patch.object(server, "_codex_logs_for_session", return_value=[(1, str(self.log))]),
            mock.patch.object(server, "_codex_thread_row", return_value={"first_user_message": "Original request", "model": "example-model"}),
            mock.patch.object(server, "_get_queued_events_for_session", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        server._CONV_PARSE_CACHE.clear()
        from ccc_server import codex_log_recovery
        codex_log_recovery._NATIVE_READINESS.clear()
        codex_log_recovery._CAPTURE_PATHS.clear()

    def write_log(self):
        self.log.write_text("diagnostic header\n" + "\n".join(json.dumps(row) for row in self.rows) + "\n")

    def test_completed_output_renders_prompt_tools_answer_and_usage(self):
        result = server.parse_conversation("thread-1")
        events = result["events"]
        self.assertEqual(events[0]["text"], "Original request")
        text = [b["text"] for e in events for b in e.get("blocks", []) if b.get("kind") == "text"]
        self.assertEqual(text, ["Checking.", "Recovered answer."])
        tools = [b for e in events for b in e.get("blocks", []) if b.get("kind") == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(next(e for e in events if e["type"] == "tool_result")["text"], "example")
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["token_usage"]["input_tokens"], 20)
        self.assertFalse(events[-1].get("no_agent_output"))

    def test_wrong_thread_never_recovers(self):
        self.rows[0]["thread_id"] = "other-thread"
        self.write_log()
        result = server.parse_conversation("thread-1", use_cache=False)
        self.assertFalse(any(e["type"] == "assistant" for e in result["events"]))

    def test_native_output_wins_without_duplicate_answer(self):
        with self.native.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Native answer."}}) + "\n")
        result = server.parse_conversation("thread-1", use_cache=False)
        text = [b["text"] for e in result["events"] for b in e.get("blocks", []) if b.get("kind") == "text"]
        self.assertEqual(text, ["Native answer."])

    def test_native_completion_is_authoritative(self):
        with self.native.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Native answer."}}) + "\n")
        result = server.parse_conversation("thread-1", use_cache=False)
        self.assertFalse(any(e.get("recovered_from_log") for e in result["events"]))

    def test_native_readiness_does_not_rescan_unchanged_history(self):
        from ccc_server import codex_log_recovery
        server.parse_conversation("thread-1")
        with mock.patch.object(codex_log_recovery.json, "loads", wraps=json.loads) as decode:
            codex_log_recovery._codex_native_recovery_metadata(self.native)
        decode.assert_not_called()

    def test_http_cache_fingerprint_changes_when_capture_grows(self):
        server.parse_conversation("thread-1")
        first = server._conv_overlay_fingerprint("thread-1")
        self.rows.append({"type": "item.completed", "item": {"id": "msg-3", "type": "agent_message", "text": "Extra output"}})
        self.write_log()
        self.assertNotEqual(first, server._conv_overlay_fingerprint("thread-1"))

    def test_http_cache_cannot_hide_first_capture_created_later(self):
        with mock.patch.object(server, "_codex_logs_for_session", return_value=[]):
            server.parse_conversation("thread-1")
            server._conv_response_bytes_put("thread-1", 0, b"stale-empty", b"stale-empty")
        self.assertIsNone(server._conv_response_bytes_get("thread-1", 0))
        result = server.parse_conversation("thread-1")
        self.assertTrue(any(e.get("recovered_from_log") for e in result["events"]))

    def test_appended_log_output_bypasses_stale_native_cache(self):
        self.rows = self.rows[:2]
        self.write_log()
        first = server.parse_conversation("thread-1")
        self.rows.append({"type": "item.completed", "item": {"id": "msg-2", "type": "agent_message", "text": "Later answer."}})
        self.write_log()
        later = server.parse_conversation("thread-1", after_line=first["last_line"])
        self.assertEqual(later["events"][0]["blocks"][0]["text"], "Later answer.")

    def test_tail_and_earlier_pages_do_not_duplicate(self):
        full = server.parse_conversation("thread-1")
        tail = server.parse_conversation("thread-1", tail=2)
        self.assertTrue(tail["truncated_before"])
        earlier = server.parse_conversation("thread-1", tail=100, before=tail["first_line"])
        self.assertEqual(earlier["events"] + tail["events"], full["events"])

    def make_handler(self):
        handler = server.CommandCenterHandler.__new__(server.CommandCenterHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        return handler

    def test_stream_follows_capture_append_with_unchanged_native_rollout(self):
        self.rows = self.rows[:2]
        self.write_log()
        first = server.parse_conversation("thread-1")
        handler = self.make_handler()
        sleeps = 0

        def append_then_disconnect(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                self.rows.append({"type": "item.completed", "item": {"id": "msg-2", "type": "agent_message", "text": "Streamed answer."}})
                self.write_log()
            else:
                raise BrokenPipeError()

        with mock.patch.object(server.time, "sleep", side_effect=append_then_disconnect):
            handler._stream_codex_capture("thread-1", self.native, first["last_line"])
        payload = json.loads(handler.wfile.getvalue().decode().strip().removeprefix("data: "))
        self.assertEqual(payload["events"][0]["blocks"][0]["text"], "Streamed answer.")
        self.assertEqual(len(payload["events"]), 1)

    def test_stream_requests_reload_when_native_source_becomes_available(self):
        server.parse_conversation("thread-1")
        with self.native.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Native answer."}}) + "\n")
        handler = self.make_handler()
        handler._stream_codex_capture("thread-1", self.native, 8)
        self.assertIn(b"event: source_reset", handler.wfile.getvalue())

    def test_stream_discovers_new_capture_after_existing_capture(self):
        first = server.parse_conversation("thread-1")
        later_log = Path(self.tmp.name, "resume.log")
        handler = self.make_handler()
        logs = [(1, str(self.log))]
        sleeps = 0

        def create_then_disconnect(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                later_log.write_text(json.dumps(self.rows[0]) + "\n" + json.dumps({"type": "item.completed", "item": {"id": "msg-3", "type": "agent_message", "text": "Resume answer."}}) + "\n")
                logs.append((2, str(later_log)))
            else:
                raise BrokenPipeError()

        with mock.patch.object(server, "_codex_logs_for_session", side_effect=lambda _: list(logs)), mock.patch.object(server.time, "sleep", side_effect=create_then_disconnect):
            handler._stream_codex_capture("thread-1", self.native, first["last_line"])
        payload = json.loads(handler.wfile.getvalue().decode().strip().removeprefix("data: "))
        self.assertEqual(payload["events"][0]["blocks"][0]["text"], "Resume answer.")

    def test_stream_discovers_first_capture_created_after_subscription(self):
        handler = self.make_handler()
        logs = []
        sleeps = 0

        def create_then_disconnect(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                logs.append((1, str(self.log)))
            else:
                raise BrokenPipeError()

        with mock.patch.object(server, "_codex_logs_for_session", side_effect=lambda _: list(logs)), mock.patch.object(server.time, "sleep", side_effect=create_then_disconnect):
            handler._stream_codex_capture("thread-1", self.native, 0)
        payload = json.loads(handler.wfile.getvalue().decode().strip().removeprefix("data: "))
        self.assertTrue(any(b.get("text") == "Recovered answer." for e in payload["events"] for b in e.get("blocks", [])))


if __name__ == "__main__":
    unittest.main()
