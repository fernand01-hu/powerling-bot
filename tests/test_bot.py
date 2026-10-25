import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import (
    MIN_HOLD,
    OPPOSITE_DIRECTIONS,
    generate_random_nickname,
    pick_next_direction,
    reassign_proxies,
    sample_respawn_delay,
)
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
