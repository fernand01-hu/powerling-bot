#!/usr/bin/env python3
"""
Humanized Powerline.io Multi-Instance Bot
-----------------------------------------
Features:
- Ultra-Low Proxy Data Mode (~95%+ reduction without triggering Cloudflare Turnstile):
  * Pre-seeds a local HTTP disk cache on your direct connection once (`ensure_http_cache_seed`),
    then gives each bot its own copy via `--disk-cache-dir` while keeping `--user-data-dir`
    (cookies & localStorage) 100% isolated per bot.
  * Result: 2.5 MB `index.x_1H9pPP.mjs`, fonts, CSS, and PNG sprites are loaded from disk
    cache (`transferSize = 0` bytes over proxy), while all `powerline.io`, `challenges.cloudflare.com`,
    and game WebSocket traffic share the exact same Webshare proxy IP!
  * Blocks ad networks, analytics, and audio files via CDP `Network.setBlockedURLs`
  * Live proxy bandwidth counter (KB used per bot)
- Window Focus Protection & Minimize Mode:
  * Prevents Chrome from stealing focus from your currently active window (via xprop/wmctrl)
  * Optional `--minimize` (`-m`) flag to minimize all bot windows right after connection
    while keeping full 60fps execution in the background
- Server / Region / Room selection (`--server` and `--list-servers`)
- Human-like behavior (CDP stealth, staggered startup, natural typing, keydown/keyup hold
  times, Arrow/WASD mix, CW/CCW circle variation, and reaction delays)
"""

import argparse
import base64
import concurrent.futures
import json
import os
import random
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# Clockwise circle sequence:
# 1 = UP, 4 = RIGHT, 3 = DOWN, 2 = LEFT
CIRCLE_DIRS_ARROW = [
    (1, "UP", "ArrowUp", 38),
    (4, "RIGHT", "ArrowRight", 39),
    (3, "DOWN", "ArrowDown", 40),
    (2, "LEFT", "ArrowLeft", 37),
]

CIRCLE_DIRS_WASD = [
    (1, "UP", "KeyW", 87),
    (4, "RIGHT", "KeyD", 68),
    (3, "DOWN", "KeyS", 83),
    (2, "LEFT", "KeyA", 65),
]

REGION_ALIASES = {
    "eu": "europe",
    "eur": "europe",
    "europe": "europe",
    "na": "north america",
    "us": "north america",
    "usa": "north america",
    "north america": "north america",
    "sa": "south america",
    "br": "south america",
    "south america": "south america",
    "as": "asia",
    "asia": "asia",
    "jp": "asia",
    "sg": "asia",
}

BLOCKED_URL_PATTERNS = [
    "*vntsm.com*",
    "*venatusmedia.com*",
    "*ccgateway.net*",
    "*doubleclick.net*",
    "*googlesyndication.com*",
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*fundingchoicesmessages.google.com*",
    "*criteo.com*",
    "*amazon-adsystem.com*",
    "*fastclick.net*",
    "*hadronid.net*",
    "*affec.tv*",
    "*mixpanel.com*",
    "*id5-sync.com*",
    "*atmtd.com*",
    "*clean.gg*",
    "*dotomi.com*",
    "*rubiconproject.com*",
    "*crwdcntrl.net*",
    "*adsrvr.org*",
    "*cpx.to*",
    "*adinplay.com*",
    "*playwire.com*",
    "*fonts.googleapis.com*",
    "*fonts.gstatic.com*",
    "*.ogg*",
    "*.mp3*",
    "*.wav*",
]

HTTP_CACHE_SEED_DIR = "/tmp/powerline_http_cache_seed"
_focus_lock = threading.Lock()


def get_active_window_id():
    """Return the X11 window ID (hex string) of the user's currently focused window."""
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
        if "#" in out:
            win_id = out.split("#")[-1].strip().split(",")[0].strip()
            if win_id and win_id != "0x0":
                return win_id
    except Exception:
        pass
    return None


