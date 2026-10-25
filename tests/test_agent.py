import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import Agent
from bot import BAD_PROXY_EXIT


def fake_bad_proxy_worker(bot_id, room, proxy, headless, start_delay, nickname, updates, debug_dir, home, reports):
    """Like a worker whose proxy never connects: say which proxy failed, then exit."""
    reports.put({"bot": bot_id, "bad_proxy": proxy})
    sys.exit(BAD_PROXY_EXIT)


def fake_steady_worker(bot_id, room, proxy, headless, start_delay, nickname, updates, debug_dir, home, reports):
    """Like a healthy worker: runs until told to stop."""
    while updates.get() != "stop":
        pass


def proxy(host):
    return {"host": host, "port": "8080", "user": "u", "pass": "p", "country": "XX"}


def poll_until(agent, n, timeout=10):
    events, deadline = [], time.time() + timeout
    while len(events) < n and time.time() < deadline:
        events += agent.poll()
        time.sleep(0.05)
    return events


class AgentTest(unittest.TestCase):
    def test_bad_proxy_exit_names_the_proxy_the_worker_used(self):
        agent = Agent(name="test", worker_target=fake_bad_proxy_worker)
        agent.start_bot(0, "room", proxy("a"), "Nick", 0)
        agent.update_proxy(0, proxy("b"))  # Sent too late: the worker already failed on "a"
        events = poll_until(agent, 1)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["bad_proxy"])
        self.assertEqual(events[0]["proxy"]["host"], "a")
        self.assertEqual(agent.health()["bots"], [])

    def test_stop_bots_and_health(self):
        agent = Agent(name="test", worker_target=fake_steady_worker)
        agent.start_bot(0, "room", proxy("a"), "One", 0)
        agent.start_bot(1, "room", proxy("a"), "Two", 0)
        health = agent.health()
        self.assertEqual(health["bots"], [0, 1])
        self.assertGreater(health["cpu_count"], 0)
        agent.stop_bots([0])
        self.assertEqual(agent.health()["bots"], [1])
        agent.stop_all()
        self.assertEqual(agent.health()["bots"], [])
        self.assertEqual(agent.poll(), [])  # Stopped bots aren't reported as unexpected exits


if __name__ == "__main__":
    unittest.main()
