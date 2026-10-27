"""
Control a running coordinator from the command line, through its dashboard API.

    python3 ctl.py status                 # machines and bots
    python3 ctl.py kill-all               # force-close every bot's browser at once
    python3 ctl.py kill MACHINE           # same, only the bots on one machine
    python3 ctl.py stop-all | start-all   # graceful stop / start every stopped bot
    python3 ctl.py stop MACHINE           # graceful stop of one machine's bots

Needs the dashboard (coordinator started with --dashboard). The password comes from
$POWERLINE_DASHBOARD_PASSWORD, or from ~/.config/powerline-bot/env when it isn't set.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

ENV_FILE = os.path.expanduser("~/.config/powerline-bot/env")


def dashboard_password() -> str:
    password = os.environ.get("POWERLINE_DASHBOARD_PASSWORD")
    if password:
        return password
    try:
        with open(ENV_FILE) as f:
            for line in f:
                name, _, value = line.strip().partition("=")
                if name == "POWERLINE_DASHBOARD_PASSWORD":
                    return value.strip().strip("'\"")
    except OSError:
        pass
    sys.exit("No dashboard password: set POWERLINE_DASHBOARD_PASSWORD (or put it in ~/.config/powerline-bot/env).")


def call(base: str, password: str, path: str, body: dict = None) -> dict:
    headers = {"Authorization": "Basic " + base64.b64encode(f"ctl:{password}".encode()).decode()}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "X-Requested-With": "dashboard"})
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"{path}: HTTP {e.code}" + (" (wrong password)" if e.code == 401 else ""))
    except OSError as e:
        sys.exit(f"Can't reach the dashboard at {base}: {e}")


def print_status(state: dict):
    print(f"Room #{state['room']} · code version {state['code_version']}")
    for m in state["machines"]:
        limit = f" (max {m['max_bots']})" if m.get("max_bots") is not None else ""
        flags = " paused" if m.get("paused") else ""
        print(f"  {m['name']:<32} cpu {m['cpu']:5.1f}%  bots {len(m['bots'])}{limit}{flags}")
    counts = {}
    for b in state["bots"]:
        counts[b["state"]] = counts.get(b["state"], 0) + 1
    print(f"Bots: {len(state['bots'])} (" + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ")")


def main():
    parser = argparse.ArgumentParser(description="Control a running Powerline coordinator")
    parser.add_argument("--dashboard", default="127.0.0.1:4042", help="Dashboard HOST[:PORT] (default: 127.0.0.1:4042)")
    parser.add_argument("command", choices=["status", "kill-all", "stop-all", "start-all", "kill", "stop"])
    parser.add_argument("machine", nargs="?", help="Machine name for kill / stop (as shown by status)")
    args = parser.parse_args()

    host = args.dashboard if ":" in args.dashboard else args.dashboard + ":4042"
    base = "http://" + host
    password = dashboard_password()
    state = call(base, password, "/api/state")

    if args.command == "status":
        print_status(state)
        return
    if args.command in ("kill", "stop"):
        names = [m["name"] for m in state["machines"]]
        if args.machine not in names:
            sys.exit(f"Name a machine: {', '.join(names) or 'none connected'}")
        action = "kill_machine_bots" if args.command == "kill" else "stop_machine_bots"
        call(base, password, "/api/command", {"action": action, "machine": args.machine})
        print(f"Sent: {args.command} the bots on {args.machine}")
    else:
        action = args.command.replace("-", "_")
        call(base, password, "/api/command", {"action": action})
        print(f"Sent: {args.command}")


if __name__ == "__main__":
    main()
