"""
Dashboard: a small password-protected website served by the coordinator.

- GET  /            the dashboard page (dashboard.html)
- GET  /api/state   everything it shows, as JSON (the coordinator's latest snapshot)
- GET  /api/logs?machine=NAME&bot=N&limit=200   recent log lines from the machines
- GET  /api/history  CPU/RAM per machine and bots running/playing, for the last hour
- GET  /api/screenshot?bot=N   the latest screenshot a bot sent (base64 JPEG)
- POST /api/command a control action; queued and carried out by the coordinator's main loop

The password comes from $POWERLINE_DASHBOARD_PASSWORD (any user name works). Command
requests must send an X-Requested-With header, which other websites open in the same
browser can't add, so they can't send commands on your behalf.
"""

import base64
import hmac
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse


def int_param(query: dict, name: str) -> Optional[int]:
    try:
        return int(query[name][0])
    except (KeyError, IndexError, ValueError):
        return None

logger = logging.getLogger("PowerlineBot.Dashboard")

DEFAULT_DASHBOARD_PORT = 4042
DASHBOARD_PASSWORD_ENV = "POWERLINE_DASHBOARD_PASSWORD"
PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
ACTIONS = {
    "stop_bot", "start_bot", "restart_bot", "stop_all", "start_all",
    "stop_bots", "start_bots", "restart_bots", "screenshot",
    "pause_machine", "resume_machine", "set_max_bots", "restart_machine", "stop_machine_bots",
    "set_groups", "set_settings", "refresh_proxies",
}
MAX_BODY = 4096


def make_handler(coordinator, password: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PowerlineDashboard"

        def log_message(self, fmt, *args):
            pass  # Keep the coordinator's log readable

        def authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                except ValueError:
                    given = ""
                if hmac.compare_digest(given.encode(), password.encode()):
                    return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Powerline bots"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def send_body(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, data):
            self.send_body(status, json.dumps(data).encode(), "application/json")

        def do_GET(self):
            if not self.authorized():
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                with open(PAGE_PATH, "rb") as f:
                    self.send_body(200, f.read(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self.send_json(200, coordinator.snapshot)
            elif url.path == "/api/logs":
                machine = query.get("machine", [None])[0] or None
                limit = int_param(query, "limit") or 200
                self.send_json(200, coordinator.logs_view(machine=machine, bot=int_param(query, "bot"), limit=limit))
            elif url.path == "/api/history":
                self.send_json(200, coordinator.history_view())
            elif url.path == "/api/screenshot":
                bot = int_param(query, "bot")
                shot = coordinator.screenshot_view(bot) if bot is not None else None
                self.send_json(200 if shot else 404, shot or {"error": "no screenshot yet"})
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/api/command":
                self.send_json(404, {"error": "not found"})
                return
            if self.headers.get("X-Requested-With") != "dashboard":
                self.send_json(403, {"error": "missing X-Requested-With header"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self.send_json(400, {"error": "bad request size"})
                return
            try:
                cmd = json.loads(self.rfile.read(length))
            except ValueError:
                self.send_json(400, {"error": "invalid JSON"})
                return
            if not isinstance(cmd, dict) or cmd.get("action") not in ACTIONS:
                self.send_json(400, {"error": "unknown action"})
                return
            coordinator.commands.put(cmd)
            self.send_json(202, {"ok": True})

    return Handler


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser closing the page mid-request isn't worth a traceback in the coordinator's log.
        logger.warning(f"[Dashboard] Request from {client_address[0]} failed: {sys.exc_info()[1]!r}")


def start_dashboard(coordinator, address: Tuple[str, int], password: str) -> ThreadingHTTPServer:
    """Serve the dashboard in a background thread."""
    server = DashboardServer(address, make_handler(coordinator, password))
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    logger.info(f"[Dashboard] Serving on http://{address[0]}:{server.server_address[1]}/")
    return server
