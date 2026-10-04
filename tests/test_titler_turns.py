import unittest
from unittest import mock

import server

TITLER = "Produce a concise 4-8 word title summarizing what the user is trying to do"


def _turn(sid, folder, preview, tin=30000, tout=300, end="2026-10-04T21:00:00Z"):
    return {"session_id": sid, "folder_path": folder, "trigger_preview": preview,
            "assistant_preview": "A title", "tokens_in": tin, "tokens_out": tout,
            "t_end": end, "dur_sec": 3.0, "model": "claude-haiku-4-5"}


class TitlerTurnsPayload(unittest.TestCase):
    def setUp(self):
        server._TITLER_TURNS_CACHE.update(ts=0.0, payload=None)

    def test_only_titler_turns_in_scratch_are_counted(self):
        turns = [
            _turn("a", "-x--claude-command-center-scratch", TITLER + " below"),
            _turn("b", "-x--claude-command-center-scratch", "some Ask question"),
            _turn("c", "-x-Apps-repo", TITLER),
        ]
        with mock.patch.object(server, "_throughput_window_turns", return_value=turns) as w:
            p = server._titler_turns_payload()
            server._titler_turns_payload()  # TTL cache: no second scan
        self.assertEqual(w.call_count, 1)
        self.assertEqual(p["turn_count"], 1)
        self.assertEqual(p["total_tokens"], 30300)
        self.assertEqual(p["turns"][0]["session_id"], "a")


if __name__ == "__main__":
    unittest.main()
