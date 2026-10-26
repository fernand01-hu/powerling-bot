import itertools
import multiprocessing
import os
import queue
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
from bot import (
    BAD_PROXY_EXIT,
    RANDOM_NAME_BASES,
    STOP_WORKER,
    PowerlineBot,
    generate_random_nickname,
    kill_processes,
    pick_next_direction,
    process_tree,
    to_playwright_proxy,
)
from fakes import FakeBrowser, FakePage, proxy
from humanizer import Humanizer

no_sleep = mock.patch.multiple("humanizer.time", sleep=lambda *_: None)


def make_bot(**kwargs):
    defaults = dict(instance_id=7, room="abcd", proxy=proxy("1.2.3.4"), nickname="Tester")
    defaults.update(kwargs)
    return PowerlineBot(**defaults)


def drain(q):
    items = []
    while True:
        try:
            items.append(q.get(timeout=0.2))
        except queue.Empty:
            return items


class NicknameTest(unittest.TestCase):
    def test_names_are_short_and_simple(self):
        for _ in range(3000):
            name = generate_random_nickname()
            self.assertRegex(name, r"^[A-Za-z0-9_]{1,15}$")
            self.assertTrue(any(name.startswith(base[:len(name)]) for base in RANDOM_NAME_BASES), name)

    def test_names_vary(self):
        self.assertGreater(len({generate_random_nickname() for _ in range(500)}), 200)


class SteeringTest(unittest.TestCase):
    def test_turns_favour_down_and_left(self):
        after_vertical = Counter(pick_next_direction("ArrowDown")[0] for _ in range(20000))
        self.assertAlmostEqual(after_vertical["ArrowLeft"] / 20000, 0.9, delta=0.02)
        after_horizontal = Counter(pick_next_direction("ArrowLeft")[0] for _ in range(20000))
        self.assertAlmostEqual(after_horizontal["ArrowDown"] / 20000, 0.9, delta=0.02)

    def test_first_move_is_even(self):
        first = Counter(pick_next_direction(None)[0] for _ in range(20000))
        self.assertAlmostEqual(first["ArrowDown"] / 20000, 0.5, delta=0.02)

    def test_unknown_direction_treated_as_first_move(self):
        self.assertIn(pick_next_direction("Space")[0], ("ArrowDown", "ArrowLeft"))


class ProxyFormatTest(unittest.TestCase):
    def test_playwright_proxy(self):
        self.assertEqual(
            to_playwright_proxy({"host": "1.2.3.4", "port": "8080", "user": "bob", "pass": "pw"}),
            {"server": "http://1.2.3.4:8080", "username": "bob", "password": "pw"},
        )


class StateReportTest(unittest.TestCase):
    def setUp(self):
        self.reports = multiprocessing.Queue()
        self.bot = make_bot(reports=self.reports)

    def test_reports_each_change(self):
        self.bot.set_state("loading")
        self.bot.set_state("lobby")
        states = [r["status"]["state"] for r in drain(self.reports)]
        self.assertEqual(states, ["loading", "lobby"])

    def test_same_state_is_throttled_unless_forced(self):
        self.bot.set_state("playing")
        self.bot.set_state("playing")
        self.bot.set_state("playing")
        self.assertEqual(len(drain(self.reports)), 1)
        self.bot.set_state("playing", force=True)
        self.assertEqual(len(drain(self.reports)), 1)

    def test_same_state_repeats_after_the_interval(self):
        self.bot.set_state("playing")
        self.bot.last_status -= bot.STATUS_INTERVAL + 1
        self.bot.set_state("playing")
        self.assertEqual(len(drain(self.reports)), 2)

    def test_report_contents(self):
        self.bot.rounds = 4
        self.bot.last_respawn = 2.5
        self.bot.set_state("dead")
        report = drain(self.reports)[0]
        self.assertEqual(report["bot"], 7)
        status = report["status"]
        self.assertEqual((status["state"], status["rounds"], status["last_respawn"]), ("dead", 4, 2.5))
        self.assertEqual((status["proxy"], status["name"]), ("1.2.3.4", "Tester"))
        self.assertLessEqual(status["since"], status["updated"])

    def test_since_only_moves_on_change(self):
        self.bot.set_state("playing")
        since = self.bot.state_since
        self.bot.set_state("playing", force=True)
        self.assertEqual(self.bot.state_since, since)

    def test_no_queue_is_fine(self):
        make_bot().set_state("loading")

    def test_broken_queue_is_ignored(self):
        class Broken:
            def put_nowait(self, _):
                raise RuntimeError("full")

        make_bot(reports=Broken()).set_state("loading")


