#!/usr/bin/env python3
"""
Coordinator: decides what the bots do and which machine runs them.

- Splits the bots into groups that share one Webshare proxy IP, and keeps groups spread over
  the proxies as Webshare rotates them (reloading a group's bots on its new IP).
- Benches proxies that can't reach the game and moves their group.
- Gives every bot one nickname for the whole run.
- Places each bot on an agent (agent.py) - on this machine and/or on other machines that
  connect over the LAN - and restarts bots whose worker exits.
- Watches every machine's CPU: new bots go where there's room, and a machine that stays
  overloaded hands a bot to one with headroom.

Run it with `python3 coordinator.py` (`python3 bot.py` does the same).
"""

import argparse
import base64
import json
import logging
import math
import queue
import multiprocessing.connection
import os
import random
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from agent import (
    AGENT_KEY_ENV, DEFAULT_PORT, HEALTH_INTERVAL, Agent, agent_key, code_version, parse_address, recv_msg, send_msg,
)
from bot import DEFAULT_ROOM, generate_random_nickname
from dashboard import DASHBOARD_PASSWORD_ENV, DEFAULT_DASHBOARD_PORT, start_dashboard

logger = logging.getLogger("PowerlineBot.Coordinator")

DEFAULT_WEBSHARE_API_KEY = os.environ.get("WEBSHARE_API_KEY", "")
DEFAULT_BOTS_PER_PROXY = 3
DEFAULT_GROUPS = 3
DEFAULT_STAGGER_INTERVAL = 3.0       # Seconds between each bot in a group connecting
DEFAULT_PROXY_CHECK_INTERVAL = 60.0  # Seconds between Webshare checks for rotated IPs
BAD_PROXY_COOLDOWN = 30 * 60         # Seconds a bad proxy is left out of the rotation

GAME_WS_HOST = "51-91-214-104.powerline.io"
GAME_WS_PORT = 9187
GAME_WS_PATH = "/_/db1fa2b8"


