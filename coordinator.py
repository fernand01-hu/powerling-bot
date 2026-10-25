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
import json
import logging
import multiprocessing.connection
import os
import random
import signal
import sys
import threading
import time
import urllib.request
from collections import deque
from typing import Dict, List, Optional, Tuple

from agent import AGENT_KEY_ENV, DEFAULT_PORT, HEALTH_INTERVAL, Agent, agent_key, code_version, parse_address
from bot import DEFAULT_ROOM, generate_random_nickname

logger = logging.getLogger("PowerlineBot.Coordinator")

DEFAULT_WEBSHARE_API_KEY = os.environ.get("WEBSHARE_API_KEY", "")
DEFAULT_BOTS_PER_PROXY = 3
DEFAULT_GROUPS = 3
DEFAULT_STAGGER_INTERVAL = 3.0       # Seconds between each bot in a group connecting
DEFAULT_PROXY_CHECK_INTERVAL = 60.0  # Seconds between Webshare checks for rotated IPs
BAD_PROXY_COOLDOWN = 30 * 60         # Seconds a bad proxy is left out of the rotation


def proxy_key(proxy: Dict[str, str]) -> Tuple[str, str]:
    return proxy["host"], proxy["port"]


def reassign_proxies(current: List[Optional[Dict[str, str]]], available: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Keep each group on its proxy while it's still available and not carrying more than its share
    of groups; every other group moves to the least-used proxy, so groups stay spread out."""
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

    def fetch_proxies(self) -> List[Dict[str, str]]:
        url: Optional[str] = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100"
        headers = {"Authorization": f"Token {self.api_key}"}
        valid = []
        try:
            # The list is paginated; follow "next" until every page is read.
            while url:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for p in data.get("results", []):
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
            return self.proxies
        if valid:
            self.proxies = valid
            logger.info(f"[Webshare] Fetched {len(valid)} active proxies from API.")
        return self.proxies




OVERLOAD_CPU = 85.0        # Average CPU % at which a machine counts as overloaded
HEADROOM_CPU = 65.0        # A machine must be below this to take a bot moved off an overloaded one
CPU_WINDOW = 6             # Health samples averaged (one every HEALTH_INTERVAL seconds -> 30s)
REBALANCE_COOLDOWN = 60.0  # Seconds between moving bots off overloaded machines
DEFAULT_WAIT_AGENTS = 15.0  # Seconds to let remote agents connect before placing bots
AGENT_TIMEOUT = 30.0       # An agent silent this long (it reports every HEALTH_INTERVAL) counts as gone
MIN_REFETCH_INTERVAL = 15.0  # Seconds between extra Webshare refreshes triggered by proxy failures
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
            self.conn.send(msg)
        except (OSError, EOFError, ValueError):
            self.alive = False

    def receive(self):
        try:
            while self.alive and self.conn.poll(0):
                msg = self.conn.recv()
                self.last_seen = time.time()
                if msg["type"] == "health":
                    self.last_health = msg
                elif msg["type"] == "exited":
                    self.events.append(msg)
        except (OSError, EOFError):
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
    def __init__(self, args: argparse.Namespace, agents: List):
        self.args = args
        self.agents = list(agents)  # Agent (this machine) and RemoteAgent (other machines)
        self.proxy_mgr = WebshareManager(api_key=args.webshare_key)
        self.group_proxies: List[Dict[str, str]] = []
        self.total = args.groups * args.bots_per_proxy
        # One name per bot for the whole run, kept across deaths, worker restarts and moves.
        self.nicknames = [generate_random_nickname() for _ in range(self.total)]
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

    # --- Agents -------------------------------------------------------------
    def listen(self, address: Tuple[str, int], key: bytes):
        """Accept agents from other machines in the background."""
        listener = multiprocessing.connection.Listener(address, authkey=key)
        logger.info(f"[Coordinator] Waiting for agents on {address[0]}:{address[1]} (code version {code_version()})")

        def accept_loop():
            while True:
                try:
                    conn = listener.accept()
                except (multiprocessing.AuthenticationError, OSError) as e:
                    logger.warning(f"[Coordinator] Refused a connection: {e}")
                    continue
                try:
                    hello = conn.recv() if conn.poll(10) else None
                except (OSError, EOFError):
                    hello = None
                if not hello or hello.get("type") != "hello":
                    conn.close()
                    continue
                if hello["version"] != code_version():
                    reason = f"code version {hello['version']} differs from the coordinator's {code_version()}; update its files"
                    logger.warning(f"[Coordinator] Agent {hello['agent']} rejected: {reason}")
                    try:
                        conn.send({"type": "rejected", "reason": reason})
                    except OSError:
                        pass
                    conn.close()
                    continue
                with self.joined_lock:
                    self.joined.append(RemoteAgent(conn, hello))

        threading.Thread(target=accept_loop, name="agent-listener", daemon=True).start()

    def add_joined_agents(self):
        with self.joined_lock:
            joined, self.joined = self.joined, []
        for agent in joined:
            self.agents.append(agent)
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
            orphans = sorted(b for b, a in self.bot_agent.items() if a is agent)
            logger.warning(f"[Coordinator] Agent {agent.name} disconnected; re-placing its bots {orphans}.")
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
                "max_bots": self.max_bots.get(key),
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
        proxy = self.group_proxies[bot_id // self.args.bots_per_proxy]
        dst.start_bot(bot_id, self.args.room, proxy, self.nicknames[bot_id], random.uniform(1.0, 3.5))
        self.bot_agent[bot_id] = dst
        self.last_move = time.time()
        # Judge both machines afresh after the move.
        self.cpu_samples.pop(id(src), None)
        self.cpu_samples.pop(id(dst), None)

    def usable_proxies(self) -> List[Dict[str, str]]:
        """Webshare's list minus proxies benched as bad; all of them if every one is benched."""
        now = time.time()
        usable = [p for p in self.proxy_mgr.proxies if self.bad_until.get(proxy_key(p), 0) <= now]
        return usable or self.proxy_mgr.proxies

    def apply_proxies(self):
        new = reassign_proxies(self.group_proxies, self.usable_proxies())
        for g, (old, cur) in enumerate(zip(self.group_proxies, new)):
            if proxy_key(old) != proxy_key(cur):
                logger.info(
                    f"[Coordinator] Group {g} IP rotated: {old['host']}:{old['port']} -> {cur['host']}:{cur['port']}"
                )
                for bot_id in self.group_bots(g):
                    if bot_id in self.bot_agent:
                        self.bot_agent[bot_id].update_proxy(bot_id, cur)
        self.group_proxies = new

    def check_proxies(self):
        self.last_fetch = time.time()
        if self.proxy_mgr.fetch_proxies():
            self.apply_proxies()

    def handle_exits(self):
        """Restart bots whose worker exited, on their group's current proxy and with the same name."""
        for agent in list(self.agents):
            for event in agent.poll():
                bot_id = event["bot"]
                if self.bot_agent.get(bot_id) is not agent:
                    continue  # Already moved or re-placed
                del self.bot_agent[bot_id]
                if time.time() - self.started_at.get(bot_id, 0.0) >= HEALTHY_RUN:
                    self.failures[bot_id] = 0
                self.failures[bot_id] = self.failures.get(bot_id, 0) + 1
                if event["bad_proxy"]:
                    current = self.group_proxies[bot_id // self.args.bots_per_proxy]
                    bad = event.get("proxy") or current
                    moved = proxy_key(bad) != proxy_key(current)
                    logger.warning(
                        f"[Coordinator] Bot #{bot_id} reports proxy {bad['host']}:{bad['port']} can't reach the game; "
                        f"benching it for {BAD_PROXY_COOLDOWN // 60} min"
                        + (" (its group has already moved)." if moved else " and moving its group.")
                    )
                    self.bad_until[proxy_key(bad)] = time.time() + BAD_PROXY_COOLDOWN
                    if time.time() - self.last_fetch >= MIN_REFETCH_INTERVAL:
                        # Failing proxies usually mean Webshare just rotated: pick up the new list now.
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

    def shutdown(self, *_):
        logger.info("[Coordinator] Shutting down all bots...")
        for agent in self.agents:
            agent.stop_all()
        logger.info("[Coordinator] All bots stopped cleanly.")
        sys.exit(0)

    def run(self, wait_agents: float = 0.0):
        args = self.args
        while not self.proxy_mgr.fetch_proxies():
            logger.error("[Coordinator] No proxies available from Webshare, retrying in 30s...")
            time.sleep(30)
        if len(self.proxy_mgr.proxies) < args.groups:
            logger.warning(
                f"[Coordinator] Only {len(self.proxy_mgr.proxies)} proxies for {args.groups} groups; some groups share an IP."
            )
        self.group_proxies = reassign_proxies([None] * args.groups, self.proxy_mgr.proxies)

        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self.shutdown)  # Windows: Ctrl+Break, how background consoles get stopped

        if wait_agents > 0:
            logger.info(f"[Coordinator] Giving agents {wait_agents:.0f}s to connect before placing bots...")
            time.sleep(wait_agents)
        self.add_joined_agents()
        self.refresh_health()

        for g in range(args.groups):
            proxy = self.group_proxies[g]
            logger.info(f"[Coordinator] Group {g} on {proxy['host']}:{proxy['port']} ({args.bots_per_proxy} bots)")
            for k, bot_id in enumerate(self.group_bots(g)):
                self.place_bot(bot_id, (k * args.stagger_interval) + random.uniform(1.0, 3.5))

        self.last_fetch = time.time()
        next_proxy_check = time.time() + args.proxy_check_interval
        while True:
            self.add_joined_agents()
            self.drop_dead_agents()
            self.refresh_health()
            if time.time() >= next_proxy_check:
                self.check_proxies()
                next_proxy_check = time.time() + args.proxy_check_interval
            self.handle_exits()
            self.place_pending()
            self.rebalance()
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
    parser.add_argument("--no-local-agent", action="store_true", help="Run no bots on this machine (needs --listen)")
    parser.add_argument("--local-max-bots", type=int, help="Never run more than this many bots on this machine")
    args = parser.parse_args()

    if not args.webshare_key:
        parser.error("no Webshare API key: set WEBSHARE_API_KEY or pass --webshare-key")
    if args.groups < 1 or args.bots_per_proxy < 1 or args.proxy_check_interval <= 0:
        parser.error("--groups and --bots-per-proxy must be >= 1, --proxy-check-interval > 0")
    if args.no_local_agent and not args.listen:
        parser.error("--no-local-agent needs --listen, or no machine would run bots")

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
    coordinator = Coordinator(args, agents)
    if args.listen:
        coordinator.listen(parse_address(args.listen), agent_key())
    coordinator.run(wait_agents=args.wait_agents if args.listen else 0.0)


if __name__ == "__main__":
    main()
