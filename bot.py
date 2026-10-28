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
4. Always-On Browsers:
   - Every bot's browser stays open for the whole run; crashed workers are restarted.
   - When Webshare rotates a group's proxy IP, its bots reload the game on the new IP.
   - Each bot keeps one nickname for the whole run and respawns after a short,
     right-skewed delay (usually under 2s, occasionally up to 15s). The game ignores
     input for ~2s after a death, so most respawns land at ~2.5s.
5. Cloudflare Turnstile & GDPR:
   - EzSolver integration for Turnstile checkbox targeting (x + 28, y + h / 2).
   - Automatic consent dismissal and room reconnect handling.
"""

import base64
import ctypes
import logging
import multiprocessing
import os
import queue
import random
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import psutil
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

DEFAULT_ROOM = "2cjg"
BAD_PROXY_EXIT = 3                   # Worker exit code: its proxy can't reach the game
MAX_FAILED_RELOADS = 2               # Stuck reloads in a row without reaching the game -> bad proxy
STOP_WORKER = "stop"                 # Sent on a worker's update queue to make it close its browser and exit
SCREENSHOT_REQUEST = {"cmd": "screenshot"}  # Sent on a worker's update queue to get a picture of its page
IS_WINDOWS = sys.platform == "win32"
STATUS_INTERVAL = 5.0                # Seconds between status reports while a bot's state doesn't change
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


def sample_respawn_delay() -> float:
    """Seconds between the death screen appearing and pressing respawn: usually under 2s,
    occasionally a longer break of up to 15s."""
    if random.random() < 0.85:
        delay = np.random.normal(1.1, 0.25) + np.random.exponential(0.3)
    else:
        delay = 2.0 + np.random.exponential(3.5)
    return float(np.clip(delay, 0.6, 15.0))


def to_playwright_proxy(proxy: Dict[str, str]) -> Dict[str, str]:
    return {
        "server": f"http://{proxy['host']}:{proxy['port']}",
        "username": proxy["user"],
        "password": proxy["pass"],
    }


# ============================================================================
# 1. EzSolver Cloudflare Turnstile Solver
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
            # If already in the active gameplay arena, challenge is passed
            in_arena = page.evaluate("""
                (() => {
                    const isVis = (el) => {
                        if (!el || el.hidden) return false;
                        if (el.style && (el.style.display === 'none' || el.style.visibility === 'hidden')) return false;
                        return el.offsetParent !== null;
                    };
                    const canvas = document.getElementById('xcanvas');
                    if (!canvas) return false;

                    // Cannot be in active arena if any lobby/death/connecting/error elements are visible
                    if (isVis(document.getElementById('deathPlayAgain'))) return false;
                    if (isVis(document.getElementById('topGuiConnecting'))) return false;
                    if (isVis(document.getElementById('nick'))) return false;
                    if (isVis(document.getElementById('captchaFailed'))) return false;
                    if (isVis(document.getElementById('roomTimeout'))) return false;
                    if (isVis(document.getElementById('roomFailed'))) return false;

                    const stats = document.getElementById('statsPanel');
                    if (stats && !stats.hidden && stats.style.display !== 'none') return false;

                    const topGui = document.getElementById('topGui');
                    if (topGui && !topGui.hidden && topGui.style.display !== 'none') return false;

                    return true;
                })()
            """)
            if in_arena:
                return True

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

            h.sleep(0.65, 0.08, 0.12, min_floor=0.35)

        return False


# ============================================================================
# 2. Autonomous Powerline Game Controller
# ============================================================================
class PowerlineBot:
    """Camoufox-powered autonomous Powerline.io player."""

    def __init__(
        self,
        instance_id: int = 0,
        room: str = DEFAULT_ROOM,
        proxy: Optional[Dict[str, str]] = None,
        headless: bool = False,
        nickname: Optional[str] = None,
        proxy_updates: Optional["multiprocessing.Queue"] = None,
        debug_dir: Optional[str] = None,
        reports: Optional["multiprocessing.Queue"] = None,
    ):
        self.instance_id = instance_id
        self.log_prefix = f"[Bot-{instance_id}]"
        self.room = room
        self.proxy = proxy
        self.headless = headless
        self.nickname = nickname or generate_random_nickname()
        self.proxy_updates = proxy_updates
        self.pending_proxy: Optional[Dict[str, str]] = None
        self.debug_dir = debug_dir
        self.reports = reports
        self.target_url = f"https://powerline.io/#{self.room}"
        self.humanizer: Optional[Humanizer] = None
        # What this bot is doing, reported to the agent for the coordinator's dashboard.
        self.state = "starting"
        self.state_since = time.time()
        self.last_status = 0.0
        self.rounds = 0
        self.last_respawn: Optional[float] = None
        self.screenshot_wanted = False
        self.ws_blocked = False
        self.ws_blocked_reason = ""

    def setup_page(self, page):
        """Hook page events to immediately catch blocked proxies, WebSocket 400 errors, or HTTP 407."""
        self.ws_blocked = False
        self.ws_blocked_reason = ""
        if not hasattr(page, "on"):
            return

        def _on_ws(ws):
            def _on_err(err):
                err_str = str(err)
                url = getattr(ws, "url", "")
                logger.warning(f"{self.log_prefix} WebSocket error on {url or 'unknown'}: {err_str}")
                # The game client first probes ws:// which is rejected with 400 Bad Request
                # before falling back to wss://. Ignore 400 on unencrypted ws:// probes.
                if url.startswith("ws://") and ("400" in err_str or "Bad Request" in err_str):
                    return
                if any(code in err_str for code in ("400", "403", "407", "Bad Request", "Forbidden")):
                    self.ws_blocked = True
                    self.ws_blocked_reason = f"WebSocket error: {err_str} on {url}"
            try:
                ws.on("socketerror", _on_err)
            except Exception:
                pass

        def _on_resp(resp):
            try:
                status = resp.status
                # Note: 407 is standard proxy authentication challenge; do not abort on it
                if status in (400, 403):
                    url = getattr(resp, "url", "")
                    # Ignore unencrypted http:// probes to powerline game servers
                    if url.startswith("http://"):
                        return
                    # Ignore Cloudflare challenge scripts / asset probes
                    if "challenges.cloudflare" not in url and ("powerline.io" in url or "/_/" in url):
                        logger.warning(f"{self.log_prefix} HTTP {status} response on {url}")
                        self.ws_blocked = True
                        self.ws_blocked_reason = f"HTTP {status} on {url}"
            except Exception:
                pass

        def _on_req_failed(req):
            try:
                err = req.failure or ""
                if any(k in err.upper() for k in ("PROXY", "REFUSED", "RESET")):
                    logger.warning(f"{self.log_prefix} Network request failed: {req.url} ({err})")
                    self.ws_blocked = True
                    self.ws_blocked_reason = f"Network failure: {err}"
            except Exception:
                pass

        try:
            page.on("websocket", _on_ws)
            page.on("response", _on_resp)
            page.on("requestfailed", _on_req_failed)
        except Exception as e:
            logger.warning(f"{self.log_prefix} Failed to attach page monitors: {e}")

    def set_state(self, state: str, force: bool = False):
        """Report the bot's state (loading, connecting, lobby, playing, dead); at most every
        STATUS_INTERVAL seconds unless it changes."""
        now = time.time()
        if state == self.state and not force and now - self.last_status < STATUS_INTERVAL:
            return
        if state != self.state:
            self.state_since = now
        self.state, self.last_status = state, now
        if self.reports is None:
            return
        try:
            self.reports.put_nowait({"bot": self.instance_id, "status": {
                "state": state,
                "since": self.state_since,
                "rounds": self.rounds,
                "last_respawn": self.last_respawn,
                "proxy": self.proxy["host"] if self.proxy else None,
                "name": self.nickname,
                "updated": now,
            }})
        except Exception:
            pass

    def dismiss_overlays_and_consent(self, page, humanizer: Optional[Humanizer] = None):
        """Decline GDPR consent banners (InMobi "We value your privacy", Google Funding Choices, Quantcast Choice)."""
        h = humanizer or self.humanizer or Humanizer(page=page)
        # Matched by button text so it keeps working if the CMP's markup changes; the banner
        # can be in the page itself or inside a CMP iframe, so check every frame.
        for frame in getattr(page, "frames", []):
            try:
                decline_btn = frame.get_by_role("button", name=CONSENT_DECLINE_PATTERN)
                if decline_btn.count() > 0 and decline_btn.first.is_visible():
                    logger.info(f"{self.log_prefix} Declining GDPR consent banner with biometric click...")
                    h.click_locator(decline_btn.first)
                    h.sleep(0.7, 0.08, 0.12, min_floor=0.35)
                    break
            except Exception:
                pass

        # Remove Quantcast Choice / CMP2 consent banners that intercept pointer events
        try:
            page.evaluate("""
                (() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    for (const b of buttons) {
                        const t = (b.innerText || '').trim().toUpperCase();
                        if (t === 'AGREE' || t === 'ACCEPT ALL' || t === 'I AGREE' || t === 'ACCEPT') {
                            b.click();
                            break;
                        }
                    }
                    const disagree = document.getElementById('disagree-btn');
                    if (disagree) disagree.click();

                    const qc = document.getElementById('qc-cmp2-container') || document.querySelector('.qc-cmp2-container');
                    if (qc) qc.remove();
                    const qcUi = document.getElementById('qc-cmp2-ui') || document.querySelector('.qc-cmp2-ui');
                    if (qcUi) qcUi.remove();
                    document.body.classList.remove('qc-cmp2-showing');
                })()
            """)
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
        """True while in the lobby, connecting, dead, or disconnected.
        False only while actively playing in the arena."""
        return bool(page.evaluate("""
            (() => {
                const isVis = (el) => {
                    if (!el) return false;
                    if (el.hidden) return false;
                    if (el.style && (el.style.display === 'none' || el.style.visibility === 'hidden')) return false;
                    try {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                    } catch (e) {}
                    return el.offsetParent !== null;
                };

                const canvas = document.getElementById('xcanvas');
                if (!canvas) return true;

                const death = document.getElementById('deathPlayAgain');
                if (isVis(death)) return true;
                const stats = document.getElementById('statsPanel');
                if (stats && stats.style.display !== 'none' && !stats.hidden) return true;

                const connecting = document.getElementById('topGuiConnecting');
                if (isVis(connecting)) return true;

                const nick = document.getElementById('nick');
                if (isVis(nick)) return true;

                const captcha = document.getElementById('captchaFailed');
                if (isVis(captcha)) return true;
                const timeout = document.getElementById('roomTimeout');
                if (isVis(timeout)) return true;
                const failed = document.getElementById('roomFailed');
                if (isVis(failed)) return true;

                const overlay = document.getElementById('overlay');
                return false;
            })()
        """))

    @staticmethod
    def is_connected(page) -> bool:
        """True while playing or on the lobby / death screen, i.e. not stuck connecting or blocked."""
        return bool(page.evaluate("""
            (() => {
                const overlay = document.getElementById('overlay');
                const death = document.getElementById('deathPlayAgain');
                const isVis = (el) => !!el && !el.hidden && el.style.display !== 'none' && el.offsetParent !== null;
                if (isVis(death) || isVis(document.getElementById('nick'))) return true;
                const connecting = document.getElementById('topGuiConnecting');
                if (isVis(connecting)) return false;
                return true;
            })()
        """))

    def debug_shot(self, page, tag: str):
        """Save a screenshot when --debug-dir is set, to check afterwards what the bot saw."""
        if not self.debug_dir:
            return
        try:
            path = os.path.join(self.debug_dir, f"bot{self.instance_id}_{time.strftime('%H%M%S')}_{tag}.png")
            page.screenshot(path=path)
        except Exception as e:
            logger.warning(f"{self.log_prefix} Debug screenshot failed: {e}")

    def open_game(self, page):
        """Load the room and wait for the nickname lobby, so the bot never starts a game before typing its name."""
        self.set_state("loading")
        logger.info(f"{self.log_prefix} Navigating to {self.target_url} ...")
        try:
            page.goto(self.target_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.warning(f"{self.log_prefix} Navigation warning: {e}")
        # Wait up to 30s for the lobby, a second at a time, answering the dashboard (screenshots)
        # and noticing a stop, a new proxy, or a blocked WebSocket connection meanwhile.
        deadline = time.time() + 30
        while True:
            if self.ws_blocked:
                logger.warning(
                    f"{self.log_prefix} Game server connection blocked ({self.ws_blocked_reason}); aborting lobby wait."
                )
                break
            self.dismiss_overlays_and_consent(page, humanizer=self.humanizer)
            EzSolver.solve(page, log_prefix=self.log_prefix, timeout=2.0, humanizer=self.humanizer)
            try:
                page.wait_for_selector("#nick", state="visible", timeout=1000)
                break
            except Exception:
                if self.ws_blocked or self.poll_proxy_update():
                    break  # The caller switches to the new proxy or exits on bad proxy
                self.serve_screenshot(page)
                if time.time() >= deadline:
                    logger.warning(f"{self.log_prefix} Lobby didn't appear within 30s, continuing anyway.")
                    break
        self.set_low_graphics(page)
        self.debug_shot(page, "loaded")

    def set_low_graphics(self, page):
        """Switch the game to Low graphics: headless browsers render on the CPU, so this is what
        decides how many bots one machine can run."""
        try:
            if page.evaluate("""
                (() => {
                    const b = document.getElementById('settingsGraphicsLow');
                    if (!b) return false;
                    b.click();
                    return true;
                })()
            """):
                logger.info(f"{self.log_prefix} Graphics set to Low.")
        except Exception as e:
            logger.warning(f"{self.log_prefix} Couldn't set Low graphics: {e}")

    def respawn(self, page, died_at: float):
        """Wait a sampled delay on the death screen, then play again."""
        delay = sample_respawn_delay()
        self.set_state("dead")
        logger.info(f"{self.log_prefix} Death screen, respawn delay {delay:.1f}s...")
        self.debug_shot(page, "death")
        wait = died_at + delay - time.time()
        if wait > 0:
            time.sleep(wait)
        # The death panel slides in, so its small button can move under a click; the game also takes Enter.
        # It ignores input while the death animation plays (~1.5-2s), so retry until it respawns.
        give_up = time.time() + 6
        while True:
            try:
                page.evaluate("""() => {
                    const btn = document.getElementById('deathPlayAgain');
                    const isVis = (el) => !!el && !el.hidden && el.style.display !== 'none' && el.offsetParent !== null;
                    if (isVis(btn)) btn.click();
                }""")
            except Exception:
                pass
            self.humanizer.press_key("Enter")
            self.humanizer.sleep(0.35, 0.05, 0.08, min_floor=0.25)
            if not self.is_out_of_game(page):
                self.last_respawn = time.time() - died_at
                logger.info(f"{self.log_prefix} Respawned {self.last_respawn:.1f}s after death.")
                return
            if time.time() > give_up:
                return  # Leave it to the outer loop (lobby, kicked, ...)

    def poll_proxy_update(self) -> bool:
        """Pick up the newest proxy sent by the coordinator; True if the bot should switch to it."""
        if self.proxy_updates is None:
            return False
        try:
            while True:
                update = self.proxy_updates.get_nowait()
                if update == STOP_WORKER:
                    raise SystemExit(0)  # Unwinds the Camoufox context, which closes the browser
                if update == SCREENSHOT_REQUEST:
                    self.screenshot_wanted = True
                    continue
                self.pending_proxy = update
        except queue.Empty:
            pass
        return self.pending_proxy is not None

    def serve_screenshot(self, page):
        """Send a small picture of the page to the agent, if the dashboard asked for one."""
        if not self.screenshot_wanted or self.reports is None:
            return
        self.screenshot_wanted = False
        try:
            data = page.screenshot(type="jpeg", quality=55)
            self.reports.put_nowait({
                "bot": self.instance_id,
                "screenshot": base64.b64encode(data).decode("ascii"),
                "time": time.time(),
            })
        except Exception as e:
            logger.warning(f"{self.log_prefix} Screenshot for the dashboard failed: {e}")

    def switch_proxy(self, browser, page):
        """Reload the game on the pending proxy IP without closing the browser.

        A context's proxy is fixed when it's created, so the reload happens in a fresh
        context on the new proxy, and the old one is closed.
        """
        proxy_info, self.pending_proxy = self.pending_proxy, None
        logger.info(
            f"{self.log_prefix} Proxy IP changed to {proxy_info['host']}:{proxy_info['port']} "
            f"({proxy_info.get('country')}), refreshing the page..."
        )
        new_page = browser.new_page(proxy=to_playwright_proxy(proxy_info))
        self.setup_page(new_page)
        self.open_game(new_page)
        try:
            page.context.close()
        except Exception:
            pass
        self.proxy = proxy_info
        self.humanizer.page = new_page
        self.humanizer.reset_watchdog()
        self.humanizer.sleep(2.0, 0.3, 0.4, min_floor=1.2)
        return new_page

    def run_session(self) -> Optional[int]:
        """Keep one browser open and play until terminated, following proxy IP changes."""
        proxy_opt = to_playwright_proxy(self.proxy) if self.proxy else None
        if self.proxy:
            logger.info(
                f"{self.log_prefix} Starting Camoufox with proxy {self.proxy['host']}:{self.proxy['port']} "
                f"({self.proxy.get('country')}) as '{self.nickname}' | Headless={self.headless}"
            )
        else:
            logger.info(f"{self.log_prefix} Starting Camoufox without proxy as '{self.nickname}' | Headless={self.headless}")

        self.rounds = 0
        self.reached_game = False  # Whether this proxy ever got past "Connecting..."
        failed_reloads = 0         # Stuck reloads in a row that never reached the lobby or game
        seen_game = False          # Lobby, death screen or game seen since the last stuck reload

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
            self.setup_page(page)
            # Mouse coordinates are relative to the viewport, which is smaller than the window.
            try:
                viewport = tuple(page.evaluate("() => [window.innerWidth, window.innerHeight]"))
            except Exception:
                viewport = WINDOW_SIZE
            self.humanizer = Humanizer(page=page, window_size=viewport)
            h = self.humanizer
            self.open_game(page)
            if self.ws_blocked and not self.reached_game:
                logger.warning(
                    f"{self.log_prefix} Proxy {self.proxy['host'] if self.proxy else 'direct'} was blocked by game server ({self.ws_blocked_reason}); exiting."
                )
                return BAD_PROXY_EXIT

            # Natural post-navigation dwell with ambient drift
            h.sleep(2.5, 0.3, 0.5, min_floor=1.8)
            h.idle_drift(duration=0.8)

            died_at: Optional[float] = None  # When the current death happened, if known

            while True:
                # 0. Follow a proxy IP change from the coordinator
                if self.poll_proxy_update():
                    page = self.switch_proxy(browser, page)
                    died_at = None
                    self.reached_game, failed_reloads, seen_game = False, 0, False
                if self.ws_blocked and not self.reached_game:
                    if self.poll_proxy_update():
                        page = self.switch_proxy(browser, page)
                        died_at = None
                        self.reached_game, failed_reloads, seen_game = False, 0, False
                        continue
                    logger.warning(
                        f"{self.log_prefix} Proxy {self.proxy['host'] if self.proxy else 'direct'} was blocked by game server ({self.ws_blocked_reason}); exiting."
                    )
                    return BAD_PROXY_EXIT
                self.serve_screenshot(page)

                # 1. Dismiss popups / consent
                self.dismiss_overlays_and_consent(page, humanizer=h)

                # 2. Check for Turnstile
                EzSolver.solve(page, log_prefix=self.log_prefix, timeout=5.0, humanizer=h)

                # 3. Check current page state (In lobby, Dead, In game)
                death_btn = page.locator("#deathPlayAgain")
                nick_input = page.locator("#nick")

                death_visible = death_btn.count() > 0 and death_btn.first.is_visible()
                nick_visible = not death_visible and nick_input.count() > 0 and nick_input.first.is_visible()
                if death_visible:
                    self.reached_game = seen_game = True
                    failed_reloads = 0

                if death_visible:
                    self.respawn(page, died_at or time.time())
                    died_at = None
                elif nick_visible:
                    died_at = None
                    self.set_state("lobby")
                    h.sleep(0.8, 0.12, 0.2, min_floor=0.4)
                    logger.info(f"{self.log_prefix} Spawning round #{self.rounds + 1} as '{self.nickname}'")
                    h.click_locator(nick_input.first)
                    h.sleep(0.25, 0.04, 0.06, min_floor=0.15)
                    # The name is typed once; afterwards the field normally still holds it.
                    try:
                        current_name = nick_input.first.input_value()
                    except Exception:
                        current_name = ""
                    if current_name != self.nickname:
                        nick_input.first.fill("")
                        h.sleep(0.15, 0.03, 0.05, min_floor=0.10)
                        h.type_text(self.nickname, typo_prob=0.04)
                        h.sleep(0.45, 0.08, 0.12, min_floor=0.25)
                    self.debug_shot(page, "spawn")
                    try:
                        nick_input.first.press("Enter")
                    except Exception:
                        h.press_key("Enter")
                    try:
                        page.evaluate("""(name) => {
                            const nick = document.getElementById('nick');
                            const isVis = (el) => !!el && !el.hidden && el.style.display !== 'none' && el.offsetParent !== null;
                            if (isVis(nick)) {
                                const win = window.wrappedJSObject || window;
                                if (typeof win.clickPlay === 'function') {
                                    win.clickPlay(name);
                                }
                            }
                        }""", self.nickname)
                    except Exception:
                        pass
                    # Wait up to 3 seconds for the spawn transition into arena
                    spawn_start = time.time()
                    while time.time() - spawn_start < 3.0:
                        if not self.is_out_of_game(page):
                            break
                        h.sleep(0.2, 0.03, 0.05, min_floor=0.1)
                else:
                    # In game or still loading: pressing Enter here could start a game without the name.
                    h.sleep(0.3, 0.05, 0.08, min_floor=0.15)

                # Only steer once the lobby/death screens are gone; otherwise manage watchdog & drift.
                if self.is_out_of_game(page):
                    if not (death_visible or nick_visible):
                        self.set_state("connecting")
                    # Stuck State Watchdog: prevent infinite loops when trapped in lobby
                    if h.check_watchdog(is_stuck=True, max_iterations=60, max_seconds=30.0):
                        logger.warning(
                            f"{self.log_prefix} Stuck state watchdog triggered (trapped out-of-game). Attempting page reload..."
                        )
                        self.debug_shot(page, "stuck")
                        if not seen_game:
                            failed_reloads += 1
                            if failed_reloads >= MAX_FAILED_RELOADS:
                                if self.poll_proxy_update():
                                    continue  # The coordinator already moved this bot; switch instead
                                logger.warning(
                                    f"{self.log_prefix} Proxy {self.proxy['host'] if self.proxy else 'direct'} "
                                    f"trapped out of game ({failed_reloads} consecutive reloads); reporting it as bad."
                                )
                                return BAD_PROXY_EXIT
                        seen_game = False
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=15000)
                            h.reset_watchdog()
                            h.sleep(2.0, 0.3, 0.4, min_floor=1.0)
                        except Exception as e:
                            logger.warning(f"{self.log_prefix} Page reload failed ({e}); restarting the browser.")
                            # A proxy that now refuses connections was most likely rotated out by Webshare.
                            return BAD_PROXY_EXIT if ("PROXY" in str(e).upper() or self.ws_blocked) else None
                        continue

                    h.sleep(0.3, 0.05, 0.08, min_floor=0.15)
                    continue
                else:
                    h.check_watchdog(is_stuck=False)
                    self.reached_game = seen_game = True
                    failed_reloads = 0

                # 4. Gameplay loop for this round
                round_start = time.time()
                current_dir = None

                next_dir, hold_time = pick_next_direction(current_dir)
                h.press_key(next_dir, hesitation=True)
                current_dir = next_dir
                last_turn = time.time()

                logger.info(f"{self.log_prefix} In game! Steering snake (Down/Left biased)...")
                self.set_state("playing", force=True)
                self.debug_shot(page, "ingame")
                next_heartbeat = time.time() + 30
                ticks, slowest_tick, last_tick = 0, 0.0, time.time()
                while True:
                    now = time.time()
                    ticks, slowest_tick, last_tick = ticks + 1, max(slowest_tick, now - last_tick), now
                    if now >= next_heartbeat:
                        # Steering loop timing: ~150ms per tick is normal; much more means the machine is overloaded.
                        logger.info(
                            f"{self.log_prefix} Still in game ({now - round_start:.0f}s this round, "
                            f"avg tick {30000 / max(1, ticks):.0f}ms, slowest {slowest_tick * 1000:.0f}ms)."
                        )
                        self.debug_shot(page, "ingame")
                        next_heartbeat = now + 30  # Not += 30: after a stall (e.g. the machine slept) it would fire every tick to catch up
                        ticks, slowest_tick = 0, 0.0
                    self.set_state("playing")
                    if self.poll_proxy_update():
                        break  # Leave the round; the outer loop reloads on the new IP
                    self.serve_screenshot(page)
                    if self.is_out_of_game(page):
                        self.rounds += 1
                        died_at = time.time()
                        logger.info(f"{self.log_prefix} Round ended after {time.time() - round_start:.1f}s.")
                        # Handle the death screen right away, so the respawn delay isn't eaten by the
                        # consent/Turnstile checks at the top of the outer loop.
                        try:
                            page.locator("#deathPlayAgain:visible, #nick:visible").first.wait_for(timeout=3000)
                        except Exception:
                            break  # Not a normal death (kicked, disconnected...); the outer loop handles it
                        if not page.locator("#deathPlayAgain").first.is_visible():
                            break  # Back at the lobby; the outer loop spawns from there
                        self.respawn(page, died_at)
                        died_at = None
                        if self.is_out_of_game(page):
                            break  # Respawn didn't take; the outer loop sorts it out
                        # Back in game: start the next round right here, turning on the next tick.
                        round_start = time.time()
                        current_dir, last_turn, hold_time = None, 0.0, 0.0
                        next_heartbeat = time.time() + 30
                        ticks, slowest_tick, last_tick = 0, 0.0, time.time()
                        logger.info(f"{self.log_prefix} In game! Steering snake (Down/Left biased)...")
                        self.set_state("playing", force=True)
                        continue

                    now = time.time()
                    if now - last_turn > hold_time:
                        chosen_dir, next_hold = pick_next_direction(current_dir)
                        # Biometric key press with human contact duration and micro-hesitation
                        h.press_key(chosen_dir, hesitation=True)
                        current_dir = chosen_dir
                        # Add organic reaction jitter to next turn interval
                        reaction_jitter = float(np.random.normal(0.0, 0.08))
                        hold_time = max(MIN_HOLD, next_hold + reaction_jitter)
                        last_turn = time.time()

                    # Variable tick interval with Ex-Gaussian jitter instead of rigid 0.15s
                    h.sleep(mean=0.13, sigma=0.015, tau=0.02, min_floor=0.11)

    def run(self) -> Optional[int]:
        """Run until terminated; an error ends the worker so the coordinator restarts it.
        Returns BAD_PROXY_EXIT when the proxy never let the game connect."""
        self.reached_game = False
        try:
            return self.run_session()
        except (KeyboardInterrupt, SystemExit):
            logger.info(f"{self.log_prefix} Terminated, closing browser.")
        except Exception as e:
            logger.error(f"{self.log_prefix} Session error: {e}", exc_info=True)
            if self.ws_blocked or not self.reached_game or any(k in str(e).upper() for k in ("TIMEOUT", "PROXY", "TARGET CLOSED")):
                return BAD_PROXY_EXIT
        return None


# ============================================================================
# Multi-Process Worker Function
# ============================================================================
def kill_processes(procs: List[psutil.Process]):
    """Kill processes (e.g. a worker's browser) that may already be gone."""
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=3)


