import argparse
import io
import json
import math
import multiprocessing
import os
import random
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coordinator
from agent import recv_msg, send_msg
from coordinator import (
    BAD_PROXY_COOLDOWN,
    CPU_WINDOW,
    HEALTHY_RUN,
    OVERLOAD_CPU,
    Coordinator,
    RemoteAgent,
    WebshareManager,
    pick_agent,
    pick_move,
    proxy_key,
    reassign_proxies,
    restart_delay,
)
from fakes import FakeAgent, proxy


def make(*agents, groups=2, bpp=2, proxies=("a", "b", "c")):
    args = argparse.Namespace(webshare_key="x", room="room", groups=groups, bots_per_proxy=bpp,
                              stagger_interval=0.0, proxy_check_interval=60.0)
    coord = Coordinator(args, list(agents))
    coord.proxy_mgr.proxies = [proxy(h) for h in proxies]
    coord.group_proxies = reassign_proxies([None] * groups, coord.proxy_mgr.proxies)
    coord.last_fetch = time.time()
    return coord


def start_all(coord):
    coord.refresh_health()
    for bot_id in range(coord.total):
        coord.place_bot(bot_id, 0)


def fake_response(payload):
    resp = io.BytesIO(json.dumps(payload).encode())
    resp.__enter__ = lambda self=resp: self
    resp.__exit__ = lambda *a: None
    return resp


class FakeUrlopen:
    """Answers Webshare URLs from a dict, or raises."""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        answer = self.pages[url]
        if isinstance(answer, Exception):
            raise answer
        return fake_response(answer)


LIST_URL = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100"
PAGE2 = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=2&page_size=100"


def raw(host, valid=True, created="2026-10-25T20:00:00Z"):
    return {"proxy_address": host, "port": 6000, "username": "u", "password": "p",
            "country_code": "NL", "valid": valid, "created_at": created}


