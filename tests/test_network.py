import multiprocessing.connection
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coordinator
from agent import Agent, serve
from coordinator import RemoteAgent
from test_agent import fake_bad_proxy_worker, fake_steady_worker, proxy

KEY = b"test-key"


def connect(worker_target):
    """A RemoteAgent on the coordinator side, wired over localhost to a real agent serve() loop."""
    listener = multiprocessing.connection.Listener(("127.0.0.1", 0), authkey=KEY)
    agent = Agent(name="remote-test", worker_target=worker_target)

    def run_agent():
        conn = multiprocessing.connection.Client(listener.address, authkey=KEY)
        try:
            serve(conn, agent)
        except (EOFError, OSError):
            agent.stop_all()

    threading.Thread(target=run_agent, daemon=True).start()
    conn = listener.accept()
    hello = conn.recv()
    listener.close()
    return RemoteAgent(conn, hello), agent


def wait_for(check, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    return None


class NetworkTest(unittest.TestCase):
    def test_hello_start_stop_over_the_network(self):
        remote, agent = connect(fake_steady_worker)
        self.assertEqual(remote.name, "remote-test")
        self.assertGreater(remote.last_health["cpu_count"], 0)
        remote.start_bot(3, "room", proxy("a"), "Nick", 0)
        self.assertTrue(wait_for(lambda: agent.health()["bots"] == [3]))
        remote.stop_bots([3])
        self.assertTrue(wait_for(lambda: agent.health()["bots"] == []))
        remote.conn.close()

    def test_bad_proxy_exit_reaches_the_coordinator(self):
        remote, _ = connect(fake_bad_proxy_worker)
        remote.start_bot(5, "room", proxy("a"), "Nick", 0)
        events = wait_for(remote.poll)
        self.assertEqual(events[0]["bot"], 5)
        self.assertTrue(events[0]["bad_proxy"])
        self.assertEqual(events[0]["proxy"]["host"], "a")
        remote.conn.close()

    def test_silent_agent_counts_as_gone(self):
        remote, _ = connect(fake_steady_worker)
        self.assertTrue(remote.alive)
        remote.last_seen -= coordinator.AGENT_TIMEOUT + 1
        remote.receive()
        self.assertFalse(remote.alive)


if __name__ == "__main__":
    unittest.main()
