import argparse
import base64
import json
import os
import queue
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import Agent
from coordinator import Coordinator
from dashboard import start_dashboard
from test_agent import fake_steady_worker, proxy


class FakeCoordinator:
    def __init__(self):
        self.snapshot = {"time": 1.0, "bots": []}
        self.commands = queue.Queue()


def call(server, path, password="pw", body=None, headers=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"admin:{password}".encode()).decode())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class DashboardServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.coord = FakeCoordinator()
        cls.server = start_dashboard(cls.coord, ("127.0.0.1", 0), "pw")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_needs_password(self):
        self.assertEqual(call(self.server, "/api/state", password="wrong")[0], 401)

    def test_page_and_state(self):
        status, body = call(self.server, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Powerline Bots", body)
        status, body = call(self.server, "/api/state")
        self.assertEqual((status, json.loads(body)), (200, self.coord.snapshot))

    def test_commands_need_header_and_known_action(self):
        self.assertEqual(call(self.server, "/api/command", body={"action": "stop_all"})[0], 403)
        ok = {"X-Requested-With": "dashboard", "Content-Type": "application/json"}
        self.assertEqual(call(self.server, "/api/command", body={"action": "rm -rf"}, headers=ok)[0], 400)
        self.assertEqual(call(self.server, "/api/command", body={"action": "stop_bot", "bot": 1}, headers=ok)[0], 202)
        self.assertEqual(self.coord.commands.get_nowait(), {"action": "stop_bot", "bot": 1})


def make_coordinator(*agents, groups=2, bpp=2):
    args = argparse.Namespace(webshare_key="x", room="room", groups=groups, bots_per_proxy=bpp, stagger_interval=0.0)
    coord = Coordinator(args, list(agents))
    coord.proxy_mgr.proxies = [proxy("a"), proxy("b"), proxy("c")]
    coord.group_proxies = [proxy("a"), proxy("b")][:groups]
    return coord


class CoordinatorCommandTest(unittest.TestCase):
    def setUp(self):
        self.one = Agent(name="one", worker_target=fake_steady_worker)
        self.two = Agent(name="two", worker_target=fake_steady_worker)
        self.coord = make_coordinator(self.one, self.two)
        for bot_id in range(4):
            self.coord.place_bot(bot_id, 0)

    def tearDown(self):
        self.one.stop_all()
        self.two.stop_all()

    def machine_bots(self, agent):
        return sorted(b for b, a in self.coord.bot_agent.items() if a is agent)

    def test_stop_and_start_bot(self):
        self.coord.run_command({"action": "stop_bot", "bot": 1})
        self.assertNotIn(1, self.coord.bot_agent)
        self.coord.place_pending()
        self.assertNotIn(1, self.coord.bot_agent)  # Stopped bots aren't restarted
        self.coord.run_command({"action": "start_bot", "bot": 1})
        self.assertIn(1, self.coord.bot_agent)

    def test_pause_moves_bots_and_resume(self):
        self.coord.run_command({"action": "pause_machine", "machine": "one"})
        self.assertEqual(self.machine_bots(self.one), [])
        self.assertEqual(self.machine_bots(self.two), [0, 1, 2, 3])
        self.coord.run_command({"action": "resume_machine", "machine": "one"})
        self.assertNotIn("one", self.coord.paused)

    def test_set_max_bots_moves_extras(self):
        self.coord.run_command({"action": "set_max_bots", "machine": "two", "max_bots": 0})
        self.assertEqual(self.machine_bots(self.two), [])
        self.assertEqual(self.one.max_bots, None)
        self.assertEqual(self.two.max_bots, 0)

    def test_set_groups_adds_and_removes_bots(self):
        self.coord.run_command({"action": "set_groups", "groups": 3})
        self.assertEqual(sorted(self.coord.bot_agent), [0, 1, 2, 3, 4, 5])
        self.assertEqual(len(self.coord.group_proxies), 3)
        self.coord.run_command({"action": "set_groups", "groups": 1})
        self.assertEqual(sorted(self.coord.bot_agent), [0, 1])
        self.assertEqual(len(self.coord.group_proxies), 1)

    def test_invalid_commands_are_ignored(self):
        before = dict(self.coord.bot_agent)
        for cmd in ({"action": "stop_bot", "bot": 99}, {"action": "pause_machine", "machine": "nope"},
                    {"action": "set_groups", "groups": 0}, {"action": "explode"}):
            self.coord.run_command(cmd)
        self.assertEqual(self.coord.bot_agent, before)

    def test_snapshot_is_json(self):
        self.coord.refresh_health()
        snap = self.coord.build_snapshot()
        json.dumps(snap)
        self.assertEqual(len(snap["bots"]), 4)
        self.assertEqual({m["name"] for m in snap["machines"]}, {"one", "two"})
        self.assertEqual(snap["group_proxies"][1]["host"], "b")


if __name__ == "__main__":
    unittest.main()