class ProxyUpdateTest(unittest.TestCase):
    def test_no_queue(self):
        self.assertFalse(make_bot().poll_proxy_update())

    def test_keeps_the_newest_update(self):
        updates = multiprocessing.Queue()
        b = make_bot(proxy_updates=updates)
        self.assertFalse(b.poll_proxy_update())
        updates.put(proxy("a"))
        updates.put(proxy("b"))
        time.sleep(0.2)
        self.assertTrue(b.poll_proxy_update())
        self.assertEqual(b.pending_proxy["host"], "b")

    def test_stop_message_exits(self):
        updates = multiprocessing.Queue()
        updates.put(STOP_WORKER)
        time.sleep(0.2)
        with self.assertRaises(SystemExit):
            make_bot(proxy_updates=updates).poll_proxy_update()


class PageHelpersTest(unittest.TestCase):
    def test_game_state_checks_pass_page_answers_through(self):
        self.assertTrue(PowerlineBot.is_out_of_game(FakePage(evaluate=lambda s: True)))
        self.assertFalse(PowerlineBot.is_out_of_game(FakePage(evaluate=lambda s: False)))
        page = FakePage(evaluate=lambda s: True)
        PowerlineBot.is_connected(page)
        self.assertIn("overlay", page.scripts[0])
        self.assertIn("deathPlayAgain", page.scripts[0])

    def test_debug_shot_only_with_a_folder(self):
        page = FakePage()
        make_bot().debug_shot(page, "death")
        self.assertEqual(page.screenshots, [])
        folder = tempfile.mkdtemp()
        make_bot(debug_dir=folder).debug_shot(page, "death")
        self.assertEqual(len(page.screenshots), 1)
        self.assertRegex(os.path.basename(page.screenshots[0]), r"^bot7_\d{6}_death\.png$")

    def test_debug_shot_failure_is_harmless(self):
        page = FakePage()
        page.screenshot = mock.Mock(side_effect=RuntimeError("closed"))
        make_bot(debug_dir=tempfile.mkdtemp()).debug_shot(page, "x")

    def test_open_game_navigates_and_sets_low_graphics(self):
        page = FakePage(evaluate=lambda s: True)
        b = make_bot()
        b.open_game(page)
        self.assertEqual(page.gotos, ["https://powerline.io/#abcd"])
        self.assertTrue(any("settingsGraphicsLow" in s for s in page.scripts))
        self.assertEqual(b.state, "loading")

    def test_open_game_survives_navigation_and_lobby_failures(self):
        page = FakePage(evaluate=lambda s: True)
        page.goto_error = TimeoutError("slow")
        page.lobby_appears = False
        fake_clock = itertools.count(1000.0, 10.0)  # Each look at the clock is 10s later: no real 30s wait
        with mock.patch("bot.time.time", lambda: next(fake_clock)):
            make_bot().open_game(page)  # Logs warnings, never raises
        self.assertLessEqual(page.waits, 5)

    def test_low_graphics_failure_is_harmless(self):
        page = FakePage(evaluate=mock.Mock(side_effect=RuntimeError("detached")))
        make_bot().set_low_graphics(page)


class SwitchProxyTest(unittest.TestCase):
    def test_moves_to_a_new_context_on_the_new_proxy(self):
        b = make_bot()
        old = FakePage()
        b.humanizer = Humanizer(page=old)
        b.pending_proxy = proxy("9.9.9.9", "7000")
        browser = FakeBrowser()
        with no_sleep:
            new = b.switch_proxy(browser, old)
        self.assertIs(new, browser.pages[0])
        self.assertEqual(browser.proxies[0]["server"], "http://9.9.9.9:7000")
        self.assertTrue(old.context.closed)
        self.assertIs(b.humanizer.page, new)
        self.assertIsNone(b.pending_proxy)
        self.assertEqual(b.proxy["host"], "9.9.9.9")
        self.assertEqual(new.gotos, ["https://powerline.io/#abcd"])


