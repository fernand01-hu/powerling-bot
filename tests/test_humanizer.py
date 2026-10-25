import math
import os
import random
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import humanizer
from fakes import FakePage
from humanizer import (
    ADJACENT_KEYS,
    QWERTY_COORDS,
    Humanizer,
    calculate_overshoot_point,
    generate_idle_drift_points,
    human_key_tap,
    human_move,
    human_type_string,
    is_same_bounding_box,
    key_spatial_distance,
    sample_bivariate_gaussian_box,
    sample_contact_duration,
    sample_ex_gaussian,
    wind_mouse_trajectory,
)

no_sleep = mock.patch("humanizer.time.sleep", lambda *_: None)


def seed(n=1234):
    random.seed(n)
    np.random.seed(n)


class TimingTest(unittest.TestCase):
    def test_ex_gaussian_never_below_floor(self):
        seed()
        for _ in range(5000):
            self.assertGreaterEqual(sample_ex_gaussian(0.1, 0.05, 0.02, min_floor=0.08), 0.08)

    def test_ex_gaussian_mean_is_mean_plus_tau(self):
        seed()
        values = [sample_ex_gaussian(0.5, 0.05, 0.1, min_floor=0.0) for _ in range(20000)]
        self.assertAlmostEqual(sum(values) / len(values), 0.6, delta=0.01)

    def test_ex_gaussian_is_right_skewed(self):
        seed()
        values = np.array([sample_ex_gaussian(0.5, 0.05, 0.1, min_floor=0.0) for _ in range(20000)])
        self.assertGreater(float(np.mean(values)), float(np.median(values)))

    def test_fatigue_slows_reactions(self):
        seed()
        fresh = np.mean([sample_ex_gaussian(0.3, 0.03, 0.05, min_floor=0.0) for _ in range(5000)])
        tired = np.mean([sample_ex_gaussian(0.3, 0.03, 0.05, min_floor=0.0, fatigue_scale=1.25) for _ in range(5000)])
        self.assertAlmostEqual(tired / fresh, 1.25, delta=0.03)

    def test_ex_gaussian_handles_zero_spread(self):
        self.assertGreaterEqual(sample_ex_gaussian(0.2, 0.0, 0.0, min_floor=0.1), 0.1)

    def test_contact_duration_range(self):
        seed()
        values = [sample_contact_duration() for _ in range(5000)]
        self.assertTrue(all(0.065 <= v <= 0.135 for v in values))

    def test_human_sleep_returns_what_it_slept(self):
        slept = []
        with mock.patch("humanizer.time.sleep", slept.append):
            got = humanizer.human_sleep(0.2, 0.01, 0.01, min_floor=0.1)
        self.assertEqual(slept, [got])


class TrajectoryTest(unittest.TestCase):
    def test_starts_at_start_and_ends_exactly_on_target(self):
        seed()
        for _ in range(200):
            sx, sy, tx, ty = (random.uniform(0, 700) for _ in range(4))
            path = wind_mouse_trajectory(sx, sy, tx, ty)
            self.assertEqual(path[0], (sx, sy))
            self.assertEqual(path[-1], (tx, ty))

    def test_path_is_bounded_and_continuous(self):
        seed()
        path = wind_mouse_trajectory(10, 10, 600, 400)
        self.assertLess(len(path), 1002)  # max_iterations guard
        biggest_step = max(math.dist(a, b) for a, b in zip(path, path[1:-1]))
        self.assertLess(biggest_step, 40)  # No teleporting

    def test_zero_length_move(self):
        path = wind_mouse_trajectory(100, 100, 100, 100)
        self.assertEqual(path[-1], (100.0, 100.0))
        self.assertLessEqual(len(path), 2)

    def test_overshoot_lands_past_the_target(self):
        seed()
        for _ in range(500):
            ox, oy = calculate_overshoot_point(0, 0, 200, 0)
            self.assertGreaterEqual(ox, 206 - 1e-9)
            self.assertLessEqual(ox, 215 + 1e-9)
            self.assertLessEqual(abs(oy), 5 + 1e-9)

    def test_overshoot_on_tiny_move_is_the_target(self):
        self.assertEqual(calculate_overshoot_point(5, 5, 5.5, 5), (5.5, 5))

    def test_human_move_ends_on_target(self):
        seed()
        page = FakePage()
        with no_sleep:
            for _ in range(50):
                end = human_move(page, 400, 300, start_pos=(10, 20))
                self.assertEqual(end, (400, 300))
                self.assertEqual(page.moves()[-1], (400, 300))

    def test_short_moves_never_overshoot(self):
        seed()
        page = FakePage()
        with no_sleep:
            for _ in range(100):
                page.log.clear()
                human_move(page, 60, 0, start_pos=(0, 0))
                self.assertLess(max(x for x, _ in page.moves()), 65)  # Jitter may pass by ~2px; a real overshoot is 6-15px


