#!/usr/bin/env python3
"""
Agent: runs bot workers on one machine for the coordinator.

The coordinator decides which bots exist, their names and proxies, and which machine runs
them; an agent only starts and stops browser workers on its own machine and reports back:
- start_bot / update_proxy / stop_bots: commands from the coordinator.
- poll(): workers that exited since the last call (and whether their proxy was bad).
- health(): CPU / RAM and the bots it runs, so the coordinator can balance machines.

Run on its own (`python3 agent.py --coordinator HOST:PORT`), it connects to a coordinator
over the LAN and serves it: commands arrive as messages, and exits and health go back the
same way. Both sides must share the secret in $POWERLINE_AGENT_KEY. If the connection
drops, the agent stops its bots (the coordinator re-places them) and keeps reconnecting.
"""

import argparse
import hashlib
import logging
import multiprocessing
import multiprocessing.connection
import os
import platform
import queue
import shutil
import signal
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

import psutil

from bot import BAD_PROXY_EXIT, IS_WINDOWS, STOP_WORKER, bot_worker_process, kill_processes, process_tree

logger = logging.getLogger("PowerlineBot.Agent")

DEFAULT_PORT = 4041
HEALTH_INTERVAL = 5.0     # Seconds between health reports to the coordinator
RECONNECT_DELAY = 5.0     # Seconds between attempts to reach the coordinator
AGENT_KEY_ENV = "POWERLINE_AGENT_KEY"


def code_version() -> str:
    """Fingerprint of the code that runs the bots; coordinator and agents must match."""
    digest = hashlib.sha256()
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("bot.py", "agent.py", "humanizer.py"):
        with open(os.path.join(here, name), "rb") as f:
            digest.update(f.read().replace(b"\r\n", b"\n"))  # Same fingerprint with Windows line endings
    return digest.hexdigest()[:12]


def parse_address(address: str) -> Tuple[str, int]:
    host, _, port = address.rpartition(":")
    return (host, int(port)) if host else (address, DEFAULT_PORT)


def agent_key() -> bytes:
    key = os.environ.get(AGENT_KEY_ENV, "")
    if not key:
        sys.exit(f"Set ${AGENT_KEY_ENV} to the secret shared by the coordinator and its agents.")
    return key.encode()


class Agent:
    """Runs bot worker processes on this machine."""

    def __init__(
        self,
        name: Optional[str] = None,
        headless: bool = False,
        debug_dir: Optional[str] = None,
        worker_target: Callable = bot_worker_process,  # Swappable so tests can run without a browser
        max_bots: Optional[int] = None,
    ):
        self.name = name or platform.node()
        self.max_bots = max_bots
        self.worker_target = worker_target
        self.headless = headless
        self.debug_dir = debug_dir
        self.workers: Dict[int, Tuple[multiprocessing.Process, "multiprocessing.Queue"]] = {}
        self.reports = multiprocessing.Queue()  # Workers say which proxy failed before exiting
        self.failed_proxies: Dict[int, Dict[str, str]] = {}  # Reports waiting for their worker's exit
        self.bot_status: Dict[int, dict] = {}  # Latest status each bot reported (for the dashboard)
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
        psutil.cpu_percent()  # Prime the counter; the first reading is always 0

    def worker_home(self, bot_id: int) -> str:
        # Chosen here (unique per agent run) so the agent can delete it even if it has to kill the worker.
        return os.path.join(tempfile.gettempdir(), f"camoufox_worker_{os.getpid()}_{bot_id}")

    def start_bot(self, bot_id: int, room: str, proxy: Dict[str, str], nickname: str, start_delay: float):
        # Fresh queue, so a restarted worker doesn't replay updates meant for the old one.
        updates = multiprocessing.Queue()
        p = multiprocessing.Process(
            target=self.worker_target,
            args=(
                bot_id, room, proxy, self.headless, start_delay, nickname, updates,
                self.debug_dir, self.worker_home(bot_id), self.reports,
            ),
            name=f"PowerlineWorker-{bot_id}",
        )
        p.daemon = True
        p.start()
        self.workers[bot_id] = (p, updates)
        logger.info(f"[Agent {self.name}] Started worker #{bot_id} '{nickname}' (PID: {p.pid})")

    def update_proxy(self, bot_id: int, proxy: Dict[str, str]):
        """Tell a running bot to reload the game on a new proxy IP."""
        entry = self.workers.get(bot_id)
        if entry and entry[0].is_alive():
            entry[1].put(proxy)

    def stop_bots(self, bot_ids: List[int]):
        """Stop bots and every browser process they started, with one shared deadline."""
        items = [(i, *self.workers.pop(i)) for i in bot_ids if i in self.workers]
        for i, _, _ in items:
            self.bot_status.pop(i, None)
        # Record each worker's browser processes first: once a worker is killed, Windows has no
        # process group to find them by.
        trees = {i: process_tree(p.pid) for i, p, _ in items}
        for i, p, updates in items:
            if p.is_alive():
                updates.put(STOP_WORKER)  # Asks the worker to close its browser itself
                if not IS_WINDOWS:
                    p.terminate()  # SIGTERM also unwinds it (on Windows this would be a hard kill)
        deadline = time.time() + 8
        for i, p, _ in items:
            p.join(timeout=max(0.0, deadline - time.time()))
            if p.is_alive():
                p.kill()
                p.join(timeout=2)
            self._clean_up(i, p, trees[i])

    def stop_all(self):
        self.stop_bots(list(self.workers))

    def _clean_up(self, bot_id: int, p: multiprocessing.Process, tree: List[psutil.Process]):
        kill_processes(tree)
        if hasattr(os, "killpg"):
            # Each worker leads its own process group; also catches browser processes started after the snapshot.
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(self.worker_home(bot_id), ignore_errors=True)

    def poll(self) -> List[dict]:
        """Report workers that exited on their own since the last call, and clean up after them.
        For a bad-proxy exit, "proxy" is the proxy the worker was actually using."""
        self._drain_reports()
        events = []
        for bot_id, (p, _) in list(self.workers.items()):
            if p.is_alive():
                continue
            del self.workers[bot_id]
            self._clean_up(bot_id, p, [])
            self.bot_status.pop(bot_id, None)
            if p.exitcode == BAD_PROXY_EXIT and bot_id not in self.failed_proxies:
                # The report can trail the exit by a moment.
                deadline = time.time() + 1
                while bot_id not in self.failed_proxies and time.time() < deadline:
                    try:
                        report = self.reports.get(timeout=max(0.01, deadline - time.time()))
                    except queue.Empty:
                        break
                    self._file_report(report)
            events.append({
                "type": "exited",
                "bot": bot_id,
                "code": p.exitcode,
                "bad_proxy": p.exitcode == BAD_PROXY_EXIT,
                "proxy": self.failed_proxies.pop(bot_id, None),
            })
        return events

    def _file_report(self, report: dict):
        if "bad_proxy" in report:
            self.failed_proxies[report["bot"]] = report["bad_proxy"]
        elif "status" in report and report["bot"] in self.workers:
            self.bot_status[report["bot"]] = report["status"]

    def _drain_reports(self):
        while True:
            try:
                report = self.reports.get_nowait()
            except queue.Empty:
                return
            self._file_report(report)

    def health(self) -> dict:
        """A snapshot for the coordinator's placement decisions and dashboard."""
        self._drain_reports()
        mem = psutil.virtual_memory()
        return {
            "agent": self.name,
            "cpu_percent": psutil.cpu_percent(),
            "cpu_count": psutil.cpu_count(),
            "mem_available_mb": mem.available // (1024 * 1024),
            "bots": sorted(self.workers),
            "max_bots": self.max_bots,
            "mem_total_mb": mem.total // (1024 * 1024),
            "platform": platform.system(),
            "bot_status": {str(b): st for b, st in self.bot_status.items()},
        }

    def handle(self, msg: dict):
        """Apply a command message from the coordinator."""
        if msg["type"] == "start":
            self.start_bot(msg["bot"], msg["room"], msg["proxy"], msg["nickname"], msg["start_delay"])
        elif msg["type"] == "update_proxy":
            self.update_proxy(msg["bot"], msg["proxy"])
        elif msg["type"] == "stop":
            self.stop_bots(msg["bots"])
        elif msg["type"] == "stop_all":
            self.stop_all()
        elif msg["type"] == "set_max_bots":
            self.max_bots = msg["max_bots"]
        elif msg["type"] == "rejected":
            logger.error(f"[Agent {self.name}] Coordinator refused this agent: {msg['reason']}")
            sys.exit(1)


