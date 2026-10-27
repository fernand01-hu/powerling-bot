"""Stand-ins for a Playwright page/browser and for agents, so tests run without a browser or network."""
import os
import sys
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeMouse:
    def __init__(self, log: List[tuple]):
        self.log = log

    def move(self, x, y):
        self.log.append(("move", x, y))

    def down(self):
        self.log.append(("mouse_down",))

    def up(self):
        self.log.append(("mouse_up",))


class FakeKeyboard:
    def __init__(self, log: List[tuple]):
        self.log = log

    def down(self, key):
        self.log.append(("key_down", key))

    def up(self, key):
        self.log.append(("key_up", key))


class FakeContext:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakePage:
    """Records input and answers page.evaluate() with `evaluate`, a function of the script text."""

    def __init__(self, evaluate: Optional[Callable[[str], object]] = None):
        self.log: List[tuple] = []
        self.mouse = FakeMouse(self.log)
        self.keyboard = FakeKeyboard(self.log)
        self.context = FakeContext()
        self.evaluate_fn = evaluate or (lambda script: None)
        self.scripts: List[str] = []
        self.gotos: List[str] = []
        self.screenshots: List[str] = []
        self.goto_error: Optional[Exception] = None
        self.lobby_appears = True

    def evaluate(self, script):
        self.scripts.append(script)
        return self.evaluate_fn(script)

    def goto(self, url, **_):
        self.gotos.append(url)
        if self.goto_error:
            raise self.goto_error

    def wait_for_selector(self, selector, **_):
        self.waits = getattr(self, "waits", 0) + 1
        if not self.lobby_appears or self.waits <= getattr(self, "lobby_after", 0):
            raise TimeoutError(selector)

    def screenshot(self, path=None, **_):
        self.screenshots.append(path)
        return b"\xff\xd8fake-jpeg"

    def keys_pressed(self) -> List[str]:
        return [entry[1] for entry in self.log if entry[0] == "key_down"]

    def moves(self) -> List[tuple]:
        return [(entry[1], entry[2]) for entry in self.log if entry[0] == "move"]


class FakeBrowser:
    def __init__(self):
        self.pages: List[FakePage] = []
        self.proxies: List[dict] = []

    def new_page(self, proxy=None):
        page = FakePage(evaluate=lambda script: True)
        self.pages.append(page)
        self.proxies.append(proxy)
        return page


class FakeAgent:
    """Records the coordinator's calls; tests feed it exit events and health."""

    def __init__(self, name: str, cpu_count: int = 8, cpu: float = 10.0, max_bots: Optional[int] = None):
        self.name = name
        self.alive = True
        self.cpu_count = cpu_count
        self.cpu = cpu
        self.max_bots = max_bots
        self.running: Dict[int, dict] = {}
        self.calls: List[tuple] = []
        self.events: List[dict] = []
        self.bot_status: Dict[str, dict] = {}

    def start_bot(self, bot_id, room, proxy, nickname, start_delay):
        self.running[bot_id] = {"proxy": proxy, "nickname": nickname, "start_delay": start_delay}
        self.calls.append(("start", bot_id))

    def update_proxy(self, bot_id, proxy):
        if bot_id in self.running:
            self.running[bot_id]["proxy"] = proxy
        self.calls.append(("update_proxy", bot_id, proxy["host"]))

    def stop_bots(self, bot_ids, wait=False):
        for b in bot_ids:
            self.running.pop(b, None)
        self.calls.append(("stop", tuple(bot_ids)))

    def stop_all(self, wait=False):
        self.stop_bots(list(self.running))

    def request_screenshot(self, bot_id):
        self.calls.append(("screenshot", bot_id))

    def restart(self):
        self.calls.append(("restart",))

    def exit(self, bot_id, code=1, bad_proxy=False, proxy=None):
        """Simulate the worker for bot_id exiting on its own."""
        self.running.pop(bot_id, None)
        self.events.append({"type": "exited", "bot": bot_id, "code": code, "bad_proxy": bad_proxy, "proxy": proxy})

    def poll(self):
        events, self.events = self.events, []
        return events

    def health(self):
        return {
            "agent": self.name, "cpu_percent": self.cpu, "cpu_count": self.cpu_count,
            "mem_available_mb": 4000, "mem_total_mb": 8000, "bots": sorted(self.running),
            "max_bots": self.max_bots, "platform": "Test", "bot_status": self.bot_status,
            "logs": self.pop_logs(),
        }

    def pop_logs(self):
        logs, self.logs = getattr(self, "logs", []), []
        return logs


def proxy(host: str, port: str = "8080", country: str = "XX") -> dict:
    return {"host": host, "port": port, "user": "u", "pass": "p", "country": country}
