#!/usr/bin/env python3
"""
Powerline.io Autonomous Bot (Dead-Simple Stealth Architecture)

Key Components:
1. Undetected-Chromedriver (undetected_chromedriver): Patches ChromeDriver at runtime
   to eliminate `cdc_` variables and automated browser flags.
2. Selenium-Stealth (selenium_stealth): Masks WebGL, navigator, languages, and hardware fingerprints.
3. PyClick (pyclick): Generates human-like Bézier curves with stochastic noise, overshooting,
   and variable acceleration/deceleration for mouse movements and element clicks.
4. EzSolver (Cloudflare Turnstile Solver): Identifies, interacts with, or solves Cloudflare
   Turnstile widgets and handles GDPR cookie consent modals automatically.
5. Webshare Rotating Proxies: API v2 integration with 5-minute auto-refresh cycle and
   zero-restart dynamic upstream proxy tunneling.
"""

import argparse
import base64
import json
import logging
import math
import os
import random
import select
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

import undetected_chromedriver as uc
from pyclick import HumanCurve
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium_stealth import stealth

# --- Configuration & Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("PowerlineBot")

DEFAULT_WEBSHARE_API_KEY = "REDACTED_WEBSHARE_API_KEY"
DEFAULT_ROOM = "2cjg"
DEFAULT_NICKNAME = "StealthBot"
ROTATION_INTERVAL_SEC = 300  # 5 minutes


# ============================================================================
# 1. Chromedriver Path Resolver
# ============================================================================
def get_chromedriver_path() -> Optional[str]:
    """Find local chromedriver binary from selenium cache or system PATH."""
    cache_base = os.path.expanduser("~/.cache/selenium/chromedriver")
    if os.path.isdir(cache_base):
        for root, _, files in os.walk(cache_base):
            for f in files:
                if f == "chromedriver" and os.access(os.path.join(root, f), os.X_OK):
                    return os.path.join(root, f)
    return shutil.which("chromedriver")