def restore_window_focus(win_id):
    """Restore X11 focus back to `win_id` using wmctrl so Chrome doesn't steal focus."""
    if not win_id:
        return
    try:
        subprocess.run(
            ["wmctrl", "-i", "-a", win_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
    except Exception:
        pass


def ensure_http_cache_seed():
    """
    Populate `/tmp/powerline_http_cache_seed` once so that static files
    (`index.x_1H9pPP.mjs` 2.5MB, `.css`, `.png`, `.ttf`) are cached on disk and
    bots only use ~90 KB of proxy bandwidth instead of ~4-5 MB.
    """
    default_cache = os.path.join(HTTP_CACHE_SEED_DIR, "Default", "Cache")
    if os.path.isdir(default_cache) and os.listdir(default_cache):
        return HTTP_CACHE_SEED_DIR

    print("[*] Warming local HTTP static asset cache (one-time fetch to save ~95% proxy data)...")
    shutil.rmtree(HTTP_CACHE_SEED_DIR, ignore_errors=True)
    os.makedirs(HTTP_CACHE_SEED_DIR, exist_ok=True)

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument(f"--disk-cache-dir={HTTP_CACHE_SEED_DIR}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=opts)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": BLOCKED_URL_PATTERNS})
        driver.get("https://powerline.io/")
        time.sleep(3.5)
    finally:
        driver.quit()
    print("[+] Static asset cache ready (bots will load 2.5MB game bundle from local disk cache).")
    return HTTP_CACHE_SEED_DIR


class LocalProxyForwarder:
    """
    Local HTTP/HTTPS/WSS proxy forwarder that:
    - Injects Proxy-Authorization headers for an upstream `host:port:user:pass` proxy
    - Supports live upstream IP switching without restarting Chrome
    - Enforces a strict allowlist of game & Turnstile hosts + game WebSocket ports (8000-8999)
      so Chrome background binary downloads (on port 80/443 raw IPs) and ad networks consume 0 bytes
    - Tracks exact proxy bandwidth consumed (in bytes)
    """

    ALLOWED_HOST_SUFFIXES = (
        "powerline.io",
        "sdg.games",
        "challenges.cloudflare.com",
        "code.jquery.com",
    )

    def __init__(self, proxy_info):
        self._lock = threading.Lock()
        self._active_sockets = set()
        self.proxy_bytes_used = 0
        self.set_upstream(proxy_info)

        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", 0))
        self.server_sock.listen(128)
        self.local_port = self.server_sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def set_upstream(self, proxy_info):
        """Switch upstream Webshare proxy dynamically and drop existing proxy sockets."""
        with self._lock:
            self.proxy_info = proxy_info
            self.upstream_host = proxy_info["host"]
            self.upstream_port = int(proxy_info["port"])
            token = base64.b64encode(
                f"{proxy_info['user']}:{proxy_info['pass']}".encode("utf-8")
            ).decode("ascii")
            self.auth_header = f"Proxy-Authorization: Basic {token}\r\n".encode("ascii")
            old_sockets = list(self._active_sockets)
            self._active_sockets.clear()

        for s in old_sockets:
            try:
                s.close()
            except Exception:
                pass

    def _extract_target_host_port(self, first_line_bytes):
        try:
            line = first_line_bytes.decode("latin-1", errors="ignore").strip()
            parts = line.split(" ")
            if len(parts) < 2:
                return None, None
            target = parts[1]
            if parts[0].upper() == "CONNECT":
                if ":" in target:
                    h, p = target.rsplit(":", 1)
                    return h.lower(), int(p)
                return target.lower(), 443
            parsed = urlparse(target)
            host = (parsed.hostname or "").lower()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return host, int(port)
        except Exception:
            return None, None

    def _is_blocked_host(self, host, port):
        if not host:
            return True
        # Allow raw IPv4 addresses ONLY on Powerline.io game WebSocket ports (8000-8999)
        parts = host.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            return not (port is not None and 8000 <= port <= 8999)
        # Allow only Powerline.io, master server (sdg.games), Cloudflare Turnstile, and jQuery
        allowed = any(
            host == suffix or host.endswith("." + suffix)
            for suffix in self.ALLOWED_HOST_SUFFIXES
        )
        return not allowed

    def _accept_loop(self):
        while self._running:
            try:
                client_sock, _ = self.server_sock.accept()
                threading.Thread(
                    target=self._handle_client, args=(client_sock,), daemon=True
                ).start()
            except Exception:
                break

    def _handle_client(self, client_sock):
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

            header_end = buf.index(b"\r\n\r\n")
            headers_part = buf[:header_end]
            remainder = buf[header_end + 4 :]
            first_line = headers_part.split(b"\r\n", 1)[0]
            target_host, target_port = self._extract_target_host_port(first_line)

            if self._is_blocked_host(target_host, target_port):
                client_sock.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return

            with self._lock:
                upstream_host = self.upstream_host
                upstream_port = self.upstream_port
                auth_header = self.auth_header
                self._active_sockets.add(client_sock)

            is_connect = headers_part.upper().startswith(b"CONNECT ")
            extra_headers = auth_header
            if not is_connect:
                extra_headers += b"Connection: close\r\nProxy-Connection: close\r\n"

            forwarded_initial = (
                headers_part + b"\r\n" + extra_headers + b"\r\n" + remainder
            )

            upstream_sock = socket.create_connection(
                (upstream_host, upstream_port), timeout=15
            )
            with self._lock:
                self._active_sockets.add(upstream_sock)
                self.proxy_bytes_used += len(forwarded_initial)

            upstream_sock.sendall(forwarded_initial)

            sockets = [client_sock, upstream_sock]
            while self._running:
                rlist, _, xlist = select.select(sockets, [], sockets, 30.0)
                if xlist or not rlist:
                    break
                for s in rlist:
                    data = s.recv(16384)
                    if not data:
                        return
                    with self._lock:
                        self.proxy_bytes_used += len(data)
                    target = upstream_sock if s is client_sock else client_sock
                    target.sendall(data)
        except Exception:
            pass
        finally:
            with self._lock:
                self._active_sockets.discard(client_sock)
                if upstream_sock is not None:
                    self._active_sockets.discard(upstream_sock)
            for s in (client_sock, upstream_sock):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

    def get_kb_used(self):
        with self._lock:
            return self.proxy_bytes_used / 1024.0

    def close(self):
        self._running = False
        try:
            self.server_sock.close()
        except Exception:
            pass
        with self._lock:
            old_sockets = list(self._active_sockets)
            self._active_sockets.clear()
        for s in old_sockets:
            try:
                s.close()
            except Exception:
                pass


FLAGGED_IPS_FILE = os.path.join(tempfile.gettempdir(), "powerline_flagged_ips.txt")


class ProxyPool:
    """
    Coordinates proxy IP assignment across bots so that:
    - Each proxy IP is used by at most `bots_per_ip` bots concurrently
    - Automatically skips IPs recently flagged by Cloudflare Turnstile
    - Provides a per-IP handshake lock (`get_ip_lock`) so bots sharing the same IP
      do not hit Cloudflare Turnstile at the exact same millisecond
    - When a bot rotates IP, it moves to a clean proxy IP with available capacity (< `bots_per_ip`).
    """

    def __init__(self, proxies, bots_per_ip=2):
        self.proxies = proxies
        self.bots_per_ip = max(1, bots_per_ip)
        self._lock = threading.Lock()
        self._flagged_hosts = self._load_flagged_hosts()
        self._ip_locks = {}
        self._bot_assigned_host = {}
        self._bot_visited_hosts = {}

    def get_ip_lock(self, proxy_info):
        if not proxy_info:
            return threading.Lock()
        with self._lock:
            host = proxy_info["host"]
            if host not in self._ip_locks:
                self._ip_locks[host] = threading.Lock()
            return self._ip_locks[host]

    def _load_flagged_hosts(self):
        flagged = set()
        try:
            if os.path.exists(FLAGGED_IPS_FILE):
                if time.time() - os.path.getmtime(FLAGGED_IPS_FILE) < 600:
                    with open(FLAGGED_IPS_FILE, "r", encoding="utf-8") as f:
                        for line in f:
                            h = line.strip()
                            if h:
                                flagged.add(h)
        except Exception:
            pass
        return flagged

    def is_flagged(self, proxy_info):
        if not proxy_info:
            return False
        with self._lock:
            return proxy_info["host"] in self._flagged_hosts

    def mark_flagged(self, proxy_info):
        if not proxy_info:
            return
        with self._lock:
            host = proxy_info["host"]
            self._flagged_hosts.add(host)
            try:
                with open(FLAGGED_IPS_FILE, "a", encoding="utf-8") as f:
                    f.write(host + "\n")
            except Exception:
                pass

    def _host_active_count(self, host, exclude_instance_id=None):
        return sum(
            1
            for inst_id, h in self._bot_assigned_host.items()
            if h == host and inst_id != exclude_instance_id
        )

    def _pick_best_proxy(self, instance_id):
        visited = self._bot_visited_hosts.setdefault(instance_id, set())
        cur_host = self._bot_assigned_host.get(instance_id)

        clean = [p for p in self.proxies if p["host"] not in self._flagged_hosts]
        candidates = clean if clean else self.proxies

        if len(visited) >= len(candidates):
            visited.clear()

        # 1. Prefer unvisited clean proxy IPs that currently have < bots_per_ip active bots
        for p in candidates:
            h = p["host"]
            if h != cur_host and h not in visited and self._host_active_count(h, instance_id) < self.bots_per_ip:
                self._bot_assigned_host[instance_id] = h
                visited.add(h)
                return p

        # 2. Any clean proxy IP that currently has < bots_per_ip active bots
        for p in candidates:
            h = p["host"]
            if h != cur_host and self._host_active_count(h, instance_id) < self.bots_per_ip:
                self._bot_assigned_host[instance_id] = h
                visited.add(h)
                return p

        # 3. Fallback: proxy with the fewest active bots
        best = min(
            candidates,
            key=lambda p: (p["host"] == cur_host, self._host_active_count(p["host"], instance_id)),
        )
        self._bot_assigned_host[instance_id] = best["host"]
        visited.add(best["host"])
        return best

    def get_initial_proxy(self, instance_id, num_instances=4):
        if not self.proxies:
            return None
        with self._lock:
            return self._pick_best_proxy(instance_id)

    def rotate_proxy(self, instance_id, num_instances=4):
        if not self.proxies:
            return None
        with self._lock:
            return self._pick_best_proxy(instance_id)

    def release_bot(self, instance_id):
        with self._lock:
            self._bot_assigned_host.pop(instance_id, None)


def load_proxies(proxy_file):
    """Parse lines of `host:port:username:password` from the Webshare proxy file."""
    if not proxy_file or not os.path.exists(proxy_file):
        return []
    proxies = []
    with open(proxy_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) == 4:
                proxies.append(
                    {
                        "host": parts[0],
                        "port": int(parts[1]),
                        "user": parts[2],
                        "pass": parts[3],
                    }
                )
    return proxies


def check_proxy_game_server(proxy_info, server_host, server_port, timeout=3.5):
    """
    Test if proxy_info can establish a TLS WebSocket handshake with the target game server.
    Returns (proxy_info, is_ok, reason_str).
    """
    h, pt, u, pw = (
        proxy_info["host"],
        int(proxy_info["port"]),
        proxy_info["user"],
        proxy_info["pass"],
    )
    try:
        s = socket.create_connection((h, pt), timeout=timeout)
        token = base64.b64encode(f"{u}:{pw}".encode()).decode()
        connect_req = (
            f"CONNECT {server_host}:{server_port} HTTP/1.1\r\n"
            f"Host: {server_host}:{server_port}\r\n"
            f"Proxy-Authorization: Basic {token}\r\n\r\n"
        )
        s.sendall(connect_req.encode())
        connect_resp = s.recv(4096).decode("latin-1", errors="ignore")
        if "200" not in connect_resp:
            s.close()
            reason = connect_resp.split("\r\n")[0] if connect_resp else "CONNECT failed"
            return proxy_info, False, reason

        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(s, server_hostname=server_host)
        tls.settimeout(timeout)
        ws_req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {server_host}:{server_port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: https://powerline.io\r\n\r\n"
        )
        tls.sendall(ws_req.encode())
        resp = tls.recv(4096).decode("latin-1", errors="ignore")
        tls.close()
        if "101" in resp:
            return proxy_info, True, "OK"
        else:
            reason = resp.split("\r\n")[0] if resp else "Empty response"
            return proxy_info, False, reason
    except Exception as e:
        return proxy_info, False, str(e)


def filter_working_proxies(proxies, target_server_host, target_server_port, timeout=3.5):
    """
    Quickly test all proxies in parallel using ThreadPoolExecutor.
    Discards proxies that the Powerline game server rejects (e.g. HTTP 400 Bad Request).
    """
    if not proxies:
        return []

    print(
        f"[*] Pre-checking {len(proxies)} proxies against game server "
        f"{target_server_host}:{target_server_port}..."
    )
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(proxies)) as ex:
        futures = [
            ex.submit(check_proxy_game_server, p, target_server_host, target_server_port, timeout)
            for p in proxies
        ]
        results = [f.result() for f in futures]

    elapsed = time.time() - t0
    working = [p for p, ok, _ in results if ok]
    rejected = [(p, r) for p, ok, r in results if not ok]

    if working:
        working_hosts = ", ".join(p["host"] for p in working)
        print(
            f"[+] {len(working)}/{len(proxies)} proxies verified in {elapsed:.2f}s: {working_hosts}"
        )
    if rejected:
        print(f"[!] {len(rejected)} proxies rejected by game server and skipped:")
        for p, reason in rejected:
            print(f"    - {p['host']}:{p['port']} ({reason})")

    return working