class TargetingTest(unittest.TestCase):
    BOX = {"x": 100.0, "y": 200.0, "width": 80.0, "height": 20.0}

    def test_clicks_stay_inside_padding(self):
        seed()
        for _ in range(5000):
            x, y = sample_bivariate_gaussian_box(self.BOX)
            self.assertTrue(112 <= x <= 168)
            self.assertTrue(203 <= y <= 217)

    def test_clicks_center_on_the_middle(self):
        seed()
        points = [sample_bivariate_gaussian_box(self.BOX) for _ in range(5000)]
        self.assertAlmostEqual(np.mean([p[0] for p in points]), 140, delta=1)
        self.assertAlmostEqual(np.mean([p[1] for p in points]), 210, delta=0.5)

    def test_same_bounding_box(self):
        self.assertTrue(is_same_bounding_box(self.BOX, dict(self.BOX, x=103)))
        self.assertFalse(is_same_bounding_box(self.BOX, dict(self.BOX, x=110)))
        self.assertFalse(is_same_bounding_box(self.BOX, None))
        self.assertFalse(is_same_bounding_box(None, None))


class KeyboardTest(unittest.TestCase):
    def test_key_distance(self):
        self.assertEqual(key_spatial_distance("a", "a"), 0)
        self.assertAlmostEqual(key_spatial_distance("q", "w"), 1.0)
        self.assertEqual(key_spatial_distance("A", "a"), 0)  # Case doesn't matter
        self.assertGreater(key_spatial_distance("q", "p"), key_spatial_distance("q", "w"))
        key_spatial_distance("€", "a")  # Unknown keys don't crash

    def test_adjacent_keys_are_real_keys(self):
        for key, neighbours in ADJACENT_KEYS.items():
            self.assertIn(key, QWERTY_COORDS)
            self.assertNotIn(key, neighbours)
            for n in neighbours:
                self.assertIn(n, QWERTY_COORDS, f"{key} -> {n}")

    def test_typing_without_typos_types_exactly(self):
        page = FakePage()
        with no_sleep:
            human_type_string(page, "Viper_99", typo_prob=0.0)
        self.assertEqual(page.keys_pressed(), list("Viper_99"))
        self.assertEqual([e for e in page.log if e[0] == "key_up"], [("key_up", c) for c in "Viper_99"])

    def test_typos_are_always_corrected(self):
        seed()
        page = FakePage()
        with no_sleep:
            human_type_string(page, "Shadow", typo_prob=1.0)
        text = []
        for key in page.keys_pressed():
            if key == "Backspace":
                text.pop()
            else:
                text.append(key)
        self.assertEqual("".join(text), "Shadow")
        self.assertEqual(page.keys_pressed().count("Backspace"), 6)

    def test_typo_keeps_letter_case(self):
        seed()
        page = FakePage()
        with no_sleep:
            human_type_string(page, "S", typo_prob=1.0)
        wrong = page.keys_pressed()[0]
        self.assertTrue(wrong.isupper())
        self.assertIn(wrong.lower(), ADJACENT_KEYS["s"])

    def test_key_tap_is_down_then_up(self):
        page = FakePage()
        with no_sleep:
            human_key_tap(page, "ArrowLeft", hesitation=True)
        self.assertEqual(page.log, [("key_down", "ArrowLeft"), ("key_up", "ArrowLeft")])


