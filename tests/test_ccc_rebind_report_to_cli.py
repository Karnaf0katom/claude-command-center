"""`ccc rebind-report-to` CLI (MEMORY-5): the summary should report open WT
tickets and queue subscriptions the forward now also covers, not just the
report-route count. No live server/wt -- _post_json/_resolve_target/_wt_argv
are mocked, matching tests/test_ccc_continuation_cli.py's pattern.
"""

import argparse
import importlib.machinery
import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


def _load_ccc_cli():
    ccc_path = Path(__file__).resolve().parent.parent / "ccc"
    loader = importlib.machinery.SourceFileLoader("ccc_cli_rebind_report_to", str(ccc_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _rebind_args(**overrides):
    base = dict(
        server="http://127.0.0.1:8099", new_report_to="new-sid",
        from_report_to=None, child=None, json=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class RebindReportToCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ccc = _load_ccc_cli()

    def test_from_report_to_reports_ticket_and_subscription_counts(self):
        ccc = self.ccc

        def fake_resolve_target(base, target):
            return target, None

        def fake_post_json(base, path, payload):
            return 200, {"ok": True, "report_to": "new-sid", "rebound": ["rr_1", "rr_2"]}

        with mock.patch.object(ccc, "_resolve_server", return_value="http://127.0.0.1:8099"), \
             mock.patch.object(ccc, "_resolve_target", side_effect=fake_resolve_target), \
             mock.patch.object(ccc, "_post_json", side_effect=fake_post_json), \
             mock.patch.object(ccc, "_rebind_wt_counts",
                                return_value={"open_tickets": 3, "queue_subscriptions": 2}):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = ccc.cmd_rebind_report_to(
                    _rebind_args(from_report_to="old-sid")
                )
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("2 child report route(s)", text)
        self.assertIn("3 open ticket(s)", text)
        self.assertIn("2 queue subscription(s)", text)
        self.assertIn("new-sid", text)

    def test_child_only_skips_wt_counts(self):
        ccc = self.ccc

        def fake_resolve_target(base, target):
            return target, None

        def fake_post_json(base, path, payload):
            return 200, {"ok": True, "report_to": "new-sid", "rebound": ["rr_1"]}

        with mock.patch.object(ccc, "_resolve_server", return_value="http://127.0.0.1:8099"), \
             mock.patch.object(ccc, "_resolve_target", side_effect=fake_resolve_target), \
             mock.patch.object(ccc, "_post_json", side_effect=fake_post_json), \
             mock.patch.object(ccc, "_rebind_wt_counts") as counts_mock:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = ccc.cmd_rebind_report_to(_rebind_args(child="child-sid"))
        self.assertEqual(rc, 0)
        counts_mock.assert_not_called()
        self.assertIn("1 child report route(s)", out.getvalue())
        self.assertNotIn("open ticket(s)", out.getvalue())

    def test_json_output_includes_wt_counts(self):
        ccc = self.ccc

        def fake_resolve_target(base, target):
            return target, None

        def fake_post_json(base, path, payload):
            return 200, {"ok": True, "report_to": "new-sid", "rebound": ["rr_1"]}

        with mock.patch.object(ccc, "_resolve_server", return_value="http://127.0.0.1:8099"), \
             mock.patch.object(ccc, "_resolve_target", side_effect=fake_resolve_target), \
             mock.patch.object(ccc, "_post_json", side_effect=fake_post_json), \
             mock.patch.object(ccc, "_rebind_wt_counts",
                                return_value={"open_tickets": 1, "queue_subscriptions": 0}):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = ccc.cmd_rebind_report_to(
                    _rebind_args(from_report_to="old-sid", json=True)
                )
        self.assertEqual(rc, 0)
        import json
        body = json.loads(out.getvalue())
        self.assertEqual(body["wt_counts"], {"open_tickets": 1, "queue_subscriptions": 0})

    def test_wt_argv_absent_returns_none_counts(self):
        ccc = self.ccc
        with mock.patch.object(ccc.shutil, "which", return_value=None), \
             mock.patch.object(ccc.os, "access", return_value=False):
            self.assertIsNone(ccc._wt_argv())
            self.assertIsNone(ccc._rebind_wt_counts("old-sid"))

    def test_rebind_wt_counts_filters_by_submitter_and_target(self):
        ccc = self.ccc

        def fake_wt_json(wt, args, timeout=6):
            if args[:1] == ["status"]:
                return [{"queue": "MEMORY"}, {"queue": "WT"}]
            if args[:1] == ["ls"]:
                queue = args[args.index("-q") + 1]
                if queue == "MEMORY":
                    return [{"submitter": "old-sid"}, {"submitter": "other-sid"}]
                return [{"submitter": "old-sid"}]
            if args[:1] == ["subscribe"]:
                queue = args[1]
                return ["old-sid"] if queue == "MEMORY" else []
            return None

        with mock.patch.object(ccc, "_wt_argv", return_value=["wt"]), \
             mock.patch.object(ccc, "_wt_json", side_effect=fake_wt_json):
            counts = ccc._rebind_wt_counts("old-sid")
        self.assertEqual(counts, {"open_tickets": 2, "queue_subscriptions": 1})


if __name__ == "__main__":
    unittest.main()
