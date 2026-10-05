import importlib.util
import json
import os
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location(
    "ccc_stop_hook", os.path.join(os.path.dirname(__file__), "..", "hooks", "stop.py"))
stop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stop)


class FirstPromptSnippetTests(unittest.TestCase):
    def _snippet(self, *lines):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(json.dumps(l, separators=(",", ":")) for l in lines))
        try:
            return stop.first_prompt_snippet(f.name)
        finally:
            os.unlink(f.name)

    def test_user_message(self):
        got = self._snippet({"type": "user", "message": {"content": [{"type": "text", "text": "Fix the  bug"}]}})
        self.assertEqual(got, "Fix the bug")

    def test_queue_enqueue_and_truncation(self):
        got = self._snippet({"type": "queue-operation", "operation": "enqueue", "content": "x" * 100})
        self.assertEqual(got, "x" * 60 + "…")

    def test_missing_file(self):
        self.assertEqual(stop.first_prompt_snippet("/nonexistent"), "")


if __name__ == "__main__":
    unittest.main()
