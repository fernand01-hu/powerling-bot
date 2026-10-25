#!/usr/bin/env python3
"""
Coordinator: decides what the bots do and which machine runs them.

- Splits the bots into groups that share one Webshare proxy IP, and keeps groups spread over
  the proxies as Webshare rotates them (reloading a group's bots on its new IP).
- Benches proxies that can't reach the game and moves their group.
- Gives every bot one nickname for the whole run.
- Places each bot on an agent (agent.py) and restarts bots whose worker exits.

Run it with `python3 coordinator.py` (`python3 bot.py` does the same).
"""

import argparse
import json
import logging
import os
import random
import signal
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from agent import Agent
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




def pick_agent(bot_counts: List[int], cpu_counts: List[int]) -> int:
    """Index of the agent that should take the next bot: the one running the fewest bots per CPU core."""
    return min(range(len(bot_counts)), key=lambda a: (bot_counts[a] / max(1, cpu_counts[a]), a))


# ============================================================================
# Coordinator
# ============================================================================
class Coordinator:
    def __init__(self, args: argparse.Namespace, agents: List[Agent]):
        self.args = args
        self.agents = agents
        self.proxy_mgr = WebshareManager(api_key=args.webshare_key)
        self.group_proxies: List[Dict[str, str]] = []
        self.total = args.groups * args.bots_per_proxy
        # One name per bot for the whole run, kept across deaths and worker restarts.
        self.nicknames = [generate_random_nickname() for _ in range(self.total)]
        self.bot_agent: Dict[int, int] = {}  # Bot id -> index of the agent running it
        self.bad_until: Dict[Tuple[str, str], float] = {}

    def group_bots(self, g: int) -> range:
        return range(g * self.args.bots_per_proxy, (g + 1) * self.args.bots_per_proxy)

    def place_bot(self, bot_id: int, start_delay: float):
        """Start a bot on the agent with the most room for it."""
        counts = [sum(1 for a in self.bot_agent.values() if a == idx) for idx in range(len(self.agents))]
        cpus = [agent.health()["cpu_count"] for agent in self.agents]
        idx = pick_agent(counts, cpus)
        proxy = self.group_proxies[bot_id // self.args.bots_per_proxy]
        self.agents[idx].start_bot(bot_id, self.args.room, proxy, self.nicknames[bot_id], start_delay)
        self.bot_agent[bot_id] = idx

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
                        self.agents[self.bot_agent[bot_id]].update_proxy(bot_id, cur)
        self.group_proxies = new

    def check_proxies(self):
        if self.proxy_mgr.fetch_proxies():
            self.apply_proxies()

    def handle_exits(self):
        """Restart bots whose worker exited, on their group's current proxy and with the same name."""
        for agent in self.agents:
            for event in agent.poll():
                bot_id = event["bot"]
                self.bot_agent.pop(bot_id, None)
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
                    self.apply_proxies()
                else:
                    logger.warning(f"[Coordinator] Bot #{bot_id} exited unexpectedly (code {event['code']}). Restarting...")
                self.place_bot(bot_id, random.uniform(1.0, 3.5))

    def shutdown(self, *_):
        logger.info("[Coordinator] Shutting down all bots...")
        for agent in self.agents:
            agent.stop_all()
        logger.info("[Coordinator] All bots stopped cleanly.")
        sys.exit(0)

    def run(self):
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

        for g in range(args.groups):
            proxy = self.group_proxies[g]
            logger.info(f"[Coordinator] Group {g} on {proxy['host']}:{proxy['port']} ({args.bots_per_proxy} bots)")
            for k, bot_id in enumerate(self.group_bots(g)):
                self.place_bot(bot_id, (k * args.stagger_interval) + random.uniform(1.0, 3.5))

        next_proxy_check = time.time() + args.proxy_check_interval
        while True:
            if time.time() >= next_proxy_check:
                self.check_proxies()
                next_proxy_check = time.time() + args.proxy_check_interval
            self.handle_exits()
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
    args = parser.parse_args()

    if not args.webshare_key:
        parser.error("no Webshare API key: set WEBSHARE_API_KEY or pass --webshare-key")
    if args.groups < 1 or args.bots_per_proxy < 1 or args.proxy_check_interval <= 0:
        parser.error("--groups and --bots-per-proxy must be >= 1, --proxy-check-interval > 0")

    # Ensure DISPLAY is set for non-headless runs
    if sys.platform.startswith("linux") and not args.headless and not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"

    print("=" * 65)
    print(" Powerline.io Autonomous Bot (Camoufox C++ Always-On Groups)")
    print(f" Room: #{args.room} | Groups: {args.groups} x {args.bots_per_proxy} bots (1 IP per group)")
    print(f" Proxy check every {args.proxy_check_interval:.0f}s | Stagger: {args.stagger_interval}s | Headless: {args.headless}")
    print("=" * 65)

    Coordinator(args, [Agent(headless=args.headless, debug_dir=args.debug_dir)]).run()


if __name__ == "__main__":
    main()
