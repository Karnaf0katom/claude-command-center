"""Phone access on a paired peer, end to end, across two real CCC nodes.

Node A drives setup on node B (its "Phone access" flag off) through the
federation route envelope, the way the Fleet page does. Node B runs a
stand-in `tailscale` CLI that keeps its serve config in a JSON file, so the
only thing faked is the Tailscale daemon. Covers: serve-port conflict
avoidance, origin trust with no restart, the PIN gate over HTTP, admin
endpoints refusing proxied callers, and removing only what CCC created.
"""

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from two_node_harness import TwoNodeFleet

HOST_B = "node-b.example-tailnet.ts.net"
FOREIGN_443 = "http://127.0.0.1:18765"  # someone else's (dead) serve entry

FAKE_TAILSCALE = r'''#!/usr/bin/env python3
import json, sys
CFG = __CFG__
HOST = __HOST__
args = sys.argv[1:]
cfg = json.load(open(CFG))
def save():
    json.dump(cfg, open(CFG, "w"))
if args[:2] == ["status", "--json"]:
    print(json.dumps({"BackendState": "Running", "Version": "1.0-fake",
        "Self": {"DNSName": HOST + ".", "UserID": 1, "TailscaleIPs": ["100.100.1.2"]},
        "User": {"1": {"LoginName": "tester@example.com"}},
        "CurrentTailnet": {"Name": "tester@example.com", "MagicDNSEnabled": True},
        "CertDomains": [HOST]}))
elif args[:3] == ["serve", "status", "--json"]:
    print(json.dumps(cfg))
elif args[:2] == ["serve", "--bg"]:
    port = args[2].split("=", 1)[1]
    cfg.setdefault("TCP", {})[port] = {"HTTPS": True}
    cfg.setdefault("Web", {})[HOST + ":" + port] = {"Handlers": {"/": {"Proxy": args[3]}}}
    save()
elif args[0] == "serve" and args[-1] == "off":
    port = args[1].split("=", 1)[1]
    cfg.get("TCP", {}).pop(port, None)
    cfg.get("Web", {}).pop(HOST + ":" + port, None)
    save()
else:
    sys.exit("fake tailscale: unsupported " + " ".join(args))
'''


class TestPhoneAccessTwoNode(unittest.TestCase):
    fleet: TwoNodeFleet = None

    @classmethod
    def setUpClass(cls):
        cls.fleet = TwoNodeFleet()
        cls.serve_cfg = cls.fleet.base / "fake-serve.json"
        cls.serve_cfg.write_text(json.dumps({
            "TCP": {"443": {"HTTPS": True}},
            "Web": {f"{HOST_B}:443": {"Handlers": {"/": {"Proxy": FOREIGN_443}}}},
        }))
        fake = cls.fleet.base / "fake-tailscale"
        fake.write_text(FAKE_TAILSCALE.replace("__CFG__", repr(str(cls.serve_cfg)))
                        .replace("__HOST__", repr(HOST_B)))
        fake.chmod(0o755)
        cls.fleet.node_a.start()
        cls.fleet.node_b.start(extra_env={"CCC_TAILSCALE_BIN": str(fake)})
        cls.fleet.node_a.wait_ready()
        cls.fleet.node_b.wait_ready()
        cls.fleet.pair()
        cls.b_id = cls.fleet.node_b.get("/api/federation/v1/hello")["node_id"]

    @classmethod
    def tearDownClass(cls):
        cls.fleet.cleanup()

    def _raw(self, node, method, path, body=None, headers=None):
        """(status, text, headers) without the harness's JSON assumptions."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{node.port}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers

    def test_routed_setup_trust_pin_and_teardown(self):
        a, b = self.fleet.node_a, self.fleet.node_b
        origin = f"https://{HOST_B}:8443"
        echo = "/api/phone-access/echo"

        # Before setup, the phone's origin is refused like any foreign site.
        status, text, _ = self._raw(b, "POST", echo, {"nonce": "n1"}, {"Origin": origin})
        self.assertEqual(status, 403, text)

        # B's own browser surface is gated by B's (off) flag...
        status, payload = b.post("/api/phone-access/enable", {}, expect_error=True)
        self.assertEqual((status, payload["error"]), (403, "feature_disabled"))

        # ...but a routed call from paired A sets B up, skipping the foreign 443.
        _, res = a.post("/api/phone-access/enable", {"node_id": self.b_id})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["url"], origin + "/")
        self.assertEqual(res["created_by"], "ccc")
        self.assertEqual(res.get("routed_to"), self.b_id)
        cfg = json.loads(self.serve_cfg.read_text())
        self.assertEqual(cfg["Web"][f"{HOST_B}:443"]["Handlers"]["/"]["Proxy"], FOREIGN_443)
        self.assertEqual(cfg["Web"][f"{HOST_B}:8443"]["Handlers"]["/"]["Proxy"],
                         f"http://127.0.0.1:{b.port}")

        # The new origin is trusted immediately: no restart happened.
        status, text, _ = self._raw(b, "POST", echo, {"nonce": "n2"}, {"Origin": origin})
        self.assertEqual(status, 200, text)
        self.assertEqual(json.loads(text)["nonce"], "n2")

        # The Fleet page's per-node list shows B's phone URL.
        nodes = a.get("/api/phone-access/nodes")["nodes"]
        row = next(n for n in nodes if n.get("node_id") == self.b_id)
        self.assertTrue(row["enabled"], row)
        self.assertEqual(row["url"], origin + "/")

        # Admin endpoints refuse anything that came through the proxy.
        proxied = {"Origin": origin, "Host": f"{HOST_B}:8443", "X-Forwarded-For": "100.64.0.9"}
        status, text, _ = self._raw(b, "POST", "/api/phone-access/disable", {}, proxied)
        self.assertEqual(status, 403, text)

        # PIN gate: proxied callers need the cookie, loopback never does.
        b.post("/api/phone-access/pin", {"pin": "482913"})
        status, text, _ = self._raw(b, "GET", "/", headers=proxied)
        self.assertEqual(status, 401)
        self.assertIn("PIN", text)
        status, _, _ = self._raw(b, "GET", "/api/network-config", headers=proxied)
        self.assertEqual(status, 401)
        status, _, _ = self._raw(b, "GET", "/")
        self.assertEqual(status, 200)
        status, text, _ = self._raw(b, "POST", "/api/phone-access/unlock", {"pin": "000000"}, proxied)
        self.assertEqual(status, 401, text)
        status, text, hdrs = self._raw(b, "POST", "/api/phone-access/unlock", {"pin": "482913"}, proxied)
        self.assertEqual(status, 200, text)
        cookie = hdrs.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        status, _, _ = self._raw(b, "GET", "/", headers={**proxied, "Cookie": cookie.split(";", 1)[0]})
        self.assertEqual(status, 200)
        b.post("/api/phone-access/pin", {"clear": True})

        # Turning it off removes only CCC's entry, and trust goes with it.
        _, off = a.post("/api/phone-access/disable", {"node_id": self.b_id})
        self.assertTrue(off.get("ok") and off.get("removed"), off)
        cfg = json.loads(self.serve_cfg.read_text())
        self.assertIn(f"{HOST_B}:443", cfg["Web"])
        self.assertNotIn(f"{HOST_B}:8443", cfg["Web"])
        status, text, _ = self._raw(b, "POST", echo, {"nonce": "n3"}, {"Origin": origin})
        self.assertEqual(status, 403, text)


if __name__ == "__main__":
    unittest.main()
