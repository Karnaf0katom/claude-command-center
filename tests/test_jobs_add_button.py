"""Jobs tab "+ Add" (CCC-1223).

The button opens a dialog whose submit spawns an agent session with a
composed prompt; CCC itself never writes systemd units or LaunchAgents.
This runs the real prompt composer under node for both hosts.
"""

import json
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIELDS = {"repo": "/srv/proj", "what": "Summarize failed CI runs", "when": "daily 7am"}


def _compose(host):
    src = (ROOT / "static" / "jobs-tab.js").read_text()
    m = re.search(r"  function composePrompt\(f\) \{.*?\n  \}\n", src, re.S)
    assert m, "composePrompt not found"
    code = m.group(0) + "process.stdout.write(composePrompt(" + json.dumps(dict(FIELDS, host=host)) + "));"
    res = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return res.stdout


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ComposePromptTest(unittest.TestCase):
    def test_fields_and_common_contract(self):
        for host in ("hermes", "laptop"):
            p = _compose(host)
            self.assertIn("Summarize failed CI runs", p)
            self.assertIn("daily 7am", p)
            self.assertIn("/srv/proj", p)
            self.assertIn("CCC_OUTCOME:", p)
            self.assertIn("/api/jobs", p)

    def test_hermes_uses_systemd_timer(self):
        p = _compose("hermes")
        self.assertIn("ssh hermes", p)
        self.assertIn("/etc/systemd/system/<name>.timer", p)
        self.assertIn("User=hermes", p)
        self.assertIn("enable --now", p)
        self.assertNotIn("LaunchAgents", p)

    def test_laptop_uses_launchd(self):
        p = _compose("laptop")
        self.assertIn("~/Library/LaunchAgents/", p)
        self.assertIn("StartCalendarInterval", p)
        self.assertIn("~/Library/Logs/<name>/", p)
        self.assertIn("launchctl bootstrap", p)
        self.assertNotIn("systemd", p)


class WiringTest(unittest.TestCase):
    def test_button_and_spawn_path(self):
        js = (ROOT / "static" / "jobs-tab.js").read_text()
        self.assertIn("data-jobs-add", js)
        self.assertIn("window.cccSpawnPromptSession", js)
        app = (ROOT / "static" / "app.js").read_text()
        self.assertIn("window.cccSpawnPromptSession = async function", app)


if __name__ == "__main__":
    unittest.main()
