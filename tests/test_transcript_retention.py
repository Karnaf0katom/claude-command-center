"""Unit tests for ccc_server.transcript_retention (cleanupPeriodDays writer)."""

import glob
import json
import os
import shutil
import tempfile
import unittest

from ccc_server import transcript_retention as tr


class TranscriptRetentionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def _read(self):
        with open(self.path) as f:
            return json.load(f)

    def _backups(self):
        return glob.glob(self.path + ".ccc-backup-*")

    def test_read_missing_file_reports_default(self):
        st = tr.read_claude_retention(self.path)
        self.assertIsNone(st["configured_days"])
        self.assertEqual(st["effective_days"], 30)
        self.assertIsNone(st["error"])

    def test_read_malformed_reports_error(self):
        self._write("{not json")
        st = tr.read_claude_retention(self.path)
        self.assertIsNone(st["effective_days"])
        self.assertIn("malformed", st["error"])

    def test_missing_file_is_created(self):
        res = tr.ensure_claude_retention(3650, self.path)
        self.assertTrue(res["ok"] and res["changed"])
        self.assertIsNone(res["previous_days"])
        self.assertIsNone(res["backup"])
        self.assertEqual(self._read(), {"cleanupPeriodDays": 3650})

    def test_missing_key_added_other_keys_preserved(self):
        self._write(json.dumps({"model": "sonnet", "hooks": {"Stop": [{"x": 1}]}}))
        res = tr.ensure_claude_retention(3650, self.path)
        self.assertTrue(res["changed"])
        data = self._read()
        self.assertEqual(data["cleanupPeriodDays"], 3650)
        self.assertEqual(data["model"], "sonnet")
        self.assertEqual(data["hooks"], {"Stop": [{"x": 1}]})
        self.assertEqual(len(self._backups()), 1)

    def test_lower_value_is_raised(self):
        self._write(json.dumps({"cleanupPeriodDays": 30}))
        res = tr.ensure_claude_retention(3650, self.path)
        self.assertTrue(res["changed"])
        self.assertEqual(res["previous_days"], 30)
        self.assertEqual(self._read()["cleanupPeriodDays"], 3650)
        with open(self._backups()[0]) as f:
            self.assertEqual(json.load(f), {"cleanupPeriodDays": 30})

    def test_higher_value_never_lowered(self):
        self._write(json.dumps({"cleanupPeriodDays": 99999}))
        res = tr.ensure_claude_retention(3650, self.path)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual(self._read()["cleanupPeriodDays"], 99999)
        self.assertEqual(self._backups(), [])

    def test_malformed_json_untouched(self):
        self._write("{not json")
        res = tr.ensure_claude_retention(3650, self.path)
        self.assertFalse(res["ok"])
        self.assertIn("malformed", res["error"])
        with open(self.path) as f:
            self.assertEqual(f.read(), "{not json")
        self.assertEqual(self._backups(), [])

    def test_symlink_writes_real_file_and_keeps_link(self):
        real_dir = os.path.join(self.dir, "dotfiles")
        os.makedirs(real_dir)
        real = os.path.join(real_dir, "settings.json")
        with open(real, "w") as f:
            json.dump({"theme": "dark"}, f)
        os.symlink(real, self.path)
        res = tr.ensure_claude_retention(3650, self.path)
        self.assertTrue(res["changed"])
        self.assertTrue(os.path.islink(self.path))
        self.assertEqual(os.path.realpath(self.path), os.path.realpath(real))
        with open(real) as f:
            self.assertEqual(json.load(f), {"theme": "dark", "cleanupPeriodDays": 3650})
        self.assertEqual(len(glob.glob(real + ".ccc-backup-*")), 1)

    def test_invalid_days_rejected(self):
        res = tr.ensure_claude_retention(0, self.path)
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
