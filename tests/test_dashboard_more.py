import base64
import http.client
import json
import os
import queue
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard
from coordinator import RemoteAgent
from dashboard import ACTIONS, MAX_BODY, PAGE_PATH, start_dashboard
from fakes import FakeAgent
from test_coordinator import make, start_all

AUTH = {"Authorization": "Basic " + base64.b64encode(b"anyone:pw").decode()}
CMD = dict(AUTH, **{"X-Requested-With": "dashboard", "Content-Type": "application/json"})


class FakeCoordinator:
    def __init__(self):
        self.snapshot = {"time": 1.0}
        self.commands = queue.Queue()


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.coord = FakeCoordinator()
        cls.server = start_dashboard(cls.coord, ("127.0.0.1", 0), "pw")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_no_login_asks_for_one(self):
        resp, _ = self.request("GET", "/")
        self.assertEqual(resp.status, 401)
        self.assertIn("Basic", resp.getheader("WWW-Authenticate"))

    def test_garbage_login_is_refused(self):
        resp, _ = self.request("GET", "/", headers={"Authorization": "Basic !!!not-base64"})
        self.assertEqual(resp.status, 401)

    def test_any_user_name_with_the_password(self):
        for user in ("admin", "fernand", ""):
            auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:pw".encode()).decode()}
            self.assertEqual(self.request("GET", "/api/state", headers=auth)[0].status, 200)

    def test_page_headers(self):
        resp, body = self.request("GET", "/", headers=AUTH)
        self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        with open(PAGE_PATH, "rb") as f:
            self.assertEqual(body, f.read())

    def test_unknown_paths(self):
        self.assertEqual(self.request("GET", "/etc/passwd", headers=AUTH)[0].status, 404)
        self.assertEqual(self.request("GET", "/../dashboard.py", headers=AUTH)[0].status, 404)
        self.assertEqual(self.request("POST", "/api/state", body="{}", headers=CMD)[0].status, 404)

    def test_bad_command_bodies(self):
        cases = [
            ("not json", 400),
            ("[1, 2]", 400),
            ('{"bot": 1}', 400),
            ('{"action": "shutdown_server"}', 400),
            ("x" * (MAX_BODY + 1), 400),
        ]
        for body, status in cases:
            self.assertEqual(self.request("POST", "/api/command", body=body, headers=CMD)[0].status, status, body[:30])
        self.assertTrue(self.coord.commands.empty())

    def test_empty_command_body(self):
        resp, _ = self.request("POST", "/api/command", body="", headers=CMD)
        self.assertEqual(resp.status, 400)

    def test_cross_site_post_without_header_is_refused(self):
        headers = dict(AUTH, **{"Content-Type": "application/x-www-form-urlencoded"})
        resp, _ = self.request("POST", "/api/command", body="action=stop_all", headers=headers)
        self.assertEqual(resp.status, 403)

    def test_every_action_is_accepted(self):
        for action in sorted(ACTIONS):
            resp, body = self.request("POST", "/api/command", body=json.dumps({"action": action}), headers=CMD)
            self.assertEqual((resp.status, json.loads(body)), (202, {"ok": True}))
        got = []
        while not self.coord.commands.empty():
            got.append(self.coord.commands.get_nowait()["action"])
        self.assertEqual(sorted(got), sorted(ACTIONS))


class PageTest(unittest.TestCase):
    def test_every_action_the_page_sends_is_known(self):
        with open(PAGE_PATH) as f:
            html = f.read()
        sent = (set(re.findall(r'action: "([a-z_]+)"', html)) | set(re.findall(r'data-action="([a-z_]+)"', html))
                | set(re.findall(r'data-bulk="([a-z_]+)"', html)))
        self.assertTrue(sent)
        self.assertEqual(sent - ACTIONS, set())
        self.assertEqual(ACTIONS - sent, set(), "an action the server supports has no button")

    def test_page_escapes_names_and_events(self):
        with open(PAGE_PATH) as f:
            html = f.read()
        for field in ("b.name", "m.name", "e.message", "b.machine", "b.proxy", "l.message", "l.machine", "a.text", "s.name"):
            self.assertNotIn("${" + field + "}", html, f"{field} is inserted without esc()")


class MoreCommandTest(unittest.TestCase):
    def setUp(self):
        self.a = FakeAgent("a")
        self.coord = make(self.a, groups=2, bpp=2)
        start_all(self.coord)

    def test_restart_resets_backoff(self):
        self.coord.failures[2] = 5
        self.coord.run_command({"action": "restart_bot", "bot": 2})
        self.assertEqual(self.coord.failures[2], 0)
        self.assertIn(("stop", (2,)), self.a.calls)
        self.assertIn(2, self.a.running)

    def test_start_all_only_starts_existing_bots(self):
        self.coord.run_command({"action": "stop_all"})
        self.assertEqual(self.a.running, {})
        self.coord.stopped.add(99)  # A bot from a larger setup, no longer exists
        self.coord.run_command({"action": "start_all"})
        self.assertEqual(sorted(self.a.running), [0, 1, 2, 3])
        self.assertEqual(self.coord.stopped, set())

    def test_negative_or_bogus_limit_means_no_limit(self):
        for value in (-3, "lots", None):
            self.coord.run_command({"action": "set_max_bots", "machine": "a", "max_bots": value})
            self.assertIsNone(self.a.max_bots)

    def test_group_count_limits(self):
        for groups in (0, 51, "3", 2.5):
            self.coord.run_command({"action": "set_groups", "groups": groups})
            self.assertEqual(self.coord.groups, 2)

    def test_stopped_bots_stay_stopped_through_crashes_and_rebalance(self):
        self.coord.run_command({"action": "stop_bot", "bot": 1})
        self.coord.handle_exits()
        self.coord.place_pending()
        self.assertNotIn(1, self.a.running)
        snap = {b["id"]: b["state"] for b in self.coord.build_snapshot()["bots"]}
        self.assertEqual(snap[1], "stopped")

    def test_limit_on_a_remote_machine_is_sent_to_it(self):
        import multiprocessing
        from agent import recv_msg
        mine, theirs = multiprocessing.Pipe()
        remote = RemoteAgent(mine, {"agent": "pc", "cpu_count": 12, "cpu_percent": 1.0})
        self.coord.agents.append(remote)
        self.coord.run_command({"action": "set_max_bots", "machine": "pc", "max_bots": 4})
        self.assertEqual(recv_msg(theirs), {"type": "set_max_bots", "max_bots": 4})
        self.assertEqual(self.coord.saved_max["pc"], 4)

    def test_events_feed_records_dashboard_actions(self):
        self.coord.run_command({"action": "stop_bot", "bot": 0})
        messages = [e["message"] for e in self.coord.build_snapshot()["events"]]
        self.assertTrue(any("Dashboard: stopping bot #0" in m for m in messages))

    def test_a_failing_command_is_logged_not_fatal(self):
        self.coord.commands.put({"action": "stop_bot", "bot": 0})
        with mock.patch.object(self.coord, "unplace", side_effect=RuntimeError("boom")):
            self.coord.run_commands()  # Must not raise
        self.assertTrue(any("failed" in e["message"] for e in self.coord.events))


if __name__ == "__main__":
    unittest.main()
