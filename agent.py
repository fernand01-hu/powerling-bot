#!/usr/bin/env python3
"""
Agent: runs bot workers on one machine for the coordinator.

The coordinator decides which bots exist, their names and proxies, and which machine runs
them; an agent only starts and stops browser workers on its own machine and reports back:
- start_bot / update_proxy / stop_bots: commands from the coordinator.
- poll(): workers that exited since the last call (and whether their proxy was bad).
- health(): CPU / RAM and the bots it runs, so the coordinator can balance machines.

Commands and reports are plain picklable values, so the same calls can later be sent over
the network to agents on other machines.
"""

import logging
import multiprocessing
import os
import platform
import queue
import shutil
import signal
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

import psutil

from bot import BAD_PROXY_EXIT, IS_WINDOWS, STOP_WORKER, bot_worker_process, kill_processes, process_tree

logger = logging.getLogger("PowerlineBot.Agent")


class Agent:
    """Runs bot worker processes on this machine."""

    def __init__(
        self,
        name: Optional[str] = None,
        headless: bool = False,
        debug_dir: Optional[str] = None,
        worker_target: Callable = bot_worker_process,  # Swappable so tests can run without a browser
    ):
        self.name = name or platform.node()
        self.worker_target = worker_target
        self.headless = headless
        self.debug_dir = debug_dir
        self.workers: Dict[int, Tuple[multiprocessing.Process, "multiprocessing.Queue"]] = {}
        self.reports = multiprocessing.Queue()  # Workers say which proxy failed before exiting
        self.failed_proxies: Dict[int, Dict[str, str]] = {}  # Reports waiting for their worker's exit
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
        while True:
            try:
                report = self.reports.get_nowait()
            except queue.Empty:
                break
            self.failed_proxies[report["bot"]] = report["bad_proxy"]
        events = []
        for bot_id, (p, _) in list(self.workers.items()):
            if p.is_alive():
                continue
            del self.workers[bot_id]
            self._clean_up(bot_id, p, [])
            if p.exitcode == BAD_PROXY_EXIT and bot_id not in self.failed_proxies:
                # The report can trail the exit by a moment.
                deadline = time.time() + 1
                while bot_id not in self.failed_proxies and time.time() < deadline:
                    try:
                        report = self.reports.get(timeout=max(0.01, deadline - time.time()))
                    except queue.Empty:
                        break
                    self.failed_proxies[report["bot"]] = report["bad_proxy"]
            events.append({
                "type": "exited",
                "bot": bot_id,
                "code": p.exitcode,
                "bad_proxy": p.exitcode == BAD_PROXY_EXIT,
                "proxy": self.failed_proxies.pop(bot_id, None),
            })
        return events

    def health(self) -> dict:
        """A snapshot for the coordinator's placement decisions."""
        mem = psutil.virtual_memory()
        return {
            "agent": self.name,
            "cpu_percent": psutil.cpu_percent(),
            "cpu_count": psutil.cpu_count(),
            "mem_available_mb": mem.available // (1024 * 1024),
            "bots": sorted(self.workers),
        }
