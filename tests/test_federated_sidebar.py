"""Federated sidebar backend: each peer's browser-reachable web URL is derived
from its phone-access status, cached per peer per TTL (never per row), and
attached to the `nodes` entries of /api/sessions?federated=1."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: F401  (initializes ccc_server core state)
from ccc_server import fleet, phone_access


def _status(**kw):
    base = {
        "ok": True, "url": "", "pin_set": False,
        "tailscale": {"hostname": "box.tail1.ts.net"},
        "serve": {"plan": {"action": "reuse", "https_port": 443}},
    }
    base.update(kw)
    return base


class TestWebUrlFromStatus(unittest.TestCase):
    def test_created_entry_wins(self):
        self.assertEqual(
            phone_access.web_url_from_status(_status(url="https://box.tail1.ts.net:8443/")),
            "https://box.tail1.ts.net:8443/")

    def test_reuses_hand_made_serve_entry(self):
        self.assertEqual(phone_access.web_url_from_status(_status()),
                         "https://box.tail1.ts.net/")
        st = _status(serve={"plan": {"action": "reuse", "https_port": 8443}})
        self.assertEqual(phone_access.web_url_from_status(st),
                         "https://box.tail1.ts.net:8443/")

    def test_no_address(self):
        self.assertEqual(phone_access.web_url_from_status(
            _status(serve={"plan": {"action": "create", "https_port": 443}})), "")
        self.assertEqual(phone_access.web_url_from_status(
            _status(tailscale={"hostname": ""})), "")
        self.assertEqual(phone_access.web_url_from_status({"ok": False}), "")
        self.assertEqual(phone_access.web_url_from_status(None), "")


class TestPeerWebUrlCache(unittest.TestCase):
    def setUp(self):
        fleet._PEER_WEB_URL_CACHE.clear()
        fleet._FEDERATED_SESSIONS_CACHE.clear()

    def _proxy(self, result):
        return mock.patch.object(fleet._core, "_federation_proxy_session_action",
                                 return_value=result)

    def test_states(self):
        with self._proxy(_status()):
            self.assertEqual(fleet._federation_peer_web_url("a"),
                             {"web_url": "https://box.tail1.ts.net/", "web_url_state": "ok"})
        with self._proxy(_status(pin_set=True)):
            self.assertEqual(fleet._federation_peer_web_url("b"),
                             {"web_url": None, "web_url_state": "pin"})
        with self._proxy(_status(serve={"plan": {"action": "create"}})):
            self.assertEqual(fleet._federation_peer_web_url("c"),
                             {"web_url": None, "web_url_state": "none"})
        with self._proxy({"ok": False, "error": "unreachable"}):
            self.assertEqual(fleet._federation_peer_web_url("d"),
                             {"web_url": None, "web_url_state": "unknown"})

    def test_failure_keeps_last_good_address(self):
        with self._proxy(_status()):
            fleet._federation_peer_web_url("a")
        fleet._PEER_WEB_URL_CACHE["a"]["expires"] = 0  # TTL elapsed
        with self._proxy({"ok": False}):
            out = fleet._federation_peer_web_url("a")
        self.assertEqual(out["web_url"], "https://box.tail1.ts.net/")

    def test_no_fetch_when_peer_unreachable(self):
        with self._proxy(_status()) as m:
            out = fleet._federation_peer_web_url("a", allow_fetch=False)
        self.assertEqual(out["web_url_state"], "unknown")
        m.assert_not_called()

    def test_one_call_per_peer_per_ttl_never_per_row(self):
        peers = [{"node_id": f"n{i}", "name": f"peer{i}"} for i in range(3)]
        rows = [{"session_id": f"s{j}", "timestamp": 1.0 + j} for j in range(300)]
        payload = {"observed_at": 1.0, "sessions": rows}
        with mock.patch.object(fleet, "_federation_self_hello",
                               return_value={"node_id": "me", "display_name": "me"}), \
             mock.patch.object(fleet.federation, "load_peers", return_value=peers), \
             mock.patch.object(fleet, "_federation_touch_peer"), \
             mock.patch.object(fleet.federation, "PeerClient") as pc, \
             self._proxy(_status()) as proxy:
            pc.return_value.request.side_effect = lambda *a, **k: {
                **payload, "sessions": [dict(r) for r in rows]}
            for _ in range(5):
                out = fleet._federation_federated_sessions(limit=300, peers_only=True)
        self.assertEqual(len(out["sessions"]), 900)
        self.assertEqual(proxy.call_count, 3)          # one per peer, not per row/poll
        self.assertEqual(pc.return_value.request.call_count, 3)  # 10s session cache
        peer_nodes = [n for n in out["nodes"] if not n["self"]]
        self.assertTrue(all(n["web_url"] == "https://box.tail1.ts.net/" for n in peer_nodes))
        self.assertTrue(all(n["web_url_state"] == "ok" for n in peer_nodes))

    def test_peers_only_skips_local_inventory(self):
        with mock.patch.object(fleet, "_federation_self_hello",
                               return_value={"node_id": "me", "display_name": "me"}), \
             mock.patch.object(fleet, "_federation_sessions_inventory") as inv, \
             mock.patch.object(fleet.federation, "load_peers", return_value=[]):
            out = fleet._federation_federated_sessions(limit=5, peers_only=True)
        inv.assert_not_called()
        self.assertEqual(out["sessions"], [])
        self.assertEqual([n["self"] for n in out["nodes"]], [True])


if __name__ == "__main__":
    unittest.main()
