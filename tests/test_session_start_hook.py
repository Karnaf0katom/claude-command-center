import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import server

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "session-start.py"


class SpawnMarkerCallerTests(unittest.TestCase):
    def test_caller_only_marker_has_no_lane(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"caller": "grade-findings.ts", "parent_session_id": "aaaaaaaa-1111"}))
            m = server._decode_spawn_marker_file(p)
        self.assertEqual(m, {"caller": "grade-findings.ts", "parent_session_id": "aaaaaaaa-1111"})

    def test_apply_stamps_caller_and_parent_without_lane(self):
        rows = [{"session_id": "child-0001"}, {"session_id": "kid-00002", "parent_session_id": "keep-me-1"}]
        markers = {
            "child-0001": {"caller": "x.ts", "parent_session_id": "parent-01"},
            "kid-00002": {"caller": "y.ts", "parent_session_id": "other-002"},
        }
        server._apply_spawn_markers(rows, markers)
        self.assertEqual(rows[0]["spawn_caller"], "x.ts")
        self.assertEqual(rows[0]["parent_session_id"], "parent-01")
        self.assertNotIn("spawned_lane", rows[0])
        self.assertEqual(rows[1]["parent_session_id"], "keep-me-1")


class HookLabelTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ccc_session_start_hook", HOOK)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_labels(self):
        L = self.mod._label
        self.assertEqual(L("node /x/node_modules/.bin/tsx scripts/becky-replay/grade-findings.ts --a"), "grade-findings.ts")
        self.assertEqual(L("/bin/zsh -c foo"), "")
        self.assertEqual(L("/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"), "Terminal")
        self.assertEqual(L("/usr/bin/python3 /a/b/run_eval.py"), "run_eval.py")
        self.assertEqual(
            L("/opt/homebrew/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python my-eval-script.py"),
            "my-eval-script.py")

    def test_bad_payload_is_silent(self):
        r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
