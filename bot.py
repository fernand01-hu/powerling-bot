#!/usr/bin/env python3
"""
Powerline.io Autonomous Bot (Camoufox C++ Engine + EzSolver)

Features:
1. Camoufox C++ Anti-Detect Browser:
   - Firefox C++ engine level spoofing without detectable CDP hooks.
   - Mouse, keyboard and timing input go through humanizer.py (Camoufox's own humanize is off).
   - Isolated HOME per worker process to eliminate Firefox bookmarks/places.sqlite locks.
2. Directional Bias:
   - Primarily moves Down and Left (90%+), with rare Up and Right maneuvers.
   - Prevents 180-degree self-collisions by only making valid orthogonal turns.
   - Short hold duration for Up/Right to quickly recover back to Down/Left.
3. Proxy Groups:
   - Bots are split into groups (default 3 groups x 3 bots); each group shares one proxy IP.
   - Gradual staggered connection intervals so bots in a group connect one by one.
4. Rotating Duty Cycle (2 Min Active / 1 Min Resting per group):
   - Each group plays for 2 minutes, then closes its browsers for 1 minute.
   - Groups are spread evenly over the cycle (offset = cycle / groups); with the defaults
     2 groups play while 1 rests.
5. Cloudflare Turnstile & GDPR:
   - EzSolver integration for Turnstile checkbox targeting (x + 28, y + h / 2).
   - Automatic consent dismissal and room reconnect handling.
"""

import argparse
import ctypes
import json
import logging
import multiprocessing
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

import numpy as np
from camoufox.pkgman import launch_path
from camoufox.sync_api import Camoufox
from humanizer import Humanizer

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("PowerlineBot")

DEFAULT_WEBSHARE_API_KEY = os.environ.get("WEBSHARE_API_KEY", "")
DEFAULT_ROOM = "2cjg"
DEFAULT_BOTS_PER_PROXY = 3
DEFAULT_GROUPS = 3
DEFAULT_STAGGER_INTERVAL = 3.0  # Seconds between each bot in a group connecting
DEFAULT_ACTIVE_DURATION = 120   # 2 minutes active per group
DEFAULT_PAUSE_DURATION = 60     # 1 minute resting per group
MIN_USEFUL_WINDOW = 20          # Don't (re)start bots with less active time than this
WINDOW_SIZE = (720, 480)        # Browser window size
MIN_HOLD = 0.28                 # Shortest time a direction is held

# Consent banner "decline" buttons: InMobi Choice ("DISAGREE") and Google Funding Choices ("Do not consent").
CONSENT_DECLINE_PATTERN = re.compile(r"^\s*(disagree|reject all|do not consent)\s*$", re.IGNORECASE)

RANDOM_NAME_BASES = [
    "Shadow", "Viper", "NeonPulse", "Zenith", "Echo", "Drift",
    "Pixel", "Frost", "Blaze", "Cosmo", "Nova", "Storm", "Apex", "Rogue",
    "Titan", "Vortex", "Phantom", "Cipher", "Byte", "Glitch", "Spark",
    "Havoc", "Specter", "Razor", "Omega", "Krypton", "Pulse", "Quantum",
    "Cobalt", "Turbo", "Solar", "Orbit", "Aura", "Sonic", "Matrix",
    "Hyper", "Static", "Zephyr", "Rift", "Quasar", "Inferno", "Volt"
]

RANDOM_TAGS = ["X", "Pro", "GG", "01", "io", "99", "Dev", "Max"]

OPPOSITE_DIRECTIONS = {
    "ArrowDown": "ArrowUp",
    "ArrowUp": "ArrowDown",
    "ArrowLeft": "ArrowRight",
    "ArrowRight": "ArrowLeft",
}


def generate_random_nickname() -> str:
    """Generate a realistic random gamer nickname under 15 characters."""
    base = random.choice(RANDOM_NAME_BASES)
    pattern = random.choice(["num", "tag", "plain"])
    if pattern == "num":
        name = f"{base}{random.randint(10, 999)}"
    elif pattern == "tag":
        tag = random.choice(RANDOM_TAGS)
        name = f"{base}_{tag}"
    else:
        name = base
    return name[:15]