def resolve_game_server_endpoint(server_arg=None, resolved_srv=None):
    """
    Derive the SSL host and port for WebSocket connections.
    E.g. Europe server '51.91.214.104:8187' -> '51-91-214-104.powerline.io', 9187
    """
    srv_str = None
    if resolved_srv and resolved_srv.get("server"):
        srv_str = resolved_srv["server"]
    else:
        regions = fetch_master_servers()
        target = resolve_target_server(regions, server_arg)
        if target and target.get("server"):
            srv_str = target["server"]
        elif regions:
            srv_str = regions[0].get("server", "")

    if srv_str and ":" in srv_str:
        ip, port_str = srv_str.split(":", 1)
        host = ip.replace(".", "-") + ".powerline.io"
        try:
            port = int(port_str)
            ssl_port = port + 1000 if port < 9000 else port
            return host, ssl_port
        except ValueError:
            pass
    return "51-91-214-104.powerline.io", 9187


def configure_stealth_and_bandwidth_saver(driver):
    """
    Enable CDP Network cache and block ad/analytics/audio URLs via Network.setBlockedURLs
    without tampering with native `document` or `navigator` property descriptors
    (which Cloudflare Turnstile inspects).
    """
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": False})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs", {"urls": BLOCKED_URL_PATTERNS}
        )
    except Exception:
        pass


def get_game_state(driver):
    """Read key state fields from the powerline.io global object (Ub)."""
    return driver.execute_script("""
        const G = window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global;
        if (!G) return null;
        const lp = (G.localPlayer && G.localPlayer.id === G.localPlayerID && !G.localPlayer.dead)
            ? G.localPlayer
            : null;
        const topGui = document.getElementById('topGui');
        const nickInput = document.getElementById('nick');
        const net = G.network || {};
        const topGuiVisible = !!(nickInput && nickInput.offsetParent !== null) || !!(topGui && !topGui.hidden) || !!net.hasConnection;
        const entryturn = document.getElementById('entryturn');
        const captchaVisible = !!(entryturn && !entryturn.hidden && (entryturn.children.length > 0 || (entryturn.innerHTML && entryturn.innerHTML.trim().length > 0)));
        const statsVisible = !!(document.getElementById('statsPanel') &&
            document.getElementById('statsPanel').offsetParent !== null);
        return {
            hasConnection: !!net.hasConnection,
            receivedConfig: !!G.receivedConfig,
            roomID: net.roomID || null,
            selectedRegionHash: net.selectedRegionHash || null,
            regionServers: Array.isArray(net.regionServers) ? net.regionServers : [],
            isInGame: !!(G.isInGame && lp),
            uIVisible: !!G.UIVisible,
            highQuality: !!G.highQuality,
            dpr: window.devicePixelRatio,
            canvasW: G.canvas ? G.canvas.width : null,
            canvasH: G.canvas ? G.canvas.height : null,
            localPlayerID: G.localPlayerID || 0,
            direction: (lp && lp.direction) || (G.input && G.input.direction) || 1,
            x: lp ? Math.round(lp.x) : null,
            y: lp ? Math.round(lp.y) : null,
            topGuiVisible,
            captchaVisible,
            statsVisible,
        };
    """)


def human_press_key(driver, key_code, key_name, ctrl_key=False, hold_range=(0.035, 0.085)):
    """Simulate a realistic human key press: keydown -> natural hold -> keyup."""
    driver.execute_script(
        """
        document.dispatchEvent(new KeyboardEvent('keydown', {
            key: arguments[1],
            code: arguments[1],
            keyCode: arguments[0],
            which: arguments[0],
            ctrlKey: arguments[2],
            bubbles: true,
            cancelable: true
        }));
        """,
        key_code,
        key_name,
        ctrl_key,
    )
    if hold_range:
        time.sleep(random.uniform(hold_range[0], hold_range[1]))
    driver.execute_script(
        """
        document.dispatchEvent(new KeyboardEvent('keyup', {
            key: arguments[1],
            code: arguments[1],
            keyCode: arguments[0],
            which: arguments[0],
            ctrlKey: arguments[2],
            bubbles: true,
            cancelable: true
        }));
        """,
        key_code,
        key_name,
        ctrl_key,
    )


