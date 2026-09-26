"""Annotations route to the queue only where a queue is configured for CCC."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import server


class _Queue:
    def __init__(self, configured):
        self.configured = configured

    def _queue_for_repo_path(self, repo_path):
        return self.configured


class TestAnnotateTarget(unittest.TestCase):
    def _target(self, configured, env=None):
        environ = {k: v for k, v in os.environ.items() if k != "CCC_ANNOTATE_TARGET"}
        environ.update(env or {})
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(server, "_q", _Queue(configured)):
            return server._annotate_target()

    def test_configured_queue_keeps_queue(self):
        self.assertEqual(self._target("CCC"), "queue")

    def test_public_install_uses_github(self):
        self.assertEqual(self._target(""), "github")

    def test_env_override_wins(self):
        self.assertEqual(self._target("CCC", {"CCC_ANNOTATE_TARGET": "github"}), "github")
        self.assertEqual(self._target("", {"CCC_ANNOTATE_TARGET": "queue"}), "queue")

    def test_queue_without_matcher_falls_back_to_github(self):
        with mock.patch.object(server, "_q", object()):
            self.assertEqual(server._annotate_target(), "github")


if __name__ == "__main__":
    unittest.main()