class RespawnTest(unittest.TestCase):
    def run_respawn(self, out_of_game_answers):
        answers = iter(out_of_game_answers)
        page = FakePage(evaluate=lambda s: next(answers) if "overlay" in s else None)
        b = make_bot()
        b.humanizer = Humanizer(page=page)
        clock = itertools.count(1000.0, 0.5)  # Every time.time() call advances half a second
        with no_sleep, mock.patch("bot.time.sleep", lambda *_: None), mock.patch("bot.time.time", lambda: next(clock)), \
                mock.patch("bot.sample_respawn_delay", return_value=1.0):
            b.respawn(page, died_at=1000.0)
        return b, page

    def test_presses_enter_until_back_in_game(self):
        b, page = self.run_respawn([True, True, False])
        self.assertEqual(page.keys_pressed(), ["Enter", "Enter", "Enter"])
        self.assertIsNotNone(b.last_respawn)
        self.assertEqual(b.state, "dead")

    def test_gives_up_when_it_never_respawns(self):
        b, page = self.run_respawn(itertools.repeat(True))
        self.assertGreater(len(page.keys_pressed()), 1)
        self.assertIsNone(b.last_respawn)


class RunExitCodeTest(unittest.TestCase):
    def test_failure_before_reaching_the_game_blames_the_proxy(self):
        b = make_bot()
        with mock.patch.object(b, "run_session", side_effect=RuntimeError("proxy refused")):
            self.assertEqual(b.run(), BAD_PROXY_EXIT)

    def test_failure_after_playing_is_a_plain_crash(self):
        b = make_bot()

        def crash():
            b.reached_game = True
            raise RuntimeError("page crashed")

        with mock.patch.object(b, "run_session", side_effect=crash):
            self.assertIsNone(b.run())

    def test_being_stopped_is_a_clean_exit(self):
        b = make_bot()
        with mock.patch.object(b, "run_session", side_effect=SystemExit(0)):
            self.assertIsNone(b.run())

    def test_session_result_is_passed_through(self):
        b = make_bot()
        with mock.patch.object(b, "run_session", return_value=BAD_PROXY_EXIT):
            self.assertEqual(b.run(), BAD_PROXY_EXIT)

    def test_setup_page_detects_websocket_socketerror(self):
        b = make_bot()
        handlers = {}
        class MockPage:
            def on(self, event, handler):
                handlers[event] = handler

        mock_page = MockPage()
        b.setup_page(mock_page)

        class MockWS:
            def on(self, event, handler):
                if event == "socketerror":
                    handler("WebSocket error: 400 Bad Request")

        handlers["websocket"](MockWS())
        self.assertTrue(b.ws_blocked)
        self.assertIn("400 Bad Request", b.ws_blocked_reason)

    def test_setup_page_detects_http_400_response(self):
        b = make_bot()
        handlers = {}
        class MockPage:
            def on(self, event, handler):
                handlers[event] = handler

        mock_page = MockPage()
        b.setup_page(mock_page)

        class MockResp:
            status = 400
            url = "https://51-91-214-104.powerline.io:9187/_/db1fa2b8"

        handlers["response"](MockResp())
        self.assertTrue(b.ws_blocked)
        self.assertIn("HTTP 400", b.ws_blocked_reason)


@unittest.skipIf(sys.platform == "win32", "uses a POSIX shell")
class ProcessCleanupTest(unittest.TestCase):
    def test_finds_and_kills_a_whole_process_tree(self):
        parent = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60 & wait"])
        try:
            deadline = time.time() + 5
            while len(process_tree(parent.pid)) < 2 and time.time() < deadline:
                time.sleep(0.05)
            children = process_tree(parent.pid)
            self.assertEqual(len(children), 2)
            kill_processes(children)
            self.assertTrue(all(not c.is_running() or c.status() == "zombie" for c in children))
        finally:
            parent.kill()
            parent.wait()

    def test_missing_process_is_harmless(self):
        self.assertEqual(process_tree(2 ** 22 + 12345), [])
        kill_processes([])


if __name__ == "__main__":
    unittest.main()
