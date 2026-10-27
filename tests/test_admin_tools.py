"""The dashboard's admin tools: live logs, history, screenshots, settings, machine controls, alerts, bulk actions."""
import argparse
import base64
import http.client
import json
import logging
import multiprocessing
import os
import queue
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
from agent import Agent, recv_msg, send_msg, serve
from bot import SCREENSHOT_REQUEST, PowerlineBot, ReportLogHandler
from coordinator import ALERT_MEMORY, CPU_WINDOW, FAILING_STREAK, Coordinator, RemoteAgent, proxy_key
from dashboard import start_dashboard
from fakes import FakeAgent, proxy
from test_agent import fake_steady_worker
from test_coordinator import make, start_all


def drain(q, timeout=0.3):
    items = []
    while True:
        try:
            items.append(q.get(timeout=timeout))
        except queue.Empty:
            return items


def wait_for(check, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    return None


def shooting_worker(bot_id, room, proxy_, headless, start_delay, nickname, updates, debug_dir, home, reports):
    """Answers screenshot requests the way a real bot does, with a tiny fake JPEG."""
    while True:
        msg = updates.get()
        if msg == "stop":
            return
        if msg == SCREENSHOT_REQUEST:
            reports.put({"bot": bot_id, "screenshot": base64.b64encode(b"\xff\xd8jpeg").decode(), "time": time.time()})


class BotSideTest(unittest.TestCase):
    def test_screenshot_request_is_answered_once(self):
        updates, reports = multiprocessing.Queue(), multiprocessing.Queue()
        b = PowerlineBot(instance_id=3, proxy_updates=updates, reports=reports)
        taken = []

        class Page:
            def screenshot(self, type, quality):
                taken.append((type, quality))
                return b"JPEGDATA"

        updates.put(SCREENSHOT_REQUEST)
        time.sleep(0.2)
        self.assertFalse(b.poll_proxy_update())  # A screenshot request is not a proxy change
        b.serve_screenshot(Page())
        b.serve_screenshot(Page())  # Not asked again: nothing more
        self.assertEqual(taken, [("jpeg", 55)])
        report = drain(reports)[0]
        self.assertEqual((report["bot"], base64.b64decode(report["screenshot"])), (3, b"JPEGDATA"))

    def test_screenshot_is_answered_while_the_page_loads(self):
        from fakes import FakePage
        updates, reports = multiprocessing.Queue(), multiprocessing.Queue()
        b = PowerlineBot(instance_id=4, proxy_updates=updates, reports=reports)
        page = FakePage(evaluate=lambda s: True)
        page.lobby_after = 3  # The lobby shows up on the 4th one-second wait
        updates.put(SCREENSHOT_REQUEST)
        time.sleep(0.2)
        b.open_game(page)
        shots = [r for r in drain(reports) if "screenshot" in r]
        self.assertEqual(len(shots), 1)
        self.assertEqual(page.waits, 4)

    def test_new_proxy_during_page_load_ends_the_wait(self):
        from fakes import FakePage
        updates = multiprocessing.Queue()
        b = PowerlineBot(instance_id=4, proxy_updates=updates)
        page = FakePage(evaluate=lambda s: True)
        page.lobby_appears = False
        updates.put(proxy("new"))
        time.sleep(0.2)
        started = time.time()
        b.open_game(page)
        self.assertLess(time.time() - started, 5)  # Didn't sit out the full 30s
        self.assertEqual(b.pending_proxy["host"], "new")

    def test_failed_screenshot_is_harmless(self):
        reports = multiprocessing.Queue()
        b = PowerlineBot(instance_id=3, reports=reports)
        b.screenshot_wanted = True

        class Page:
            def screenshot(self, **_):
                raise RuntimeError("page closed")

        b.serve_screenshot(Page())
        self.assertFalse(b.screenshot_wanted)

    def test_log_lines_are_forwarded(self):
        reports = multiprocessing.Queue()
        log = logging.getLogger("test-forward")
        log.addHandler(ReportLogHandler(9, reports))
        log.setLevel(logging.INFO)
        log.info("Round ended after %.1fs.", 7.25)
        log.debug("not forwarded")
        got = drain(reports)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["bot"], got[0]["log"]["message"], got[0]["log"]["level"]), (9, "Round ended after 7.2s.", "INFO"))


