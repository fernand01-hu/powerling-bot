"""
Dashboard: a small password-protected website served by the coordinator.

- GET  /            the dashboard page (dashboard.html)
- GET  /api/state   everything it shows, as JSON (the coordinator's latest snapshot)
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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

logger = logging.getLogger("PowerlineBot.Dashboard")

DEFAULT_DASHBOARD_PORT = 4042
DASHBOARD_PASSWORD_ENV = "POWERLINE_DASHBOARD_PASSWORD"
PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
ACTIONS = {
    "stop_bot", "start_bot", "restart_bot", "stop_all", "start_all",
    "pause_machine", "resume_machine", "set_max_bots", "set_groups", "refresh_proxies",
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
            if self.path == "/":
                with open(PAGE_PATH, "rb") as f:
                    self.send_body(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.send_json(200, coordinator.snapshot)
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


def start_dashboard(coordinator, address: Tuple[str, int], password: str) -> ThreadingHTTPServer:
    """Serve the dashboard in a background thread."""
    server = ThreadingHTTPServer(address, make_handler(coordinator, password))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    logger.info(f"[Dashboard] Serving on http://{address[0]}:{server.server_address[1]}/")
    return server
