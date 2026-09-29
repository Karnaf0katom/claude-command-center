"""WatchTower ticket dependencies (WT-4 `blocked_by`) in CCC (CCC-1221).

WatchTower skips a ticket until every blocker is closed as completed. CCC
must agree: a blocked ticket is not claimable work (no fake staffing alarm),
the queue panel names the blocker instead of READY, and the header counts
blocked tickets apart from ready ones. Fixtures: open / closed / declined
blocker. The JS predicate is exercised under node against the same fixtures.
"""

import json
import pathlib
import re
import shutil
import subprocess
import unittest
from unittest import mock

import server
from ccc_server import wt_review as wr
from ccc_server.queue_events import compute_queues_health

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _t(ref, status="open", **kw):
    item = {"ref": ref, "project": "CCC", "status": status, "type": "bug",
            "readiness": "ready", "created_at": "2026-09-29T10:00:00Z",
            "updated_at": "2026-09-29T11:00:00Z"}
    item.update(kw)
    return item


ITEMS = [
    _t("CCC-1"),                                                   # open blocker
    _t("CCC-2", "closed"),                                         # completed
    _t("CCC-3", "closed", product_nack=True),                      # declined
    _t("CCC-4", "closed", resolution={"unresolved": ["todo"]}),    # unresolved
    _t("CCC-5", "in_review"),                                      # gate pending
    _t("CCC-10", blocked_by=["CCC-1"]),
    _t("CCC-11", blocked_by=["CCC-2"]),
    _t("CCC-12", blocked_by=["CCC-3"]),
    _t("CCC-13", blocked_by=["CCC-4"]),
    _t("CCC-14", blocked_by=["CCC-2", "CCC-1"]),
    _t("CCC-15", blocked_by=["CCC-5"]),
    _t("CCC-16", blocked_by=["CCC-404"]),                          # missing: ignored
]

EXPECTED = {
    "CCC-10": ["waiting", "CCC-1"],
    "CCC-11": ["ok", ""],
    "CCC-12": ["stuck", "CCC-3"],
    "CCC-13": ["stuck", "CCC-4"],
    "CCC-14": ["waiting", "CCC-1"],
    "CCC-15": ["waiting", "CCC-5"],
    "CCC-16": ["ok", ""],
}


class PredicateTest(unittest.TestCase):
    def test_python_verdicts(self):
        by_ref = wr.refs_index(ITEMS)
        got = {it["ref"]: list(wr.blocker_verdict(it, by_ref))
               for it in ITEMS if it.get("blocked_by")}
        self.assertEqual(got, EXPECTED)

    def test_escalated_stuck_blocker_answered_by_a_human_counts_as_satisfied(self):
        it = _t("CCC-20", blocked_by=["CCC-3"], blocker_escalated=["CCC-3"])
        self.assertEqual(wr.blocker_verdict(it, wr.refs_index(ITEMS)), ("ok", ""))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_verdicts_match(self):
        src = (ROOT / "static" / "app.js").read_text()
        m = re.search(r"  function _uxqBlockerVerdict\(it, byRef\) \{.*?\n  \}\n", src, re.S)
        self.assertIsNotNone(m)
        script = (m.group(0)
                  + "const items = " + json.dumps(ITEMS) + ";\n"
                  + "const byRef = new Map(items.map(i => [i.ref, i]));\n"
                  + "const out = {};\n"
                  + "items.filter(i => i.blocked_by).forEach(i => { out[i.ref] = _uxqBlockerVerdict(i, byRef); });\n"
                  + "process.stdout.write(JSON.stringify(out));\n")
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), EXPECTED)


class QueueHealthTest(unittest.TestCase):
    def test_blocked_tickets_are_not_claimable_and_counted(self):
        health = [{"project": "CCC", "depth": 8, "stuck": True}]
        with mock.patch.object(server, "_wt_read_config",
                               return_value={"CCC": {"auto_drain": True, "claim_types": []}}):
            row = [r for r in compute_queues_health(health=health, wt_workers=[], items=ITEMS)
                   if r["queue"] == "CCC"][0]
        # Claimable: CCC-1, CCC-11, CCC-16. Blocked: CCC-10, 12, 13, 14, 15.
        self.assertEqual(row["claimable"], 3)
        self.assertEqual(row["blocked"], 5)


class FrontendWiringTest(unittest.TestCase):
    def test_queue_panel_chip_and_count(self):
        src = (ROOT / "static" / "app.js").read_text()
        self.assertIn("'waiting on ' + escapeHtml(waitingOn)", src)
        self.assertIn("data-blocker-ref", src)
        self.assertIn("it.readiness && it.status !== 'closed'", src)
        self.assertIn("' blocked</span>'", src)


if __name__ == "__main__":
    unittest.main()
