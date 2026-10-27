import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import (
    MIN_HOLD,
    OPPOSITE_DIRECTIONS,
    generate_random_nickname,
    pick_next_direction,
    sample_respawn_delay,
)
from coordinator import pick_agent, pick_move, reassign_proxies, restart_delay
from humanizer import sample_bivariate_gaussian_box, sample_ex_gaussian


class PickNextDirectionTest(unittest.TestCase):
    def test_never_reverses(self):
        for current in OPPOSITE_DIRECTIONS:
            for _ in range(500):
                chosen, _ = pick_next_direction(current)
                self.assertNotIn(chosen, (current, OPPOSITE_DIRECTIONS[current]))

    def test_first_move_is_down_or_left(self):
        for _ in range(500):
            chosen, _ = pick_next_direction(None)
            self.assertIn(chosen, ("ArrowDown", "ArrowLeft"))

    def test_hold_ranges(self):
        for current in (None, *OPPOSITE_DIRECTIONS):
            for _ in range(500):
                chosen, hold = pick_next_direction(current)
                if chosen in ("ArrowDown", "ArrowLeft"):
                    self.assertTrue(0.9 <= hold <= 2.8, hold)
                else:
                    self.assertTrue(MIN_HOLD <= hold <= 0.65, hold)


class NicknameTest(unittest.TestCase):
    def test_length(self):
        for _ in range(1000):
            name = generate_random_nickname()
            self.assertTrue(0 < len(name) <= 15, name)


def proxy(host):
    return {"host": host, "port": "8080", "user": "u", "pass": "p", "country": "XX"}


class ReassignProxiesTest(unittest.TestCase):
    def test_initial_assignment_uses_distinct_proxies(self):
        result = reassign_proxies([None] * 3, [proxy("a"), proxy("b"), proxy("c"), proxy("d")])
        self.assertEqual([p["host"] for p in result], ["a", "b", "c"])

    def test_keeps_proxies_still_listed(self):
        current = [proxy("a"), proxy("b"), proxy("c")]
        result = reassign_proxies(current, [proxy("c"), proxy("x"), proxy("a"), proxy("b")])
        self.assertEqual([p["host"] for p in result], ["a", "b", "c"])

    def test_rotated_out_proxy_gets_unused_one(self):
        current = [proxy("a"), proxy("b"), proxy("c")]
        result = reassign_proxies(current, [proxy("a"), proxy("x"), proxy("c"), proxy("b")])
        self.assertEqual([p["host"] for p in result], ["a", "b", "c"])
        result = reassign_proxies(current, [proxy("a"), proxy("x"), proxy("c")])
        self.assertEqual([p["host"] for p in result], ["a", "x", "c"])

    def test_spreads_out_a_pile_up(self):
        result = reassign_proxies([proxy("a")] * 3, [proxy("a"), proxy("b"), proxy("c")])
        self.assertEqual([p["host"] for p in result], ["a", "b", "c"])

    def test_shares_evenly_when_not_enough(self):
        result = reassign_proxies([None] * 5, [proxy("a"), proxy("b")])
        self.assertEqual(sorted(p["host"] for p in result), ["a", "a", "a", "b", "b"])

    def test_shares_when_not_enough(self):
        result = reassign_proxies([None] * 3, [proxy("a")])
        self.assertEqual([p["host"] for p in result], ["a", "a", "a"])


def stat(bots, cpu_count, cpu=0.0, max_bots=None):
    return {"bots": bots, "cpu_count": cpu_count, "cpu": cpu, "max_bots": max_bots}


class PickAgentTest(unittest.TestCase):
    def test_single_agent(self):
        self.assertEqual(pick_agent([stat(7, 16)]), 0)

    def test_fewest_bots_per_core(self):
        # 16 cores with 6 bots (0.375/core) has more room than 4 cores with 2 bots (0.5/core).
        self.assertEqual(pick_agent([stat(6, 16), stat(2, 4)]), 0)
        self.assertEqual(pick_agent([stat(8, 16), stat(1, 4)]), 1)

    def test_ties_go_to_first_agent(self):
        self.assertEqual(pick_agent([stat(0, 16), stat(0, 4), stat(0, 12)]), 0)

    def test_skips_overloaded_and_full_agents(self):
        self.assertEqual(pick_agent([stat(1, 16, cpu=95), stat(5, 4)]), 1)
        self.assertEqual(pick_agent([stat(1, 16, max_bots=1), stat(5, 4)]), 1)

    def test_all_overloaded_still_places(self):
        self.assertEqual(pick_agent([stat(6, 16, cpu=95), stat(1, 4, cpu=90)]), 1)

    def test_no_room_anywhere(self):
        self.assertIsNone(pick_agent([stat(2, 4, max_bots=2)]))
        self.assertIsNone(pick_agent([]))


class PickMoveTest(unittest.TestCase):
    def test_moves_from_overloaded_to_headroom(self):
        self.assertEqual(pick_move([stat(8, 16, cpu=92), stat(1, 12, cpu=30)]), (0, 1))

    def test_no_move_without_overload(self):
        self.assertIsNone(pick_move([stat(8, 16, cpu=80), stat(0, 12, cpu=5)]))

    def test_no_move_without_headroom(self):
        self.assertIsNone(pick_move([stat(8, 16, cpu=92), stat(3, 12, cpu=70)]))
        self.assertIsNone(pick_move([stat(8, 16, cpu=92), stat(2, 12, cpu=10, max_bots=2)]))

    def test_moves_to_least_loaded_target(self):
        self.assertEqual(pick_move([stat(8, 16, cpu=92), stat(3, 4, cpu=40), stat(3, 12, cpu=40)]), (0, 2))


class RestartDelayTest(unittest.TestCase):
    def test_first_restart_is_quick(self):
        for _ in range(100):
            self.assertTrue(1.0 <= restart_delay(1) <= 3.5)

    def test_backs_off_and_caps(self):
        self.assertTrue(2.0 <= restart_delay(2) <= 4.0)
        self.assertTrue(8.0 <= restart_delay(4) <= 10.0)
        self.assertTrue(60.0 <= restart_delay(20) <= 62.0)


class RespawnDelayTest(unittest.TestCase):
    def test_mostly_fast_sometimes_long(self):
        delays = [sample_respawn_delay() for _ in range(20000)]
        self.assertTrue(all(0.6 <= d <= 15.0 for d in delays))
        fast = sum(d < 2.0 for d in delays) / len(delays)
        self.assertTrue(0.7 < fast < 0.9, fast)
        self.assertTrue(any(d > 8.0 for d in delays))


class HumanizerSamplingTest(unittest.TestCase):
    def test_ex_gaussian_respects_floor(self):
        for _ in range(1000):
            self.assertGreaterEqual(sample_ex_gaussian(0.05, 0.05, 0.01, min_floor=0.04), 0.04)

    def test_click_point_inside_box(self):
        box = {"x": 100.0, "y": 50.0, "width": 80.0, "height": 30.0}
        for _ in range(1000):
            x, y = sample_bivariate_gaussian_box(box)
            self.assertTrue(box["x"] <= x <= box["x"] + box["width"])
            self.assertTrue(box["y"] <= y <= box["y"] + box["height"])


if __name__ == "__main__":
    unittest.main()
