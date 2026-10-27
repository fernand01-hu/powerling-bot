import multiprocessing.connection
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coordinator
from agent import Agent, recv_msg, serve
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
    hello = recv_msg(conn)
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


class ListenTest(unittest.TestCase):
    def make(self):
        import argparse
        import socket
        from coordinator import Coordinator

        with socket.socket() as s:  # Find a free port
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        args = argparse.Namespace(webshare_key="x", room="room", groups=1, bots_per_proxy=1, stagger_interval=0.0)
        coord = Coordinator(args, [])
        coord.listen(("127.0.0.1", port), KEY)
        return coord, ("127.0.0.1", port)

    def start_agent(self, address, key=KEY):
        agent = Agent(name="joiner", worker_target=fake_steady_worker)

        def run():
            try:
                serve(multiprocessing.connection.Client(address, authkey=key), agent)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def test_stalled_connection_does_not_block_others(self):
        import socket
        coord, address = self.make()
        staller = socket.create_connection(address)  # Connects, never answers the key challenge
        self.start_agent(address)
        self.assertTrue(wait_for(lambda: coord.joined))
        self.assertEqual(coord.joined[0].name, "joiner")
        staller.close()

    def test_wrong_key_is_refused(self):
        coord, address = self.make()
        with self.assertRaises(multiprocessing.AuthenticationError):
            multiprocessing.connection.Client(address, authkey=b"wrong")
        self.assertEqual(coord.joined, [])

    def test_pickled_messages_are_rejected(self):
        import pickle

        class Boom:
            def __reduce__(self):
                return (os.system, ("echo should-never-run",))

        coord, address = self.make()
        conn = multiprocessing.connection.Client(address, authkey=KEY)
        conn.send_bytes(pickle.dumps(Boom()))  # Valid key, but a pickle instead of JSON
        time.sleep(1)
        self.assertEqual(coord.joined, [])  # Rejected as malformed, never unpickled
        conn.close()