class WebshareTest(unittest.TestCase):
    def test_reads_every_page_and_skips_invalid(self):
        fake = FakeUrlopen({
            LIST_URL: {"results": [raw("1.1.1.1"), raw("2.2.2.2", valid=False)], "next": PAGE2},
            PAGE2: {"results": [raw("3.3.3.3", created="2026-10-25T20:05:00Z")], "next": None},
        })
        mgr = WebshareManager("key")
        with mock.patch("coordinator.urllib.request.urlopen", fake):
            got = mgr.fetch_proxies()
        self.assertEqual([p["host"] for p in got], ["1.1.1.1", "3.3.3.3"])
        self.assertEqual(got[0], {"host": "1.1.1.1", "port": "6000", "user": "u", "pass": "p", "country": "NL"})
        self.assertEqual(fake.urls, [LIST_URL, PAGE2])
        self.assertEqual(mgr.list_created_at, 1792958700.0)  # The newest created_at (20:05Z)

    def test_sends_the_api_key(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            return fake_response({"results": [], "next": None})

        with mock.patch("coordinator.urllib.request.urlopen", urlopen):
            WebshareManager("secret").fetch_proxies()
        self.assertEqual(seen, ["Token secret"])

    def test_keeps_the_old_list_on_errors_and_empty_answers(self):
        mgr = WebshareManager("key")
        mgr.proxies = [proxy("old")]
        with mock.patch("coordinator.urllib.request.urlopen", FakeUrlopen({LIST_URL: OSError("down")})):
            self.assertEqual(mgr.fetch_proxies(), [proxy("old")])
        with mock.patch("coordinator.urllib.request.urlopen", FakeUrlopen({LIST_URL: {"results": [], "next": None}})):
            self.assertEqual(mgr.fetch_proxies(), [proxy("old")])

    def test_a_failure_on_page_two_keeps_the_old_list(self):
        mgr = WebshareManager("key")
        mgr.proxies = [proxy("old")]
        fake = FakeUrlopen({LIST_URL: {"results": [raw("1.1.1.1")], "next": PAGE2}, PAGE2: OSError("cut")})
        with mock.patch("coordinator.urllib.request.urlopen", fake):
            self.assertEqual(mgr.fetch_proxies(), [proxy("old")])  # Never half a list

    def test_rotation_frequency_from_the_active_plan(self):
        plans = {"https://proxy.webshare.io/api/v2/subscription/plan/": {"results": [
            {"status": "cancelled", "automatic_refresh_frequency": 0},
            {"status": "active", "automatic_refresh_frequency": 300},
        ]}}
        mgr = WebshareManager("key")
        with mock.patch("coordinator.urllib.request.urlopen", FakeUrlopen(plans)):
            self.assertEqual(mgr.fetch_refresh_frequency(), 300.0)
        with mock.patch("coordinator.urllib.request.urlopen", FakeUrlopen({})):
            self.assertEqual(mgr.fetch_refresh_frequency(), 300.0)  # A failed read keeps the last value

    def test_next_rotation(self):
        mgr = WebshareManager("key")
        self.assertIsNone(mgr.next_rotation(1000))
        mgr.list_created_at, mgr.refresh_frequency = 1000.0, 300.0
        self.assertEqual(mgr.next_rotation(1001), 1300)
        self.assertEqual(mgr.next_rotation(1299), 1300)
        self.assertEqual(mgr.next_rotation(1301), 1600)  # Missed rotation: the one after
        self.assertEqual(mgr.next_rotation(900), 1300)   # Clock skew never gives a past time

    def test_replace_proxies_posts_to_v3_endpoint(self):
        seen_reqs = []
        def urlopen(req, timeout=None):
            seen_reqs.append(req)
            return fake_response({"id": 12345, "state": "validating"})

        mgr = WebshareManager("secret_key")
        with mock.patch("coordinator.urllib.request.urlopen", urlopen):
            order_id = mgr.replace_proxies(["1.2.3.4", "5.6.7.8"])

        self.assertEqual(order_id, 12345)
        self.assertEqual(len(seen_reqs), 1)
        self.assertEqual(seen_reqs[0].full_url, "https://proxy.webshare.io/api/v3/proxy/replace/")
        self.assertEqual(seen_reqs[0].get_header("Authorization"), "Token secret_key")
        body = json.loads(seen_reqs[0].data.decode())
        self.assertEqual(body["to_replace"]["ip_ranges"], ["1.2.3.4/32", "5.6.7.8/32"])

    def test_wait_for_replacement_polls_until_completed(self):
        mgr = WebshareManager("secret_key")
        responses = [
            fake_response({"state": "processing"}),
            fake_response({"state": "completed"}),
        ]
        with mock.patch("coordinator.urllib.request.urlopen", side_effect=responses), \
             mock.patch("coordinator.time.sleep", return_value=None):
            done = mgr.wait_for_replacement(12345, timeout=10, poll_interval=0.01)

        self.assertTrue(done)


class FuzzTest(unittest.TestCase):
    def test_reassign_proxies_rules(self):
        rng = random.Random(42)
        for _ in range(3000):
            available = [proxy(f"10.0.0.{i}") for i in rng.sample(range(50), rng.randint(1, 8))]
            pool = available + [proxy(f"9.9.9.{i}") for i in range(3)]  # Some current ones may be gone
            groups = rng.randint(1, 12)
            current = [rng.choice(pool + [None]) for _ in range(groups)]
            result = reassign_proxies(current, available)
            keys = [proxy_key(p) for p in result]
            self.assertEqual(len(result), groups)
            self.assertTrue(all(p in available for p in result))
            share = math.ceil(groups / len(available))
            self.assertLessEqual(max(keys.count(k) for k in set(keys)), share)
            self.assertLessEqual(max(keys.count(k) for k in set(keys)) - min(keys.count(proxy_key(p)) for p in available), share)

    def test_reassign_keeps_what_it_can(self):
        rng = random.Random(7)
        for _ in range(1000):
            available = [proxy(f"10.0.0.{i}") for i in range(rng.randint(3, 8))]
            groups = rng.randint(1, len(available))
            current = rng.sample(available, groups)  # Distinct, all still listed
            self.assertEqual(reassign_proxies(current, available), current)

    def test_pick_agent_rules(self):
        rng = random.Random(1)
        for _ in range(5000):
            stats = [{"bots": rng.randint(0, 10), "cpu_count": rng.choice([4, 12, 16]), "cpu": rng.uniform(0, 100),
                      "max_bots": rng.choice([None, None, rng.randint(0, 10)])} for _ in range(rng.randint(0, 4))]
            idx = pick_agent(stats)
            open_ = [i for i, s in enumerate(stats) if s["max_bots"] is None or s["bots"] < s["max_bots"]]
            if not open_:
                self.assertIsNone(idx)
                continue
            self.assertIn(idx, open_)
            cool = [i for i in open_ if stats[i]["cpu"] < OVERLOAD_CPU]
            if cool:
                self.assertIn(idx, cool)
                best = min(stats[i]["bots"] / stats[i]["cpu_count"] for i in cool)
                self.assertAlmostEqual(stats[idx]["bots"] / stats[idx]["cpu_count"], best)

    def test_pick_move_rules(self):
        rng = random.Random(2)
        for _ in range(5000):
            stats = [{"bots": rng.randint(0, 6), "cpu_count": 8, "cpu": rng.uniform(0, 100),
                      "max_bots": rng.choice([None, rng.randint(0, 6)])} for _ in range(rng.randint(1, 4))]
            move = pick_move(stats)
            if move is None:
                continue
            src, dst = move
            self.assertNotEqual(src, dst)
            self.assertGreaterEqual(stats[src]["cpu"], OVERLOAD_CPU)
            self.assertGreater(stats[src]["bots"], 0)
            self.assertLess(stats[dst]["cpu"], coordinator.HEADROOM_CPU)
            self.assertTrue(stats[dst]["max_bots"] is None or stats[dst]["bots"] < stats[dst]["max_bots"])

    def test_restart_delay_grows_then_caps(self):
        lows = [min(restart_delay(n) for _ in range(50)) for n in range(1, 12)]
        self.assertTrue(all(a <= b + 2 for a, b in zip(lows, lows[1:])))
        self.assertTrue(all(restart_delay(n) <= 62 for n in range(1, 40)))


class PlacementTest(unittest.TestCase):
    def test_spreads_by_bots_per_core(self):
        big, small = FakeAgent("big", cpu_count=16), FakeAgent("small", cpu_count=4)
        coord = make(big, small, groups=5, bpp=2)
        start_all(coord)
        self.assertEqual(len(big.running) + len(small.running), 10)
        self.assertEqual(len(big.running), 8)  # 8/16 = 0.5 per core, 2/4 = 0.5 per core

    def test_respects_max_bots_and_queues_the_rest(self):
        only = FakeAgent("only", max_bots=2)
        coord = make(only, groups=2, bpp=2)
        start_all(coord)
        self.assertEqual(sorted(only.running), [0, 1])
        self.assertEqual(sorted(coord.pending), [2, 3])

    def test_waiting_bots_start_when_a_machine_joins(self):
        coord = make(groups=1, bpp=2)
        start_all(coord)
        self.assertEqual(sorted(coord.pending), [0, 1])
        late = FakeAgent("late")
        coord.agents.append(late)
        coord.refresh_health()
        coord.place_pending()
        self.assertEqual(sorted(late.running), [0, 1])
        self.assertEqual(coord.pending, {})

    def test_bots_get_their_group_proxy_and_name(self):
        a = FakeAgent("a")
        coord = make(a, groups=2, bpp=2)
        start_all(coord)
        self.assertEqual(a.running[3]["proxy"], coord.group_proxies[1])
        self.assertEqual(a.running[3]["nickname"], coord.nicknames[3])


class ExitHandlingTest(unittest.TestCase):
    def setUp(self):
        self.a = FakeAgent("a")
        self.coord = make(self.a, groups=2, bpp=2)
        start_all(self.coord)

    def test_crashed_bot_restarts_with_growing_backoff(self):
        delays = []
        for _ in range(5):
            self.a.exit(1)
            self.coord.handle_exits()
            delays.append(self.a.running[1]["start_delay"])
        self.assertEqual(self.coord.failures[1], 5)
        self.assertLess(delays[0], 4)
        self.assertGreaterEqual(delays[4], 16)

    def test_backoff_resets_after_a_healthy_run(self):
        self.coord.failures[1] = 6
        self.coord.started_at[1] = time.time() - HEALTHY_RUN - 1
        self.a.exit(1)
        self.coord.handle_exits()
        self.assertEqual(self.coord.failures[1], 1)
        self.assertLess(self.a.running[1]["start_delay"], 4)

    def test_bad_proxy_is_benched_and_its_group_moves(self):
        old = self.coord.group_proxies[0]
        self.a.exit(0, code=3, bad_proxy=True, proxy=old)
        self.coord.handle_exits()
        new = self.coord.group_proxies[0]
        self.assertNotEqual(proxy_key(new), proxy_key(old))
        self.assertGreater(self.coord.bad_until[proxy_key(old)], time.time() + BAD_PROXY_COOLDOWN - 5)
        self.assertIn(("update_proxy", 1, new["host"]), self.a.calls)  # Its group mate moves too
        self.assertEqual(self.a.running[0]["proxy"], new)  # And it restarts on the new one

    def test_late_report_for_an_old_proxy_does_not_move_the_group_again(self):
        old = self.coord.group_proxies[0]
        self.a.exit(0, code=3, bad_proxy=True, proxy=old)
        self.coord.handle_exits()
        moved_to = self.coord.group_proxies[0]
        self.a.exit(1, code=3, bad_proxy=True, proxy=old)  # Bot 1 hadn't switched yet
        self.coord.handle_exits()
        self.assertEqual(self.coord.group_proxies[0], moved_to)

    def test_bad_proxy_refreshes_the_list_when_it_is_stale(self):
        self.coord.last_fetch = 0
        with mock.patch.object(self.coord.proxy_mgr, "fetch_proxies", return_value=self.coord.proxy_mgr.proxies) as fetch:
            self.a.exit(0, code=3, bad_proxy=True, proxy=self.coord.group_proxies[0])
            self.coord.handle_exits()
        fetch.assert_called_once()

    def test_events_for_bots_it_no_longer_runs_are_ignored(self):
        other = FakeAgent("other")
        self.coord.agents.append(other)
        other.events.append({"type": "exited", "bot": 0, "code": 1, "bad_proxy": False, "proxy": None})
        self.coord.handle_exits()
        self.assertIs(self.coord.bot_agent[0], self.a)
        self.assertNotIn(0, self.coord.failures)

    def test_usable_proxies(self):
        p = self.coord.proxy_mgr.proxies
        self.coord.bad_until[proxy_key(p[0])] = time.time() + 100
        self.coord.bad_until[proxy_key(p[1])] = time.time() - 1  # Bench expired
        self.assertEqual(self.coord.usable_proxies(), p[1:])
        for q in p:
            self.coord.bad_until[proxy_key(q)] = time.time() + 100
        self.assertEqual(self.coord.usable_proxies(), p)  # All benched: use them anyway

    def test_webshare_rotation_updates_running_bots(self):
        self.coord.proxy_mgr.proxies = [proxy("x"), proxy("y")]
        self.coord.apply_proxies()
        self.assertEqual({p["host"] for p in self.coord.group_proxies}, {"x", "y"})
        updated = {c[1] for c in self.a.calls if c[0] == "update_proxy"}
        self.assertEqual(updated, {0, 1, 2, 3})


class AgentLifecycleTest(unittest.TestCase):
    def test_disconnected_machine_hands_its_bots_over(self):
        a, b = FakeAgent("a"), FakeAgent("b")
        coord = make(a, b, groups=2, bpp=2)
        start_all(coord)
        a_bots = sorted(a.running)
        a.alive = False
        coord.drop_dead_agents()
        self.assertNotIn(a, coord.agents)
        coord.place_pending()
        self.assertTrue(set(a_bots) <= set(b.running))

    def test_overloaded_machine_sheds_one_bot_then_waits(self):
        hot, cool = FakeAgent("hot", cpu=95), FakeAgent("cool", cpu=20, max_bots=0)
        coord = make(hot, cool, groups=2, bpp=2)
        start_all(coord)
        cool.max_bots = None
        for _ in range(CPU_WINDOW):
            coord.next_health = 0
            coord.refresh_health()
        coord.rebalance()
        self.assertEqual(sorted(cool.running), [3])  # The highest-numbered bot moved
        self.assertIs(coord.bot_agent[3], cool)
        for _ in range(CPU_WINDOW):
            coord.next_health = 0
            coord.refresh_health()
        coord.rebalance()
        self.assertEqual(len(cool.running), 1)  # Cooldown: one move per minute

    def test_no_move_before_a_full_cpu_window(self):
        hot, cool = FakeAgent("hot", cpu=95), FakeAgent("cool", cpu=5, max_bots=0)
        coord = make(hot, cool, groups=1, bpp=2)
        start_all(coord)
        cool.max_bots = None
        coord.next_health = 0
        coord.refresh_health()
        coord.rebalance()
        self.assertEqual(cool.running, {})  # One sample could be a launch spike

    def test_bot_statuses_only_come_from_the_machine_running_them(self):
        a, b = FakeAgent("a"), FakeAgent("b", max_bots=0)
        coord = make(a, b, groups=1, bpp=1)
        start_all(coord)
        b.bot_status = {"0": {"state": "playing"}}  # A stale report from the wrong machine
        a.bot_status = {"0": {"state": "lobby"}}
        coord.next_health = 0
        coord.refresh_health()
        self.assertEqual(coord.bot_status[0]["state"], "lobby")

    def test_snapshot_states(self):
        a = FakeAgent("a", max_bots=2)
        coord = make(a, groups=2, bpp=2)
        start_all(coord)
        coord.stopped.add(1)
        coord.bot_agent.pop(1)
        a.bot_status = {"0": {"state": "playing", "rounds": 3}}
        coord.next_health = 0
        coord.refresh_health()
        states = {b["id"]: b["state"] for b in coord.build_snapshot()["bots"]}
        self.assertEqual(states, {0: "playing", 1: "stopped", 2: "waiting", 3: "waiting"})
        json.dumps(coord.build_snapshot())

    def test_shutdown_stops_every_machine(self):
        a, b = FakeAgent("a"), FakeAgent("b")
        coord = make(a, b, groups=1, bpp=2)
        start_all(coord)
        with self.assertRaises(SystemExit):
            coord.shutdown()
        self.assertEqual(a.running, {})
        self.assertEqual(b.running, {})

    def test_probe_proxies_benches_failed_handshake(self):
        coord = make(FakeAgent("a"), groups=1, bpp=1, proxies=("192.168.1.1", "192.168.1.2"))
        with mock.patch("coordinator.probe_proxy_handshake", side_effect=[(True, "101"), (False, "400 Bad Request")]):
            bad = coord.probe_proxies(coord.proxy_mgr.proxies)

        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["host"], "192.168.1.2")
        self.assertIn(("192.168.1.2", "8080"), coord.bad_until)

    def test_clean_flagged_proxies_rotates_and_applies(self):
        coord = make(FakeAgent("a"), groups=1, bpp=1, proxies=("192.168.1.1",))
        with mock.patch.object(coord, "probe_proxies", side_effect=[[proxy("192.168.1.1")], []]), \
             mock.patch.object(coord.proxy_mgr, "replace_and_refresh", return_value=[proxy("192.168.1.99")]), \
             mock.patch.object(coord, "apply_proxies") as mock_apply:
            count = coord.clean_flagged_proxies(max_rounds=2)

        self.assertEqual(count, 1)
        mock_apply.assert_called()

    def test_auto_replace_proxy_async_triggers_thread(self):
        coord = make(FakeAgent("a"), groups=1, bpp=1, proxies=("192.168.1.1",))
        replaced = []
        with mock.patch.object(coord.proxy_mgr, "replace_and_refresh", side_effect=lambda ips: replaced.extend(ips)):
            coord.auto_replace_proxy_async("192.168.1.1")
            time.sleep(0.1)

        self.assertEqual(replaced, ["192.168.1.1"])