def serve(conn: multiprocessing.connection.Connection, agent: Agent):
    """Serve one coordinator connection until it drops."""
    conn.send({"type": "hello", "version": code_version(), **agent.health()})
    next_health = time.time() + HEALTH_INTERVAL
    while True:
        if conn.poll(0.5):
            agent.handle(conn.recv())
        for event in agent.poll():
            conn.send(event)
        if time.time() >= next_health:
            conn.send({"type": "health", **agent.health()})
            next_health = time.time() + HEALTH_INTERVAL


def run_agent(address: Tuple[str, int], key: bytes, agent: Agent):
    """Stay connected to the coordinator, reconnecting whenever the connection drops."""
    while True:
        try:
            conn = multiprocessing.connection.Client(address, authkey=key)
        except multiprocessing.AuthenticationError:
            sys.exit(f"Coordinator at {address[0]}:{address[1]} rejected the key in ${AGENT_KEY_ENV}.")
        except OSError as e:
            logger.info(f"[Agent {agent.name}] Coordinator at {address[0]}:{address[1]} not reachable ({e}); retrying...")
            time.sleep(RECONNECT_DELAY)
            continue
        logger.info(f"[Agent {agent.name}] Connected to coordinator at {address[0]}:{address[1]}.")
        try:
            serve(conn, agent)
        except (EOFError, OSError):
            logger.warning(f"[Agent {agent.name}] Lost the coordinator; stopping bots until it's back.")
            agent.stop_all()
            conn.close()
            time.sleep(RECONNECT_DELAY)


def main():
    parser = argparse.ArgumentParser(description="Run Powerline bots on this machine for a coordinator")
    parser.add_argument("--coordinator", required=True, help=f"Coordinator address HOST[:PORT] (default port {DEFAULT_PORT})")
    parser.add_argument("--name", help="Name for this machine in the coordinator's logs (default: hostname)")
    parser.add_argument("--headless", action="store_true", help="Run browsers headless")
    parser.add_argument("--max-bots", type=int, help="Never run more than this many bots here")
    parser.add_argument("--debug-dir", help="Save screenshots of key moments to this folder")
    args = parser.parse_args()

    agent = Agent(name=args.name, headless=args.headless, debug_dir=args.debug_dir, max_bots=args.max_bots)

    def shutdown(*_):
        logger.info(f"[Agent {agent.name}] Shutting down, stopping bots...")
        agent.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown)  # Windows: Ctrl+Break, how background consoles get stopped

    run_agent(parse_address(args.coordinator), agent_key(), agent)


if __name__ == "__main__":
    main()