def human_mouse_wiggle(driver):
    """Dispatch a subtle mousemove on document so the client sees occasional cursor activity."""
    x = random.randint(40, 200)
    y = random.randint(40, 150)
    driver.execute_script(
        """
        document.dispatchEvent(new MouseEvent('mousemove', {
            clientX: arguments[0],
            clientY: arguments[1],
            bubbles: true
        }));
        """,
        x,
        y,
    )


def human_type_nickname(driver, nick):
    """
    Type the nickname character-by-character with human timing WITHOUT calling
    element.focus() (so Chrome never attempts to steal OS window focus).
    """
    current_val = driver.execute_script(
        "const el = document.getElementById('nick'); return el ? el.value : null;"
    )
    if current_val == nick:
        return

    driver.execute_script(
        """
        const el = document.getElementById('nick');
        if (el) {
            el.value = '';
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }
        """
    )
    time.sleep(random.uniform(0.08, 0.16))
    for ch in nick:
        driver.execute_script(
            """
            const el = document.getElementById('nick');
            if (el) {
                const code = arguments[0].toUpperCase().charCodeAt(0);
                el.dispatchEvent(new KeyboardEvent('keydown', { key: arguments[0], keyCode: code, which: code, bubbles: true }));
                el.value += arguments[0];
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keyup', { key: arguments[0], keyCode: code, which: code, bubbles: true }));
            }
            """,
            ch,
        )
        time.sleep(random.uniform(0.045, 0.11))
    driver.execute_script(
        "const el = document.getElementById('nick'); if (el) el.dispatchEvent(new Event('change', { bubbles: true }));"
    )


