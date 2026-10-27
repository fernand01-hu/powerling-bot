# Powerline Bot: Complete Guide

How every part of the system works, how it spreads bots over several machines, how to set it up and run it day to day, and what to do when something goes wrong. `README.md` is the short reference; this file is the long version.

**Contents**

1. [What it is](#1-what-it-is)
2. [Words used in this guide](#2-words-used-in-this-guide)
3. [The files](#3-the-files)
4. [The big picture](#4-the-big-picture)
5. [Multiple machines in depth](#5-multiple-machines-in-depth)
6. [What happens when something fails](#6-what-happens-when-something-fails)
7. [Proxies and Webshare](#7-proxies-and-webshare)
8. [What a bot does in its browser](#8-what-a-bot-does-in-its-browser)
9. [One-time setup](#9-one-time-setup)
10. [Running it](#10-running-it)
11. [The dashboard](#11-the-dashboard)
12. [Autostart](#12-autostart)
13. [Day-to-day recipes](#13-day-to-day-recipes)
14. [Capacity planning](#14-capacity-planning)
15. [Troubleshooting](#15-troubleshooting)
16. [Reference: messages, API, state file, constants](#16-reference)
17. [Security checklist](#17-security-checklist)
18. [Tests](#18-tests)

---

## 1. What it is

The system runs many Camoufox (Firefox-based) browsers that join one powerline.io room and play non-stop. Each browser:

- connects through a Webshare proxy that it shares with the other bots in its **group**,
- keeps one random nickname for as long as the system runs (even across restarts),
- steers with humanized arrow-key input and respawns after it dies.

The bots can be spread over any number of computers on your network, and you control everything from one web **dashboard**.

**Status, honestly:** everything in this guide works: the machines, the proxies, the dashboard and the restarts. When the game server refuses an IP (HTTP 400 on its WebSocket), the coordinator automatically detects it and uses Webshare API v3 to rotate the blocked IP out for a fresh clean IP from your replacement pool.

---

## 2. Words used in this guide

| Word | Meaning |
| --- | --- |
| **Bot** | One player in the game. It has a fixed number (`#0`, `#1`…) and a fixed nickname. |
| **Worker** | The process that runs one bot: one Camoufox browser plus the Python code driving it. A bot that restarts gets a new worker. |
| **Group** | A set of bots that share one proxy IP. Bot `#n` is in group `n ÷ bots-per-group` (rounded down). With 3 bots per group: `#0–#2` are group 0, `#3–#5` are group 1, and so on. |
| **Proxy / IP** | A Webshare proxy (`host:port` + login). Each group uses one at a time. |
| **Agent** | The program that runs workers on one machine (`agent.py`). One per machine. |
| **Machine** | A computer running an agent. The dashboard names it by its hostname (e.g. `DESKTOP-HOQA23I`). |
| **Coordinator** | The one program that decides everything (`coordinator.py`). It also runs the dashboard and its own local agent. |
| **Benched** | A proxy that failed is left out for 30 minutes. |
| **Rotation** | Webshare replaces all your IPs on a fixed schedule (every 300 s on your plan). |

---

## 3. The files

| File | Role | Runs on |
| --- | --- | --- |
| `coordinator.py` | The brain: decides which bot runs where and on which proxy, restarts failed bots, talks to Webshare, keeps the state file, serves the dashboard. | One machine (the old server) |
| `agent.py` | Starts and stops workers on its machine when the coordinator asks; reports CPU, RAM, bot states and logs. | Every machine that runs bots |
| `bot.py` | The worker: one browser = one bot. Loads the game, joins, plays, respawns, switches proxy. | Started by an agent |
| `humanizer.py` | Every mouse move, click, keypress and pause (curved mouse paths, random timing, corrected typos). | Inside each worker |
| `dashboard.py` | The password-protected web server (port 4042) and its JSON API. | Inside the coordinator |
| `dashboard.html` | The dashboard page itself. | Served by `dashboard.py` |
| `deploy/` | Autostart: systemd units (Linux), `start-agent.cmd` (Windows), env template. | Copied onto each machine |
| `requirements.txt` | `camoufox[geoip]`, `numpy`, `psutil`. | Every machine |
| `tests/` | 215 unit tests, no browser or network needed. | Anywhere |

**Code fingerprint:** `bot.py`, `agent.py` and `humanizer.py` together make the *code version*, a 12-character hash shown in the logs (e.g. `89498858807e`). The coordinator refuses any agent whose version differs from its own. Windows line endings don't change the hash. `coordinator.py`, `dashboard.py` and `dashboard.html` are not part of it, because only the coordinator runs them.

---

## 4. The big picture

```
                         Webshare API  (proxy list every 60 s, plan's rotation period once)
                              ▲
                              │ HTTPS
   your phone / PC ──HTTP──►  │
   (dashboard, :4042)         │
                      ┌───────┴─────────────────────────────┐
                      │ COORDINATOR  (server, 192.168.1.96)  │
                      │  • main loop, once per second        │
                      │  • dashboard web server (thread)     │
                      │  • listener for agents :4041 (thread)│
                      │  • local agent (same process)        │
                      └──┬───────────────┬───────────────┬───┘
            in-process   │     TCP 4041  │     TCP 4041  │   JSON messages,
                         │   (LAN only)  │               │   shared-key handshake
                ┌────────▼───┐   ┌───────▼──────┐  ┌─────▼────────┐
                │ local agent│   │ agent        │  │ agent        │
                │ (server)   │   │ (laptop)     │  │ (Windows PC) │
                └────┬───────┘   └──────┬───────┘  └──────┬───────┘
                     │ starts           │                 │
                 worker #0          workers #1 #3 …   workers #2 #4 …
                     │                  │                 │
                 Camoufox ──► its group's proxy ──► powerline.io game server
```

What each part never does:

- **Agents never decide anything.** They don't choose bots, names, proxies or restarts. They only do what the coordinator says and report back.
- **Only the coordinator talks to Webshare's API** and needs the Webshare key. Workers only *use* the proxy they were given.
- **Agents dial out to the coordinator.** Only the coordinator needs an open port. The laptop and the Windows PC open no ports at all.
- **Machines never talk to each other**, only to the coordinator.

---

## 5. Multiple machines in depth

### 5.1 The connection, step by step

When you start `python3 agent.py --coordinator 192.168.1.96`, this happens:

1. **TCP connect.** The agent opens a TCP connection to `192.168.1.96:4041`. If that fails (coordinator down, wrong address, firewall), it logs "not reachable" once and then about once a minute, retrying every 5 s forever.
2. **Mutual key check.** The coordinator sends a random challenge and the agent answers with an HMAC of it made with `POWERLINE_AGENT_KEY`. Then the agent challenges the coordinator the same way. The key itself never crosses the network. If either side has a different key, the connection is closed. The agent then **exits** with "rejected the key", because retrying with a wrong key would never work.
   - Every incoming connection gets its own handshake thread on the coordinator. A connection that connects and goes silent can't block other machines from joining, and it's dropped after 10 s without a `hello`.
3. **Hello.** The agent sends `{"type": "hello", "version": "<code version>", …health…}`. Its health includes its name, CPU threads, RAM, current bots, its bot limit and its OS.
4. **Version check.** If the version differs from the coordinator's, the coordinator replies `{"type": "rejected", "reason": "code version X differs from the coordinator's Y; update its files"}` and closes the connection. The agent logs the reason and **exits**.
5. **Join.** The coordinator's main loop picks up the new agent on its next tick (within 1 s) and logs `Agent NAME joined (12 CPU threads, 20000 MB free)`. If a link with the **same name** already exists (for example the machine restarted and came back before the old link timed out), the old link is closed first and its bots are re-placed. The machine then appears on the dashboard.
6. **Steady state.** From then on:
   - The coordinator sends commands whenever it needs to (start, stop, new proxy, screenshot, limit, restart).
   - The agent checks for commands every 0.5 s and sends an `exited` event as soon as one of its workers ends. Every **5 s** it sends a `health` message with CPU %, RAM, which bots it runs, each bot's state, and new log lines (at most 200 per message).
7. **Disconnect.** When the TCP connection breaks, or the coordinator hears nothing for **30 s**, the link is dead:
   - The **coordinator** removes the machine, logs `Agent NAME disconnected; re-placing its bots [...]`, shows a "Machine NAME disconnected" alert for 10 minutes, and restarts those bots on the other machines after 1–3.5 s.
   - The **agent** stops all its workers (if it noticed the break) and goes back to step 1, retrying every 5 s.

Messages are JSON, with each message framed by its length (Python's `multiprocessing.connection`). Pickle is never used, so a connection can only carry data, never code. A message over 1 MB is refused and the connection dropped.

### 5.2 The coordinator's main loop

Everything the coordinator decides happens in one loop that runs **once per second**, in this order:

| Step | What it does |
| --- | --- |
| 1. `add_joined_agents` | Adds machines that finished the handshake since the last tick, and re-applies a bot limit saved from the dashboard for that machine name. |
| 2. `drop_dead_agents` | Reads pending messages, notices closed or silent (30 s) links, and queues their bots to be placed elsewhere. |
| 3. `refresh_health` | Every 5 s: reads each machine's last health, keeps the last 6 CPU readings (30 s) per machine, updates bot states, appends to the 1-hour charts and stores the new log lines. |
| 4. Proxy check | Every `--proxy-check-interval` seconds (60): re-reads Webshare's list and moves groups whose IP disappeared. |
| 5. `handle_exits` | For each worker that exited: counts the failure, benches the proxy if it was a bad-proxy exit, and schedules a restart after the backoff delay (section 6.3). |
| 6. `run_commands` | Runs the buttons you clicked on the dashboard, then saves the state file. |
| 7. `place_pending` | Tries to place bots waiting for a machine (restarts, bots from a machine that left, newly started bots). |
| 8. `rebalance` | At most once a minute: moves one bot off an overloaded machine (section 5.4). |
| 9. Snapshot | Builds the JSON the dashboard reads. |

The dashboard web server and the agent listener run in their own threads. They never change anything themselves: a dashboard click is **queued** and carried out at step 6. That's why a button can take up to about 1 s to show an effect.

### 5.3 Where a new bot goes (placement)

Whenever a bot needs a machine (first start, restart, its machine left, you clicked Start), the coordinator picks one this way:

1. **Skip full machines:** machines at their bot limit. A **paused** machine counts as limit 0.
2. **Skip overloaded machines** (average CPU ≥ 85 % over the last 30 s), unless every machine is overloaded.
3. Of what's left, pick the machine with the **fewest bots per CPU thread**. On a tie, the machine that joined earliest wins. The coordinator's own machine always comes first.
4. If no machine has room, the bot **waits** (state "waiting", plus an alert) and is placed as soon as one has room.

**Worked example, your 3 machines, 15 bots** (server limited to 1 bot, laptop 16 threads, Windows PC 12 threads). This is the output of the real `pick_agent` function:

| Bot | Goes to | Server / Laptop / Windows |
| --- | --- | --- |
| #0 | server | 1 / 0 / 0 |
| #1 | laptop | 1 / 1 / 0 |
| #2 | Windows | 1 / 1 / 1 |
| #3 | laptop | 1 / 2 / 1 |
| #4 | Windows | 1 / 2 / 2 |
| … | … | … |
| #14 | laptop | **1 / 8 / 6** |

So the bots of one group usually end up on **different machines**. That's normal and fine: a group is about the proxy IP, not the machine.

The proxy a bot uses comes from its **group**, not its machine. When a bot is placed, the coordinator sends the agent `start` with the bot's number, room, its group's current proxy, its nickname and a start delay.

**Start delays.** Bots don't all connect at once. At startup, bot *k* of each group waits `k × stagger + 1–3.5 s` (stagger = 3 s by default). Restarts wait the backoff delay (section 6.3). "Start all" staggers the bots the same way.

### 5.4 Overload: moving bots between machines

Once a minute at most, the coordinator checks for an overloaded machine:

- A machine is **hot** if its CPU averaged **≥ 85 %** over a full 30 s window and it runs at least one bot. Readings from a machine that just joined or just had a move are ignored until it has 30 s of new readings, so launch spikes don't count.
- A machine is **cool** if its CPU is **< 65 %** and it's below its bot limit.
- If there's a hot machine and a cool one, the coordinator moves **one** bot (the highest-numbered on the hottest machine) to the cool machine with the fewest bots per thread. The bot keeps its name and group proxy, and the log says `X overloaded (91% CPU over 30s); moving bot #14 'Nova_io' to Y (40% CPU)`.
- Then it waits 60 s and judges again. Overload is fixed gradually, one bot per minute, so it never sends everything flying at once.

Bots **don't** move just because another machine is emptier. A bot only moves when its machine is overloaded, its machine leaves, or you stop and start it. To spread bots onto a machine that just joined, select a few bots on the dashboard and click **Restart**. Each restarted bot goes wherever placement says, usually the new, empty machine.

### 5.5 Limits and pausing per machine

| Control | Where | Effect |
| --- | --- | --- |
| `--max-bots N` | agent command line | The machine never takes more than N bots. |
| `--local-max-bots N` | coordinator command line | Same, for the coordinator's own machine. |
| **Max bots** box + **Set** | dashboard machine card | Same, changed live. Bots above the new limit move elsewhere right away. **Saved by machine name** in the state file and re-applied every time that machine connects, so it overrides the command-line value. Empty box = no limit. |
| **Pause** | dashboard machine card | Moves all its bots elsewhere and gives it no new ones (limit 0). Saved too. **Resume** undoes it. |

> The server's card currently shows a limit of 2. That value was saved from the dashboard, and it wins over `--local-max-bots 1` until you change it on the dashboard again.

### 5.6 Inside one machine

```
agent.py  (one process; on the server it's inside coordinator.py)
 ├── worker #3   (Python process, own process group, own temp HOME)
 │    └── camoufox-bin + its content processes   (the browser)
 ├── worker #7
 │    └── camoufox-bin …
 └── …
```

- **One worker process per bot.** On Linux it's forked from the agent. On Windows it's *spawned*: a fresh Python that re-imports `bot.py`, which is why a Windows PC shows several `python.exe` processes.
- **Own temp folder.** Each worker gets a fresh `HOME` (`/tmp/camoufox_worker_<agent pid>_<bot>_<n>`, or under `%TEMP%` on Windows), deleted when it exits. Browsers never share profiles.
- **Never orphaned.** A worker watches its agent. If the agent dies, the worker kills its browser and exits within about 2 s (instantly on Linux, where the kernel signals it).
- **Stopping a bot** asks the worker to close its browser cleanly. Anything still alive after **8 s** is killed, including every browser process the worker started. Other Camoufox processes on the machine are never touched.
- **Reports** from workers to the agent go through one shared queue: state changes (at most every 5 s, or right away when the state changes), log lines, screenshots, and "my proxy was bad" just before a bad-proxy exit.

### 5.7 How information flows back to you

| What you see | Path |
| --- | --- |
| Bot state (loading / connecting / lobby / playing / dead) | worker → agent queue → next `health` (≤ 5 s) → coordinator → snapshot → dashboard (polls every 2 s). Up to about 8 s behind reality. |
| Rounds, last respawn time | Same path, inside the bot's state. |
| Live logs | Worker log line → agent buffer (last 500) → next `health` (≤ 200 lines) → coordinator (last 400 per machine) → dashboard (polls every 3 s). |
| Machine CPU / RAM, charts | `health` every 5 s → 720 points kept = 1 hour → dashboard (polls every 10 s). |
| 📷 Screenshot | Dashboard → coordinator → agent `screenshot` → worker takes a JPEG (quality 55) → agent → coordinator → the dashboard checks every second, for up to 45 s. |
| Exits / restarts | Worker exits → agent sends `exited` at once → coordinator restarts it and logs an event. |

---

## 6. What happens when something fails

### 6.1 Machine-level events

| Event | What the system does | What you do |
| --- | --- | --- |
| A machine is switched off, sleeps, or loses Wi-Fi | After 30 s of silence the coordinator drops it and restarts its bots elsewhere. The alert shows for 10 min. | Nothing. When it comes back, its agent reconnects by itself, but bots don't move back on their own (5.4). |
| The network blips briefly | If the TCP connection survives, nothing. If it breaks, the agent stops its bots and reconnects, and the coordinator re-places the bots. | Nothing. |
| An agent crashes or is killed | Its workers notice and close their browsers. The coordinator drops the machine and re-places its bots. | Restart the agent (autostart does it for you, section 12). |
| **Restart agent** clicked on Linux | The agent stops its bots and restarts itself in place (same PID, so systemd keeps tracking it), loading any new code. It rejoins within seconds. | Nothing. |
| **Restart agent** clicked on Windows | The agent stops its bots and **exits**. Something must start it again: `start-agent.cmd`'s loop does that after 10 s. | **If the agent wasn't started through `start-agent.cmd` (or its scheduled task), it stays off.** Start it again by hand. |
| The coordinator stops or crashes | Every agent notices the broken connection, stops its bots and retries every 5 s. Bots on the server's local agent die with it. | Start the coordinator again (or let systemd do it). Names, limits, paused machines and settings come back from the state file. |
| An agent with old code connects | Rejected with "code version … differs". The agent exits. | Update that machine's files (13.2). |
| An agent with the wrong key connects | The agent exits with "rejected the key". The coordinator logs "Refused a connection". | Fix `POWERLINE_AGENT_KEY` on that machine. |
| Two machines have the same hostname | The second one replaces the first one's link each time it connects, so they keep kicking each other off. | Give one of them `--name something-else`. |

### 6.2 Bot-level events

| Event | What happens |
| --- | --- |
| Worker crashes (browser error, Python error) | Exit reported → restart on any machine with the same name and its group's current proxy, after the backoff delay. |
| Browser can't start through the proxy, or the game stays on "connecting" through 2 reloads | Exit code 3 ("bad proxy") → the proxy is benched for 30 min, the group moves to another IP, and the bot restarts (7.3). |
| Stuck out of the game for 30 s | The worker reloads the page itself, without restarting. |
| The group's IP changes (rotation) | The coordinator sends `update_proxy` → each bot opens a fresh browser context on the new IP and reloads the game. The browser keeps running. |

### 6.3 Restart backoff

A bot that keeps failing waits longer before each restart, so a broken bot can't start a restart storm:

| Failures in a row | Wait before restart |
| --- | --- |
| 1 | 1–3.5 s |
| 2 | 2–4 s |
| 3 | 4–6 s |
| 4 | 8–10 s |
| 5 | 16–18 s |
| 6 | 32–34 s |
| 7+ | 60–62 s |

The count goes back to 0 once a bot has run for **2 minutes** without exiting. After 3 failures in a row, the bot appears in the "keep failing" alert.

---

## 7. Proxies and Webshare

### 7.1 Reading the list
- At start and every 60 s, the coordinator reads **all pages** of your Webshare proxy list (`/api/v2/proxy/list/`, 100 per page) and keeps the ones marked valid.
- It also reads your plan's automatic rotation period once (300 s on your plan) and the list's creation time. That's what drives the "Next IP change" countdown on the dashboard.
- If Webshare doesn't answer, the coordinator keeps using the last list it got and shows the "Webshare isn't answering" alert.

### 7.2 Giving groups IPs
- Each group keeps its IP as long as that IP is still in the list and not benched.
- A group whose IP disappeared, or got benched, moves to the **least-used** IP. On a tie, it takes the IP that comes first in Webshare's list. If you have more groups than IPs, IPs get shared evenly.
- When a group moves, every running bot of that group gets `update_proxy` and switches without restarting its browser.

### 7.3 Bad proxies & Automated Webshare API v3 Rotation
A worker exits with code 3 when its browser can't connect through the proxy, when the game server blocks its WebSocket handshake (HTTP 400 Bad Request / 403 / 407), or when the game stays on "connecting" through stuck reloads. It tells the agent which proxy failed. The coordinator then:
1. benches that IP for **30 minutes**,
2. automatically dispatches an asynchronous background request to **Webshare API v3 (`/api/v3/proxy/replace/`)** to replace the blocked IP with a fresh IP from your replacement pool,
3. as soon as Webshare completes the replacement, re-fetches the proxy list, moves the group to the fresh clean IP, and pushes the change to any running bots via `update_proxy` without restarting their browsers!
4. restarts the bot.

To scan and replace all flagged proxies up front before launching any bots, run:
```bash
python3 coordinator.py --clean-proxies
```
This probes the WebSocket handshake of every proxy in the pool against the game server and automatically replaces flagged IPs round-by-round until 0 proxies are flagged.

If every proxy is benched, the coordinator uses them all anyway rather than run nothing, and shows the "Every proxy is benched" alert.

### 7.4 HTTP 407 right after a rotation
For a few seconds after Webshare rotates, old proxies answer `407 Proxy Authentication Required`. That's timing, not an account problem, and it clears at the next list refresh.

---

## 8. What a bot does in its browser

1. **Starts:** the worker waits its start delay, then launches Camoufox through its proxy (720×480 window, headless with `--headless`).
2. **Loads** `https://powerline.io/#<room>`: it dismisses consent and reconnect pop-ups, handles the Turnstile check if one shows, switches the game to low graphics, and waits up to 30 s for the name box (answering screenshot requests and proxy changes meanwhile). State: **loading**, then **connecting** if the game says "Connecting…".
3. **Joins:** types its nickname the first time (humanized, with typos that get corrected) and presses Enter. State: **lobby**, then **playing**.
4. **Plays:** while the game's menu overlay is hidden, it steers with arrow keys, holding each direction at least 0.28 s and never reversing 180°.
5. **Dies** (overlay reappears). State: **dead**. It waits a random delay (about 80 % under 2 s, at most 15 s) and presses Enter. The game ignores input for about 2 s after a death, so most respawns land at 2.4–2.9 s. That's the "Last respawn" column.
6. **Gets stuck** out of the game for 30 s: reloads the page. Two failed reloads in a row → bad-proxy exit (7.3).
7. **Reports** its state every 5 s (immediately when it changes) and writes a heartbeat log line every 30 s.

---

## 9. One-time setup

### 9.1 Every machine

```bash
git clone git@github.com:fernand01-hu/powerling-bot.git powerline-bot
cd powerline-bot
git checkout humanized-input
pip install -r requirements.txt        # camoufox[geoip], numpy, psutil
python3 -m camoufox fetch              # downloads the browser (~2.4 GB unpacked)
```

On Windows, use `python` instead of `python3`. You need Python 3.10 or newer (tested on 3.10 and 3.14).

> **Always finish `camoufox fetch` before starting any bots.** If the browser is missing, Camoufox downloads it *by itself* the moment a bot starts. With several bots starting at once, they all download into the same folder at the same time and corrupt it ("Camoufox is not installed" or "The installation seems to be corrupt" in the logs). To check that it's complete:
> ```bash
> python3 -c "from camoufox.sync_api import Camoufox; b=Camoufox(headless=True).__enter__(); print('OK'); b.close()"
> ```

### 9.2 Secrets

| Variable | Needed on | What it is |
| --- | --- | --- |
| `POWERLINE_AGENT_KEY` | **every** machine, same value | Shared secret for the coordinator ↔ agent link. Generate one with `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`. |
| `WEBSHARE_API_KEY` | coordinator only | Your Webshare API key. |
| `POWERLINE_DASHBOARD_PASSWORD` | coordinator only | Dashboard login password (any user name works). |

- **Linux, for one terminal:** `export POWERLINE_AGENT_KEY=...`
- **Linux, for the services:** put them in `~/.config/powerline-bot/env` (`chmod 600`). The server already has this file. To start the coordinator by hand with it: `set -a; . ~/.config/powerline-bot/env; set +a`.
- **Windows, permanently:** `setx POWERLINE_AGENT_KEY "..."`. This only affects **new** command prompts.
- **Windows, for one prompt:** `set POWERLINE_AGENT_KEY=...` (no quotes, no spaces around `=`).

> Never commit these values or put them in a file inside the repo. If you change the agent key, change it on **every** machine and restart everything.

### 9.3 Network and firewall

| Port | Where | Who connects | Open to |
| --- | --- | --- | --- |
| 4041/TCP | coordinator | agents | your LAN only |
| 4042/TCP | coordinator | your browser (dashboard) | your LAN only |
| 22/TCP | each machine | you (SSH) | LAN. Only the server's SSH is forwarded from the internet (public port 4040). |

The server runs `ufw`. To allow a new LAN machine, if needed: `sudo ufw allow from 192.168.1.0/24 to any port 4041,4042 proto tcp`. **Never forward 4041 or 4042 on the router.**

### 9.4 Your machines

| Machine | Role | LAN address | CPU threads | Notes |
| --- | --- | --- | --- | --- |
| Old server `fernand-ThinkCentre-M72e` | coordinator + dashboard + 1–2 bots | 192.168.1.96 | 4 | Code in `~/powerline-bot`. Secrets in `~/.config/powerline-bot/env`. Log in `~/coordinator.log` when started by hand. |
| Laptop `fernand-Aspire-A315-44P` | agent | 192.168.1.187 | 16 | **Turn off auto-suspend**, or it sleeps and drops out. About 15 bots overloads it. |
| Windows PC `DESKTOP-HOQA23I` | agent | 192.168.1.172 | 12 | Code in `C:\Users\Fernand\powerline-bot` (copied files, **not** a git checkout). Start it through `start-agent.cmd`. Its clock runs a few minutes off. |

---

## 10. Running it

### 10.1 Start order

The order doesn't matter: agents keep retrying until the coordinator is up, and the coordinator waits `--wait-agents` seconds for agents before it places bots. A good routine:

**1. Server (coordinator + dashboard):**
```bash
cd ~/powerline-bot
set -a; . ~/.config/powerline-bot/env; set +a
python3 coordinator.py --listen 0.0.0.0 --dashboard 0.0.0.0 --headless \
    --local-max-bots 1 --groups 5 --bots-per-proxy 3 --wait-agents 40
```
To keep it running after you close SSH, start it with `nohup … > ~/coordinator.log 2>&1 &`, or better, use the service (section 12).

**2. Laptop (agent):**
```bash
cd ~/playground/powerline-bot
export POWERLINE_AGENT_KEY=...           # same as the server's
python3 agent.py --coordinator 192.168.1.96 --headless --max-bots 6
```

**3. Windows PC (agent)**, in a command prompt:
```bat
cd C:\Users\Fernand\powerline-bot
set POWERLINE_AGENT_KEY=...
start-agent.cmd
```
Use `start-agent.cmd`, not `python agent.py` directly, so the dashboard's **Restart agent** button can bring it back (6.1). Its output goes to `agent.log` in that folder.

**4.** Open `http://192.168.1.96:4042`, log in with any user name and the dashboard password, and check that all 3 machines show up.

### 10.2 Stopping
- **Everything:** Ctrl+C in the coordinator (or `systemctl --user stop powerline-coordinator`). Agents stop their bots and wait for the coordinator to come back.
- **One machine:** Ctrl+C in its agent (Ctrl+Break on Windows). Its bots move to the other machines.
- **Bots only, keep everything else up:** **Stop all** on the dashboard.

### 10.3 All options

**Coordinator**

| Option | Default | Meaning |
| --- | --- | --- |
| `--room CODE` | `2cjg` | Room code (the part after `#` in the game URL). |
| `--groups N` | 3 | Number of groups (one IP each). |
| `--bots-per-proxy N` | 3 | Bots per group. Total bots = groups × this. |
| `--stagger-interval S` | 3.0 | Average seconds between the bots of a group connecting. |
| `--proxy-check-interval S` | 60 | Seconds between Webshare list refreshes. |
| `--headless` | off | No visible browser windows (recommended). |
| `--listen HOST[:PORT]` | off | Accept agents from other machines (use `0.0.0.0`; port 4041). Needs `POWERLINE_AGENT_KEY`. |
| `--wait-agents S` | 15 | With `--listen`: seconds to wait for agents before placing bots. |
| `--dashboard HOST[:PORT]` | off | Serve the dashboard (port 4042). Needs `POWERLINE_DASHBOARD_PASSWORD`. |
| `--local-max-bots N` | no limit | Most bots on the coordinator's machine. A limit saved from the dashboard overrides it. |
| `--no-local-agent` | off | Coordinator's machine runs no bots (needs `--listen`). |
| `--start-stopped` | off | All bots start stopped; press **Start all** on the dashboard. |
| `--state-file PATH` | `~/.config/powerline-bot/state.json` | Where names, limits and settings are saved. |
| `--debug-dir DIR` | off | Save screenshots at key moments. |
| `--webshare-key KEY` | `$WEBSHARE_API_KEY` | Prefer the env variable, since command lines are visible to other users. |

> Settings saved from the dashboard (room, groups, bots per group, check interval, stagger) are loaded from the state file at start and **override** these options.

**Agent**

| Option | Default | Meaning |
| --- | --- | --- |
| `--coordinator HOST[:PORT]` | required | The coordinator's address, e.g. `192.168.1.96`. |
| `--max-bots N` | no limit | Most bots on this machine (a limit saved from the dashboard overrides it). |
| `--name NAME` | hostname | Name on the dashboard. Must be unique. |
| `--headless` | off | No visible browser windows. |
| `--debug-dir DIR` | off | Save screenshots at key moments. |

### 10.4 One machine only
Leave out `--listen`. The coordinator then runs all bots itself: `python3 coordinator.py --groups 2 --bots-per-proxy 2 --headless`. Never run two coordinators at the same time, on one machine or several. Each one would start its own full set of bots.

---

## 11. The dashboard

The page refreshes itself every 2 s. The dot at the top turns green while it reaches the coordinator.

### Tiles
**Playing** (of all bots), **Not in game** (and why), **Machines** connected, **Rounds played** (since each bot started), and **Next IP change** (countdown to Webshare's rotation).

### Alerts
| Alert | Meaning | What to do |
| --- | --- | --- |
| No machine is connected | No agent online, not even the local one. | Check agents and the key. |
| *X* is overloaded | CPU ≥ 85 % for 30 s. | Bots move automatically if another machine is under 65 %. Otherwise lower that machine's limit. |
| N bot(s) waiting for a machine with room | Every machine is full, paused or overloaded. | Raise a limit, resume a machine, or add one. |
| N bot(s) keep failing | 3+ exits in a row. | Read their logs and take a 📷. A machine that can't launch browsers shows up here first (15). |
| Every proxy is benched | No proxy reached the game in the last 30 min. | Usually the game server refusing the IPs. |
| Webshare isn't answering | API error or timeout. | Check the key and the account. |
| Machine *X* disconnected (N min ago) | It left in the last 10 min. | Check that machine. |

Click ✕ to hide an alert. It comes back if the problem is still there.

### Machines
One card per machine: CPU, RAM, bots, OS. **Pause/Resume**, **Max bots + Set**, **Restart agent**, **Stop its bots** (5.5, 6.1). On the coordinator's own machine, **Restart agent** restarts its bots instead, since it can't restart the coordinator.

### Bots
Columns: #, name, state (with how long it has been in it), machine, group, IP, rounds, last respawn time.

| State | Meaning |
| --- | --- |
| stopped | You stopped it; it stays stopped (even after a coordinator restart). |
| waiting | No machine has room. |
| starting | Sent to a machine; waiting out its start delay or launching the browser. |
| loading | Loading the game page. |
| connecting | Page loaded, but the game shows "Connecting…" (a refused IP stays here). |
| lobby | On the menu, about to join or respawn. |
| playing | In the game. |
| dead | Just died; respawning. |

Per-row buttons: **📷** (current view of that browser; allow up to 45 s), **Restart**, **Stop** (or **Start**). Tick checkboxes for the bulk bar (Start / Stop / Restart the selection). **Stop all / Start all** sit at the top of the table.

### Last hour
Charts of CPU and RAM per machine, and of bots running vs playing (one point per 5 s). Hover a chart for exact values.

### Proxies
Which group is on which IP, benched IPs and when they come back, the last Webshare refresh. **Refresh now** re-reads Webshare. **Groups** (1–50) changes the number of groups: new groups' bots start, removed groups' bots stop.

### Settings
| Setting | Range | Effect |
| --- | --- | --- |
| Room | 1–16 letters/digits | Restarts all bots in the new room. |
| Bots per group | 1–10 | Restarts all bots. |
| Proxy check interval | 15–3600 s | Applies right away. |
| Join stagger | 0–60 s | Applies to the next joins. |

All of it is saved in the state file and survives restarts.

### Live logs
Lines from every machine and bot, filterable by machine, bot and level (all, or warnings and errors only). Under it, **Coordinator events**: joins, disconnects, moves, IP changes, benchings, restarts.

---

## 12. Autostart

Without autostart, everything stops when the terminal or SSH session that started it closes, or when the machine reboots.

### 12.1 Server: coordinator as a systemd user service

```bash
mkdir -p ~/.config/systemd/user
cp ~/powerline-bot/deploy/powerline-coordinator.service ~/.config/systemd/user/
nano ~/.config/powerline-bot/env       # the 3 secrets + COORDINATOR_ARGS (see deploy/env.example)
chmod 600 ~/.config/powerline-bot/env
systemctl --user daemon-reload
systemctl --user enable --now powerline-coordinator
loginctl enable-linger                  # run at boot without anyone logged in
```

Example `COORDINATOR_ARGS` line:
```
COORDINATOR_ARGS=--listen 0.0.0.0 --dashboard 0.0.0.0 --headless --local-max-bots 1 --groups 5 --bots-per-proxy 3 --start-stopped
```
With `--start-stopped`, the bots wait for **Start all** after a reboot. Remove it to have them start on their own.

Everyday commands:
```bash
systemctl --user status powerline-coordinator
journalctl --user -u powerline-coordinator -f      # live log
systemctl --user restart powerline-coordinator     # e.g. after new code
```
Stop any hand-started coordinator first (`pkill -INT -f coordinator.py`). Two coordinators can't share port 4041.

### 12.2 Laptop: agent as a systemd user service
Same steps with `deploy/powerline-agent.service`. Its env file only needs:
```
POWERLINE_AGENT_KEY=...
AGENT_ARGS=--coordinator 192.168.1.96 --headless --max-bots 6
```
Then `systemctl --user enable --now powerline-agent`, and `loginctl enable-linger`.

### 12.3 Windows PC: scheduled task at logon
In a normal command prompt:
```bat
setx POWERLINE_AGENT_KEY "same-secret-as-the-server"
schtasks /Create /TN "Powerline agent" /SC ONLOGON /RL LIMITED /F /TR "\"%USERPROFILE%\powerline-bot\start-agent.cmd\""
schtasks /Run /TN "Powerline agent"
```
`start-agent.cmd` runs the agent and starts it again 10 s after it exits. That also covers the **Restart agent** button. Output goes to `C:\Users\Fernand\powerline-bot\agent.log`. To stop it: `schtasks /End /TN "Powerline agent"`, then end the `python.exe` processes. Set the PC's power plan to never sleep.

---

## 13. Day-to-day recipes

### 13.1 Start a session
1. Check that the coordinator is running: open the dashboard.
2. Check that the **Machines** section shows all 3 machines.
3. If bots are stopped: **Start all**.
4. After a few minutes, check that the **Playing** tile is going up.

### 13.2 Update the code on all machines
The coordinator refuses agents whose code differs, so update everything together:
1. **Laptop / server** (git checkouts): `cd …/powerline-bot && git pull`.
2. **Windows PC** (copied files): from the laptop,
   `scp bot.py agent.py humanizer.py Fernand@192.168.1.172:C:/Users/Fernand/powerline-bot/`
3. Check that the versions match: `python3 -c "import agent; print(agent.code_version())"` on each machine (`python` on Windows).
4. Restart the coordinator, then click **Restart agent** on each remote machine. Linux reloads the code in place; on Windows the `start-agent.cmd` loop starts the new code.

### 13.3 Add a 4th machine
1. Install (9.1) and run `camoufox fetch` to the end.
2. Copy or clone the **same** code; the version must match.
3. Set the **same** `POWERLINE_AGENT_KEY`.
4. Check it can reach the coordinator: `nc -vz 192.168.1.96 4041` (Linux) or `Test-NetConnection 192.168.1.96 -Port 4041` (PowerShell).
5. Start `agent.py --coordinator 192.168.1.96 --headless`, with `--max-bots` if it's weak.
6. On the dashboard, select a few bots and **Restart** them so they move onto it (5.4).
7. Set up autostart (12).

### 13.4 Take a machine out
- **For a while:** **Pause** it on the dashboard. Its bots move away and it stays connected.
- **For good:** **Pause** it, then stop its agent (and disable its service or task).

### 13.5 Change how many bots run
- **More or fewer groups:** Proxies → **Groups**.
- **More or fewer bots per group:** Settings → **Bots per group** (restarts all bots).
- **Total bots** = groups × bots per group. Check you have enough machine capacity (14).

### 13.6 Look into one bot
1. Click its 📷 to see its screen.
2. Live logs → filter on that bot.
3. If it's in "keep failing", check which machine it was on. If every failing bot is on one machine, that machine has a problem (15).

### 13.7 Start completely fresh
Stop the coordinator, delete `~/.config/powerline-bot/state.json` on the server, and start again. You get new names, no saved limits, and the command-line settings.

---

## 14. Capacity planning

Each bot is a full browser. What has been measured on your machines:

| Machine | Threads | Observed |
| --- | --- | --- |
| Windows PC | 12 | 8 bots → about 55–75 % CPU |
| Laptop | 16 | 15 bots → load 70–85, badly overloaded; 3–4 bots → 15–30 % CPU |
| Server | 4 | 1–2 bots plus the coordinator → 20–70 % CPU |

Rules of thumb:
- Plan for roughly **one bot per 1.5 CPU threads** and leave headroom, because launches and rotations cause CPU spikes.
- Always use `--headless`. Visible windows cost much more.
- Keep the coordinator's machine light (`--local-max-bots 1`). If it's overloaded, the whole system responds slowly.
- Every 5 minutes all IPs rotate and every bot reloads at once. That's the heaviest moment, so judge capacity then, not when things are quiet.

---

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Bots sit on **connecting**, proxies get benched | The game server refuses those IPs (operator block). | Nothing in this code changes that. Bots on IPs it accepts play normally. |
| Every bot **on one machine** keeps failing within seconds | That machine can't launch the browser. | Look at its log for `Camoufox is not installed` / `installation seems to be corrupt`: stop its agent, delete `~/.cache/camoufox/browsers/official/<version>`, run `python3 -m camoufox fetch` once, test (9.1), start the agent again. |
| A machine never shows up | Wrong key, wrong address, firewall, PC off or asleep. | Check the agent's own output. `nc -vz 192.168.1.96 4041`. |
| Agent says **rejected: code version … differs** | Old code on that machine. | 13.2. |
| Agent says **rejected the key** | Different `POWERLINE_AGENT_KEY`. | Same key everywhere. On Windows, `setx` only affects new prompts. |
| Windows machine vanished after **Restart agent** | Agent wasn't started via `start-agent.cmd`. | Start it with `start-agent.cmd` (10.1) or the scheduled task (12.3). |
| A machine keeps disconnecting and coming back | Sleep/suspend, flaky Wi-Fi, or two machines with the same name. | Disable sleep; use a cable; use `--name`. |
| Bots don't move to a new machine | By design: bots only move on overload or restart (5.4). | Select some bots → **Restart**. |
| Bots "waiting" | Every machine full, paused or overloaded. | Raise limits, resume, or add a machine. |
| Twice as many browsers as bots | Two coordinators running (e.g. an old one on the laptop). | Keep only the server's. Check with `pgrep -af coordinator.py` on each Linux machine. |
| Many HTTP 407 for a few seconds | Webshare rotation. | Normal. |
| "Webshare isn't answering" | Key wrong/expired, or account issue. | Check `WEBSHARE_API_KEY`. |
| Dashboard login fails | Wrong password, or the coordinator started without the new one. | It's `POWERLINE_DASHBOARD_PASSWORD` as the coordinator saw it at start. Restart it after changing the password. |
| Dashboard dot grey, buttons do nothing | Coordinator stopped or unreachable. | Check it (`systemctl --user status powerline-coordinator` or `pgrep -af coordinator.py` on the server). |
| A setting "won't stick" | A value saved in the state file overrides the command line (10.3, 5.5). | Change it on the dashboard, or delete the state file. |
| Leftover browsers after a hard kill | Worker killed without clean-up. | Only when no bots should run: `pkill -f camoufox-bin` (Linux) or end `camoufox.exe` in Task Manager. |
| Screenshot never arrives | Bot is still starting, or it just restarted. | Try again once it's loading or later. |

For a deeper look, start agents with `--debug-dir ./debug`: screenshots at load, spawn, death, and every 30 s in game.

---

## 16. Reference

### 16.1 Messages: coordinator → agent

| `type` | Fields | Effect |
| --- | --- | --- |
| `start` | `bot`, `room`, `proxy`, `nickname`, `start_delay` | Start a worker for that bot. |
| `update_proxy` | `bot`, `proxy` | The bot switches to the new proxy without restarting its browser. |
| `stop` | `bots` (list) | Stop those workers (clean close, killed after 8 s). |
| `stop_all` | – | Stop every worker on the machine. |
| `set_max_bots` | `max_bots` (number or null) | New bot limit. |
| `screenshot` | `bot` | Ask that bot for a picture. |
| `restart` | – | Stop all bots and restart the agent (6.1). |
| `rejected` | `reason` | Sent instead of joining; the agent exits. |

`proxy` = `{"host", "port", "user", "pass", "country"}`.

### 16.2 Messages: agent → coordinator

| `type` | When | Fields |
| --- | --- | --- |
| `hello` | Once, right after the key check | `version` + all health fields |
| `health` | Every 5 s | `agent`, `cpu_percent`, `cpu_count`, `mem_available_mb`, `mem_total_mb`, `bots`, `max_bots`, `platform`, `bot_status` (per bot: state, rounds, last respawn…), `logs` (new lines) |
| `exited` | A worker ended on its own | `bot`, `code`, `bad_proxy` (true for exit code 3), `proxy` (the proxy that failed, or null) |
| `screenshot` | A requested picture is ready | `bot`, `data` (base64 JPEG), `time` |

### 16.3 Dashboard HTTP API

All requests need HTTP Basic auth (any user, the dashboard password).

| Method & path | Returns / does |
| --- | --- |
| `GET /` | The dashboard page. |
| `GET /api/state` | Everything shown: machines, bots, proxies, alerts, settings, events. |
| `GET /api/logs?machine=NAME&bot=N&limit=200` | Recent log lines (filters optional). |
| `GET /api/history` | Last hour of CPU/RAM per machine and bots running/playing. |
| `GET /api/screenshot?bot=N` | Latest screenshot from that bot (base64 JPEG). |
| `POST /api/command` | Runs an action. Needs the header `X-Requested-With: dashboard` and a JSON body of at most 4 KB. |

Actions for `POST /api/command`:

| `action` | Extra fields |
| --- | --- |
| `stop_bot` / `start_bot` / `restart_bot` | `bot` |
| `stop_bots` / `start_bots` / `restart_bots` | `bots` (list) |
| `stop_all` / `start_all` | – |
| `screenshot` | `bot` |
| `pause_machine` / `resume_machine` / `restart_machine` / `stop_machine_bots` | `machine` (name) |
| `set_max_bots` | `machine`, `max_bots` (number, or null for no limit) |
| `set_groups` | `groups` (1–50) |
| `set_settings` | any of `room`, `bots_per_proxy`, `proxy_check_interval`, `stagger_interval` |
| `refresh_proxies` | – |

Example, from any LAN machine:
```bash
curl -u "me:$POWERLINE_DASHBOARD_PASSWORD" http://192.168.1.96:4042/api/state | python3 -m json.tool | less
curl -u "me:$POWERLINE_DASHBOARD_PASSWORD" -H "X-Requested-With: dashboard" \
     -H "Content-Type: application/json" -d '{"action":"restart_bots","bots":[2,5,7]}' \
     http://192.168.1.96:4042/api/command
```

### 16.4 State file (`~/.config/powerline-bot/state.json` on the server)

```json
{
  "nicknames": ["Havoc_GG", "Aura588", "Phantom_X", "..."],
  "paused": [],
  "stopped": [],
  "max_bots": {"fernand-ThinkCentre-M72e": 2},
  "settings": {"room": "2cjg", "groups": 5, "bots_per_proxy": 3,
               "proxy_check_interval": 60.0, "stagger_interval": 3.0}
}
```
It's written after every dashboard command, through a temporary file, so it's never half-written. Edit it only while the coordinator is stopped.

### 16.5 Timing constants

| Constant | Value | Where |
| --- | --- | --- |
| Agent health report | every 5 s | `agent.py` `HEALTH_INTERVAL` |
| Agent reconnect | every 5 s | `RECONNECT_DELAY` |
| Silent agent counted as gone | 30 s | `coordinator.py` `AGENT_TIMEOUT` |
| CPU averaging window | 6 samples = 30 s | `CPU_WINDOW` |
| Overloaded / room to take a bot | ≥ 85 % / < 65 % | `OVERLOAD_CPU` / `HEADROOM_CPU` |
| Between overload moves | 60 s | `REBALANCE_COOLDOWN` |
| Proxy bench time | 30 min | `BAD_PROXY_COOLDOWN` |
| Min gap between failure-triggered Webshare reads | 15 s | `MIN_REFETCH_INTERVAL` |
| Run time that resets the backoff | 120 s | `HEALTHY_RUN` |
| Failures before "keep failing" | 3 | `FAILING_STREAK` |
| Past alerts kept | 10 min | `ALERT_MEMORY` |
| Lobby wait / stuck-reload watchdog | 30 s / 30 s | `bot.py` |
| Failed reloads before bad proxy | 2 | `MAX_FAILED_RELOADS` |
| Bot state report | every 5 s or on change | `STATUS_INTERVAL` |
| Clean-stop grace before kill | 8 s | `Agent.stop_bots` |
| Largest message | 1 MB | `MAX_MESSAGE` |

---

## 17. Security checklist

- [ ] Ports **4041 and 4042 are not forwarded** on the router. Only SSH (public 4040) is, and 4041 was checked to be unreachable from outside.
- [ ] Switch to **SSH keys** for the server and the Windows PC, then **change both passwords** (they were typed into a chat).
- [ ] **Regenerate the Webshare proxy-list download token** (it was shown once in a log). Keep the API key only in env files.
- [ ] `~/.config/powerline-bot/env` is `chmod 600` (it is on the server).
- [ ] Use a long random `POWERLINE_AGENT_KEY`. Anyone with it can join as a machine and receive proxy logins.
- [ ] Optional: purge the old pre-cleanup git objects locally: `git reflog expire --expire=now --all && git gc --prune=now`. GitHub history is already clean.

## 18. Tests

```bash
python3 -m unittest discover -s tests     # 215 tests, about 17 s
```

Fakes (`tests/fakes.py`) stand in for browsers, pages, agents and Webshare, so the tests never touch the network. They cover the humanizer, the bot logic, the coordinator (placement, proxies, backoff, alerts, state file), the agent link (JSON only, size limit, key check, reconnects, version rejection), and the dashboard (login, headers, every button and command). Run them after every change. If they pass on one machine, copy the code to the others (13.2).