def pick_next_direction(current_dir: Optional[str]) -> Tuple[str, float]:
    """
    Select next direction with heavy bias toward Down and Left.
    Returns (direction_key, duration_seconds).
    
    Probabilities:
    - Down & Left: ~90% preference.
    - Up & Right: ~10% rare recovery moves.
    - The first move (no current direction) is a 50/50 pick between Down and Left.
    - Down & Left are held for 0.9s - 2.8s; Up & Right for only 0.28s - 0.65s to quickly return Down/Left.
    - Durations sampled from right-skewed (normal + exponential) distributions rather than flat uniform.
    - Reversing 180 degrees into self is strictly avoided.
    """
    if current_dir in ("ArrowDown", "ArrowUp"):
        ortho = ["ArrowLeft", "ArrowRight"]
        weights = [0.90, 0.10]
    elif current_dir in ("ArrowLeft", "ArrowRight"):
        ortho = ["ArrowDown", "ArrowUp"]
        weights = [0.90, 0.10]
    else:
        ortho = ["ArrowDown", "ArrowLeft"]
        weights = [0.50, 0.50]

    chosen = random.choices(ortho, weights=weights, k=1)[0]

    if chosen in ("ArrowDown", "ArrowLeft"):
        duration = float(np.clip(np.random.normal(1.6, 0.25) + np.random.exponential(0.2), 0.9, 2.8))
    else:
        duration = float(np.clip(np.random.normal(0.38, 0.05) + np.random.exponential(0.06), MIN_HOLD, 0.65))

    return chosen, duration