class AgentSideTest(unittest.TestCase):
    def setUp(self):
        self.agent = Agent(name="logger-test", worker_target=shooting_worker)

    def tearDown(self):
        self.agent.stop_all(wait=True)

    def test_logs_are_sent_once_and_capped(self):
        self.agent.workers[1] = (None, None, "")  # Pretend bot 1 runs here
        for i in range(250):
            self.agent._file_report({"bot": 1, "log": {"time": i, "level": "INFO", "message": f"line {i}"}})
        first = self.agent.new_logs()
        self.assertEqual(len(first), 200)  # Capped per report
        self.assertEqual(first[-1]["message"], "line 249")
        self.assertEqual(self.agent.new_logs(), [])  # Sent once
        del self.agent.workers[1]

    def test_own_lines_only(self):
        other = Agent(name="someone-else", worker_target=fake_steady_worker)
        logging.getLogger("PowerlineBot.Agent").info("[Agent logger-test] hello")
        mine = [l["message"] for l in self.agent.new_logs()]
        theirs = [l["message"] for l in other.new_logs()]
        self.assertIn("[Agent logger-test] hello", mine)
        self.assertNotIn("[Agent logger-test] hello", theirs)

    def test_screenshot_round_trip_through_a_real_worker(self):
        self.agent.start_bot(2, "r", proxy("a"), "N", 0)
        self.agent.handle({"type": "screenshot", "bot": 2})
        events = wait_for(lambda: [e for e in self.agent.poll() if e["type"] == "screenshot"])
        self.assertEqual(events[0]["bot"], 2)
        self.assertEqual(base64.b64decode(events[0]["data"]), b"\xff\xd8jpeg")

    def test_screenshot_for_a_bot_not_here_is_ignored(self):
        self.agent.request_screenshot(42)
        self.agent._file_report({"bot": 42, "screenshot": "x", "time": 1.0})
        self.assertEqual(self.agent.poll(), [])

    def test_restart_message_ends_serve(self):
        coord_end, agent_end = multiprocessing.Pipe()
        recv_hello = None
        import threading
        done = []
        t = threading.Thread(target=lambda: (serve(agent_end, self.agent), done.append(True)), daemon=True)
        t.start()
        recv_hello = recv_msg(coord_end)
        send_msg(coord_end, {"type": "restart"})
        t.join(5)
        self.assertEqual(recv_hello["type"], "hello")
        self.assertTrue(done and self.agent.restart_requested)


class CoordinatorDataTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = FakeAgent("alpha", cpu=40), FakeAgent("beta", cpu=60)
        self.coord = make(self.a, self.b, groups=2, bpp=2)
        start_all(self.coord)

    def tick(self):
        self.coord.next_health = 0
        self.coord.refresh_health()

    def test_logs_are_collected_and_filtered(self):
        self.a.logs = [{"seq": 1, "bot": 0, "time": 10.0, "level": "INFO", "message": "bot zero"},
                       {"seq": 2, "bot": None, "time": 11.0, "level": "WARNING", "message": "agent alpha"}]
        self.b.logs = [{"seq": 1, "bot": 1, "time": 10.5, "level": "INFO", "message": "bot one"}]
        self.tick()
        everything = self.coord.logs_view()
        self.assertEqual([l["message"] for l in everything], ["bot zero", "bot one", "agent alpha"])  # By time
        self.assertEqual({l["machine"] for l in everything}, {"alpha", "beta"})
        self.assertEqual([l["message"] for l in self.coord.logs_view(machine="alpha")], ["bot zero", "agent alpha"])
        self.assertEqual([l["message"] for l in self.coord.logs_view(bot=1)], ["bot one"])
        self.assertEqual([l["message"] for l in self.coord.logs_view(limit=1)], ["agent alpha"])
        self.assertEqual(self.coord.logs_view(machine="nope"), [])

    def test_history_records_cpu_ram_and_bots(self):
        self.a.bot_status = {"0": {"state": "playing"}}
        samples_before = len(self.coord.history_view()["bots"])  # The setup already took one
        for _ in range(3):
            self.tick()
        hist = self.coord.history_view()
        self.assertEqual(set(hist["machines"]), {"alpha", "beta"})
        t, cpu, ram = hist["machines"]["alpha"][-1]
        self.assertEqual((cpu, ram), (40.0, 50.0))  # FakeAgent: 4000 of 8000 MB free
        self.assertEqual(hist["bots"][-1][1:], [4, 1])  # 4 running, 1 playing
        self.assertEqual(len(hist["bots"]) - samples_before, 3)
        json.dumps(hist)

    def test_screenshot_events_are_kept_per_bot(self):
        self.a.events.append({"type": "screenshot", "bot": 0, "data": "QUJD", "time": 123.0})
        self.coord.handle_exits()
        self.assertEqual(self.coord.screenshot_view(0), {"time": 123.0, "data": "QUJD", "machine": "alpha"})
        self.assertEqual(self.coord.build_snapshot()["screenshot_times"], {"0": 123.0})
        self.assertIn(0, self.coord.bot_agent)  # Not mistaken for an exit

    def test_screenshot_command_goes_to_the_right_machine(self):
        owner = self.coord.bot_agent[3]
        self.coord.run_command({"action": "screenshot", "bot": 3})
        self.assertIn(("screenshot", 3), owner.calls)

    def test_screenshot_of_a_stopped_bot_fails_politely(self):
        self.coord.run_command({"action": "stop_bot", "bot": 3})
        self.coord.commands.put({"action": "screenshot", "bot": 3})
        self.coord.run_commands()
        self.assertTrue(any("isn't running" in e["message"] for e in self.coord.events))


class BulkAndMachineTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = FakeAgent("alpha"), FakeAgent("beta")
        self.coord = make(self.a, self.b, groups=2, bpp=2)
        start_all(self.coord)

    def running(self):
        return sorted(self.coord.bot_agent)

    def test_bulk_stop_start_restart(self):
        self.coord.run_command({"action": "stop_bots", "bots": [0, 2, 99, "x", True]})
        self.assertEqual(self.running(), [1, 3])
        self.assertEqual(self.coord.stopped, {0, 2})
        self.coord.run_command({"action": "start_bots", "bots": [0, 2]})
        self.assertEqual(self.running(), [0, 1, 2, 3])
        self.coord.failures[1] = 7
        self.coord.run_command({"action": "restart_bots", "bots": [1]})
        self.assertEqual(self.coord.failures[1], 0)

    def test_empty_or_invalid_bulk_is_ignored(self):
        before = self.running()
        for bots in ([], [99], "all", None):
            self.coord.run_command({"action": "stop_bots", "bots": bots})
        self.assertEqual(self.running(), before)

    def test_stop_machine_bots(self):
        alpha_bots = sorted(b for b, ag in self.coord.bot_agent.items() if ag is self.a)
        self.coord.run_command({"action": "stop_machine_bots", "machine": "alpha"})
        self.assertEqual(self.a.running, {})
        self.assertTrue(set(alpha_bots) <= self.coord.stopped)

    def test_kill_machine_bots_force_stops_only_that_machine(self):
        alpha_bots = sorted(b for b, ag in self.coord.bot_agent.items() if ag is self.a)
        self.coord.run_command({"action": "kill_machine_bots", "machine": "alpha"})
        self.assertEqual(self.a.running, {})
        self.assertIn(("kill", tuple(alpha_bots)), self.a.calls)
        self.assertTrue(set(alpha_bots) <= self.coord.stopped)
        self.assertFalse(any(c[0] == "kill" for c in self.b.calls))

    def test_kill_all_force_stops_everything(self):
        self.coord.run_command({"action": "kill_all"})
        self.assertEqual(self.a.running, {})
        self.assertEqual(self.b.running, {})
        self.assertTrue(any(c[0] == "kill" for c in self.a.calls))
        self.assertEqual(self.coord.stopped, set(range(self.coord.total)))
        self.assertEqual(self.coord.bot_agent, {})

    def test_restart_local_machine_restarts_its_bots(self):
        before = sorted(self.a.running)
        self.coord.run_command({"action": "restart_machine", "machine": "alpha"})
        self.assertEqual(sorted(self.a.running), before)
        self.assertIn(("stop", tuple(before)), self.a.calls)

    def test_restart_remote_machine_sends_restart(self):
        mine, theirs = multiprocessing.Pipe()
        remote = RemoteAgent(mine, {"agent": "pc", "cpu_count": 12, "cpu_percent": 1.0})
        self.coord.agents.append(remote)
        self.coord.run_command({"action": "restart_machine", "machine": "pc"})
        self.assertEqual(recv_msg(theirs), {"type": "restart"})