def fetch_master_servers():
    """Fetch live server regions and room hashes from https://master.powerline.io/servers in ~50ms."""
    try:
        req = urllib.request.Request(
            "https://master.powerline.io/servers",
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return data.get("regions", [])
    except Exception:
        return []


def resolve_target_server(region_servers, server_arg):
    """Match `--server` against available `regionServers` by region name, alias, hash, or IP."""
    if not server_arg or server_arg.strip().lower() == "auto":
        return None

    raw = server_arg.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.fragment:
            raw = parsed.fragment.strip()
    elif raw.startswith("#"):
        raw = raw[1:].strip()

    low = raw.lower()
    canonical_region = REGION_ALIASES.get(low, low)

    for srv in region_servers or []:
        srv_hash = str(srv.get("hash", "")).lower()
        srv_region = str(srv.get("region", "")).lower()
        srv_addr = str(srv.get("server", "")).lower()

        if (
            low == srv_hash
            or canonical_region == srv_region
            or canonical_region in srv_region
            or low == srv_addr
            or low == srv_addr.split(":")[0]
        ):
            return srv

    return {"hash": raw, "region": f"Custom Room ({raw})", "server": "custom"}


def build_target_url(server_arg, master_regions=None):
    """
    Resolve `--server` into a direct `https://powerline.io/#<hash>` URL before launching Chrome
    so bots connect directly to the target server on initial load (even through foreign proxy IPs)
    without needing a second Turnstile handshake or mid-session region switch!
    """
    if not server_arg or server_arg.strip().lower() == "auto":
        return "https://powerline.io/", None
    regions = master_regions if master_regions is not None else fetch_master_servers()
    target = resolve_target_server(regions, server_arg)
    if target and target.get("hash"):
        return f"https://powerline.io/#{target['hash']}", target
    return "https://powerline.io/", None


def navigate_using_cache(driver, target_url):
    """
    Navigate to `target_url` without triggering Chromium's same-URL ReloadType::NORMAL
    (which sends Cache-Control: max-age=0 and bypasses the disk cache).
    By stepping through `about:blank` when already on powerline.io, Chromium always
    serves the 2.5MB bundle from `--disk-cache-dir` (0 bytes of proxy data).
    """
    try:
        cur = driver.current_url or ""
        if "powerline.io" in cur:
            driver.get("about:blank")
    except Exception:
        pass
    driver.get(target_url)


def select_target_server(driver, server_arg, tag, target_url="https://powerline.io/"):
    """Verify the browser is connected to the user-requested server/region."""
    state = get_game_state(driver)
    if not server_arg or server_arg.strip().lower() == "auto":
        if state:
            cur_hash = state.get("roomID")
            matched = next(
                (s for s in state.get("regionServers", []) if s.get("hash") == cur_hash),
                None,
            )
            region_label = matched["region"] if matched else "Auto"
            print(f"{tag} [+] Server: {region_label} (room #{cur_hash})")
        return

    region_servers = state.get("regionServers", []) if state else []
    target = resolve_target_server(region_servers, server_arg)
    if not target:
        return

    target_hash = target["hash"]
    target_region = target.get("region", target_hash)
    target_addr = target.get("server", "")

    if state and state.get("roomID") == target_hash:
        print(
            f"{tag} [+] Connected to selected server: {target_region} "
            f"(room #{target_hash} | {target_addr})"
        )
        return

    print(
        f"{tag} [*] Switching server to {target_region} "
        f"(room #{target_hash} | {target_addr})..."
    )
    navigate_using_cache(driver, f"https://powerline.io/#{target_hash}")

    switch_deadline = time.time() + 30
    while time.time() < switch_deadline:
        state = get_game_state(driver)
        if (
            state
            and state["hasConnection"]
            and state["receivedConfig"]
            and state["topGuiVisible"]
            and state["roomID"] == target_hash
        ):
            print(
                f"{tag} [+] Connected to selected server: {target_region} "
                f"(room #{target_hash} | {target_addr})"
            )
            return
        time.sleep(0.25)

    print(f"{tag} [!] Server switch to #{target_hash} timed out; continuing on current room.")


def list_available_servers():
    """Fetch and display all live Powerline.io servers/regions."""
    regions = fetch_master_servers()
    if regions:
        print("\nAvailable Powerline.io Servers:")
        print("-" * 68)
        print(f"{'Region':<18} {'Room Hash':<12} {'Server IP:Port':<22} {'Players':<8}")
        print("-" * 68)
        for srv in regions:
            print(
                f"{srv.get('region', '?'):<18} "
                f"#{srv.get('hash', '?'):<11} "
                f"{srv.get('server', '?'):<22} "
                f"{srv.get('players', 0):<8}"
            )
        print("-" * 68)
        print("Example usage:")
        print('  python3 bot.py --server "Europe"')
        print('  python3 bot.py --server "North America"')
        print('  python3 bot.py --server "2cjg"\n')
        return
    print("[!] Could not fetch server list from master.powerline.io.")


def apply_low_graphics_and_max_zoom_in(driver, tag):
    """
    Press Ctrl + '+' repeatedly and 'G' (keyCode 71) after connecting so:
    1. Cloudflare Turnstile's 300x65px widget is never clipped during handshake.
    2. Graphics quality is set to Low (`highQuality = false`) and `defaultRetinaValue = 0.2`
       (500% zoom equivalent), shrinking the WebGL canvas to ~60x32 pixels!
    """
    for _ in range(9):
        human_press_key(driver, 187, "Equal", ctrl_key=True, hold_range=(0.015, 0.03))

    state = get_game_state(driver)
    if state and state["highQuality"]:
        driver.execute_script(
            "const el = document.getElementById('nick'); if (el) el.blur();"
        )
        time.sleep(random.uniform(0.05, 0.12))
        human_press_key(driver, 71, "KeyG", hold_range=(0.04, 0.08))

    info = driver.execute_script(
        """
        const G = window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global;
        if (!G) return null;
        if (G.highQuality && typeof window.toggleGraphics === 'function') {
            window.toggleGraphics();
        }
        G.defaultRetinaValue = 0.2;
        if (G.app && typeof G.app.resizeCanvas === 'function') {
            G.app.resizeCanvas();
        }
        // Prevent unfocused windows from triggering powerline.io's blur disconnect timer
        window.onblur = null;
        G.focus = true;
        if (G.disconnectTimer) {
            clearTimeout(G.disconnectTimer);
            G.disconnectTimer = null;
        }
        return {
            highQuality: !!G.highQuality,
            innerW: window.innerWidth,
            innerH: window.innerHeight,
            canvasW: G.canvas ? G.canvas.width : null,
            canvasH: G.canvas ? G.canvas.height : null,
        };
        """
    )
    if info:
        gfx_mode = "HIGH" if info["highQuality"] else "LOW"
        print(
            f"{tag} [+] Graphics={gfx_mode} (via 'G' key), "
            f"Zoom=500% (Ctrl+'+'), "
            f"Canvas={info['canvasW']}x{info['canvasH']}"
        )


def wait_for_connection_and_optimize(
    driver,
    tag,
    server_arg=None,
    target_url="https://powerline.io/",
    minimize=False,
    forwarder=None,
    proxy_pool=None,
    instance_id=1,
    num_instances=4,
):
    """
    Wait until Powerline.io connects, verify target server, and apply Ctrl+'+' & 'G'.
    If a proxy IP triggers a persistent Cloudflare Turnstile challenge (>5s), automatically
    rotate to the next Webshare proxy IP from `proxy_pool` and reload via disk cache.
    """
    print(f"{tag} [*] Waiting for game connection...")
    deadline = time.time() + 65
    attempt_start = time.time()
    captcha_start = None
    while time.time() < deadline:
        state = get_game_state(driver)
        if state:
            if state["hasConnection"]:
                print(f"{tag} [+] Connected to server! Ready to join.")
                break
            if state["captchaVisible"] and captcha_start is None:
                captcha_start = time.time()
                print(f"{tag} [!] Cloudflare Turnstile captcha visible; waiting...")
            elif not state["captchaVisible"]:
                captcha_start = None

            captcha_stalled = captcha_start is not None and (time.time() - captcha_start >= 10.0)
            handshake_stalled = (time.time() - attempt_start >= 12.0) and not state["hasConnection"]
            if forwarder and proxy_pool and (captcha_stalled or handshake_stalled):
                old_ip = f"{forwarder.proxy_info['host']}:{forwarder.proxy_info['port']}"
                proxy_pool.mark_flagged(forwarder.proxy_info)
                new_proxy = proxy_pool.rotate_proxy(instance_id, num_instances)
                new_ip = f"{new_proxy['host']}:{new_proxy['port']}"
                reason = "Turnstile persisted" if captcha_stalled else "Handshake stalled"
                print(
                    f"{tag} [!] {reason} on {old_ip}; "
                    f"switching to fresh proxy IP {new_ip}..."
                )
                try:
                    driver.execute_script("""
                        try {
                            const G = window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global;
                            if (G && G.network && G.network.webSocket) {
                                G.network.webSocket.close(1000, "Normal Closure");
                            }
                        } catch (e) {}
                    """)
                    time.sleep(0.5)
                except Exception:
                    pass
                forwarder.set_upstream(new_proxy)
                tag = f"[Bot-{instance_id} | {new_ip}]"
                captcha_start = None
                attempt_start = time.time()
                try:
                    driver.delete_all_cookies()
                    driver.execute_script(
                        "window.localStorage.clear(); window.sessionStorage.clear();"
                    )
                except Exception:
                    pass
                navigate_using_cache(driver, target_url)
        time.sleep(0.35)
    else:
        raise RuntimeError(f"{tag} Timed out waiting for Powerline.io server connection.")

    select_target_server(driver, server_arg, tag, target_url=target_url)
    apply_low_graphics_and_max_zoom_in(driver, tag)
    return forwarder.proxy_info if forwarder else None


def rotate_bot_ip_if_due(
    driver,
    forwarder,
    proxy_pool,
    instance_id,
    num_instances,
    last_ip_time,
    next_rotate_interval,
    server_arg=None,
    target_url="https://powerline.io/",
    minimize=False,
):
    """
    Check if this bot has been on the same IP for >= next_rotate_interval (~5 mins ± jitter).
    Switches upstream proxy IP and reconnects (using disk cache so static files cost 0 bytes).
    """
    if not forwarder or not proxy_pool or next_rotate_interval <= 0:
        return (forwarder.proxy_info if forwarder else None), last_ip_time, next_rotate_interval, False

    elapsed = time.time() - last_ip_time
    if elapsed < next_rotate_interval:
        return forwarder.proxy_info, last_ip_time, next_rotate_interval, False

    old_ip = f"{forwarder.proxy_info['host']}:{forwarder.proxy_info['port']}"
    new_proxy = proxy_pool.rotate_proxy(instance_id, num_instances)
    new_ip = f"{new_proxy['host']}:{new_proxy['port']}"
    kb_used = forwarder.get_kb_used()
    tag = f"[Bot-{instance_id} | {old_ip} -> {new_ip}]"
    print(
        f"{tag} [*] {int(elapsed)}s elapsed on {old_ip} (Proxy Data: {kb_used:.1f} KB); "
        f"rotating to {new_ip}..."
    )
    try:
        driver.execute_script("""
            try {
                const G = window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global;
                if (G && G.network && G.network.webSocket) {
                    G.network.webSocket.close(1000, "Normal Closure");
                }
            } catch (e) {}
        """)
        time.sleep(0.5)
    except Exception:
        pass

    forwarder.set_upstream(new_proxy)

    try:
        driver.delete_all_cookies()
        driver.execute_script(
            """
            window.localStorage.clear();
            window.sessionStorage.clear();
            window.localStorage.lq = 'true';
            """
        )
    except Exception:
        pass

    new_tag = f"[Bot-{instance_id} | {new_ip}]"
    with proxy_pool.get_ip_lock(new_proxy):
        navigate_using_cache(driver, target_url)
        updated_proxy = wait_for_connection_and_optimize(
            driver,
            new_tag,
            server_arg=server_arg,
            target_url=target_url,
            minimize=minimize,
            forwarder=forwarder,
            proxy_pool=proxy_pool,
            instance_id=instance_id,
            num_instances=num_instances,
        )

    jittered_interval = next_rotate_interval * random.uniform(0.92, 1.08)
    return updated_proxy or new_proxy, time.time(), jittered_interval, True


def spawn_now(
    driver,
    nick,
    tag,
    is_first_spawn=False,
    server_arg=None,
    target_url="https://powerline.io/",
    minimize=False,
    forwarder=None,
    proxy_pool=None,
    instance_id=1,
    num_instances=4,
):
    """
    Human-like spawn/respawn:
    - On first spawn: types nickname character-by-character, pauses briefly, presses Enter.
    - On respawn: waits a brief human reaction delay (0.32-0.72s), then presses Enter.
    - Avoids spamming `clickPlay` while `nicknameModerationStatus === 'awaiting'` and resets
      `playClicks = 0` so Powerline.io never triggers video ad prerolls (`U7()`).
    """
    for attempt in range(4):
        if is_first_spawn and attempt == 0:
            human_mouse_wiggle(driver)
            human_type_nickname(driver, nick)
            time.sleep(random.uniform(0.18, 0.40))
        else:
            time.sleep(random.uniform(0.32, 0.72))

        driver.execute_script(
            """
            try {
                const nickInput = document.getElementById("nick");
                if (nickInput) nickInput.value = arguments[0];
                const G = window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global;
                const mod = G && G.network ? G.network.nicknameModerationStatus : '';
                if (G) {
                    G.playClicks = 0;
                    G.focus = true;
                    if (G.disconnectTimer) {
                        clearTimeout(G.disconnectTimer);
                        G.disconnectTimer = null;
                    }
                }
                if (mod !== 'awaiting' && typeof window.clickPlay === 'function') {
                    window.clickPlay(arguments[0]);
                }
            } catch (e) {}
            """,
            nick,
        )

        spawn_deadline = time.time() + 20
        last_click = time.time()
        while time.time() < spawn_deadline:
            state = get_game_state(driver)
            if state and state["isInGame"] and state["x"] is not None:
                print(
                    f"{tag} [+] Spawned! ID={state['localPlayerID']} "
                    f"room=#{state['roomID']} pos=({state['x']}, {state['y']}) dir={state['direction']}"
                )
                return state
            if time.time() - last_click >= 1.5:
                driver.execute_script(
                    """
                    try {
                        const nickInput = document.getElementById("nick");
                        if (nickInput) nickInput.value = arguments[0];
                        const G = window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global;
                        const mod = G && G.network ? G.network.nicknameModerationStatus : '';
                        if (G) {
                            G.playClicks = 0;
                            G.focus = true;
                            if (G.disconnectTimer) {
                                clearTimeout(G.disconnectTimer);
                                G.disconnectTimer = null;
                            }
                        }
                        if (mod !== 'awaiting' && typeof window.clickPlay === 'function') {
                            window.clickPlay(arguments[0]);
                        }
                    } catch (e) {}
                    """,
                    nick,
                )
                last_click = time.time()
            time.sleep(0.06)

        if attempt < 2:
            print(f"{tag} [!] Spawn attempt {attempt + 1} taking longer than expected; retrying clickPlay...")
            continue

        if forwarder and proxy_pool:
            old_ip = f"{forwarder.proxy_info['host']}:{forwarder.proxy_info['port']}"
            new_proxy = proxy_pool.rotate_proxy(instance_id, num_instances)
            new_ip = f"{new_proxy['host']}:{new_proxy['port']}"
            print(
                f"{tag} [!] Spawn attempt {attempt + 1} timed out on {old_ip}; "
                f"switching to {new_ip} and reloading..."
            )
            try:
                driver.execute_script("""
                    try {
                        const G = window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global;
                        if (G && G.network && G.network.webSocket) {
                            G.network.webSocket.close(1000, "Normal Closure");
                        }
                    } catch (e) {}
                """)
                time.sleep(0.5)
            except Exception:
                pass
            forwarder.set_upstream(new_proxy)
            tag = f"[Bot-{instance_id} | {new_ip}]"
        else:
            print(f"{tag} [!] Spawn attempt {attempt + 1} timed out; reloading page...")

        navigate_using_cache(driver, target_url)
        wait_for_connection_and_optimize(
            driver,
            tag,
            server_arg=server_arg,
            target_url=target_url,
            minimize=minimize,
            forwarder=forwarder,
            proxy_pool=proxy_pool,
            instance_id=instance_id,
            num_instances=num_instances,
        )

    raise RuntimeError(f"{tag} Timed out waiting for snake to spawn.")


def next_circle_index(cur_dir, key_table, step_sign=1):
    """Return the next index in key_table for a valid 90-degree turn (CW or CCW)."""
    dir_order = [d[0] for d in key_table]
    if cur_dir in dir_order:
        return (dir_order.index(cur_dir) + step_sign) % 4
    return 0


def list_x11_window_ids():
    """Return a set of all currently open X11 window IDs (as integers)."""
    ids = set()
    try:
        out = subprocess.check_output(
            ["wmctrl", "-l"], stderr=subprocess.DEVNULL, text=True, timeout=1.5
        )
        for line in out.splitlines():
            parts = line.split()
            if parts:
                ids.add(int(parts[0], 16))
    except Exception:
        pass
    return ids


def launch_chrome_without_stealing_focus(options, minimize=False):
    """
    Launch webdriver.Chrome while preserving the user's currently focused X11 window
    so opening bot windows never interrupts what the user is typing/doing.
    If `minimize=True`, places the new Chrome window in X11 'shaded,below' state
    (rolled up behind all windows) instead of IconicState so Chromium never halts
    requestAnimationFrame or Cloudflare Turnstile!
    """
    with _focus_lock:
        user_active_win = get_active_window_id()
        before_wins = list_x11_window_ids()
        driver = webdriver.Chrome(options=options)
        after_wins = list_x11_window_ids()
        new_wins = after_wins - before_wins

        for wid in new_wins:
            hex_id = hex(wid)
            props = "add,below,shaded" if minimize else "add,below"
            try:
                subprocess.run(
                    ["wmctrl", "-i", "-r", hex_id, "-b", props],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                )
            except Exception:
                pass

        restore_window_focus(user_active_win)
        time.sleep(0.12)
        restore_window_focus(user_active_win)
    return driver


def run_bot_instance(
    instance_id=1,
    num_instances=4,
    nick="CircleBot",
    loops=4,
    circle_seconds=5.0,
    step_delay=0.35,
    keep_open=3.0,
    proxy_pool=None,
    ip_rotate_seconds=300.0,
    server_arg=None,
    target_url="https://powerline.io/",
    minimize=False,
    cache_seed_dir=None,
):
    if instance_id > 1:
        time.sleep(random.uniform(1.2, 1.8) * (instance_id - 1))

    proxy_info = (
        proxy_pool.get_initial_proxy(instance_id, num_instances) if proxy_pool else None
    )
    forwarder = LocalProxyForwarder(proxy_info) if proxy_info else None
    last_ip_time = time.time()
    next_rotate_interval = (
        ip_rotate_seconds * random.uniform(0.92, 1.08) if ip_rotate_seconds > 0 else 0
    )

    key_table = CIRCLE_DIRS_WASD if (instance_id % 2 == 0) else CIRCLE_DIRS_ARROW
    key_style = "WASD" if key_table is CIRCLE_DIRS_WASD else "Arrows"

    def make_tag(p_info):
        ip_label = f"{p_info['host']}:{p_info['port']}" if p_info else "direct"
        return f"[Bot-{instance_id} | {ip_label}]"

    tag = make_tag(proxy_info)

    profile_dir = tempfile.mkdtemp(prefix=f"powerline_bot_{instance_id}_")
    bot_cache_dir = tempfile.mkdtemp(prefix=f"powerline_cache_{instance_id}_")
    if cache_seed_dir and os.path.isdir(cache_seed_dir):
        shutil.copytree(cache_seed_dir, bot_cache_dir, dirs_exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument(f"--disk-cache-dir={bot_cache_dir}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--mute-audio")

    # Prevent Chrome background/occluded throttling and background model downloads
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument(
        "--disable-features=CalculateNativeWinOcclusion,OptimizationHints,"
        "OptimizationGuideModelDownloading,OptimizationTargetPrediction,MediaRouter,Translate"
    )

    # Disable Chrome background traffic to save proxy bandwidth
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--no-pings")

    col = (instance_id - 1) % 2
    row = (instance_id - 1) // 2
    options.add_argument("--window-size=640,460")
    if forwarder:
        options.add_argument(f"--proxy-server=http://127.0.0.1:{forwarder.local_port}")

    if minimize:
        options.add_argument("--headless=new")
        mode_str = "headless-minimized"
        print(
            f"{tag} [*] Launching Chrome ({mode_str}, keys={key_style}, "
            f"profile={os.path.basename(profile_dir)})..."
        )
        driver = webdriver.Chrome(options=options)
    else:
        options.add_argument(f"--window-position={col * 640},{row * 460}")
        mode_str = "background-unfocused"
        print(
            f"{tag} [*] Launching Chrome ({mode_str}, keys={key_style}, "
            f"profile={os.path.basename(profile_dir)})..."
        )
        driver = launch_chrome_without_stealing_focus(options, minimize=False)

    try:
        configure_stealth_and_bandwidth_saver(driver)

        # If another bot flagged our initial IP while we were launching Chrome, switch before connecting
        if proxy_pool and forwarder and proxy_pool.is_flagged(proxy_info):
            proxy_info = proxy_pool.rotate_proxy(instance_id, num_instances)
            forwarder.set_upstream(proxy_info)
            tag = make_tag(proxy_info)

        # Serialize initial Turnstile handshake per proxy IP so multiple bots sharing an IP
        # do not hit Cloudflare Turnstile at the exact same millisecond
        ip_lock = proxy_pool.get_ip_lock(proxy_info) if proxy_pool else threading.Lock()
        with ip_lock:
            if proxy_pool and forwarder and proxy_pool.is_flagged(proxy_info):
                proxy_info = proxy_pool.rotate_proxy(instance_id, num_instances)
                forwarder.set_upstream(proxy_info)
                tag = make_tag(proxy_info)

            navigate_using_cache(driver, target_url)
            updated_proxy = wait_for_connection_and_optimize(
                driver,
                tag,
                server_arg=server_arg,
                target_url=target_url,
                minimize=minimize,
                forwarder=forwarder,
                proxy_pool=proxy_pool,
                instance_id=instance_id,
                num_instances=num_instances,
            )
            if updated_proxy:
                proxy_info = updated_proxy
                tag = make_tag(proxy_info)
            time.sleep(random.uniform(0.6, 1.0))

        for round_idx in range(1, loops + 1):
            proxy_info, last_ip_time, next_rotate_interval, _ = rotate_bot_ip_if_due(
                driver,
                forwarder,
                proxy_pool,
                instance_id,
                num_instances,
                last_ip_time,
                next_rotate_interval,
                server_arg=server_arg,
                target_url=target_url,
                minimize=minimize,
            )
            tag = make_tag(proxy_info)

            kb_str = f" | ProxyData={forwarder.get_kb_used():.1f}KB" if forwarder else ""
            print(f"{tag} === Round {round_idx}/{loops} (joining as '{nick}'{kb_str}) ===")
            state = spawn_now(
                driver,
                nick,
                tag,
                is_first_spawn=(round_idx == 1),
                server_arg=server_arg,
                target_url=target_url,
                minimize=minimize,
                forwarder=forwarder,
                proxy_pool=proxy_pool,
                instance_id=instance_id,
                num_instances=num_instances,
            )

            step_sign = random.choice([1, -1])
            idx = next_circle_index(state["direction"], key_table, step_sign)
            round_duration = circle_seconds * random.uniform(0.88, 1.14)

            print(
                f"{tag} [*] Moving in a {'CW' if step_sign == 1 else 'CCW'} circle "
                f"for {round_duration:.1f}s (base_step={step_delay}s)..."
            )
            start_circle = time.time()
            turns_made = 0

            while time.time() - start_circle < round_duration:
                proxy_info, last_ip_time, next_rotate_interval, rotated = rotate_bot_ip_if_due(
                    driver,
                    forwarder,
                    proxy_pool,
                    instance_id,
                    num_instances,
                    last_ip_time,
                    next_rotate_interval,
                    server_arg=server_arg,
                    target_url=target_url,
                    minimize=minimize,
                )
                if rotated:
                    tag = make_tag(proxy_info)
                    state = spawn_now(
                        driver,
                        nick,
                        tag,
                        is_first_spawn=True,
                        server_arg=server_arg,
                        target_url=target_url,
                        minimize=minimize,
                        forwarder=forwarder,
                        proxy_pool=proxy_pool,
                        instance_id=instance_id,
                        num_instances=num_instances,
                    )
                    idx = next_circle_index(state["direction"], key_table, step_sign)

                state = get_game_state(driver)
                if not state or not state["isInGame"]:
                    print(f"{tag} [!] Died during circle! Respawning...")
                    state = spawn_now(
                        driver,
                        nick,
                        tag,
                        server_arg=server_arg,
                        target_url=target_url,
                        minimize=minimize,
                        forwarder=forwarder,
                        proxy_pool=proxy_pool,
                        instance_id=instance_id,
                        num_instances=num_instances,
                    )
                    idx = next_circle_index(state["direction"], key_table, step_sign)

                target_dir, dir_label, key_name, key_code = key_table[idx]
                human_press_key(driver, key_code, key_name, hold_range=(0.035, 0.085))
                turns_made += 1
                print(
                    f"{tag}     -> Turn #{turns_made}: {dir_label} ({key_name}) "
                    f"(pos=({state['x']}, {state['y']}))"
                )

                if random.random() < 0.20:
                    human_mouse_wiggle(driver)
                if turns_made > 3 and random.random() < 0.15:
                    step_sign = -step_sign

                idx = (idx + step_sign) % 4

                jittered_step = step_delay * random.uniform(0.75, 1.30)
                step_end = time.time() + jittered_step
                while time.time() < step_end:
                    check = get_game_state(driver)
                    if not check or not check["isInGame"]:
                        print(f"{tag} [!] Died mid-step! Respawning...")
                        state = spawn_now(
                            driver,
                            nick,
                            tag,
                            server_arg=server_arg,
                            target_url=target_url,
                            minimize=minimize,
                            forwarder=forwarder,
                            proxy_pool=proxy_pool,
                            instance_id=instance_id,
                            num_instances=num_instances,
                        )
                        idx = next_circle_index(state["direction"], key_table, step_sign)
                        break
                    time.sleep(0.05)

            state = get_game_state(driver)
            if state and state["isInGame"]:
                time.sleep(random.uniform(0.08, 0.22))
                print(f"{tag} [*] Pressing 'K' (keyCode=75) to kill self...")
                for attempt in range(10):
                    human_press_key(driver, 75, "KeyK", hold_range=(0.045, 0.095))
                    time.sleep(random.uniform(0.14, 0.24))
                    state = get_game_state(driver)
                    if not state or not state["isInGame"]:
                        print(f"{tag} [+] Killed self with 'K' key! (attempt {attempt + 1})")
                        break
                else:
                    print(f"{tag} [!] Sent 'K' key, isInGame={state['isInGame']}")

            if round_idx < loops:
                print(f"{tag} [*] Preparing respawn for Round {round_idx + 1}...")

        total_kb = forwarder.get_kb_used() if forwarder else 0.0
        print(
            f"{tag} [*] Completed all {loops} rounds! "
            f"Total Proxy Data Used: {total_kb:.1f} KB. Keeping browser open for {keep_open}s..."
        )
        time.sleep(keep_open)

    finally:
        print(f"{tag} [*] Closing browser and cleaning up isolated profile.")
        try:
            driver.execute_script("""
                try {
                    const G = window.vm_0x12d41d_50e8a6 && window.vm_0x12d41d_50e8a6.Global;
                    if (G && G.network && G.network.webSocket) {
                        G.network.webSocket.close(1000, "Normal Closure");
                    }
                } catch (e) {}
            """)
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass
        finally:
            if proxy_pool:
                proxy_pool.release_bot(instance_id)
            if forwarder:
                forwarder.close()
            shutil.rmtree(profile_dir, ignore_errors=True)
            shutil.rmtree(bot_cache_dir, ignore_errors=True)


def run_multi_bots(
    instances=4,
    bots_per_ip=2,
    proxy_file="Webshare 10 proxies(4).txt",
    ip_rotate_seconds=300.0,
    server_arg=None,
    minimize=False,
    nick="CircleBot",
    loops=4,
    circle_seconds=5.0,
    step_delay=0.35,
    keep_open=3.0,
):
    proxies = load_proxies(proxy_file)
    if proxies:
        print(
            f"[*] Loaded {len(proxies)} proxies from '{proxy_file}' "
            f"({bots_per_ip} bots per IP, rotating IP every ~{ip_rotate_seconds:g}s)."
        )
    else:
        print(f"[!] No proxies loaded from '{proxy_file}'; running with direct connection.")

    target_url, resolved_srv = build_target_url(server_arg)
    if resolved_srv:
        print(
            f"[*] Target server resolved: {resolved_srv.get('region')} "
            f"(room #{resolved_srv.get('hash')} | {resolved_srv.get('server')}) -> {target_url}"
        )
    elif server_arg:
        print(f"[*] Target server selection: '{server_arg}'")

    if proxies:
        srv_host, srv_port = resolve_game_server_endpoint(server_arg, resolved_srv)
        working_proxies = filter_working_proxies(proxies, srv_host, srv_port)
        if not working_proxies:
            print("[!] None of the proxies can connect to the game server; falling back to direct connection.")
            proxies = []
        else:
            proxies = working_proxies
            needed_per_ip = (instances + len(proxies) - 1) // len(proxies)
            if bots_per_ip < needed_per_ip:
                print(
                    f"[*] Adjusted bots-per-ip from {bots_per_ip} to {needed_per_ip} "
                    f"to allocate {instances} bots across {len(proxies)} active proxies."
                )
                bots_per_ip = needed_per_ip

    if minimize:
        print("[*] Window mode: MINIMIZED (--minimize)")
    else:
        print("[*] Window mode: Background (will not steal focus from your active window)")

    cache_seed_dir = ensure_http_cache_seed()
    proxy_pool = ProxyPool(proxies, bots_per_ip=bots_per_ip) if proxies else None
    threads = []

    for i in range(1, instances + 1):
        bot_nick = f"{nick}{i}" if instances > 1 else nick
        t = threading.Thread(
            target=run_bot_instance,
            kwargs={
                "instance_id": i,
                "num_instances": instances,
                "nick": bot_nick,
                "loops": loops,
                "circle_seconds": circle_seconds,
                "step_delay": step_delay,
                "keep_open": keep_open,
                "proxy_pool": proxy_pool,
                "ip_rotate_seconds": ip_rotate_seconds,
                "server_arg": server_arg,
                "target_url": target_url,
                "minimize": minimize,
                "cache_seed_dir": cache_seed_dir,
            },
            daemon=False,
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Humanized Multi-instance Powerline.io Bot with Proxy Data Saver & Minimize Mode"
    )
    parser.add_argument(
        "--list-servers",
        action="store_true",
        help="List all live Powerline.io regions/servers with room hashes and exit",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="Target server region ('Europe', 'eu', 'North America', 'na', 'Asia', 'South America'), "
        "room hash ('2cjg' or '#2cjg'), or full link ('https://powerline.io/#2cjg')",
    )
    parser.add_argument(
        "-m",
        "--minimize",
        action="store_true",
        help="Minimize all Chrome bot windows right after connection while keeping them running at full speed",
    )
    parser.add_argument("--instances", type=int, default=4, help="Number of concurrent bot instances")
    parser.add_argument("--bots-per-ip", type=int, default=2, help="Number of bots sharing each proxy IP")
    parser.add_argument(
        "--proxy-file",
        default="Webshare 10 proxies(4).txt",
        help="Path to Webshare proxy list file (host:port:user:pass)",
    )
    parser.add_argument(
        "--ip-rotate-seconds",
        type=float,
        default=300.0,
        help="Rotate each bot to a new proxy IP after this many seconds (default: 300s = 5 minutes)",
    )
    parser.add_argument("--nick", default="CircleBot", help="Base nickname to use")
    parser.add_argument("--loops", type=int, default=4, help="Number of rounds per instance")
    parser.add_argument("--duration", type=float, default=5.0, help="Seconds to move in a circle per round")
    parser.add_argument("--step", type=float, default=0.35, help="Seconds between 90-degree turns")
    parser.add_argument("--keep-open", type=float, default=3.0, help="Seconds to wait after final round")
    args = parser.parse_args()

    if args.list_servers:
        list_available_servers()
        sys.exit(0)

    run_multi_bots(
        instances=args.instances,
        bots_per_ip=args.bots_per_ip,
        proxy_file=args.proxy_file,
        ip_rotate_seconds=args.ip_rotate_seconds,
        server_arg=args.server,
        minimize=args.minimize,
        nick=args.nick,
        loops=args.loops,
        circle_seconds=args.duration,
        step_delay=args.step,
        keep_open=args.keep_open,
    )