def process_tree(pid: int) -> List[psutil.Process]:
    """A process's descendants (a worker's Playwright driver and browser processes)."""
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return []


class ReportLogHandler(logging.Handler):
    """Copies a worker's log lines to its agent, for the dashboard's live logs."""

    def __init__(self, bot_id: int, reports: "multiprocessing.Queue"):
        super().__init__(level=logging.INFO)
        self.bot_id = bot_id
        self.reports = reports

    def emit(self, record: logging.LogRecord):
        try:
            self.reports.put_nowait({"bot": self.bot_id, "log": {
                "time": record.created, "level": record.levelname, "message": record.getMessage(),
            }})
        except Exception:
            pass  # Never let log forwarding break the bot


def exit_with_parent():
    """Make sure a worker and its browser never outlive the agent that started them."""
    if sys.platform.startswith("linux"):
        # Fast path: the kernel sends SIGTERM as soon as the agent dies.
        try:
            PR_SET_PDEATHSIG = 1
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        except OSError:
            pass
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch():
        while parent.is_alive():
            time.sleep(2)
        kill_processes(process_tree(os.getpid()))
        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def bot_worker_process(
    instance_id: int,
    room: str,
    proxy: Optional[Dict[str, str]],
    headless: bool,
    start_delay: float,
    nickname: str,
    proxy_updates: "multiprocessing.Queue",
    debug_dir: Optional[str] = None,
    worker_home: Optional[str] = None,
    reports: Optional["multiprocessing.Queue"] = None,
):
    """Entry point for each independent bot worker process with isolated profile and gradual connection."""
    # Forked workers inherit the agent's signal handlers; reset them so a worker never
    # kills its siblings. Ctrl+C is handled by the coordinator, SIGTERM unwinds the Camoufox context.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if hasattr(os, "setsid"):
        # Own process group, so the agent can kill this worker's browser without touching other groups.
        os.setsid()
    exit_with_parent()
    if reports is not None:
        logger.addHandler(ReportLogHandler(instance_id, reports))

    # Re-seed RNG per worker process to guarantee independent stochastic biometrics
    worker_seed = (os.getpid() ^ int(time.time() * 1000) ^ (instance_id << 16)) & 0xFFFFFFFF
    random.seed(worker_seed)
    np.random.seed(worker_seed)

    # Ensure fully isolated HOME directory per worker instance to completely prevent places.sqlite lock contention
    # Unique per worker process, so concurrent runs never share (or delete) each other's HOME.
    worker_home = worker_home or tempfile.mkdtemp(prefix=f"camoufox_worker_{instance_id}_")
    os.makedirs(worker_home, exist_ok=True)
    os.environ["HOME"] = worker_home

    try:
        if start_delay > 0:
            logger.info(f"[Bot-{instance_id}] Waiting {start_delay:.1f}s before connecting (gradual join)...")
            time.sleep(start_delay)

        bot = PowerlineBot(
            instance_id=instance_id,
            room=room,
            proxy=proxy,
            headless=headless,
            nickname=nickname,
            proxy_updates=proxy_updates,
            debug_dir=debug_dir,
            reports=reports,
        )
        exit_code = bot.run()
        if exit_code == BAD_PROXY_EXIT and reports is not None:
            # Say which proxy failed: the coordinator may have moved this bot's group in the meantime.
            reports.put({"bot": instance_id, "bad_proxy": bot.proxy})
    finally:
        shutil.rmtree(worker_home, ignore_errors=True)
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    # Kept so `python3 bot.py ...` still works; the manager now lives in coordinator.py.
    from coordinator import main

    main()