class ReconnectTest(unittest.TestCase):
    def test_a_machine_reconnecting_replaces_its_old_link(self):
        coord = make(groups=1, bpp=2)
        old_mine, old_theirs = multiprocessing.Pipe()
        old = RemoteAgent(old_mine, {"agent": "pc", "cpu_count": 8, "cpu_percent": 1.0})
        coord.agents.append(old)
        coord.refresh_health()
        coord.place_bot(0, 0)
        self.assertIs(coord.bot_agent[0], old)
        new_mine, _ = multiprocessing.Pipe()
        coord.joined.append(RemoteAgent(new_mine, {"agent": "pc", "cpu_count": 8, "cpu_percent": 1.0,
                                                   "mem_available_mb": 1, "max_bots": None}))
        coord.add_joined_agents()
        self.assertEqual([a.name for a in coord.agents], ["pc"])  # Listed once
        self.assertIsNot(coord.agents[0], old)
        self.assertIn(0, coord.pending)  # Its bot gets re-placed, on the new link
        coord.place_pending()
        self.assertIs(coord.bot_agent[0], coord.agents[0])
        messages = [e["message"] for e in coord.events]
        left = next(i for i, m in enumerate(messages) if "disconnected" in m)
        joined = next(i for i, m in enumerate(messages) if "joined" in m)
        self.assertLess(left, joined)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.a = FakeAgent("alpha")
        self.coord = make(self.a, groups=2, bpp=2)
        self.coord.args.proxy_check_interval = 60.0
        start_all(self.coord)

    def settings(self, **change):
        cmd = {"action": "set_settings", "room": "2cjg", "bots_per_proxy": 2,
               "proxy_check_interval": 60, "stagger_interval": 0}
        cmd.update(change)
        self.coord.commands.put(cmd)
        self.coord.run_commands()

    def test_intervals_change_without_restarting_bots(self):
        calls_before = len(self.a.calls)
        self.coord.args.room = "2cjg"
        self.settings(proxy_check_interval=120, stagger_interval=2.5)
        self.assertEqual((self.coord.args.proxy_check_interval, self.coord.args.stagger_interval), (120.0, 2.5))
        self.assertEqual(len(self.a.calls), calls_before)

    def test_new_room_restarts_the_bots(self):
        self.settings(room="abc123")
        self.assertEqual(self.coord.args.room, "abc123")
        self.assertIn(("stop", (0, 1, 2, 3)), self.a.calls)
        self.assertEqual(sorted(self.a.running), [0, 1, 2, 3])

    def test_more_bots_per_group(self):
        names = list(self.coord.nicknames)
        self.settings(room=self.coord.args.room, bots_per_proxy=3)
        self.assertEqual(self.coord.total, 6)
        self.assertEqual(sorted(self.a.running), [0, 1, 2, 3, 4, 5])
        self.assertEqual(self.coord.nicknames[:4], names[:4])  # Existing names kept
        self.assertEqual(self.a.running[5]["proxy"], self.coord.group_proxies[1])  # Bot 5 is in group 1 now

    def test_fewer_bots_per_group_keeps_everything_stopped_stopped(self):
        self.coord.run_command({"action": "stop_all"})
        self.settings(room=self.coord.args.room, bots_per_proxy=1)
        self.assertEqual(self.coord.total, 2)
        self.assertEqual(self.a.running, {})
        self.assertEqual(self.coord.stopped, {0, 1})

    def test_invalid_settings_change_nothing(self):
        room = self.coord.args.room
        for bad in ({"room": "has space"}, {"room": ""}, {"room": "x" * 17}, {"bots_per_proxy": 0},
                    {"bots_per_proxy": 11}, {"proxy_check_interval": 5}, {"stagger_interval": -1}, {"bots_per_proxy": True}):
            self.settings(**dict({"room": room}, **bad))
        self.assertEqual((self.coord.args.room, self.coord.args.bots_per_proxy), (room, 2))
        self.assertTrue(sum("failed" in e["message"] for e in self.coord.events) >= 8)

    def test_settings_survive_a_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        self.coord.state_file = path
        self.settings(room="zz9", bots_per_proxy=3, proxy_check_interval=90, stagger_interval=1.5)
        self.coord.run_command({"action": "set_groups", "groups": 3})
        self.coord.save_state()
        args = argparse.Namespace(webshare_key="x", room="2cjg", groups=2, bots_per_proxy=2,
                                  stagger_interval=0.0, proxy_check_interval=60.0)
        again = Coordinator(args, [], state_file=path)
        self.assertEqual((again.args.room, again.groups, again.args.bots_per_proxy), ("zz9", 3, 3))
        self.assertEqual((again.args.proxy_check_interval, again.args.stagger_interval), (90.0, 1.5))
        self.assertEqual(again.nicknames, self.coord.nicknames)
        self.assertEqual(again.build_snapshot()["settings"]["room"], "zz9")


