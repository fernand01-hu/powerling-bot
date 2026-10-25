import multiprocessing
import multiprocessing.connection
import os
import re
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_module
from agent import (
    MAX_MESSAGE,
    Agent,
    agent_key,
    code_version,
    parse_address,
    recv_msg,
    run_agent,
    send_msg,
    serve,
)
from fakes import proxy
from test_agent import fake_steady_worker


def echo_worker(bot_id, room, proxy_, headless, start_delay, nickname, updates, debug_dir, home, reports):
    """Reports every proxy update it receives as its 'state', until told to stop."""
    reports.put({"bot": bot_id, "status": {"state": "started:" + proxy_["host"]}})
    while True:
        update = updates.get()
        if update == "stop":
            return
        reports.put({"bot": bot_id, "status": {"state": "switched:" + update["host"]}})


def crash_worker(bot_id, *args):
    sys.exit(1)


def wait_for(check, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    return None


class HelpersTest(unittest.TestCase):
    def test_code_version_is_a_stable_fingerprint(self):
        self.assertRegex(code_version(), r"^[0-9a-f]{12}$")
        self.assertEqual(code_version(), code_version())

    def test_parse_address(self):
        self.assertEqual(parse_address("192.168.1.96"), ("192.168.1.96", agent_module.DEFAULT_PORT))
        self.assertEqual(parse_address("192.168.1.96:5000"), ("192.168.1.96", 5000))
        self.assertEqual(parse_address("server.lan:4041"), ("server.lan", 4041))

    def test_agent_key(self):
        with mock.patch.dict(os.environ, {"POWERLINE_AGENT_KEY": "s3cret"}):
            self.assertEqual(agent_key(), b"s3cret")
        with mock.patch.dict(os.environ, {"POWERLINE_AGENT_KEY": ""}):
            with self.assertRaises(SystemExit):
                agent_key()


class MessageTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = multiprocessing.Pipe()

    def test_round_trip(self):
        msg = {"type": "start", "bot": 3, "proxy": proxy("x"), "nickname": "Ünïcode✓", "start_delay": 1.5}
        send_msg(self.a, msg)
        self.assertEqual(recv_msg(self.b), msg)

    def test_rejects_non_objects_and_untyped_messages(self):
        for raw in (b"[1, 2]", b'"text"', b'{"bot": 1}', b"\xff\xfe"):
            self.a.send_bytes(raw)
            with self.assertRaises(ValueError):
                recv_msg(self.b)

    def test_rejects_oversized_messages(self):
        # Written from another thread: a pipe only buffers ~64KB, so writing 1MB here first would block forever.
        writer = threading.Thread(target=lambda: self._send_quietly(b"x" * (MAX_MESSAGE + 1)), daemon=True)
        writer.start()
        with self.assertRaises(OSError):
            recv_msg(self.b)
        self.b.close()
        writer.join(5)

    def _send_quietly(self, data):
        try:
            self.a.send_bytes(data)
        except OSError:
            pass  # The reader refuses it and closes its end


class AgentCommandTest(unittest.TestCase):
    def setUp(self):
        self.agent = Agent(name="t", worker_target=echo_worker)

    def tearDown(self):
        self.agent.stop_all(wait=True)

    def test_start_update_stop_via_messages(self):
        self.agent.handle({"type": "start", "bot": 1, "room": "r", "proxy": proxy("a"), "nickname": "N", "start_delay": 0})
        self.assertTrue(wait_for(lambda: self.agent.health()["bot_status"].get("1", {}).get("state") == "started:a"))
        self.agent.handle({"type": "update_proxy", "bot": 1, "proxy": proxy("b")})
        self.assertTrue(wait_for(lambda: self.agent.health()["bot_status"]["1"]["state"] == "switched:b"))
        self.agent.handle({"type": "stop", "bots": [1]})
        self.assertEqual(self.agent.health()["bots"], [])
        self.assertNotIn("1", self.agent.health()["bot_status"])

    def test_set_max_bots_and_stop_all(self):
        self.agent.handle({"type": "set_max_bots", "max_bots": 2})
        self.assertEqual(self.agent.health()["max_bots"], 2)
        for b in (1, 2):
            self.agent.start_bot(b, "r", proxy("a"), "N", 0)
        self.agent.handle({"type": "stop_all"})
        self.assertEqual(self.agent.health()["bots"], [])

    def test_rejection_ends_the_agent(self):
        with self.assertRaises(SystemExit):
            self.agent.handle({"type": "rejected", "reason": "old code"})

    def test_update_for_unknown_bot_is_ignored(self):
        self.agent.update_proxy(99, proxy("x"))

    def test_status_for_a_bot_it_does_not_run_is_ignored(self):
        self.agent._file_report({"bot": 5, "status": {"state": "playing"}})
        self.assertEqual(self.agent.bot_status, {})

    def test_health_fields(self):
        health = self.agent.health()
        for key in ("agent", "cpu_percent", "cpu_count", "mem_available_mb", "mem_total_mb", "bots",
                    "max_bots", "platform", "bot_status"):
            self.assertIn(key, health)
        self.assertGreater(health["mem_total_mb"], health["mem_available_mb"] - 1)

    def test_crash_is_reported_once_and_not_as_bad_proxy(self):
        agent = Agent(name="t2", worker_target=crash_worker)
        agent.start_bot(4, "r", proxy("a"), "N", 0)
        events = wait_for(agent.poll)
        self.assertEqual(events, [{"type": "exited", "bot": 4, "code": 1, "bad_proxy": False, "proxy": None}])
        self.assertEqual(agent.poll(), [])

    def test_every_worker_gets_its_own_temp_folder(self):
        homes = {self.agent.worker_home(1) for _ in range(5)}
        self.assertEqual(len(homes), 5)


class ServeTest(unittest.TestCase):
    def test_serve_says_hello_then_reports_health_and_exits(self):
        coord_end, agent_end = multiprocessing.Pipe()
        agent = Agent(name="srv", worker_target=crash_worker)
        with mock.patch.object(agent_module, "HEALTH_INTERVAL", 0.2):
            thread = threading.Thread(target=lambda: self._serve(agent_end, agent), daemon=True)
            thread.start()
            hello = recv_msg(coord_end)
            self.assertEqual((hello["type"], hello["agent"], hello["version"]), ("hello", "srv", code_version()))
            send_msg(coord_end, {"type": "start", "bot": 2, "room": "r", "proxy": proxy("a"), "nickname": "N", "start_delay": 0})
            seen = []
            deadline = time.time() + 10
            while time.time() < deadline and not ({"health", "exited"} <= {m["type"] for m in seen}):
                if coord_end.poll(0.2):
                    seen.append(recv_msg(coord_end))
            self.assertIn("health", [m["type"] for m in seen])
            self.assertIn({"type": "exited", "bot": 2, "code": 1, "bad_proxy": False, "proxy": None}, seen)
            coord_end.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())  # Serve ends when the coordinator goes away

    @staticmethod
    def _serve(conn, agent):
        try:
            serve(conn, agent)
        except (EOFError, OSError):
            pass


class RunAgentTest(unittest.TestCase):
    def test_wrong_key_stops_the_agent(self):
        listener = multiprocessing.connection.Listener(("127.0.0.1", 0), authkey=b"right")
        threading.Thread(target=lambda: self._accept(listener), daemon=True).start()
        with self.assertRaises(SystemExit):
            run_agent(listener.address, b"wrong", Agent(name="x", worker_target=fake_steady_worker))

    @staticmethod
    def _accept(listener):
        try:
            listener.accept()
        except Exception:
            pass

    def test_keeps_retrying_an_unreachable_coordinator(self):
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise KeyboardInterrupt  # Enough retries seen

        with mock.patch.object(agent_module.time, "sleep", fake_sleep):
            with self.assertRaises(KeyboardInterrupt):
                run_agent(("127.0.0.1", 1), b"k", Agent(name="x", worker_target=fake_steady_worker))
        self.assertEqual(sleeps, [agent_module.RECONNECT_DELAY] * 3)


if __name__ == "__main__":
    unittest.main()