class IdleDriftTest(unittest.TestCase):
    def test_drift_stays_on_screen(self):
        seed()
        for _ in range(500):
            pts = generate_idle_drift_points(random.uniform(0, 720), random.uniform(0, 480), bounds=(720, 480))
            x, y = pts[-1]
            self.assertTrue(25 <= x <= 695 and 25 <= y <= 455)

    def test_drift_is_small(self):
        seed()
        pts = generate_idle_drift_points(360, 240, max_offset=18)
        self.assertLess(math.dist(pts[0], pts[-1]), 60)


class HumanizerClassTest(unittest.TestCase):
    def test_tracks_the_cursor_between_moves(self):
        page = FakePage()
        h = Humanizer(page=page)
        with no_sleep:
            h.move(100, 100)
            h.move(300, 200)
        self.assertEqual((h.current_x, h.current_y), (300, 200))
        self.assertTrue(all(-20 <= x <= 740 for x, _ in page.moves()))

    def test_click_coords_moves_then_clicks(self):
        page = FakePage()
        with no_sleep:
            Humanizer(page=page).click_coords(50, 60)
        self.assertEqual([e[0] for e in page.log[-2:]], ["mouse_down", "mouse_up"])
        self.assertEqual(page.moves()[-1], (50, 60))

    def test_click_locator_clicks_inside_the_box(self):
        class Locator:
            def bounding_box(self, timeout):
                return {"x": 100.0, "y": 100.0, "width": 50.0, "height": 20.0}

        page = FakePage()
        h = Humanizer(page=page)
        with no_sleep:
            for _ in range(20):
                x, y = h.click_locator(Locator())
                self.assertTrue(100 <= x <= 150 and 100 <= y <= 120)

    def test_click_locator_falls_back_without_a_box(self):
        clicked = []

        class Locator:
            def bounding_box(self, timeout):
                return None

            def click(self, timeout):
                clicked.append(True)

        with no_sleep:
            self.assertIsNone(Humanizer(page=FakePage()).click_locator(Locator()))
        self.assertEqual(clicked, [True])

    def test_fatigue_ramps_and_caps(self):
        h = Humanizer(page=None, session_start=1000.0, fatigue_horizon=100.0)
        with mock.patch("humanizer.time.time", return_value=1000.0):
            self.assertAlmostEqual(h.get_fatigue_scale(), 1.0)
        with mock.patch("humanizer.time.time", return_value=1050.0):
            self.assertAlmostEqual(h.get_fatigue_scale(), 1.125)
        with mock.patch("humanizer.time.time", return_value=99999.0):
            self.assertAlmostEqual(h.get_fatigue_scale(), 1.25)

    def test_watchdog_trips_on_count_or_time(self):
        h = Humanizer(page=None)
        for _ in range(4):
            self.assertFalse(h.check_watchdog(True, max_iterations=5, max_seconds=999))
        self.assertTrue(h.check_watchdog(True, max_iterations=5, max_seconds=999))
        h.reset_watchdog()
        self.assertFalse(h.check_watchdog(True, max_iterations=5, max_seconds=999))
        h.stuck_start_time -= 1000
        self.assertTrue(h.check_watchdog(True, max_iterations=500, max_seconds=30))
        self.assertFalse(h.check_watchdog(False))
        self.assertEqual(h.stuck_counter, 0)

    def test_idle_drift_survives_a_closed_page(self):
        class ClosedMouse:
            def move(self, x, y):
                raise RuntimeError("page closed")

        page = FakePage()
        page.mouse = ClosedMouse()
        with no_sleep:
            Humanizer(page=page).idle_drift(duration=0.1)  # Must not raise


if __name__ == "__main__":
    unittest.main()