class AlertsTest(unittest.TestCase):
    def keys(self, coord):
        return {a["key"] for a in coord.alerts(time.time())}

    def test_quiet_when_all_is_well(self):
        coord = make(FakeAgent("a"), groups=1, bpp=1)
        start_all(coord)
        self.assertEqual(coord.alerts(time.time()), [])

    def test_no_machines(self):
        self.assertIn("no-machines", self.keys(make(groups=1, bpp=1)))

    def test_overloaded_machine(self):
        coord = make(FakeAgent("hot", cpu=97), groups=1, bpp=1)
        for _ in range(CPU_WINDOW):
            coord.next_health = 0
            coord.refresh_health()
        self.assertIn("hot:hot", self.keys(coord))

    def test_waiting_and_failing_bots(self):
        coord = make(FakeAgent("small", max_bots=1), groups=1, bpp=2)
        start_all(coord)
        coord.failures[0] = FAILING_STREAK
        keys = self.keys(coord)
        self.assertIn("waiting", keys)
        self.assertIn("failing", keys)
        coord.run_command({"action": "stop_bot", "bot": 0})
        self.assertNotIn("failing", self.keys(coord))  # Stopped bots don't alarm

    def test_all_proxies_benched(self):
        coord = make(FakeAgent("a"), groups=1, bpp=1)
        for p in coord.proxy_mgr.proxies:
            coord.bad_until[proxy_key(p)] = time.time() + 100
        self.assertIn("all-benched", self.keys(coord))

    def test_webshare_down(self):
        coord = make(FakeAgent("a"), groups=1, bpp=1)
        coord.proxy_mgr.last_error = "timed out"
        coord.proxy_mgr.last_success = time.time() - 1000
        self.assertIn("webshare", self.keys(coord))
        coord.proxy_mgr.last_success = time.time()
        self.assertNotIn("webshare", self.keys(coord))

    def test_disconnect_is_remembered_then_forgotten(self):
        a, b = FakeAgent("a"), FakeAgent("b")
        coord = make(a, b, groups=1, bpp=1)
        b.alive = False
        coord.drop_dead_agents()
        self.assertIn("left:b", self.keys(coord))
        self.assertNotIn("left:b", {x["key"] for x in coord.alerts(time.time() + ALERT_MEMORY + 1)})
        coord.agents.append(FakeAgent("b"))  # Back again: no alert
        self.assertNotIn("left:b", self.keys(coord))


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = FakeAgent("alpha")
        cls.coord = make(cls.a, groups=1, bpp=2)
        start_all(cls.coord)
        cls.a.logs = [{"seq": 1, "bot": 0, "time": 1.0, "level": "INFO", "message": "hello"},
                      {"seq": 2, "bot": 1, "time": 2.0, "level": "INFO", "message": "world"}]
        cls.coord.next_health = 0
        cls.coord.refresh_health()
        cls.coord.screenshots[1] = {"time": 5.0, "data": "QUJD", "machine": "alpha"}
        cls.server = start_dashboard(cls.coord, ("127.0.0.1", 0), "pw")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("GET", path, headers={"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, json.loads(body)

    def test_logs_endpoint(self):
        self.assertEqual([l["message"] for l in self.get("/api/logs")[1]], ["hello", "world"])
        self.assertEqual([l["message"] for l in self.get("/api/logs?bot=1")[1]], ["world"])
        self.assertEqual([l["message"] for l in self.get("/api/logs?machine=alpha&limit=1")[1]], ["world"])
        self.assertEqual(self.get("/api/logs?bot=notanumber")[0], 200)  # Ignored, not an error

    def test_history_endpoint(self):
        status, hist = self.get("/api/history")
        self.assertEqual(status, 200)
        self.assertIn("alpha", hist["machines"])

    def test_screenshot_endpoint(self):
        self.assertEqual(self.get("/api/screenshot?bot=1"), (200, {"time": 5.0, "data": "QUJD", "machine": "alpha"}))
        self.assertEqual(self.get("/api/screenshot?bot=0")[0], 404)
        self.assertEqual(self.get("/api/screenshot")[0], 404)


if __name__ == "__main__":
    unittest.main()