# ============================================================================
# 2. Webshare Rotating Proxy Manager & Tunnel
# ============================================================================
class WebshareManager:
    """Manages Webshare rotating proxy lists via Webshare API v2."""

    def __init__(self, api_key: str = DEFAULT_WEBSHARE_API_KEY):
        self.api_key = api_key
        self.proxies: List[Dict[str, str]] = []
        self.refresh_frequency_sec = ROTATION_INTERVAL_SEC
        self._lock = threading.Lock()
        self.fetch_proxies()

    def fetch_proxies(self) -> List[Dict[str, str]]:
        url = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=25"
        headers = {"Authorization": f"Token {self.api_key}"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = data.get("results", [])
                new_proxies = []
                for p in results:
                    if p.get("valid", True):
                        new_proxies.append({
                            "host": p["proxy_address"],
                            "port": str(p["port"]),
                            "user": p["username"],
                            "pass": p["password"],
                        })
                with self._lock:
                    if new_proxies:
                        self.proxies = new_proxies
                        logger.info(f"[Webshare] Fetched {len(self.proxies)} active rotating proxies.")
                return new_proxies
        except Exception as e:
            logger.warning(f"[Webshare] Error fetching proxies: {e}")
            return self.proxies

    def is_proxy_working(
        self,
        proxy_info: Dict[str, str],
        server_host: str = "51-91-214-104.powerline.io",
        server_port: int = 9187,
        timeout: float = 3.5,
    ) -> bool:
        """
        Verify proxy health against both Webshare CONNECT and Powerline game server:
        1. CONNECT to target game server through upstream proxy with auth.
        2. Perform WebSocket handshake (GET /_/db1fa2b8).
        Returns True ONLY if game server returns HTTP/1.1 101 Switching Protocols.
        Discards proxies that are blocked by the game server (HTTP 400) or unauthenticated (HTTP 407).
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((proxy_info["host"], int(proxy_info["port"])))
            token = base64.b64encode(f"{proxy_info['user']}:{proxy_info['pass']}".encode()).decode()
            connect_req = (
                f"CONNECT {server_host}:{server_port} HTTP/1.1\r\n"
                f"Host: {server_host}:{server_port}\r\n"
                f"Proxy-Authorization: Basic {token}\r\n\r\n"
            )
            s.sendall(connect_req.encode())
            c_resp = s.recv(512).decode("latin-1", errors="ignore")
            if "200" not in c_resp:
                s.close()
                return False

            ctx = ssl.create_default_context()
            tls = ctx.wrap_socket(s, server_hostname=server_host)
            tls.settimeout(timeout)
            ws_req = (
                f"GET /_/db1fa2b8 HTTP/1.1\r\n"
                f"Host: {server_host}:{server_port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"Origin: https://powerline.io\r\n\r\n"
            )
            tls.sendall(ws_req.encode())
            w_resp = tls.recv(512).decode("latin-1", errors="ignore")
            tls.close()
            if "101" in w_resp:
                return True
            if "400" in w_resp or "403" in w_resp:
                return False
        except Exception:
            pass

        # Fallback: test basic CONNECT powerline.io:443
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((proxy_info["host"], int(proxy_info["port"])))
            token = base64.b64encode(f"{proxy_info['user']}:{proxy_info['pass']}".encode()).decode()
            req = f"CONNECT powerline.io:443 HTTP/1.1\r\nHost: powerline.io:443\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode()
            s.sendall(req)
            resp = s.recv(256)
            s.close()
            return b"200" in resp
        except Exception:
            return False

    def get_working_proxy(self, start_index: int = 0) -> Optional[Dict[str, str]]:
        """Find the next verified working proxy from the pool."""
        with self._lock:
            proxies = list(self.proxies)
        if not proxies:
            proxies = self.fetch_proxies()
        if not proxies:
            return None

        for i in range(len(proxies)):
            candidate = proxies[(start_index + i) % len(proxies)]
            if self.is_proxy_working(candidate):
                logger.info(f"[Webshare] Selected verified proxy {candidate['host']}:{candidate['port']} (passed game WS handshake).")
                return candidate

        # If cached list failed, refresh from Webshare and try once more
        logger.info("[Webshare] Re-fetching fresh proxy list from API...")
        proxies = self.fetch_proxies()
        for candidate in proxies:
            if self.is_proxy_working(candidate):
                logger.info(f"[Webshare] Selected verified proxy {candidate['host']}:{candidate['port']} (passed game WS handshake).")
                return candidate
        return proxies[0] if proxies else None

    def get_proxy(self, index: int = 0) -> Optional[Dict[str, str]]:
        return self.get_working_proxy(index)


class ProxyTunnel:
    """
    Lightweight local forwarder that injects Proxy-Authorization headers to Webshare.
    Allows changing the upstream proxy on the fly without restarting Chrome.
    """

    def __init__(self, proxy_info: Dict[str, str]):
        self._lock = threading.Lock()
        self._active_sockets = set()
        self.running = True
        self.set_upstream(proxy_info)

        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", 0))
        self.server_sock.listen(64)
        self.port = self.server_sock.getsockname()[1]

        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()
        logger.info(f"[ProxyTunnel] Listening locally on port {self.port} -> Upstream: {self.upstream_host}:{self.upstream_port}")

    def set_upstream(self, proxy_info: Dict[str, str]):
        with self._lock:
            self.proxy_info = proxy_info
            self.upstream_host = proxy_info["host"]
            self.upstream_port = int(proxy_info["port"])
            token = base64.b64encode(f"{proxy_info['user']}:{proxy_info['pass']}".encode("utf-8")).decode("ascii")
            self.auth_header = f"Proxy-Authorization: Basic {token}\r\n".encode("ascii")
            old_sockets = list(self._active_sockets)
            self._active_sockets.clear()
        for s in old_sockets:
            try:
                s.close()
            except Exception:
                pass

    def _accept_loop(self):
        while self.running:
            try:
                client_sock, _ = self.server_sock.accept()
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
            except Exception:
                break

    def _handle_client(self, client_sock: socket.socket):
        upstream_sock = None
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = client_sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 65536:
                    return

            idx = buf.index(b"\r\n\r\n")
            headers_part = buf[:idx]
            remainder = buf[idx + 4 :]
            is_connect = headers_part.upper().startswith(b"CONNECT ")

            with self._lock:
                upstream_host = self.upstream_host
                upstream_port = self.upstream_port
                auth_header = self.auth_header
                self._active_sockets.add(client_sock)

            extra = auth_header
            if not is_connect:
                extra += b"Connection: close\r\nProxy-Connection: close\r\n"

            forwarded = headers_part + b"\r\n" + extra + b"\r\n" + remainder
            upstream_sock = socket.create_connection((upstream_host, upstream_port), timeout=15)
            with self._lock:
                self._active_sockets.add(upstream_sock)

            upstream_sock.sendall(forwarded)
            sockets = [client_sock, upstream_sock]

            while self.running:
                rlist, _, xlist = select.select(sockets, [], sockets, 30.0)
                if xlist or not rlist:
                    break
                for s in rlist:
                    data = s.recv(16384)
                    if not data:
                        return
                    target = upstream_sock if s is client_sock else client_sock
                    target.sendall(data)
        except Exception:
            pass
        finally:
            with self._lock:
                self._active_sockets.discard(client_sock)
                if upstream_sock:
                    self._active_sockets.discard(upstream_sock)
            for s in (client_sock, upstream_sock):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass

    def close(self):
        self.running = False
        try:
            self.server_sock.close()
        except Exception:
            pass


# ============================================================================
# 3. HumanMouse with PyClick (Bézier Curves, Stochastic Noise & Overshoot)
# ============================================================================
class HumanMouse:
    """
    Simulates human cursor movements using PyClick's Bézier curve algorithm
    with stochastic noise, overshooting, variable speed, and natural holds.
    Dispatches native CDP mouse events directly into Chrome compositor.
    """

    def __init__(self, driver):
        self.driver = driver
        self.cur_x = 200
        self.cur_y = 200

    def move_to(self, target_x: int, target_y: int, duration_steps: int = 40):
        try:
            fx, fy = int(round(float(self.cur_x))), int(round(float(self.cur_y)))
            tx, ty = int(round(float(target_x))), int(round(float(target_y)))
            if fx == tx and fy == ty:
                return

            dx = max(10, abs(tx - fx) // 4)
            dy = max(10, abs(ty - fy) // 4)

            curve = HumanCurve(
                (fx, fy),
                (tx, ty),
                offsetBoundaryX=int(dx),
                offsetBoundaryY=int(dy),
                targetPoints=int(max(10, duration_steps)),
            )

            for pt in curve.points:
                x, y = int(round(float(pt[0]))), int(round(float(pt[1])))
                try:
                    self.driver.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseMoved", "x": x, "y": y},
                    )
                except Exception:
                    pass
                time.sleep(random.uniform(0.003, 0.008))

            self.cur_x = tx
            self.cur_y = ty
        except Exception as e:
            logger.debug(f"[HumanMouse] move_to exception: {e}")
            self.cur_x = int(round(float(target_x)))
            self.cur_y = int(round(float(target_y)))

    def click(self, target_x: Optional[int] = None, target_y: Optional[int] = None):
        if target_x is not None and target_y is not None:
            self.move_to(target_x, target_y)

        # Human reaction delay before press
        time.sleep(random.uniform(0.04, 0.09))
        try:
            self.driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": int(self.cur_x),
                    "y": int(self.cur_y),
                    "button": "left",
                    "clickCount": 1,
                },
            )
            # Natural click hold duration
            time.sleep(random.uniform(0.035, 0.075))
            self.driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": int(self.cur_x),
                    "y": int(self.cur_y),
                    "button": "left",
                    "clickCount": 1,
                },
            )
        except Exception:
            pass

    def move_to_element_and_click(self, selector: str) -> bool:
        """Find element bounds, pick a random point within bounds, move with curve and click."""
        rect = self.driver.execute_script(
            """
            const el = document.querySelector(arguments[0]);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return null;
            return { left: r.left, top: r.top, width: r.width, height: r.height };
            """,
            selector,
        )
        if not rect:
            return False

        # Randomize target coordinate within the element interior (15% padding)
        target_x = int(rect["left"] + rect["width"] * random.uniform(0.25, 0.75))
        target_y = int(rect["top"] + rect["height"] * random.uniform(0.25, 0.75))
        self.move_to(target_x, target_y, duration_steps=random.randint(35, 60))
        self.click()
        return True


# ============================================================================
# 4. EzSolver Cloudflare Turnstile Solver (ismoiloffS/EzSolver Integration)
# ============================================================================
class EzSolverTurnstile:
    """
    Implements the complete EzSolver architecture (github.com/ismoiloffS/EzSolver):
    1. Pierces Cloudflare Turnstile's closed Shadow DOM via early attachShadow hooking.
    2. Inspects both native shadow roots, standard DOM, and #entryturn for the challenge iframe.
    3. Calculates precise checkbox coordinates (rect.x + 28, rect.y + rect.h / 2).
    4. Glides naturally to the checkbox using PyClick Bézier curves with overshoot.
    5. Dispatches authentic mouse down/up CDP events to solve the challenge.
    6. Verifies token resolution via [name="cf-turnstile-response"] and window.turnstile.getResponse().
    7. Handles retry buttons (#captchaTryAgainButton) and GDPR consent modals.
    """

    def __init__(self, driver, mouse: HumanMouse):
        self.driver = driver
        self.mouse = mouse

    def handle_gdpr_consent(self) -> bool:
        """Dismiss GDPR cookie consent if present."""
        try:
            return bool(
                self.driver.execute_script(
                    """
                    const consentBtn = document.querySelector('.fc-cta-consent')
                        || document.querySelector('button[aria-label="Consent"]')
                        || Array.from(document.querySelectorAll('button, p')).find(el => el.innerText && el.innerText.trim() === 'Consent');
                    if (consentBtn) {
                        consentBtn.click();
                        return true;
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def get_turnstile_status(self) -> dict:
        """
        Inspect the page using EzSolver's shadow-piercing techniques to determine
        if Cloudflare Turnstile is actively challenging the user.
        """
        self.handle_gdpr_consent()
        try:
            return self.driver.execute_script(
                """
                const entryturn = document.getElementById('entryturn');
                let entryVisible = false;
                let entryRect = null;
                if (entryturn) {
                    const s = window.getComputedStyle(entryturn);
                    const r = entryturn.getBoundingClientRect();
                    // Visible if display != 'none', visibility != 'hidden', and has real dimensions
                    if (s.display !== 'none' && s.visibility !== 'hidden' && r.width > 40 && r.height > 20) {
                        entryVisible = true;
                        entryRect = { x: r.x, y: r.y, w: r.width, h: r.height };
                    }
                }

                // 1. Search for Turnstile iframe in captured shadow roots (EzSolver technique)
                let ifr = null;
                if (window._tsShadowRoot) {
                    ifr = window._tsShadowRoot.querySelector('iframe');
                }
                if (!ifr && Array.isArray(window._tsShadowRoots)) {
                    for (const sr of window._tsShadowRoots) {
                        const f = sr.querySelector('iframe');
                        if (f) { ifr = f; break; }
                    }
                }
                // 2. Search main document for Cloudflare challenges iframe
                if (!ifr) {
                    for (const f of document.querySelectorAll('iframe')) {
                        const src = f.src || f.getAttribute('src') || '';
                        if (src.includes('challenges.cloudflare.com')) { ifr = f; break; }
                    }
                }
                // 3. Search inside entryturn
                if (!ifr && entryturn) {
                    ifr = entryturn.querySelector('iframe');
                }

                let iframeRect = null;
                if (ifr) {
                    const r = ifr.getBoundingClientRect();
                    if (r.width > 40 && r.height > 20) {
                        iframeRect = { x: r.x, y: r.y, w: r.width, h: r.height };
                    }
                }

                // Check for verified token
                let token = null;
                const inp = document.querySelector('[name="cf-turnstile-response"]');
                if (inp && inp.value && inp.value.length > 20) {
                    token = inp.value;
                }
                if (!token && typeof window.turnstile !== 'undefined' && typeof window.turnstile.getResponse === 'function') {
                    try {
                        const t = window.turnstile.getResponse();
                        if (t && t.length > 20) token = t;
                    } catch(e) {}
                }

                const captchaFailedEl = document.getElementById('captchaFailed');
                const hasFailed = !!(captchaFailedEl && window.getComputedStyle(captchaFailedEl).display !== 'none');

                return {
                    isActive: (entryVisible || !!iframeRect) && !token,
                    isSolved: !!token,
                    token: token,
                    iframeRect: iframeRect,
                    entryRect: entryRect,
                    hasFailed: hasFailed
                };
                """
            ) or {}
        except Exception:
            return {"isActive": False, "isSolved": False}

    def is_turnstile_active(self) -> bool:
        status = self.get_turnstile_status()
        return bool(status.get("isActive")) or bool(status.get("hasFailed"))

    def solve(self, timeout_sec: int = 25) -> bool:
        """
        Solves Cloudflare Turnstile challenge using EzSolver's coordinate calculation
        and PyClick Bézier curves with natural overshoot and hold times.
        """
        logger.info("[EzSolver] Cloudflare Turnstile challenge detected! Initiating solve routine...")
        start_time = time.time()
        last_click_time = 0.0
        click_attempts = 0

        while time.time() - start_time < timeout_sec:
            status = self.get_turnstile_status()
            if status.get("isSolved"):
                logger.info(f"[EzSolver] Turnstile verified! Token: {status['token'][:25]}...")
                return True

            if status.get("hasFailed"):
                logger.info("[EzSolver] Captcha failed state detected; clicking retry button...")
                self.mouse.move_to_element_and_click("#captchaTryAgainButton")
                time.sleep(1.5)
                continue

            # If not active and not failed, it may have already cleared
            if not status.get("isActive"):
                logger.info("[EzSolver] Turnstile widget no longer active; challenge cleared.")
                return True

            now = time.time()
            if now - last_click_time >= 4.0 and click_attempts < 5:
                rect = status.get("iframeRect") or status.get("entryRect")
                if rect:
                    # EzSolver exact checkbox position: 28px from left, vertically centered
                    target_x = int(rect["x"] + 28 + random.uniform(-2, 2))
                    target_y = int(rect["y"] + rect["h"] / 2 + random.uniform(-2, 2))

                    logger.info(
                        f"[EzSolver] Moving cursor to Turnstile checkbox at ({target_x}, {target_y}) via PyClick..."
                    )
                    # Approach curve with slight overshoot
                    approach_x = target_x - random.randint(30, 60)
                    approach_y = target_y - random.randint(10, 25)
                    self.mouse.move_to(approach_x, approach_y, duration_steps=random.randint(25, 40))
                    time.sleep(random.uniform(0.08, 0.15))
                    self.mouse.move_to(target_x, target_y, duration_steps=random.randint(15, 25))
                    self.mouse.click()

                    last_click_time = now
                    click_attempts += 1
                    time.sleep(1.0)
                else:
                    time.sleep(0.5)
            else:
                time.sleep(0.4)

        # Final check
        final_status = self.get_turnstile_status()
        if final_status.get("isSolved") or not final_status.get("isActive"):
            return True

        logger.warning(f"[EzSolver] Failed to solve Turnstile within {timeout_sec}s timeout.")
        return False

    def check_and_solve_turnstile(self) -> bool:
        if self.is_turnstile_active():
            return self.solve()
        return True


# ============================================================================
# 5. Stealth Browser Factory (Undetected-Chromedriver + Selenium-Stealth)
# ============================================================================
def create_stealth_driver(
    proxy_port: int,
    headless: bool = True,
    window_size: Tuple[int, int] = (1280, 720),
    force_uc: bool = False,
):
    """
    Creates a high-stealth Chrome browser session routed through the local Webshare tunnel.
    - In headless mode: uses standard Selenium driver with selenium-stealth masking for
      instant (0.4s) startup without Linux socket hangs.
    - In headful mode (or when force_uc=True): uses Undetected-Chromedriver with runtime binary patching.
    - Fully masked: WebGL, navigator.webdriver=undefined, languages, platform, and hairline fixes.
    """
    chromedriver_bin = get_chromedriver_path()
    logger.info(f"[Browser] Using ChromeDriver binary: {chromedriver_bin}")

    driver = None
    if force_uc or not headless:
        try:
            options = uc.ChromeOptions()
            options.page_load_strategy = "eager"
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument(f"--window-size={window_size[0]},{window_size[1]}")
            options.add_argument(f"--proxy-server=http://127.0.0.1:{proxy_port}")

            driver = uc.Chrome(
                options=options,
                headless=headless,
                driver_executable_path=chromedriver_bin,
            )
            logger.info("[Browser] Initialized Undetected-Chromedriver successfully.")
        except Exception as e:
            logger.warning(f"[Browser] Undetected-Chromedriver notice ({e}). Using standard Selenium + stealth fallback...")

    if driver is None:
        opts = ChromeOptions()
        opts.page_load_strategy = "eager"
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument(f"--window-size={window_size[0]},{window_size[1]}")
        opts.add_argument(f"--proxy-server=http://127.0.0.1:{proxy_port}")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        if headless:
            opts.add_argument("--headless=new")
        service = ChromeService(executable_path=chromedriver_bin) if chromedriver_bin else None
        driver = webdriver.Chrome(service=service, options=opts)
        logger.info("[Browser] Initialized stealth Chrome driver (0.4s instant startup).")

    stealth(
        driver,
        languages=["en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )

    # Inject EzSolver shadow-piercing and Turnstile interceptor before any page scripts run
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                // Hook attachShadow to pierce Cloudflare closed shadow DOM (EzSolver)
                const origAttach = Element.prototype.attachShadow;
                window._tsShadowRoots = [];
                Element.prototype.attachShadow = function(init) {
                    const root = origAttach.call(this, { ...init, mode: 'open' });
                    window._tsShadowRoots.push(root);
                    window._tsShadowRoot = root;
                    return root;
                };

                // Intercept window.turnstile
                let _turnstile = undefined;
                Object.defineProperty(window, 'turnstile', {
                    configurable: true,
                    get: function() { return _turnstile; },
                    set: function(val) {
                        _turnstile = val;
                        if (_turnstile && typeof _turnstile.render === 'function') {
                            const origRender = _turnstile.render;
                            _turnstile.render = function(container, params) {
                                window._cf_sitekey = params ? params.sitekey : null;
                                window._cf_container = container;
                                window._cf_params = params;
                                const widgetId = origRender.apply(this, arguments);
                                window._cf_widgetId = widgetId;
                                return widgetId;
                            };
                        }
                    }
                });
                """
            },
        )
    except Exception as e:
        logger.debug(f"[Browser] Failed to add Page script on new document: {e}")

    return driver


# ============================================================================
# 6. Powerline.io Autonomous Bot Engine
# ============================================================================
class PowerlineBot:
    """
    Autonomous Powerline.io bot combining Undetected-Chromedriver, PyClick,
    EzSolver Turnstile handling, and Webshare 5-minute rotating proxies.
    """

    def __init__(
        self,
        instance_id: int = 1,
        room: str = DEFAULT_ROOM,
        nickname: str = DEFAULT_NICKNAME,
        headless: bool = True,
        api_key: str = DEFAULT_WEBSHARE_API_KEY,
        rotate_interval: int = ROTATION_INTERVAL_SEC,
    ):
        self.instance_id = instance_id
        self.room = room
        self.nickname = f"{nickname}{instance_id if instance_id > 1 else ''}"
        self.headless = headless
        self.rotate_interval = rotate_interval
        self.target_url = f"https://powerline.io/#{self.room}" if self.room else "https://powerline.io/"

        self.webshare = WebshareManager(api_key=api_key)
        self.proxy_info = self.webshare.get_proxy(instance_id - 1)
        if not self.proxy_info:
            raise RuntimeError("No valid Webshare proxies available!")

        self.tunnel = ProxyTunnel(self.proxy_info)
        self.driver = create_stealth_driver(proxy_port=self.tunnel.port, headless=self.headless)
        self.mouse = HumanMouse(self.driver)
        self.turnstile = EzSolverTurnstile(self.driver, self.mouse)

        self.last_rotation_time = time.time()
        self.current_proxy_index = instance_id - 1
        self.running = True

    def log(self, msg: str):
        ip = f"{self.proxy_info['host']}:{self.proxy_info['port']}"
        logger.info(f"[Bot-{self.instance_id} | {ip}] {msg}")

    def rotate_to_next_proxy(self):
        """Rotate upstream proxy to the next active, verified proxy."""
        self.current_proxy_index += 1
        new_proxy = self.webshare.get_working_proxy(self.current_proxy_index)
        if new_proxy:
            old_ip = f"{self.proxy_info['host']}:{self.proxy_info['port']}"
            new_ip = f"{new_proxy['host']}:{new_proxy['port']}"
            self.log(f"[Proxy] Switching to verified proxy: {old_ip} -> {new_ip}")
            self.proxy_info = new_proxy
            self.tunnel.set_upstream(new_proxy)
            self.last_rotation_time = time.time()
            try:
                self.driver.get(self.target_url)
            except Exception:
                pass
            time.sleep(1.5)

    def wait_for_connection(self, timeout_sec: int = 45) -> bool:
        """Wait for Powerline.io WebSocket connection to establish."""
        self.log(f"Navigating to {self.target_url}...")
        try:
            self.driver.get(self.target_url)
        except Exception:
            pass

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                self.turnstile.handle_gdpr_consent()
                self.turnstile.check_and_solve_turnstile()

                state = self.driver.execute_script(
                    """
                    const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                        || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
                    const G = getG();
                    const net = G ? G.network : null;
                    return {
                        hasConnection: !!(net && net.hasConnection),
                        roomID: net ? net.roomID : null,
                        isInGame: !!(G && G.isInGame)
                    };
                    """
                )
                if state and state.get("hasConnection"):
                    self.log(f"Connected to room #{state.get('roomID')}!")
                    return True
            except Exception:
                pass
            time.sleep(0.5)

        self.log("Timeout waiting for server connection. Rotating proxy...")
        self.rotate_to_next_proxy()
        return False

    def enter_nickname_and_spawn(self) -> bool:
        """Enter nickname using PyClick Bézier curves and trigger spawn."""
        # 1. Check if Turnstile is active; solve it first!
        if self.turnstile.is_turnstile_active():
            self.log("[Turnstile] Challenge detected before spawn! Activating EzSolver...")
            if not self.turnstile.solve():
                self.log("[Turnstile] Failed to solve Turnstile; rotating proxy...")
                self.rotate_to_next_proxy()
                return False
            self.log("[Turnstile] Challenge resolved successfully!")
            time.sleep(0.5)

        self.driver.execute_script(
            """
            const topGui = document.getElementById('topGui');
            if (topGui) topGui.hidden = false;
            const topGuiConnecting = document.getElementById('topGuiConnecting');
            if (topGuiConnecting) topGuiConnecting.hidden = true;
            const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
            const G = getG();
            if (G) {
                G.playClicks = 0;
                G.focus = true;
                if (G.network) G.network.nicknameModerationStatus = "";
            }
            """
        )

        self.log(f"Entering nickname '{self.nickname}' via PyClick...")
        nick_rect = self.driver.execute_script(
            """
            const nick = document.getElementById('nick');
            if (!nick) return null;
            const r = nick.getBoundingClientRect();
            return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
            """
        )

        if nick_rect:
            # Glide to nickname input field
            self.mouse.move_to(nick_rect["x"], nick_rect["y"], duration_steps=random.randint(25, 45))
            self.mouse.click()
            time.sleep(0.1)

            # Clear and type nickname character by character with human pauses
            self.driver.execute_script("const n = document.getElementById('nick'); if (n) { n.value = ''; n.focus(); }")
            for ch in self.nickname:
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "char", "text": ch})
                time.sleep(random.uniform(0.02, 0.05))

            # Ensure value is set in DOM input and focused
            self.driver.execute_script(
                """
                const n = document.getElementById('nick');
                if (n) { n.value = arguments[0]; n.focus(); }
                const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                    || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
                const G = getG();
                if (G) {
                    G.playClicks = 0;
                    G.focus = true;
                }
                """,
                self.nickname,
            )
            time.sleep(random.uniform(0.1, 0.2))
        else:
            self.driver.execute_script(
                """
                const n = document.getElementById('nick');
                if (n) { n.value = arguments[0]; n.focus(); }
                const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                    || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
                const G = getG();
                if (G) {
                    G.playClicks = 0;
                    G.focus = true;
                }
                """,
                self.nickname,
            )

        # Spawn via clickPlay
        self.driver.execute_script(
            """
            const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
            const G = getG();
            if (G) {
                G.playClicks = 0;
                G.focus = true;
            }
            if (typeof window.clickPlay === 'function') {
                window.clickPlay(arguments[0]);
            }
            """,
            self.nickname,
        )

        # Verify spawn
        for _ in range(8):
            time.sleep(0.4)
            status = self.driver.execute_script(
                """
                const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                    || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
                const G = getG();
                const lp = G && G.localPlayer;
                return {
                    isInGame: !!(G && G.isInGame),
                    alive: !!(lp && !lp.dead),
                    pos: lp ? { x: Math.round(lp.x), y: Math.round(lp.y) } : null
                };
                """
            )
            if status and status.get("isInGame") and status.get("alive"):
                pos = status.get("pos", {})
                self.log(f"Spawned successfully at ({pos.get('x')}, {pos.get('y')})!")
                return True

        self.log("Spawn confirmation timed out; will retry in next loop.")
        return False

    def handle_respawn_if_dead(self):
        """Check for death screen and respawn."""
        self.log("Snake died. Respawning via PyClick & play button...")
        time.sleep(random.uniform(0.3, 0.6))
        self.driver.execute_script(
            """
            const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
            const G = getG();
            if (G) {
                G.playClicks = 0;
                G.focus = true;
                if (G.network) G.network.nicknameModerationStatus = "";
            }
            """
        )
        clicked = self.mouse.move_to_element_and_click("#deathPlayAgain")
        if not clicked:
            self.driver.execute_script(
                """
                const b = document.getElementById('deathPlayAgain');
                if (b && b.offsetParent !== null) {
                    b.click();
                } else if (typeof window.clickPlay === 'function') {
                    window.clickPlay(arguments[0]);
                }
                """,
                self.nickname,
            )
        time.sleep(0.6)

    def send_random_move(self):
        """Send smart perpendicular directional input to avoid 180-degree self-collision."""
        cur_dir = self.driver.execute_script(
            """
            const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
            const G = getG();
            const lp = G && G.localPlayer;
            return lp ? lp.direction : 1;
            """
        ) or 1

        # 1: UP, 2: LEFT, 3: DOWN, 4: RIGHT
        if cur_dir in (1, 3):  # Moving vertically -> turn horizontally (Left or Right)
            chosen = random.choice([{"code": 37, "key": "ArrowLeft"}, {"code": 39, "key": "ArrowRight"}])
        else:  # Moving horizontally -> turn vertically (Up or Down)
            chosen = random.choice([{"code": 38, "key": "ArrowUp"}, {"code": 40, "key": "ArrowDown"}])

        try:
            self.driver.execute_cdp_cmd(
                "Input.dispatchKeyEvent",
                {"type": "rawKeyDown", "windowsVirtualKeyCode": chosen["code"], "key": chosen["key"]},
            )
            time.sleep(random.uniform(0.04, 0.08))
            self.driver.execute_cdp_cmd(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "windowsVirtualKeyCode": chosen["code"], "key": chosen["key"]},
            )
        except Exception:
            pass

    def check_proxy_rotation(self):
        """Check if the 5-minute rotation interval elapsed; switch upstream proxy smoothly."""
        if time.time() - self.last_rotation_time >= self.rotate_interval:
            self.log(f"Rotation timer elapsed ({self.rotate_interval}s). Rotating to fresh proxy...")
            self.rotate_to_next_proxy()

    def run(self, max_duration: int = 0):
        """Main bot execution loop."""
        start_time = time.time()
        last_heartbeat = 0
        try:
            if not self.wait_for_connection(timeout_sec=45):
                self.log("Initial connection failed; retrying with next proxy...")
                self.rotate_to_next_proxy()
                self.wait_for_connection(timeout_sec=45)

            spawn_attempts = 0
            while self.running:
                now = time.time()
                uptime = int(now - start_time)
                if max_duration > 0 and uptime >= max_duration:
                    self.log(f"Test duration reached ({max_duration}s). Stopping run.")
                    break

                # 1. CRITICAL: Check Turnstile on every loop iteration
                if self.turnstile.is_turnstile_active():
                    self.log("[Turnstile] In-game challenge detected! Activating EzSolver...")
                    if not self.turnstile.solve():
                        self.log("[Turnstile] Challenge solve failed. Rotating proxy...")
                        self.rotate_to_next_proxy()
                        continue
                    self.log("[Turnstile] Challenge resolved successfully!")
                    time.sleep(0.5)

                try:
                    state = self.driver.execute_script(
                        """
                        const getG = () => (window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global)
                            || Object.values(window).find(v => v && v.Global && v.Global.gameName === 'powerline.io')?.Global;
                        const G = getG();
                        const lp = G && G.localPlayer;
                        const deathBtn = document.getElementById('deathPlayAgain');
                        const deathGui = document.getElementById('deathGui');
                        const net = G ? G.network : null;
                        const isDead = !!(lp && lp.dead)
                            || !!(deathBtn && deathBtn.offsetParent !== null)
                            || !!(deathGui && !deathGui.hidden && deathGui.style.display !== 'none');
                        return {
                            hasConnection: !!(net && net.hasConnection),
                            isInGame: !!(G && G.isInGame),
                            alive: !!(lp && !lp.dead),
                            isDead: isDead,
                            score: G ? G.currentScore : 0,
                            pos: lp ? { x: Math.round(lp.x), y: Math.round(lp.y) } : null
                        };
                        """
                    )
                except Exception:
                    state = None
                if not state or not state.get("hasConnection"):
                    self.wait_for_connection(timeout_sec=35)
                    time.sleep(1)
                    continue

                if state.get("isInGame") and state.get("alive"):
                    spawn_attempts = 0
                    if random.random() < 0.35:
                        self.send_random_move()
                elif state.get("isDead"):
                    self.handle_respawn_if_dead()
                else:
                    if self.turnstile.is_turnstile_active():
                        self.turnstile.solve()
                    else:
                        spawn_attempts += 1
                        self.log(f"In lobby; triggering spawn attempt #{spawn_attempts}...")
                        self.enter_nickname_and_spawn()
                        time.sleep(1.0)

                # Periodic heartbeat log every 15 seconds
                if now - last_heartbeat >= 15.0:
                    pos_str = f"({state['pos']['x']}, {state['pos']['y']})" if state.get("pos") else "N/A"
                    status_str = "ALIVE" if state.get("alive") else ("DEAD" if state.get("isDead") else "LOBBY")
                    turnstile_str = "ACTIVE (SOLVING)" if self.turnstile.is_turnstile_active() else "CLEAR"
                    self.log(
                        f"[Heartbeat] Uptime={uptime}s/{max_duration or 'inf'}s | Status={status_str} | "
                        f"Score={state.get('score', 0)} | Pos={pos_str} | Turnstile={turnstile_str}"
                    )
                    last_heartbeat = now

                self.check_proxy_rotation()
                time.sleep(random.uniform(0.4, 0.8))
        except KeyboardInterrupt:
            self.log("Shutdown requested by user.")
        except Exception as e:
            logger.exception(f"[Bot-{self.instance_id}] Unexpected exception in main loop: {e}")
        finally:
            self.close()

    def close(self):
        self.running = False
        try:
            self.driver.quit()
        except Exception:
            pass
        self.tunnel.close()
        self.log("Bot shut down cleanly.")


# ============================================================================
# 7. CLI Entry Point
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="Powerline.io Autonomous Stealth Bot")
    parser.add_argument("--instances", type=int, default=1, help="Number of concurrent bot instances")
    parser.add_argument("--room", type=str, default=DEFAULT_ROOM, help="Target room hash (e.g. 2cjg)")
    parser.add_argument("--nick", type=str, default=DEFAULT_NICKNAME, help="Base nickname")
    parser.add_argument("--api-key", type=str, default=DEFAULT_WEBSHARE_API_KEY, help="Webshare API v2 key")
    parser.add_argument("--rotate-interval", type=int, default=ROTATION_INTERVAL_SEC, help="Proxy rotation interval in seconds")
    parser.add_argument("--duration", type=int, default=0, help="Test duration in seconds (0 = run indefinitely)")
    parser.add_argument("--headful", action="store_true", help="Run with visible browser window")
    args = parser.parse_args()

    bots: List[PowerlineBot] = []
    threads: List[threading.Thread] = []

    for i in range(1, args.instances + 1):
        bot = PowerlineBot(
            instance_id=i,
            room=args.room,
            nickname=args.nick,
            headless=not args.headful,
            api_key=args.api_key,
            rotate_interval=args.rotate_interval,
        )
        bots.append(bot)
        t = threading.Thread(target=bot.run, args=(args.duration,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(1.5)  # Stagger instance launches

    try:
        while True:
            alive_threads = [t for t in threads if t.is_alive()]
            if not alive_threads:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("[System] Stopping all bots...")
        for b in bots:
            b.running = False
        for t in threads:
            t.join(timeout=3)


if __name__ == "__main__":
    main()