def probe_proxy_handshake(
    proxy: dict,
    target_host: str = GAME_WS_HOST,
    target_port: int = GAME_WS_PORT,
    path: str = GAME_WS_PATH,
    timeout: float = 6.0,
) -> Tuple[bool, str]:
    """Test if a proxy can complete the WebSocket handshake with the Powerline game server.
    Returns (True, 'HTTP/1.1 101 Switching Protocols') if clean, or (False, reason) if blocked."""
    host = proxy.get("host", "")
    port = int(proxy.get("port", 0))
    user = proxy.get("user", "")
    passwd = proxy.get("pass", "")

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    ss = None
    try:
        s.connect((host, port))
        auth_bytes = f"{user}:{passwd}".encode("utf-8")
        auth_header = (
            f"Proxy-Authorization: Basic {base64.b64encode(auth_bytes).decode('ascii')}\r\n"
            if user
            else ""
        )
        connect_req = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n"
            f"{auth_header}"
            f"User-Agent: Mozilla/5.0\r\n"
            f"Proxy-Connection: Keep-Alive\r\n\r\n"
        )
        s.sendall(connect_req.encode("utf-8"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        status_line = resp.split(b"\r\n")[0].decode("utf-8", errors="replace")
        if not any(ok in status_line for ok in ("200 Connection established", "200 OK")):
            return False, f"Proxy CONNECT failed: {status_line}"

        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(s, server_hostname=target_host)
        ws_req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {target_host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: https://powerline.io\r\n"
            f"User-Agent: Mozilla/5.0\r\n\r\n"
        )
        ss.sendall(ws_req.encode("utf-8"))
        ws_resp = b""
        while b"\r\n\r\n" not in ws_resp:
            chunk = ss.recv(4096)
            if not chunk:
                break
            ws_resp += chunk
        ws_status = ws_resp.split(b"\r\n")[0].decode("utf-8", errors="replace")
        if "101" in ws_status:
            return True, ws_status
        return False, ws_status
    except Exception as e:
        return False, f"Handshake error: {e}"
    finally:
        if ss:
            try:
                ss.close()
            except Exception:
                pass
        try:
            s.close()
        except Exception:
            pass


def proxy_key(proxy: Optional[Dict[str, str]]) -> Tuple[str, str]:
    if not proxy:
        return ("direct", "0")
    return proxy.get("host", "direct"), str(proxy.get("port", "0"))


def reassign_proxies(current: List[Optional[Dict[str, str]]], available: List[Dict[str, str]]) -> List[Optional[Dict[str, str]]]:
    """Keep each group on its proxy while it's still available and not carrying more than its share
    of groups; every other group moves to the least-used proxy, so groups stay spread out."""
    if not available:
        return [None] * len(current)
    by_key = {proxy_key(p): p for p in available}
    share = -(-len(current) // len(available))  # Most groups any one proxy should carry (ceil)
    load = {k: 0 for k in by_key}
    result: List[Optional[Dict[str, str]]] = [None] * len(current)
    for g, p in enumerate(current):
        k = proxy_key(p) if p else None
        if k in load and load[k] < share:
            result[g] = by_key[k]
            load[k] += 1
    for g in range(len(result)):
        if result[g] is None:
            k = min(load, key=load.get)  # Ties go to the earliest proxy in Webshare's list
            result[g] = by_key[k]
            load[k] += 1
    return result



# ============================================================================
# Webshare Proxy Manager
# ============================================================================
class WebshareManager:
    """Fetches and manages rotating proxies from Webshare API v2."""

    def __init__(self, api_key: str = DEFAULT_WEBSHARE_API_KEY):
        self.api_key = api_key
        self.proxies: List[Dict[str, str]] = []
        self.list_created_at: Optional[float] = None  # When Webshare issued the current list
        self.last_success: Optional[float] = None  # Last time the list was read without error
        self.last_error: Optional[str] = None
        self.refresh_frequency: Optional[float] = None  # Seconds between Webshare's automatic rotations

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"Authorization": f"Token {self.api_key}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch_refresh_frequency(self) -> Optional[float]:
        """The active plan's automatic rotation period (e.g. 300s), for the next-IP-change countdown."""
        try:
            for plan in self._get("https://proxy.webshare.io/api/v2/subscription/plan/").get("results", []):
                if plan.get("status") == "active" and plan.get("automatic_refresh_frequency"):
                    self.refresh_frequency = float(plan["automatic_refresh_frequency"])
        except Exception as e:
            logger.warning(f"[Webshare] Couldn't read the plan's rotation frequency: {e}")
        return self.refresh_frequency

    def next_rotation(self, now: float) -> Optional[float]:
        """Estimated time of Webshare's next automatic IP rotation."""
        if not self.list_created_at or not self.refresh_frequency:
            return None
        periods = max(1, math.ceil((now - self.list_created_at) / self.refresh_frequency))
        return self.list_created_at + periods * self.refresh_frequency

    def fetch_proxies(self) -> List[Dict[str, str]]:
        url: Optional[str] = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100"
        headers = {"Authorization": f"Token {self.api_key}"}
        valid = []
        created = []
        try:
            # The list is paginated; follow "next" until every page is read.
            while url:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for p in data.get("results", []):
                    if p.get("created_at"):
                        created.append(p["created_at"])
                    if p.get("valid", True):
                        valid.append({
                            "host": p["proxy_address"],
                            "port": str(p["port"]),
                            "user": p["username"],
                            "pass": p["password"],
                            "country": p.get("country_code", "XX"),
                        })
                url = data.get("next")
        except Exception as e:
            logger.warning(f"[Webshare] Failed to fetch proxies: {e}")
            self.last_error = str(e)
            return self.proxies
        self.last_success = time.time()
        self.last_error = None
        if created:
            try:
                self.list_created_at = max(
                    datetime.fromisoformat(c.replace("Z", "+00:00")).timestamp() for c in created
                )
            except ValueError:
                pass
        if valid:
            self.proxies = valid
            logger.info(f"[Webshare] Fetched {len(valid)} active proxies from API.")
        return self.proxies

    def replace_proxies(self, bad_ips: List[str], dry_run: bool = False) -> Optional[int]:
        """Request replacement of specific bad IPs via Webshare API v3."""
        if not bad_ips or not self.api_key:
            return None
        ips = [ip.strip() for ip in bad_ips if ip.strip()]
        if not ips:
            return None
        payload = {
            "to_replace": {"type": "ip_range", "ip_ranges": [f"{ip}/32" for ip in ips]},
            "replace_with": [{"type": "any"} for _ in ips],
            "dry_run": dry_run,
        }
        url = "https://proxy.webshare.io/api/v3/proxy/replace/"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                order_id = data.get("id")
                logger.info(f"[Webshare] Replacement requested for {len(ips)} IPs (order #{order_id}).")
                return order_id
        except Exception as e:
            logger.warning(f"[Webshare] Replacement request failed: {e}")
            return None

    def wait_for_replacement(self, replace_id: int, timeout: float = 60.0, poll_interval: float = 2.0) -> bool:
        """Poll Webshare API v3 until replacement order completes."""
        url = f"https://proxy.webshare.io/api/v3/proxy/replace/{replace_id}/"
        headers = {"Authorization": f"Token {self.api_key}"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                state = data.get("state")
                if state == "completed":
                    logger.info(f"[Webshare] Replacement order #{replace_id} completed successfully.")
                    return True
                if state in ("failed", "rejected", "cancelled"):
                    logger.warning(f"[Webshare] Replacement order #{replace_id} {state}: {data.get('error') or data.get('reason')}")
                    return False
            except Exception as e:
                logger.debug(f"[Webshare] Polling replacement order #{replace_id}: {e}")
            time.sleep(poll_interval)
        logger.warning(f"[Webshare] Replacement order #{replace_id} timed out after {timeout}s.")
        return False

    def replace_and_refresh(self, bad_ips: List[str]) -> List[Dict[str, str]]:
        """Replace bad IPs via Webshare API v3 and fetch the fresh proxy list."""
        order_id = self.replace_proxies(bad_ips)
        if order_id is not None:
            if self.wait_for_replacement(order_id):
                return self.fetch_proxies()
        return self.proxies




OVERLOAD_CPU = 85.0        # Average CPU % at which a machine counts as overloaded
HEADROOM_CPU = 65.0        # A machine must be below this to take a bot moved off an overloaded one
CPU_WINDOW = 6             # Health samples averaged (one every HEALTH_INTERVAL seconds -> 30s)
REBALANCE_COOLDOWN = 60.0  # Seconds between moving bots off overloaded machines
DEFAULT_WAIT_AGENTS = 15.0  # Seconds to let remote agents connect before placing bots
DEFAULT_STATE_FILE = os.path.join(os.path.expanduser("~"), ".config", "powerline-bot", "state.json")
AGENT_TIMEOUT = 30.0       # An agent silent this long (it reports every HEALTH_INTERVAL) counts as gone
MIN_REFETCH_INTERVAL = 15.0  # Seconds between extra Webshare refreshes triggered by proxy failures
HISTORY_POINTS = 720       # One hour of history at one sample every HEALTH_INTERVAL (5s)
MACHINE_LOG_LINES = 400    # Recent log lines kept per machine for the dashboard
ALERT_MEMORY = 600.0       # Seconds a past event (e.g. a machine disconnecting) stays in the alerts
FAILING_STREAK = 3         # Exits in a row that make a bot show up in the alerts
ROOM_PATTERN = re.compile(r"^[A-Za-z0-9]{1,16}$")
HEALTHY_RUN = 120.0        # A bot that ran this long before exiting has its restart backoff reset


def restart_delay(failures: int) -> float:
    """Seconds a bot waits before restarting after `failures` exits in a row: quick at first,
    then backing off (2, 4, 8 ... 60s) so a dead proxy can't cause a restart storm."""
    if failures <= 1:
        return random.uniform(1.0, 3.5)
    return min(60.0, 2.0 ** (failures - 1)) + random.uniform(0.0, 2.0)


def pick_agent(stats: List[dict]) -> Optional[int]:
    """Index of the agent that should take the next bot, or None if none has room.
    Each stat: {"bots", "cpu_count", "cpu" (recent average %), "max_bots"}. Agents at their
    max are skipped, overloaded ones too unless every agent is, then the fewest bots per core wins."""
    open_agents = [i for i, st in enumerate(stats) if st["max_bots"] is None or st["bots"] < st["max_bots"]]
    if not open_agents:
        return None
    candidates = [i for i in open_agents if stats[i]["cpu"] < OVERLOAD_CPU] or open_agents
    return min(candidates, key=lambda i: (stats[i]["bots"] / max(1, stats[i]["cpu_count"]), i))


def pick_move(stats: List[dict]) -> Optional[Tuple[int, int]]:
    """(from, to) agent indexes when one agent is overloaded and another has headroom for a bot."""
    hot = [i for i, st in enumerate(stats) if st["cpu"] >= OVERLOAD_CPU and st["bots"] > 0]
    if not hot:
        return None
    src = max(hot, key=lambda i: stats[i]["cpu"])
    cool = [
        i for i, st in enumerate(stats)
        if i != src and st["cpu"] < HEADROOM_CPU and (st["max_bots"] is None or st["bots"] < st["max_bots"])
    ]
    if not cool:
        return None
    return src, min(cool, key=lambda i: (stats[i]["bots"] / max(1, stats[i]["cpu_count"]), stats[i]["cpu"], i))


class RemoteAgent:
    """The coordinator's handle on an agent running on another machine (same calls as Agent)."""

    def __init__(self, conn: multiprocessing.connection.Connection, hello: dict):
        self.conn = conn
        self.name = hello["agent"]
        self.last_health = hello
        self.events: List[dict] = []
        self.alive = True
        self.last_seen = time.time()

    def send(self, msg: dict):
        try:
            send_msg(self.conn, msg)
        except (OSError, EOFError, ValueError):
            self.alive = False

    def receive(self):
        try:
            while self.alive and self.conn.poll(0):
                msg = recv_msg(self.conn)
                self.last_seen = time.time()
                if msg["type"] == "health":
                    self.last_health = msg
                elif msg["type"] in ("exited", "screenshot"):
                    self.events.append(msg)
        except (OSError, EOFError, ValueError):
            self.alive = False
        if self.alive and time.time() - self.last_seen > AGENT_TIMEOUT:
            # A machine that lost power or network never closes its connection.
            self.alive = False
            self.conn.close()

    def start_bot(self, bot_id: int, room: str, proxy: Dict[str, str], nickname: str, start_delay: float):
        self.send({"type": "start", "bot": bot_id, "room": room, "proxy": proxy, "nickname": nickname, "start_delay": start_delay})

    def update_proxy(self, bot_id: int, proxy: Dict[str, str]):
        self.send({"type": "update_proxy", "bot": bot_id, "proxy": proxy})

    def stop_bots(self, bot_ids: List[int]):
        self.send({"type": "stop", "bots": bot_ids})

    def stop_all(self):
        self.send({"type": "stop_all"})

    def request_screenshot(self, bot_id: int):
        self.send({"type": "screenshot", "bot": bot_id})

    def restart(self):
        self.send({"type": "restart"})

    def poll(self) -> List[dict]:
        self.receive()
        events, self.events = self.events, []
        return events

    def health(self) -> dict:
        self.receive()
        return self.last_health


# ============================================================================
# Coordinator
# ============================================================================
class Coordinator:
    def __init__(self, args: argparse.Namespace, agents: List, state_file: Optional[str] = None):
        self.args = args
        for key, default in (("proxy_check_interval", DEFAULT_PROXY_CHECK_INTERVAL), ("stagger_interval", DEFAULT_STAGGER_INTERVAL)):
            if not hasattr(args, key):
                setattr(args, key, default)
        self.agents = list(agents)  # Agent (this machine) and RemoteAgent (other machines)
        self.proxy_mgr = WebshareManager(api_key=args.webshare_key)
        self.group_proxies: List[Dict[str, str]] = []
        self.groups = args.groups  # Can change at runtime from the dashboard
        # One name per bot for the whole run, kept across deaths, worker restarts and moves.
        self.nicknames = [generate_random_nickname() for _ in range(self.groups * args.bots_per_proxy)]
        self.bot_agent: Dict[int, object] = {}  # Bot id -> the agent running it
        self.pending: Dict[int, float] = {}      # Bots waiting for an agent with room -> start delay
        self.bad_until: Dict[Tuple[str, str], float] = {}
        self.started_at: Dict[int, float] = {}   # When each bot was last started
        self.failures: Dict[int, int] = {}       # Exits in a row for each bot (restart backoff)
        self.last_fetch = 0.0
        self.cpu_samples: Dict[int, deque] = {}  # id(agent) -> recent CPU % readings
        self.cpu_counts: Dict[int, int] = {}
        self.max_bots: Dict[int, Optional[int]] = {}
        self.next_health = 0.0
        self.last_move = 0.0
        self.joined: List[RemoteAgent] = []  # Filled by the listener thread
        self.joined_lock = threading.Lock()
        # Dashboard: commands come from the web server thread and run in the main loop.
        self.commands: "queue.Queue[dict]" = queue.Queue()
        self.paused: Set[str] = set()       # Machines (agent names) that get no bots
        self.stopped: Set[int] = set()      # Bots stopped from the dashboard (not restarted)
        self.bot_status: Dict[int, dict] = {}
        self.last_health: Dict[int, dict] = {}  # id(agent) -> latest health report
        self.events: deque = deque(maxlen=60)  # Recent coordinator log lines for the dashboard
        self.snapshot: dict = {}
        self.machine_logs: Dict[str, deque] = {}  # Machine name -> its agent's and bots' recent log lines
        self.screenshots: Dict[int, dict] = {}    # Bot id -> latest {"time", "data"} (base64 JPEG)
        self.history: Dict[str, deque] = {}       # Machine name -> [time, cpu %, ram %] samples
        self.bot_history: deque = deque(maxlen=HISTORY_POINTS)  # [time, running, playing]
        self.recent_alerts: deque = deque(maxlen=50)  # Past events worth an alert: {"time", "key", "level", "text"}
        self.next_proxy_check = 0.0
        self.auto_replace: bool = getattr(args, "auto_replace", True)
        self._replacing_ips: Set[str] = set()
        self._replace_lock = threading.Lock()
        self._replace_queue: queue.Queue = queue.Queue()
        self._last_replace_time: float = 0.0
        self._replace_cooldown: float = 60.0  # Minimum seconds between Webshare API replacement requests
        if self.auto_replace and getattr(args, "webshare_key", None):
            threading.Thread(target=self._replace_worker_loop, daemon=True, name="autoreplace-worker").start()
        self.data_lock = threading.Lock()  # Guards logs/history, which the web server thread reads
        events_handler = logging.Handler()
        events_handler.emit = lambda record: self.events.append(
            {"time": record.created, "level": record.levelname, "message": record.getMessage()}
        )
        logger.addHandler(events_handler)
        # Kept across restarts: bot names, paused machines, machine bot limits, stopped bots.
        self.state_file = state_file
        self.saved_max: Dict[str, Optional[int]] = {}  # Machine name -> bot limit set from the dashboard
        self.load_state()
        for agent in self.agents:
            self.apply_saved_limit(agent)

    def load_state(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                state = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"[Coordinator] Couldn't read {self.state_file} ({e}); starting fresh.")
            return
        settings = state.get("settings", {})
        if isinstance(settings.get("room"), str) and ROOM_PATTERN.match(settings["room"]):
            self.args.room = settings["room"]
        if isinstance(settings.get("groups"), int) and 1 <= settings["groups"] <= 50:
            self.groups = settings["groups"]
        if isinstance(settings.get("bots_per_proxy"), int) and 1 <= settings["bots_per_proxy"] <= 10:
            self.args.bots_per_proxy = settings["bots_per_proxy"]
        for key, low, high in (("proxy_check_interval", 15, 3600), ("stagger_interval", 0, 60)):
            if isinstance(settings.get(key), (int, float)) and low <= settings[key] <= high:
                setattr(self.args, key, float(settings[key]))
        if "direct" in settings:
            self.args.direct = bool(settings["direct"])
        while len(self.nicknames) < self.total:
            self.nicknames.append(generate_random_nickname())
        names = [n for n in state.get("nicknames", []) if isinstance(n, str)]
        self.nicknames[:len(names)] = names[:len(self.nicknames)]
        self.paused = set(state.get("paused", []))
        self.stopped = {b for b in state.get("stopped", []) if isinstance(b, int) and 0 <= b < self.total}
        self.saved_max = {k: v for k, v in state.get("max_bots", {}).items() if v is None or isinstance(v, int)}
        logger.info(f"[Coordinator] Restored bot names and settings from {self.state_file}.")

    def save_state(self):
        if not self.state_file:
            return
        state = {
            "nicknames": self.nicknames,
            "paused": sorted(self.paused),
            "stopped": sorted(self.stopped),
            "max_bots": self.saved_max,
            "settings": {
                "room": self.args.room,
                "groups": self.groups,
                "bots_per_proxy": self.args.bots_per_proxy,
                "proxy_check_interval": getattr(self.args, "proxy_check_interval", DEFAULT_PROXY_CHECK_INTERVAL),
                "stagger_interval": self.args.stagger_interval,
                "direct": getattr(self.args, "direct", False),
            },
        }
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_file)  # Never leave a half-written file
        except OSError as e:
            logger.warning(f"[Coordinator] Couldn't save {self.state_file}: {e}")

    def apply_saved_limit(self, agent):
        """Re-apply a bot limit set from the dashboard when its machine (re)connects."""
        if agent.name not in self.saved_max:
            return
        limit = self.saved_max[agent.name]
        if isinstance(agent, RemoteAgent):
            agent.send({"type": "set_max_bots", "max_bots": limit})
            agent.last_health = dict(agent.last_health, max_bots=limit)
        else:
            agent.max_bots = limit
        self.max_bots[id(agent)] = limit

    @property
    def total(self) -> int:
        return self.groups * self.args.bots_per_proxy

    # --- Agents -------------------------------------------------------------
    def listen(self, address: Tuple[str, int], key: bytes):
        """Accept agents from other machines in the background."""
        # No authkey on the listener itself: each connection proves the key in its own thread,
        # so one that connects and never answers can't stop other agents from joining.
        listener = multiprocessing.connection.Listener(address)
        logger.info(f"[Coordinator] Waiting for agents on {address[0]}:{address[1]} (code version {code_version()})")

        def handshake(conn: multiprocessing.connection.Connection):
            try:
                check_and_join(conn)
            except Exception as e:  # Never let one bad connection print a traceback
                logger.warning(f"[Coordinator] Dropped a connection after an unexpected error: {e!r}")
                conn.close()

        def check_and_join(conn: multiprocessing.connection.Connection):
            try:
                # Same mutual check Listener(authkey=...) does: both sides prove they know the key.
                multiprocessing.connection.deliver_challenge(conn, key)
                multiprocessing.connection.answer_challenge(conn, key)
                hello = recv_msg(conn) if conn.poll(10) else None
            except (multiprocessing.AuthenticationError, OSError, EOFError, ValueError) as e:
                logger.warning(f"[Coordinator] Refused a connection: {e or type(e).__name__}")
                conn.close()
                return
            if not hello or hello.get("type") != "hello":
                conn.close()
                return
            if hello.get("version") != code_version():
                reason = f"code version {hello.get('version')} differs from the coordinator's {code_version()}; update its files"
                logger.warning(f"[Coordinator] Agent {hello.get('agent')} rejected: {reason}")
                try:
                    send_msg(conn, {"type": "rejected", "reason": reason})
                except OSError:
                    pass
                conn.close()
                return
            with self.joined_lock:
                self.joined.append(RemoteAgent(conn, hello))

        def accept_loop():
            while True:
                try:
                    conn = listener.accept()
                except OSError as e:
                    logger.warning(f"[Coordinator] Accept failed: {e}")
                    continue
                threading.Thread(target=handshake, args=(conn,), name="agent-handshake", daemon=True).start()

        threading.Thread(target=accept_loop, name="agent-listener", daemon=True).start()

    def add_joined_agents(self):
        with self.joined_lock:
            joined, self.joined = self.joined, []
        for agent in joined:
            # The same machine reconnecting (e.g. after "Restart agent") replaces its old, stale link.
            stale = [a for a in self.agents if isinstance(a, RemoteAgent) and a.name == agent.name]
            for old in stale:
                old.alive = False
                old.conn.close()
            if stale:
                self.drop_dead_agents()
            self.agents.append(agent)
            self.apply_saved_limit(agent)
            health = agent.last_health
            logger.info(
                f"[Coordinator] Agent {agent.name} joined ({health['cpu_count']} CPU threads, "
                f"{health['mem_available_mb']} MB free"
                + (f", max {health['max_bots']} bots" if health.get("max_bots") is not None else "") + ")."
            )
            self.next_health = 0.0  # Refresh stats now so placement sees it

    def drop_dead_agents(self):
        for agent in self.agents:
            if isinstance(agent, RemoteAgent):
                agent.receive()  # Notices closed connections and silent agents
        for agent in [a for a in self.agents if not getattr(a, "alive", True)]:
            self.agents.remove(agent)
            self.last_health.pop(id(agent), None)
            orphans = sorted(b for b, a in self.bot_agent.items() if a is agent)
            logger.warning(f"[Coordinator] Agent {agent.name} disconnected; re-placing its bots {orphans}.")
            self.recent_alerts.append({"time": time.time(), "key": f"left:{agent.name}", "level": "warning",
                                       "text": f"Machine {agent.name} disconnected"})
            for bot_id in orphans:
                del self.bot_agent[bot_id]
                self.pending[bot_id] = random.uniform(1.0, 3.5)
            self.cpu_samples.pop(id(agent), None)

    def refresh_health(self):
        """Sample every agent's health (every HEALTH_INTERVAL seconds)."""
        if time.time() < self.next_health:
            return
        self.next_health = time.time() + HEALTH_INTERVAL
        for agent in self.agents:
            health = agent.health()
            key = id(agent)
            self.cpu_samples.setdefault(key, deque(maxlen=CPU_WINDOW)).append(health["cpu_percent"])
            self.cpu_counts[key] = health["cpu_count"]
            self.max_bots[key] = health.get("max_bots")
            self.last_health[key] = health
            for bot_id, status in health.get("bot_status", {}).items():
                if self.bot_agent.get(int(bot_id)) is agent:
                    self.bot_status[int(bot_id)] = status
            mem_total = health.get("mem_total_mb") or 0
            mem_used = 100.0 * (1 - health.get("mem_available_mb", 0) / mem_total) if mem_total else 0.0
            with self.data_lock:
                log = self.machine_logs.setdefault(agent.name, deque(maxlen=MACHINE_LOG_LINES))
                for line in health.get("logs", []):
                    log.append(dict(line, machine=agent.name))
                self.history.setdefault(agent.name, deque(maxlen=HISTORY_POINTS)).append(
                    [round(time.time(), 1), round(health["cpu_percent"], 1), round(mem_used, 1)]
                )
        playing = sum(1 for b, st in self.bot_status.items() if st.get("state") == "playing" and b in self.bot_agent)
        with self.data_lock:
            self.bot_history.append([round(time.time(), 1), len(self.bot_agent), playing])

    def agent_stats(self) -> List[dict]:
        stats = []
        for agent in self.agents:
            key = id(agent)
            samples = self.cpu_samples.get(key) or [0.0]
            stats.append({
                "bots": sum(1 for a in self.bot_agent.values() if a is agent),
                "cpu_count": self.cpu_counts.get(key, 1),
                "cpu": sum(samples) / len(samples),
                "full_window": len(samples) == CPU_WINDOW,
                "max_bots": 0 if agent.name in self.paused else self.max_bots.get(key),
            })
        return stats

    # --- Bots ---------------------------------------------------------------
    def group_bots(self, g: int) -> range:
        return range(g * self.args.bots_per_proxy, (g + 1) * self.args.bots_per_proxy)

    def place_bot(self, bot_id: int, start_delay: float) -> bool:
        """Start a bot on the agent with the most room for it; queue it if none has room."""
        idx = pick_agent(self.agent_stats())
        if idx is None:
            if bot_id not in self.pending:
                logger.warning(f"[Coordinator] No agent has room for bot #{bot_id}; it waits for one.")
            self.pending[bot_id] = start_delay
            return False
        agent = self.agents[idx]
        proxy = self.group_proxies[bot_id // self.args.bots_per_proxy]
        agent.start_bot(bot_id, self.args.room, proxy, self.nicknames[bot_id], start_delay)
        self.bot_agent[bot_id] = agent
        self.started_at[bot_id] = time.time()
        self.pending.pop(bot_id, None)
        logger.info(f"[Coordinator] Bot #{bot_id} '{self.nicknames[bot_id]}' -> {agent.name}")
        return True

    def place_pending(self):
        for bot_id, start_delay in sorted(self.pending.items()):
            if bot_id in self.stopped or bot_id >= self.total:
                self.pending.pop(bot_id, None)
                continue
            if not self.place_bot(bot_id, start_delay):
                break

    def rebalance(self):
        """Move one bot off a machine that has been overloaded for a while, if another has headroom."""
        if time.time() - self.last_move < REBALANCE_COOLDOWN:
            return
        stats = self.agent_stats()
        ready = [dict(st, cpu=st["cpu"] if st["full_window"] else 0.0) for st in stats]  # Ignore launch spikes
        move = pick_move(ready)
        if not move:
            return
        src, dst = self.agents[move[0]], self.agents[move[1]]
        bot_id = max(b for b, a in self.bot_agent.items() if a is src)
        logger.info(
            f"[Coordinator] {src.name} overloaded ({stats[move[0]]['cpu']:.0f}% CPU over 30s); "
            f"moving bot #{bot_id} '{self.nicknames[bot_id]}' to {dst.name} ({stats[move[1]]['cpu']:.0f}% CPU)."
        )
        src.stop_bots([bot_id])
        del self.bot_agent[bot_id]
        self.bot_status.pop(bot_id, None)
        proxy = self.group_proxies[bot_id // self.args.bots_per_proxy]
        dst.start_bot(bot_id, self.args.room, proxy, self.nicknames[bot_id], random.uniform(1.0, 3.5))
        self.bot_agent[bot_id] = dst
        self.last_move = time.time()
        # Judge both machines afresh after the move.
        self.cpu_samples.pop(id(src), None)
        self.cpu_samples.pop(id(dst), None)

    def usable_proxies(self) -> List[Dict[str, str]]:
        """Webshare's list minus proxies benched as bad; all of them if every one is benched."""
        if getattr(self.args, "direct", False) or not getattr(self.args, "webshare_key", None):
            return []
        now = time.time()
        usable = [p for p in self.proxy_mgr.proxies if self.bad_until.get(proxy_key(p), 0) <= now]
        return usable or self.proxy_mgr.proxies

    def apply_proxies(self):
        new = reassign_proxies(self.group_proxies, self.usable_proxies())
        for g, (old, cur) in enumerate(zip(self.group_proxies, new)):
            if proxy_key(old) != proxy_key(cur):
                old_desc = f"{old['host']}:{old['port']}" if old else "direct"
                cur_desc = f"{cur['host']}:{cur['port']}" if cur else "direct"
                logger.info(f"[Coordinator] Group {g} IP rotated: {old_desc} -> {cur_desc}")
                for bot_id in self.group_bots(g):
                    if bot_id in self.bot_agent:
                        self.bot_agent[bot_id].update_proxy(bot_id, cur)
        self.group_proxies = new

    def check_proxies(self):
        if getattr(self.args, "direct", False) or not getattr(self.args, "webshare_key", None):
            return
        self.last_fetch = time.time()
        if self.proxy_mgr.fetch_proxies():
            self.apply_proxies()

    def _is_real_ip(self, host: str) -> bool:
        if not host or host == "direct":
            return False
        parts = host.split(".")
        return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)

    def probe_proxies(self, proxies: Optional[List[dict]] = None) -> List[dict]:
        """Probe proxies for Powerline WebSocket handshake and bench failing ones."""
        to_probe = self.proxy_mgr.proxies if proxies is None else proxies
        if not to_probe:
            return []
        import concurrent.futures
        bad = []

        def _check(p):
            ok, reason = probe_proxy_handshake(p)
            return p, ok, reason

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, max(1, len(to_probe)))) as ex:
            results = list(ex.map(_check, to_probe))

        for p, ok, reason in results:
            if not ok:
                bad.append(p)
                self.bad_until[proxy_key(p)] = time.time() + BAD_PROXY_COOLDOWN
                logger.warning(f"[Coordinator] Proxy {p['host']}:{p['port']} flagged by game server: {reason}")
        return bad

    def clean_flagged_proxies(self, max_rounds: int = 5) -> int:
        """Scan proxy pool and rotate flagged IPs via Webshare API v3 until clean."""
        if not self.proxy_mgr.proxies:
            self.proxy_mgr.fetch_proxies()
        if not self.proxy_mgr.proxies:
            return 0

        replaced_count = 0
        for r in range(1, max_rounds + 1):
            bad = self.probe_proxies()
            if not bad:
                logger.info(f"[Coordinator] All {len(self.proxy_mgr.proxies)} proxies verified clean (0 flagged).")
                break
            bad_ips = [p["host"] for p in bad if self._is_real_ip(p["host"])]
            if not bad_ips:
                logger.info("[Coordinator] No replaceable public IPs among flagged proxies.")
                break
            logger.info(
                f"[Coordinator] Round {r}/{max_rounds}: replacing {len(bad_ips)} flagged IPs via Webshare API v3..."
            )
            new_list = self.proxy_mgr.replace_and_refresh(bad_ips)
            replaced_count += len(bad_ips)
            if new_list:
                for p in new_list:
                    self.bad_until.pop(proxy_key(p), None)
                self.apply_proxies()
            else:
                break

        return replaced_count

    def _replace_worker_loop(self):
        """Worker thread that consumes flagged IPs, batches them, and respects rate limits."""
        while True:
            try:
                bad_ip = self._replace_queue.get()
                batch = [bad_ip]
                # Collect up to 4 more pending IPs to replace in a single batch
                while len(batch) < 5:
                    try:
                        extra = self._replace_queue.get_nowait()
                        if extra not in batch and self._is_real_ip(extra):
                            batch.append(extra)
                    except queue.Empty:
                        break

                # Enforce minimum cooldown between Webshare API requests to prevent HTTP 429
                elapsed = time.time() - self._last_replace_time
                if elapsed < self._replace_cooldown:
                    time.sleep(self._replace_cooldown - elapsed)

                logger.info(f"[Coordinator] Batch replacement requested for {len(batch)} IP(s): {', '.join(batch)}...")
                new_list = self.proxy_mgr.replace_and_refresh(batch)
                self._last_replace_time = time.time()
                if new_list:
                    logger.info(f"[Coordinator] Successfully rotated {len(batch)} IP(s). Applying updated proxy pool.")
                    for p in new_list:
                        self.bad_until.pop(proxy_key(p), None)
                    self.apply_proxies()
            except Exception as e:
                logger.warning(f"[Coordinator] Auto-replacement batch failed: {e}")
                if "429" in str(e):
                    logger.warning("[Coordinator] Rate limited (429) by Webshare API; backing off 60s...")
                    time.sleep(60.0)
            finally:
                with self._replace_lock:
                    for ip in batch:
                        self._replacing_ips.discard(ip)

    def auto_replace_proxy_async(self, bad_ip: str):
        """Queue a bad proxy IP for batched, rate-limited Webshare API v3 rotation."""
        if getattr(self.args, "direct", False) or not self.auto_replace or not getattr(self.args, "webshare_key", None):
            return
        if not self._is_real_ip(bad_ip):
            return
        with self._replace_lock:
            if bad_ip in self._replacing_ips:
                return
            self._replacing_ips.add(bad_ip)
        self._replace_queue.put(bad_ip)

    def handle_exits(self):
        """Restart bots whose worker exited, on their group's current proxy and with the same name."""
        for agent in list(self.agents):
            for event in agent.poll():
                bot_id = event["bot"]
                if event.get("type") == "screenshot":
                    self.screenshots[bot_id] = {"time": event["time"], "data": event["data"], "machine": agent.name}
                    continue
                if self.bot_agent.get(bot_id) is not agent:
                    continue  # Already moved or re-placed
                del self.bot_agent[bot_id]
                self.bot_status.pop(bot_id, None)
                if time.time() - self.started_at.get(bot_id, 0.0) >= HEALTHY_RUN:
                    self.failures[bot_id] = 0
                self.failures[bot_id] = self.failures.get(bot_id, 0) + 1
                if event["bad_proxy"]:
                    current = self.group_proxies[bot_id // self.args.bots_per_proxy]
                    bad = event.get("proxy") or current
                    moved = proxy_key(bad) != proxy_key(current)
                    bad_desc = f"{bad['host']}:{bad['port']}" if bad else "direct"
                    if getattr(self.args, "direct", False) or not bad:
                        logger.warning(f"[Coordinator] Bot #{bot_id} exited on direct connection. Restarting...")
                    else:
                        logger.warning(
                            f"[Coordinator] Bot #{bot_id} reports proxy {bad_desc} can't reach the game; "
                            f"benching it for {BAD_PROXY_COOLDOWN // 60} min"
                            + (" (its group has already moved)." if moved else " and moving its group.")
                        )
                        self.bad_until[proxy_key(bad)] = time.time() + BAD_PROXY_COOLDOWN
                        if self.auto_replace and bad.get("host") and self._is_real_ip(bad["host"]):
                            self.auto_replace_proxy_async(bad["host"])
                        if time.time() - self.last_fetch >= MIN_REFETCH_INTERVAL:
                            self.check_proxies()
                        else:
                            self.apply_proxies()
                else:
                    logger.warning(
                        f"[Coordinator] Bot #{bot_id} exited unexpectedly on {agent.name} (code {event['code']}). Restarting..."
                    )
                delay = restart_delay(self.failures[bot_id])
                if self.failures[bot_id] > 1:
                    logger.info(f"[Coordinator] Bot #{bot_id} failed {self.failures[bot_id]} times in a row; restarting in {delay:.0f}s.")
                self.place_bot(bot_id, delay)

    # --- Dashboard ----------------------------------------------------------
    def agent_by_name(self, name: str):
        return next((a for a in self.agents if a.name == name), None)

    def unplace(self, bot_ids: List[int]):
        """Stop bots wherever they run (they are not restarted by this)."""
        by_agent: Dict[int, Tuple[object, List[int]]] = {}
        for bot_id in bot_ids:
            agent = self.bot_agent.pop(bot_id, None)
            self.pending.pop(bot_id, None)
            self.bot_status.pop(bot_id, None)
            if agent is not None:
                by_agent.setdefault(id(agent), (agent, []))[1].append(bot_id)
        for agent, ids in by_agent.values():
            agent.stop_bots(ids)

    def set_groups(self, groups: int):
        if groups < 1 or groups == self.groups:
            return
        bpp = self.args.bots_per_proxy
        if groups > self.groups:
            while len(self.nicknames) < groups * bpp:
                self.nicknames.append(generate_random_nickname())
            new_groups = range(self.groups, groups)
            self.group_proxies = reassign_proxies(self.group_proxies + [None] * len(new_groups), self.usable_proxies())
            self.groups = groups
            for g in new_groups:
                proxy = self.group_proxies[g]
                p_desc = f"{proxy['host']}:{proxy['port']}" if proxy else "direct"
                logger.info(f"[Coordinator] Added group {g} on {p_desc}.")
                for k, bot_id in enumerate(self.group_bots(g)):
                    self.place_bot(bot_id, (k * self.args.stagger_interval) + random.uniform(1.0, 3.5))
        else:
            removed = list(range(groups * bpp, self.groups * bpp))
            logger.info(f"[Coordinator] Removing groups {groups}..{self.groups - 1} (bots {removed[0]}..{removed[-1]}).")
            self.unplace(removed)
            self.stopped.difference_update(removed)
            self.groups = groups
            self.group_proxies = self.group_proxies[:groups]

    def valid_bots(self, value) -> List[int]:
        """Bot ids from a command: one int or a list of ints, keeping only existing bots."""
        ids = value if isinstance(value, list) else [value]
        return sorted({b for b in ids[:500] if isinstance(b, int) and not isinstance(b, bool) and 0 <= b < self.total})

    def stop_bot_ids(self, bot_ids: List[int]):
        self.unplace(bot_ids)
        self.stopped.update(bot_ids)

    def start_bot_ids(self, bot_ids: List[int]):
        self.unplace(bot_ids)
        for i, bot_id in enumerate(bot_ids):
            self.stopped.discard(bot_id)
            self.failures[bot_id] = 0
            self.place_bot(bot_id, i * self.args.stagger_interval + random.uniform(1.0, 3.5))

    def apply_settings(self, cmd: dict):
        room = cmd.get("room", self.args.room)
        bpp = cmd.get("bots_per_proxy", self.args.bots_per_proxy)
        check = cmd.get("proxy_check_interval", self.args.proxy_check_interval)
        stagger = cmd.get("stagger_interval", self.args.stagger_interval)
        if not (isinstance(room, str) and ROOM_PATTERN.match(room)):
            raise ValueError(f"room must be 1-16 letters or digits, got {room!r}")
        if not (isinstance(bpp, int) and not isinstance(bpp, bool) and 1 <= bpp <= 10):
            raise ValueError(f"bots per group must be 1-10, got {bpp!r}")
        if not (isinstance(check, (int, float)) and 15 <= check <= 3600):
            raise ValueError(f"proxy check interval must be 15-3600s, got {check!r}")
        if not (isinstance(stagger, (int, float)) and 0 <= stagger <= 60):
            raise ValueError(f"stagger must be 0-60s, got {stagger!r}")
        self.args.proxy_check_interval = float(check)
        self.next_proxy_check = min(self.next_proxy_check, time.time() + self.args.proxy_check_interval)
        self.args.stagger_interval = float(stagger)
        if room != self.args.room or bpp != self.args.bots_per_proxy:
            running = sorted(set(range(self.total)) - self.stopped)
            logger.info(f"[Coordinator] Dashboard: room #{room}, {bpp} bots per group; restarting the bots.")
            self.unplace(list(range(self.total)))
            old_total = self.total
            self.args.room = room
            self.args.bots_per_proxy = bpp
            while len(self.nicknames) < self.total:
                self.nicknames.append(generate_random_nickname())
            # Bots beyond the old total are new; they start unless everything was stopped.
            self.stopped = {b for b in self.stopped if b < self.total}
            if not running and old_total:
                self.stopped = set(range(self.total))
            self.start_bot_ids([b for b in range(self.total) if b not in self.stopped])
        logger.info(
            f"[Coordinator] Dashboard: settings saved (room #{room}, {bpp} bots per group, "
            f"proxy check every {check:.0f}s, stagger {stagger:.1f}s)."
        )

    def run_command(self, cmd: dict):
        """Apply one dashboard command (runs in the main loop, never in the web server thread)."""
        action = cmd.get("action")
        bot_id = cmd.get("bot")
        bots = self.valid_bots(cmd.get("bots", bot_id))
        if action in ("stop_bot", "stop_bots") and bots:
            logger.info(f"[Coordinator] Dashboard: stopping bot{'s' if len(bots) > 1 else ''} {', '.join('#%d' % b for b in bots)}.")
            self.stop_bot_ids(bots)
        elif action in ("start_bot", "restart_bot", "start_bots", "restart_bots") and bots:
            verb = "restarting" if action.startswith("restart") else "starting"
            logger.info(f"[Coordinator] Dashboard: {verb} bot{'s' if len(bots) > 1 else ''} {', '.join('#%d' % b for b in bots)}.")
            self.start_bot_ids(bots)
        elif action == "screenshot" and bots:
            agent = self.bot_agent.get(bots[0])
            if agent is None:
                raise ValueError(f"bot #{bots[0]} isn't running")
            agent.request_screenshot(bots[0])
        elif action in ("restart_machine", "stop_machine_bots") and self.agent_by_name(cmd.get("machine", "")):
            agent = self.agent_by_name(cmd["machine"])
            its_bots = sorted(b for b, a in self.bot_agent.items() if a is agent)
            if action == "stop_machine_bots":
                logger.info(f"[Coordinator] Dashboard: stopping the bots on {agent.name} {its_bots}.")
                self.stop_bot_ids(its_bots)
            elif isinstance(agent, RemoteAgent):
                logger.info(f"[Coordinator] Dashboard: restarting the agent on {agent.name}; its bots move meanwhile.")
                agent.restart()  # It disconnects; its bots are re-placed and it rejoins after restarting
            else:
                logger.info(f"[Coordinator] Dashboard: restarting the bots on {agent.name} (the coordinator's own machine).")
                self.start_bot_ids(its_bots)
        elif action == "set_settings":
            self.apply_settings(cmd)
        elif action == "stop_all":
            logger.info("[Coordinator] Dashboard: stopping all bots.")
            self.unplace(list(range(self.total)))
            self.stopped.update(range(self.total))
        elif action == "start_all":
            logger.info("[Coordinator] Dashboard: starting all stopped bots.")
            for i, b in enumerate(sorted(self.stopped)):
                if b < self.total and b not in self.bot_agent:
                    self.place_bot(b, i * self.args.stagger_interval + random.uniform(1.0, 3.5))
            self.stopped.clear()
        elif action in ("pause_machine", "resume_machine") and self.agent_by_name(cmd.get("machine", "")):
            agent = self.agent_by_name(cmd["machine"])
            if action == "pause_machine":
                self.paused.add(agent.name)
                bots = sorted(b for b, a in self.bot_agent.items() if a is agent)
                logger.info(f"[Coordinator] Dashboard: pausing {agent.name}; moving its bots {bots} elsewhere.")
                self.unplace(bots)
                for i, b in enumerate(bots):
                    self.place_bot(b, i * self.args.stagger_interval + random.uniform(1.0, 3.5))
            else:
                self.paused.discard(agent.name)
                logger.info(f"[Coordinator] Dashboard: resuming {agent.name}.")
        elif action == "set_max_bots" and self.agent_by_name(cmd.get("machine", "")):
            agent = self.agent_by_name(cmd["machine"])
            limit = cmd.get("max_bots")
            limit = int(limit) if isinstance(limit, (int, float)) and limit >= 0 else None
            if isinstance(agent, RemoteAgent):
                agent.send({"type": "set_max_bots", "max_bots": limit})
            else:
                agent.max_bots = limit
            self.max_bots[id(agent)] = limit
            self.saved_max[agent.name] = limit
            logger.info(
                f"[Coordinator] Dashboard: {agent.name} limited to {limit} bots."
                if limit is not None else f"[Coordinator] Dashboard: {agent.name} no longer has a bot limit."
            )
            extra = sorted(b for b, a in self.bot_agent.items() if a is agent)[limit:] if limit is not None else []
            self.unplace(extra)
            for i, b in enumerate(extra):
                self.place_bot(b, i * self.args.stagger_interval + random.uniform(1.0, 3.5))
        elif action == "set_groups" and isinstance(cmd.get("groups"), int) and 1 <= cmd["groups"] <= 50:
            self.set_groups(cmd["groups"])
        elif action == "refresh_proxies":
            logger.info("[Coordinator] Dashboard: refreshing the proxy list.")
            self.check_proxies()
        else:
            logger.warning(f"[Coordinator] Dashboard: ignored invalid command {cmd!r}.")

    def run_commands(self):
        while True:
            try:
                cmd = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                self.run_command(cmd)
            except Exception as e:
                logger.error(f"[Coordinator] Dashboard command {cmd!r} failed: {e}")
            self.save_state()

    def build_snapshot(self) -> dict:
        """Everything the dashboard shows, as plain JSON-able data."""
        now = time.time()
        bpp = self.args.bots_per_proxy
        stats = self.agent_stats()
        machines = []
        for agent, st in zip(self.agents, stats):
            health = self.last_health.get(id(agent), {})
            machines.append({
                "name": agent.name,
                "local": not isinstance(agent, RemoteAgent),
                "platform": health.get("platform"),
                "cpu": round(st["cpu"], 1),
                "cpu_count": st["cpu_count"],
                "mem_available_mb": health.get("mem_available_mb"),
                "mem_total_mb": health.get("mem_total_mb"),
                "bots": sorted(b for b, a in self.bot_agent.items() if a is agent),
                "max_bots": self.max_bots.get(id(agent)),
                "paused": agent.name in self.paused,
            })
        bots = []
        for bot_id in range(self.total):
            agent = self.bot_agent.get(bot_id)
            status = self.bot_status.get(bot_id, {})
            if bot_id in self.stopped:
                state = "stopped"
            elif bot_id in self.pending:
                state = "waiting"
            elif agent is None:
                state = "starting"
            else:
                state = status.get("state", "starting")
            bots.append({
                "id": bot_id,
                "name": self.nicknames[bot_id],
                "group": bot_id // bpp,
                "machine": agent.name if agent is not None else None,
                "state": state,
                "since": status.get("since"),
                "rounds": status.get("rounds", 0),
                "last_respawn": status.get("last_respawn"),
                "proxy": status.get("proxy"),
                "failures": self.failures.get(bot_id, 0),
                "started_at": self.started_at.get(bot_id) if agent is not None else None,
            })
        group_of = {proxy_key(p): [] for p in self.proxy_mgr.proxies}
        for g, p in enumerate(self.group_proxies):
            group_of.setdefault(proxy_key(p), []).append(g)
        return {
            "time": now,
            "room": self.args.room,
            "code_version": code_version(),
            "groups": self.groups,
            "bots_per_proxy": bpp,
            "machines": machines,
            "bots": bots,
            "group_proxies": [
                {
                    "group": g,
                    "host": p["host"] if p else "direct",
                    "port": p["port"] if p else "0",
                    "country": p.get("country", "local") if p else "local",
                }
                for g, p in enumerate(self.group_proxies)
            ],
            "proxies": [
                {
                    "host": p["host"],
                    "port": p["port"],
                    "country": p.get("country"),
                    "groups": group_of.get(proxy_key(p), []),
                    "benched_until": self.bad_until.get(proxy_key(p)) if self.bad_until.get(proxy_key(p), 0) > now else None,
                }
                for p in self.proxy_mgr.proxies
            ],
            "proxy_list_created_at": self.proxy_mgr.list_created_at,
            "rotation_frequency": self.proxy_mgr.refresh_frequency,
            "next_rotation": self.proxy_mgr.next_rotation(now),
            "last_fetch": self.last_fetch,
            "events": list(self.events),
            "alerts": self.alerts(now),
            "settings": {
                "room": self.args.room,
                "groups": self.groups,
                "bots_per_proxy": bpp,
                "proxy_check_interval": self.args.proxy_check_interval,
                "stagger_interval": self.args.stagger_interval,
            },
            "screenshot_times": {str(b): shot["time"] for b, shot in self.screenshots.items()},
        }

    def alerts(self, now: float) -> List[dict]:
        """Problems worth a banner on the dashboard."""
        alerts = []
        if not self.agents:
            alerts.append({"key": "no-machines", "level": "error", "text": "No machine is connected, so no bot can run"})
        stats = self.agent_stats()
        for agent, st in zip(self.agents, stats):
            if st["full_window"] and st["cpu"] >= OVERLOAD_CPU:
                alerts.append({"key": f"hot:{agent.name}", "level": "warning",
                               "text": f"{agent.name} is overloaded ({st['cpu']:.0f}% CPU over 30s)"})
        waiting = sorted(b for b in self.pending if b not in self.stopped)
        if waiting:
            alerts.append({"key": "waiting", "level": "warning",
                           "text": f"{len(waiting)} bot(s) waiting for a machine with room"})
        failing = [b for b in range(self.total) if self.failures.get(b, 0) >= FAILING_STREAK and b not in self.stopped]
        if failing:
            names = ", ".join(self.nicknames[b] for b in failing[:5]) + ("…" if len(failing) > 5 else "")
            alerts.append({"key": "failing", "level": "warning",
                           "text": f"{len(failing)} bot(s) keep failing: {names}"})
        if self.proxy_mgr.proxies and all(self.bad_until.get(proxy_key(p), 0) > now for p in self.proxy_mgr.proxies):
            alerts.append({"key": "all-benched", "level": "error",
                           "text": "Every proxy is benched: none could reach the game"})
        last_ok = self.proxy_mgr.last_success or 0
        if self.proxy_mgr.last_error and now - last_ok > 3 * self.args.proxy_check_interval:
            alerts.append({"key": "webshare", "level": "error",
                           "text": f"Webshare isn't answering ({self.proxy_mgr.last_error[:80]})"})
        current = {a.name for a in self.agents}
        for past in self.recent_alerts:
            name = past["key"].split(":", 1)[1]
            if now - past["time"] < ALERT_MEMORY and name not in current:
                alerts.append({"key": past["key"], "level": past["level"],
                               "text": past["text"] + f" {int((now - past['time']) // 60)} min ago"})
        return alerts

    def logs_view(self, machine: Optional[str] = None, bot: Optional[int] = None, limit: int = 200) -> List[dict]:
        """Recent log lines for the dashboard, newest last, optionally for one machine and/or bot."""
        with self.data_lock:
            sources = [self.machine_logs[machine]] if machine in self.machine_logs else (
                [] if machine else list(self.machine_logs.values()))
            lines = [line for log in sources for line in log if bot is None or line.get("bot") == bot]
        lines.sort(key=lambda line: line["time"])
        return lines[-max(1, min(limit, 1000)):]

    def history_view(self) -> dict:
        with self.data_lock:
            return {
                "machines": {name: list(points) for name, points in self.history.items()},
                "bots": list(self.bot_history),
            }

    def screenshot_view(self, bot_id: int) -> Optional[dict]:
        return self.screenshots.get(bot_id)

    def shutdown(self, *_):
        logger.info("[Coordinator] Shutting down all bots...")
        for agent in self.agents:
            if isinstance(agent, RemoteAgent):
                agent.stop_all()  # The remote agent stops its bots itself
            else:
                agent.stop_all(wait=True)
        logger.info("[Coordinator] All bots stopped cleanly.")
        sys.exit(0)

    def run(self, wait_agents: float = 0.0, start_stopped: bool = False):
        args = self.args
        if getattr(args, "direct", False) or not getattr(args, "webshare_key", None):
            logger.info("[Coordinator] Running in direct connection mode (no proxies).")
            self.group_proxies = [None] * self.groups
        else:
            while not self.proxy_mgr.fetch_proxies():
                logger.error("[Coordinator] No proxies available from Webshare, retrying in 30s...")
                time.sleep(30)
            if len(self.proxy_mgr.proxies) < self.groups:
                logger.warning(
                    f"[Coordinator] Only {len(self.proxy_mgr.proxies)} proxies for {self.groups} groups; some groups share an IP."
                )
            if getattr(self.args, "clean_proxies", False):
                self.clean_flagged_proxies()
            self.group_proxies = reassign_proxies([None] * self.groups, self.usable_proxies())
            self.proxy_mgr.fetch_refresh_frequency()

        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self.shutdown)  # Windows: Ctrl+Break, how background consoles get stopped

        if wait_agents > 0:
            logger.info(f"[Coordinator] Giving agents {wait_agents:.0f}s to connect before placing bots...")
            time.sleep(wait_agents)
        self.add_joined_agents()
        self.refresh_health()

        for g in range(self.groups):
            proxy = self.group_proxies[g]
            p_desc = f"{proxy['host']}:{proxy['port']}" if proxy else "direct (local network)"
            logger.info(f"[Coordinator] Group {g} on {p_desc} ({args.bots_per_proxy} bots)")
            if start_stopped:
                continue
            for k, bot_id in enumerate(b for b in self.group_bots(g) if b not in self.stopped):
                self.place_bot(bot_id, (k * args.stagger_interval) + random.uniform(1.0, 3.5))
        if start_stopped:
            self.stopped.update(range(self.total))
            logger.info("[Coordinator] All bots start stopped; start them from the dashboard.")
        self.save_state()

        self.last_fetch = time.time()
        self.next_proxy_check = time.time() + args.proxy_check_interval
        while True:
            self.add_joined_agents()
            self.drop_dead_agents()
            self.refresh_health()
            if time.time() >= self.next_proxy_check:
                self.check_proxies()
                self.next_proxy_check = time.time() + self.args.proxy_check_interval
            self.handle_exits()
            self.run_commands()
            self.place_pending()
            self.rebalance()
            self.snapshot = self.build_snapshot()
            time.sleep(1)


# ============================================================================
# CLI Entrypoint
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="Powerline.io Autonomous Bot (Camoufox C++ + EzSolver)")
    parser.add_argument("--room", default=DEFAULT_ROOM, help="Target game room code (default: 2cjg)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode (default: False)")
    parser.add_argument(
        "--webshare-key",
        default=DEFAULT_WEBSHARE_API_KEY,
        help="Webshare API key (default: $WEBSHARE_API_KEY)",
    )
    parser.add_argument(
        "--groups",
        type=int,
        default=DEFAULT_GROUPS,
        help="Number of bot groups, each on its own proxy IP (default: 3)",
    )
    parser.add_argument(
        "--bots-per-proxy",
        type=int,
        default=DEFAULT_BOTS_PER_PROXY,
        help="Number of bots in each group / sharing one proxy (default: 3)",
    )
    parser.add_argument(
        "--stagger-interval",
        type=float,
        default=DEFAULT_STAGGER_INTERVAL,
        help="Average seconds between each bot in a group connecting (default: 3.0)",
    )
    parser.add_argument(
        "--proxy-check-interval",
        type=float,
        default=DEFAULT_PROXY_CHECK_INTERVAL,
        help="Seconds between Webshare checks for rotated proxy IPs (default: 60)",
    )
    parser.add_argument(
        "--debug-dir",
        help="Save screenshots of key moments (load, spawn, death, every 30s in game) to this folder",
    )
    parser.add_argument(
        "--listen",
        help=f"Accept agents from other machines on HOST[:PORT], e.g. 0.0.0.0 (default port {DEFAULT_PORT}; "
             f"needs ${AGENT_KEY_ENV})",
    )
    parser.add_argument(
        "--wait-agents",
        type=float,
        default=DEFAULT_WAIT_AGENTS,
        help=f"With --listen: seconds to let agents connect before placing bots (default: {DEFAULT_WAIT_AGENTS:.0f})",
    )
    parser.add_argument(
        "--dashboard",
        help=f"Serve the web dashboard on HOST[:PORT], e.g. 0.0.0.0 (default port {DEFAULT_DASHBOARD_PORT}; "
             f"needs ${DASHBOARD_PASSWORD_ENV})",
    )
    parser.add_argument("--no-local-agent", action="store_true", help="Run no bots on this machine (needs --listen)")
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help="Where bot names and dashboard settings are kept across restarts (default: %(default)s)",
    )
    parser.add_argument(
        "--start-stopped",
        action="store_true",
        help="Start with every bot stopped, to be started from the dashboard (useful when started at boot)",
    )
    parser.add_argument("--local-max-bots", type=int, help="Never run more than this many bots on this machine")
    parser.add_argument(
        "--clean-proxies",
        action="store_true",
        help="Probe all Webshare proxies on startup and auto-rotate any flagged IPs via Webshare API v3.",
    )
    parser.add_argument(
        "--no-auto-replace",
        dest="auto_replace",
        action="store_false",
        default=True,
        help="Disable automatic Webshare API v3 rotation when a proxy is reported bad.",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Run bots directly without proxies (uses clean local network IP)",
    )
    args = parser.parse_args()

    if not args.direct and not args.webshare_key:
        parser.error("no Webshare API key: set WEBSHARE_API_KEY or pass --webshare-key (or use --direct)")
    if args.groups < 1 or args.bots_per_proxy < 1 or args.proxy_check_interval <= 0:
        parser.error("--groups and --bots-per-proxy must be >= 1, --proxy-check-interval > 0")
    if args.no_local_agent and not args.listen:
        parser.error("--no-local-agent needs --listen, or no machine would run bots")
    if args.dashboard and not os.environ.get(DASHBOARD_PASSWORD_ENV):
        parser.error(f"--dashboard needs a password in ${DASHBOARD_PASSWORD_ENV}")

    # Ensure DISPLAY is set for non-headless runs
    if sys.platform.startswith("linux") and not args.headless and not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"

    print("=" * 65)
    print(" Powerline.io Autonomous Bot (Camoufox C++ Always-On Groups)")
    print(f" Room: #{args.room} | Groups: {args.groups} x {args.bots_per_proxy} bots (1 IP per group)")
    print(f" Proxy check every {args.proxy_check_interval:.0f}s | Stagger: {args.stagger_interval}s | Headless: {args.headless}")
    print("=" * 65)

    agents = [] if args.no_local_agent else [
        Agent(headless=args.headless, debug_dir=args.debug_dir, max_bots=args.local_max_bots)
    ]
    coordinator = Coordinator(args, agents, state_file=args.state_file)
    if args.listen:
        coordinator.listen(parse_address(args.listen), agent_key())
    if args.dashboard:
        host, _, port = args.dashboard.rpartition(":")
        address = (host, int(port)) if host else (args.dashboard, DEFAULT_DASHBOARD_PORT)
        start_dashboard(coordinator, address, os.environ[DASHBOARD_PASSWORD_ENV])
    coordinator.run(wait_agents=args.wait_agents if args.listen else 0.0, start_stopped=args.start_stopped)


if __name__ == "__main__":
    main()