# ============================================================================
# 1. Webshare Proxy Manager
# ============================================================================
class WebshareManager:
    """Fetches and manages rotating proxies from Webshare API v2."""

    def __init__(self, api_key: str = DEFAULT_WEBSHARE_API_KEY):
        self.api_key = api_key
        self.proxies: List[Dict[str, str]] = []
        self.last_fetch_time = 0.0

    def fetch_proxies(self) -> List[Dict[str, str]]:
        url = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=25"
        headers = {"Authorization": f"Token {self.api_key}"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = data.get("results", [])
                valid = []
                for p in results:
                    if p.get("valid", True):
                        valid.append({
                            "host": p["proxy_address"],
                            "port": str(p["port"]),
                            "user": p["username"],
                            "pass": p["password"],
                            "country": p.get("country_code", "XX"),
                        })
                if valid:
                    self.proxies = valid
                    self.last_fetch_time = time.time()
                    logger.info(f"[Webshare] Fetched {len(valid)} active proxies from API.")
                return self.proxies
        except Exception as e:
            logger.warning(f"[Webshare] Failed to fetch proxies: {e}")
            return self.proxies

    def get_proxy_for_instance(self, instance_id: int, bots_per_proxy: int = DEFAULT_BOTS_PER_PROXY) -> Optional[Dict[str, str]]:
        """Assign proxy so that `bots_per_proxy` instances share the same proxy."""
        if not self.proxies:
            return None
        proxy_idx = (instance_id // max(1, bots_per_proxy)) % len(self.proxies)
        return self.proxies[proxy_idx]


# ============================================================================
# 2. EzSolver Cloudflare Turnstile Solver
# ============================================================================
class EzSolver:
    """
    Cloudflare Turnstile challenge detector and solver.
    Uses EzSolver checkbox coordinate targeting (x + 28, y + h / 2) with human mouse motion.
    """

    @staticmethod
    def solve(
        page,
        log_prefix: str = "[EzSolver]",
        timeout: float = 12.0,
        humanizer: Optional[Humanizer] = None,
    ) -> bool:
        start_time = time.time()
        h = humanizer or Humanizer(page=page)

        while time.time() - start_time < timeout:
            token = page.evaluate("""
                (() => {
                    const inp = document.querySelector('[name="cf-turnstile-response"]');
                    return inp && inp.value ? inp.value : null;
                })()
            """)
            if token:
                logger.info(f"{log_prefix} Turnstile verified (token: {token[:16]}...)")
                return True

            captcha_retry = page.locator("#captchaTryAgainButton")
            if captcha_retry.count() > 0 and captcha_retry.first.is_visible():
                logger.info(f"{log_prefix} Captcha retry button detected, clicking with biometric profile...")
                h.click_locator(captcha_retry.first)
                h.sleep(1.2, 0.15, 0.2, min_floor=0.8)

            iframe_rect = page.evaluate("""
                (() => {
                    let f = null;
                    for (const el of document.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            const sub = el.shadowRoot.querySelector('iframe');
                            if (sub && (sub.src || '').includes('challenges.cloudflare.com')) {
                                f = sub;
                                break;
                            }
                        }
                    }
                    if (!f) {
                        for (const el of document.querySelectorAll('iframe')) {
                            const src = el.src || el.getAttribute('src') || '';
                            if (src.includes('challenges.cloudflare.com')) {
                                f = el;
                                break;
                            }
                        }
                    }
                    if (f) {
                        const r = f.getBoundingClientRect();
                        if (r.width > 20 && r.height > 20) {
                            return {x: r.x, y: r.y, w: r.width, h: r.height};
                        }
                    }
                    return null;
                })()
            """)

            if iframe_rect:
                # 2D Gaussian targeting centered on the Turnstile checkbox (~28px from left edge)
                raw_cx = iframe_rect["x"] + 28.0 + float(np.random.normal(0.0, 1.8))
                raw_cy = iframe_rect["y"] + (iframe_rect["h"] / 2.0) + float(np.random.normal(0.0, 1.8))
                # Clamp safely within the 26x26 checkbox boundaries
                cx = float(np.clip(raw_cx, iframe_rect["x"] + 22.0, iframe_rect["x"] + 34.0))
                cy = float(np.clip(raw_cy, iframe_rect["y"] + (iframe_rect["h"] / 2.0) - 6.0, iframe_rect["y"] + (iframe_rect["h"] / 2.0) + 6.0))
                logger.info(f"{log_prefix} Clicking Turnstile checkbox with WindMouse trajectory at ({cx:.1f}, {cy:.1f})")

                # WindMouse move, then split down/up with physical contact hold duration
                h.click_coords(cx, cy, enable_overshoot=True)

                # Ambient idle drift while awaiting Turnstile verification
                h.idle_drift(duration=1.4)

                token_after = page.evaluate("""
                    (() => {
                        const inp = document.querySelector('[name="cf-turnstile-response"]');
                        return inp && inp.value ? inp.value : null;
                    })()
                """)
                if token_after:
                    logger.info(f"{log_prefix} Turnstile challenge passed!")
                    return True

            # Lobby or death screen visible means no challenge is blocking the game.
            if PowerlineBot.is_out_of_game(page):
                return True

            h.sleep(0.65, 0.08, 0.12, min_floor=0.35)

        return False


# ============================================================================
# 3. Autonomous Powerline Game Controller
# ============================================================================
class PowerlineBot:
    """Camoufox-powered autonomous Powerline.io player."""

    def __init__(
        self,
        instance_id: int = 0,
        room: str = DEFAULT_ROOM,
        proxy: Optional[Dict[str, str]] = None,
        headless: bool = False,
        session_duration: int = DEFAULT_ACTIVE_DURATION,
    ):
        self.instance_id = instance_id
        self.log_prefix = f"[Bot-{instance_id}]"
        self.room = room
        self.proxy = proxy
        self.headless = headless
        self.session_duration = session_duration
        self.target_url = f"https://powerline.io/#{self.room}"
        self.humanizer: Optional[Humanizer] = None

    def dismiss_overlays_and_consent(self, page, humanizer: Optional[Humanizer] = None):
        """Decline GDPR consent banners (InMobi "We value your privacy", Google Funding Choices)."""
        h = humanizer or self.humanizer or Humanizer(page=page)
        # Matched by button text so it keeps working if the CMP's markup changes; the banner
        # can be in the page itself or inside a CMP iframe, so check every frame.
        for frame in page.frames:
            try:
                decline_btn = frame.get_by_role("button", name=CONSENT_DECLINE_PATTERN)
                if decline_btn.count() > 0 and decline_btn.first.is_visible():
                    logger.info(f"{self.log_prefix} Declining GDPR consent banner with biometric click...")
                    h.click_locator(decline_btn.first)
                    h.sleep(0.7, 0.08, 0.12, min_floor=0.35)
                    break
            except Exception:
                pass

        # Handle room timeout / reconnect button
        try:
            reconnect_btn = page.locator("#reconnectButton")
            if reconnect_btn.count() > 0 and reconnect_btn.first.is_visible():
                logger.info(f"{self.log_prefix} Room timeout detected, clicking #reconnectButton...")
                h.click_locator(reconnect_btn.first)
                h.sleep(0.9, 0.1, 0.15, min_floor=0.45)
        except Exception:
            pass

        # Handle room failed / continue button
        try:
            continue_btn = page.locator("#continueButton")
            if continue_btn.count() > 0 and continue_btn.first.is_visible():
                logger.info(f"{self.log_prefix} Room connection failed, clicking #continueButton...")
                h.click_locator(continue_btn.first)
                h.sleep(0.9, 0.1, 0.15, min_floor=0.45)
        except Exception:
            pass

    @staticmethod
    def is_out_of_game(page) -> bool:
        """True while the death screen or the nickname lobby is visible."""
        return page.evaluate("""
            (() => {
                const d = document.getElementById('deathPlayAgain');
                const n = document.getElementById('nick');
                const d_vis = !!d && d.offsetParent !== null;
                const n_vis = !!n && n.offsetParent !== null;
                return d_vis || n_vis;
            })()
        """)

    def run_session(self, proxy_info: Optional[Dict[str, str]] = None, max_duration: int = DEFAULT_ACTIVE_DURATION):
        """Run a single browser session with the assigned proxy."""
        proxy_opt = None
        if proxy_info:
            proxy_opt = {
                "server": f"http://{proxy_info['host']}:{proxy_info['port']}",
                "username": proxy_info["user"],
                "password": proxy_info["pass"],
            }
            logger.info(
                f"{self.log_prefix} Starting Camoufox with proxy {proxy_info['host']}:{proxy_info['port']} "
                f"({proxy_info.get('country')}) | Headless={self.headless}"
            )
        else:
            logger.info(f"{self.log_prefix} Starting Camoufox without proxy | Headless={self.headless}")

        session_start = time.time()
        rounds_played = 0

        # Launch native Camoufox browser with C++ anti-detect engine
        # and isolated profile preferences to prevent places.sqlite lock conflicts
        with Camoufox(
            headless=self.headless,
            humanize=False,  # humanizer.py drives the mouse; Camoufox's would re-humanize every step
            geoip=True,
            disable_coop=True,
            i_know_what_im_doing=True,
            executable_path=str(launch_path()),
            firefox_user_prefs={
                "places.history.enabled": False,
                "browser.places.importBookmarksHTML": False,
                "browser.bookmarks.restore_default_bookmarks": False,
                "browser.shell.checkDefaultBrowser": False,
            },
            window=WINDOW_SIZE,
            proxy=proxy_opt,
        ) as browser:
            page = browser.new_page()
            # Mouse coordinates are relative to the viewport, which is smaller than the window.
            try:
                viewport = tuple(page.evaluate("() => [window.innerWidth, window.innerHeight]"))
            except Exception:
                viewport = WINDOW_SIZE
            self.humanizer = Humanizer(
                page=page,
                session_start=session_start,
                window_size=viewport,
                fatigue_horizon=max_duration,
            )
            logger.info(f"{self.log_prefix} Navigating to {self.target_url} ...")
            try:
                page.goto(self.target_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                logger.warning(f"{self.log_prefix} Navigation warning: {e}")

            # Natural post-navigation dwell with ambient drift
            self.humanizer.sleep(2.5, 0.3, 0.5, min_floor=1.8)
            self.humanizer.idle_drift(duration=0.8)

            while time.time() - session_start < max_duration:
                # 1. Dismiss popups / consent
                self.dismiss_overlays_and_consent(page, humanizer=self.humanizer)

                # 2. Check for Turnstile
                EzSolver.solve(page, log_prefix=self.log_prefix, timeout=5.0, humanizer=self.humanizer)

                # 3. Check current page state (In lobby, Dead, In game)
                death_btn = page.locator("#deathPlayAgain")
                nick_input = page.locator("#nick")

                if death_btn.count() > 0 and death_btn.first.is_visible():
                    logger.info(f"{self.log_prefix} Death screen detected, cognitive pause before respawn...")
                    self.humanizer.sleep(1.4, 0.2, 0.3, min_floor=0.8)
                    self.humanizer.click_locator(death_btn.first)
                    self.humanizer.sleep(0.8, 0.1, 0.15, min_floor=0.4)
                elif nick_input.count() > 0 and nick_input.first.is_visible():
                    self.humanizer.sleep(0.8, 0.12, 0.2, min_floor=0.4)
                    nickname = generate_random_nickname()
                    logger.info(f"{self.log_prefix} Spawning round #{rounds_played + 1} with nickname: '{nickname}'")
                    self.humanizer.click_locator(nick_input.first)
                    self.humanizer.sleep(0.25, 0.04, 0.06, min_floor=0.15)
                    nick_input.first.fill("")
                    self.humanizer.sleep(0.15, 0.03, 0.05, min_floor=0.10)
                    self.humanizer.type_text(nickname, typo_prob=0.04)
                    self.humanizer.sleep(0.45, 0.08, 0.12, min_floor=0.25)
                    self.humanizer.press_key("Enter")
                    self.humanizer.sleep(1.0, 0.15, 0.2, min_floor=0.6)
                else:
                    self.humanizer.press_key("Enter", hesitation=True)
                    self.humanizer.sleep(0.7, 0.1, 0.15, min_floor=0.35)

                # Only steer once the lobby/death screens are gone; otherwise manage watchdog & drift.
                if self.is_out_of_game(page):
                    # Stuck State Watchdog: prevent infinite loops when trapped in lobby
                    if self.humanizer.check_watchdog(is_stuck=True, max_iterations=15, max_seconds=30.0):
                        logger.warning(
                            f"{self.log_prefix} Stuck state watchdog triggered (trapped out-of-game). Attempting page reload..."
                        )
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=15000)
                            self.humanizer.reset_watchdog()
                            self.humanizer.sleep(2.0, 0.3, 0.4, min_floor=1.0)
                        except Exception as e:
                            logger.warning(f"{self.log_prefix} Page reload failed ({e}); recycling session.")
                            break
                        continue

                    self.humanizer.idle_drift(duration=0.6)
                    self.humanizer.sleep(0.7, 0.1, 0.15, min_floor=0.4)
                    continue
                else:
                    self.humanizer.check_watchdog(is_stuck=False)

                # 4. Gameplay loop for this round
                round_start = time.time()
                current_dir = None
                alive = True

                next_dir, hold_time = pick_next_direction(current_dir)
                self.humanizer.press_key(next_dir, hesitation=True)
                current_dir = next_dir
                last_turn = time.time()

                logger.info(f"{self.log_prefix} In game! Steering snake (Down/Left biased)...")
                while alive and (time.time() - session_start < max_duration):
                    if self.is_out_of_game(page):
                        alive = False
                        rounds_played += 1
                        logger.info(f"{self.log_prefix} Round ended after {time.time() - round_start:.1f}s.")
                        self.humanizer.sleep(1.2, 0.15, 0.25, min_floor=0.8)
                        break

                    now = time.time()
                    if now - last_turn > hold_time:
                        chosen_dir, next_hold = pick_next_direction(current_dir)
                        # Biometric key press with human contact duration and micro-hesitation
                        self.humanizer.press_key(chosen_dir, hesitation=True)
                        current_dir = chosen_dir
                        # Add organic reaction jitter to next turn interval
                        reaction_jitter = float(np.random.normal(0.0, 0.08))
                        hold_time = max(MIN_HOLD, next_hold + reaction_jitter)
                        last_turn = time.time()

                    # Variable tick interval with Ex-Gaussian jitter instead of rigid 0.15s
                    self.humanizer.sleep(mean=0.13, sigma=0.015, tau=0.02, min_floor=0.11)

            logger.info(f"{self.log_prefix} Session finished ({max_duration}s). Rounds played: {rounds_played}.")

    def run(self):
        """Single-session execution for the scheduled active period."""
        try:
            self.run_session(proxy_info=self.proxy, max_duration=self.session_duration)
        except (KeyboardInterrupt, SystemExit):
            logger.info(f"{self.log_prefix} Terminated, closing browser.")
        except Exception as e:
            logger.error(f"{self.log_prefix} Session error: {e}", exc_info=True)


# ============================================================================
# Multi-Process Worker Function
# ============================================================================
def bot_worker_process(
    instance_id: int,
    room: str,
    proxy: Optional[Dict[str, str]],
    headless: bool,
    session_duration: int,
    start_delay: float,
):
    """Entry point for each independent bot worker process with isolated profile and gradual connection."""
    # Forked workers inherit the manager's signal handlers; reset them so a worker never
    # kills its siblings. Ctrl+C is handled by the manager, SIGTERM unwinds the Camoufox context.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Own process group, so the manager can kill this worker's browser without touching other groups.
    os.setsid()
    # Get SIGTERM if the manager dies abruptly, so no orphaned browsers keep running.
    try:
        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except OSError:
        pass
    parent = multiprocessing.parent_process()
    if parent is not None and os.getppid() != parent.pid:
        sys.exit(0)  # Manager already gone before prctl took effect

    # Re-seed RNG per worker process to guarantee independent stochastic biometrics
    worker_seed = (os.getpid() ^ int(time.time() * 1000) ^ (instance_id << 16)) & 0xFFFFFFFF
    random.seed(worker_seed)
    np.random.seed(worker_seed)

    # Ensure fully isolated HOME directory per worker instance to completely prevent places.sqlite lock contention
    worker_home = f"/tmp/camoufox_worker_{instance_id}"
    os.makedirs(worker_home, exist_ok=True)
    os.environ["HOME"] = worker_home

    if start_delay > 0:
        logger.info(f"[Bot-{instance_id}] Waiting {start_delay:.1f}s before connecting (gradual join)...")
        time.sleep(start_delay)

    bot = PowerlineBot(
        instance_id=instance_id,
        room=room,
        proxy=proxy,
        headless=headless,
        session_duration=session_duration,
    )
    bot.run()


# ============================================================================
# CLI Entrypoint & Multi-Process Cycle Manager
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
        "--active-duration",
        type=int,
        default=DEFAULT_ACTIVE_DURATION,
        help="Seconds each group plays before resting (default: 120)",
    )
    parser.add_argument(
        "--pause-duration",
        type=int,
        default=DEFAULT_PAUSE_DURATION,
        help="Seconds each group rests with its browsers closed (default: 60)",
    )
    args = parser.parse_args()

    if not args.webshare_key:
        parser.error("no Webshare API key: set WEBSHARE_API_KEY or pass --webshare-key")
    if args.groups < 1 or args.bots_per_proxy < 1 or args.active_duration < 1 or args.pause_duration < 0:
        parser.error("--groups, --bots-per-proxy and --active-duration must be >= 1, --pause-duration >= 0")

    # Ensure DISPLAY is set for non-headless runs
    if not args.headless and not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"

    period = args.active_duration + args.pause_duration
    offset = period / args.groups  # Groups are spread evenly over one cycle

    print("=" * 65)
    print(" Powerline.io Autonomous Bot (Camoufox C++ Rotating Groups)")
    print(f" Room: #{args.room} | Groups: {args.groups} x {args.bots_per_proxy} bots (1 IP per group)")
    print(f" Per group: {args.active_duration}s active / {args.pause_duration}s resting, offset {offset:.0f}s")
    print(f" Stagger: {args.stagger_interval}s | Headless: {args.headless}")
    print("=" * 65)

    proxy_mgr = WebshareManager(api_key=args.webshare_key)
    groups: List[List[multiprocessing.Process]] = [[] for _ in range(args.groups)]
    retry_at = [0.0] * args.groups
    t0 = time.time()

    def window(g: int, now: float) -> Tuple[bool, float]:
        """(is group g in its active window, seconds until its state flips)."""
        pos = (now - t0 + g * offset) % period
        if pos < args.active_duration:
            return True, args.active_duration - pos
        return False, period - pos

    def spawn_worker(i: int, session_duration: int, start_delay: float) -> multiprocessing.Process:
        proxy = proxy_mgr.get_proxy_for_instance(i, args.bots_per_proxy)
        p = multiprocessing.Process(
            target=bot_worker_process,
            args=(i, args.room, proxy, args.headless, session_duration, start_delay),
            name=f"PowerlineWorker-{i}",
        )
        p.daemon = True
        p.start()
        return p

    def start_group(g: int, duration: float):
        # Refresh the proxy list at most once per cycle, shared by every group.
        if not proxy_mgr.proxies or time.time() - proxy_mgr.last_fetch_time > period:
            proxy_mgr.fetch_proxies()
        if not proxy_mgr.proxies:
            logger.error(f"[Manager] No proxies available for group {g}, retrying in 30s...")
            retry_at[g] = time.time() + 30
            return
        if len(proxy_mgr.proxies) < args.groups:
            logger.warning(
                f"[Manager] Only {len(proxy_mgr.proxies)} proxies for {args.groups} groups; some groups share an IP."
            )
        proxy = proxy_mgr.get_proxy_for_instance(g * args.bots_per_proxy, args.bots_per_proxy)
        logger.info(
            f"[Manager] Group {g} starting for {duration:.0f}s on {proxy['host']}:{proxy['port']} "
            f"({args.bots_per_proxy} bots)"
        )
        for k in range(args.bots_per_proxy):
            i = g * args.bots_per_proxy + k
            start_delay = (k * args.stagger_interval) + random.uniform(1.0, 3.5)
            # The session clock starts after the delay, so end it with the group's window.
            p = spawn_worker(i, max(1, int(duration - start_delay)), start_delay)
            groups[g].append(p)
            logger.info(f"[Manager] Initialized worker #{i} (PID: {p.pid})")

    def stop_group(g: int):
        for p in groups[g]:
            if p.is_alive():
                p.terminate()
        for p in groups[g]:
            p.join(timeout=5)
            if p.is_alive():
                # Worker didn't unwind in time: kill it together with its browser.
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    p.kill()
                p.join(timeout=2)
        groups[g].clear()

    def sigint_handler(sig, frame):
        logger.info("[Manager] Shutting down all worker processes...")
        for g in range(args.groups):
            stop_group(g)
        # Ensure all browser instances close completely
        try:
            subprocess.run(["pkill", "-f", "camoufox"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        logger.info("[Manager] All workers stopped cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)
    signal.signal(signal.SIGTERM, sigint_handler)

    while True:
        now = time.time()
        for g in range(args.groups):
            active, left = window(g, now)
            running = bool(groups[g])

            if active and not running:
                if left > MIN_USEFUL_WINDOW and now >= retry_at[g]:
                    start_group(g, left)
            elif not active and running:
                logger.info(f"[Manager] Group {g} resting for {left:.0f}s, closing its browsers...")
                stop_group(g)
            elif active and running and left > MIN_USEFUL_WINDOW:
                # Respawn crashed workers for the rest of the group's window
                for k, p in enumerate(groups[g]):
                    if not p.is_alive():
                        i = g * args.bots_per_proxy + k
                        logger.warning(f"[Manager] Worker #{i} exited unexpectedly (code {p.exitcode}). Respawning...")
                        start_delay = random.uniform(1.0, 3.5)
                        groups[g][k] = spawn_worker(i, max(1, int(left - start_delay)), start_delay)

        time.sleep(1)


if __name__ == "__main__":
    main()