class RemoteAgentTest(unittest.TestCase):
    def setUp(self):
        self.mine, self.theirs = multiprocessing.Pipe()
        self.remote = RemoteAgent(self.mine, {"agent": "pc", "cpu_count": 12, "cpu_percent": 1.0})

    def test_commands_go_out_as_json(self):
        self.remote.start_bot(4, "room", proxy("a"), "Nick", 1.5)
        self.remote.update_proxy(4, proxy("b"))
        self.remote.stop_bots([4])
        self.remote.stop_all()
        got = [recv_msg(self.theirs) for _ in range(4)]
        self.assertEqual([m["type"] for m in got], ["start", "update_proxy", "stop", "stop_all"])
        self.assertEqual(got[0]["nickname"], "Nick")
        self.assertEqual(got[2]["bots"], [4])

    def test_health_and_exit_events_come_in(self):
        send_msg(self.theirs, {"type": "health", "agent": "pc", "cpu_count": 12, "cpu_percent": 55.0})
        send_msg(self.theirs, {"type": "exited", "bot": 4, "code": 1, "bad_proxy": False, "proxy": None})
        self.assertEqual(self.remote.health()["cpu_percent"], 55.0)
        self.assertEqual([e["bot"] for e in self.remote.poll()], [4])
        self.assertEqual(self.remote.poll(), [])

    def test_closed_link_marks_it_gone(self):
        self.theirs.close()
        self.remote.receive()
        self.assertFalse(self.remote.alive)

    def test_garbage_marks_it_gone(self):
        self.theirs.send_bytes(b"\x80not json")
        self.remote.receive()
        self.assertFalse(self.remote.alive)

    def test_sending_to_a_closed_link_marks_it_gone(self):
        self.theirs.close()
        self.mine.close()
        self.remote.stop_all()
        self.assertFalse(self.remote.alive)


if __name__ == "__main__":
    unittest.main()
