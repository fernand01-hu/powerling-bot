# Powerline.io Multi-Instance Bot

A humanized, multi-instance browser bot for [powerline.io](https://powerline.io/) built with Python and Selenium Chrome WebDriver. It supports Webshare HTTP proxies, automatic IP rotation, ultra-low proxy bandwidth consumption (~100–130 KB per bot session), server/region selection, immediate auto-respawn on death, and background/minimized window execution without stealing window focus.

---

## Features

- **Multi-Instance Execution**: Launch multiple bots concurrently (default: `4` instances) in separate threads.
- **Pre-Flight Proxy Health Check & Filtering**:
  - Automatically verifies all proxies in parallel against the target game server's WebSocket endpoint in ~1.5 seconds on startup.
  - Skips any proxy IPs rejected with `400 Bad Request` or connect errors by the game server, ensuring only 100% verified working IPs are assigned.
  - Automatically calculates capacity and adjusts `--bots-per-ip` if necessary so all requested bot instances run reliably.
- **Proxy Pool & Automatic IP Rotation**:
  - Reads `host:port:username:password` proxies from a text file (default: `Webshare 10 proxies(4).txt`).
  - Distributes bots evenly across active verified proxies.
  - Rotates each bot to a fresh proxy IP every **5 minutes** (`--ip-rotate-seconds 300` with ±8% human jitter) without restarting Chrome.
  - Automatically detects and skips proxy IPs that trigger a persistent Cloudflare Turnstile challenge.
- **~98% Proxy Bandwidth Reduction (~100–150 KB per bot)**:
  - Pre-seeds a local static asset cache (`/tmp/powerline_http_cache_seed`) once and copies it into each bot's isolated `--disk-cache-dir` so the 2.5 MB game bundle (`index.x_1H9pPP.mjs`), CSS, fonts, and sprites cost **0 bytes** of proxy traffic.
  - Blocks all ad networks, analytics, trackers, audio files (`*.ogg`, `*.mp3`), and Chrome background updater traffic (`*.gvt1.com`, `*.googleapis.com`) at both the socket and CDP levels.
- **100% Isolated Cookies & Storage**:
  - Each bot runs in its own temporary `--user-data-dir` profile and clears cookies/`localStorage`/`sessionStorage` whenever its IP rotates.
- **Ultra-Low Graphics & Resolution**:
  - Automatically zooms Chrome in to **500%** (`Ctrl` + `+` / `setDefaultZoom(5.0)`) and toggles **Low Graphics** (`G` key, `highQuality = false`), shrinking the WebGL canvas for minimal CPU/GPU usage.
- **Human-Like Input & Stealth**:
  - Uses `--disable-blink-features=AutomationControlled` to pass Cloudflare Turnstile.
  - Types nicknames, alternates between `WASD` and `Arrow` keys across bots, varies turn intervals (`0.75x`–`1.30x`), randomly switches between clockwise (`CW`) and counter-clockwise (`CCW`) circles, and adds subtle mouse movements.
- **Immediate Death Detection & Respawn**:
  - Continuously monitors `localPlayer` state (`50ms`–`60ms` polling). If a bot crashes into another snake or wall mid-circle, it respawns immediately.
  - At the end of each round's circle timer, presses `K` (`keyCode = 75`) to self-destruct and respawn for the next round.
- **No Focus Stealing & Headless Minimize Mode**:
  - Preserves your active X11 window focus (`xprop` + `wmctrl`) when launching Chrome so it never interrupts your work.
  - Optional `--minimize` (`-m`) flag runs bots headlessly in the background (`--headless=new`) without creating desktop windows or stealing focus.

---

## Requirements

- **OS**: Linux (X11/XWayland)
- **System Packages**:
  - `google-chrome` (or `chromium`)
  - `wmctrl` and `x11-utils` (`xprop`) for window focus preservation and `--minimize` mode
- **Python Packages**:
  ```bash
  pip install selenium
  ```

---

## Proxy File Format

Place your Webshare proxy list in the project folder (default filename: `Webshare 10 proxies(4).txt`). Each line must follow the format:

```text
host:port:username:password
```

Example:
```text
64.137.96.74:6641:myuser:mypassword
198.23.243.226:6361:myuser:mypassword
```

---

## Quick Start Examples

### 1. List All Available Servers & Room Hashes
```bash
python3 bot.py --list-servers
```
Example output:
```text
Available Powerline.io Servers:
--------------------------------------------------------------------
Region             Room Hash    Server IP:Port         Players
--------------------------------------------------------------------
US East            #72d5        15.204.229.101:8180    6
Europe             #2cjg        51.91.214.104:8187     11
US West            #7620        51.81.210.172:8180     0
Asia               #f06c        15.235.182.97:8180     0
South America      #fd2d        45.79.193.153:8180     0
--------------------------------------------------------------------
```

### 2. Run 4 Bots on the Europe Server (Minimized)
```bash
python3 bot.py --server "Europe" --minimize
```

### 3. Join a Specific Room by Hash or Full Link
Allows all bots to join the exact same room where you are playing:
```bash
python3 bot.py --server "2cjg" --minimize
# or
python3 bot.py --server "https://powerline.io/#2cjg" --minimize
```

### 4. Run 6 Bots with Custom Loops, Duration, and Nickname
```bash
python3 bot.py --instances 6 --bots-per-ip 2 --server "Europe" --nick "SnakeBot" --loops 10 --duration 8 --minimize
```

### 5. Run with Shorter IP Rotation Interval (e.g., Rotate IP Every 2 Minutes)
```bash
python3 bot.py --server "Europe" --ip-rotate-seconds 120 --minimize
```

---

## Command-Line Options

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--list-servers` | `False` | Fetch and print all live Powerline.io regions, room hashes, and player counts, then exit. |
| `--server` | `None` (Auto) | Target server region (`"Europe"`, `"eu"`, `"US East"`, `"na"`, `"US West"`, `"Asia"`, `"South America"`, `"sa"`), room hash (`"2cjg"` or `"#2cjg"`), or URL (`"https://powerline.io/#2cjg"`). |
| `-m`, `--minimize` | `False` | Minimize/hide all bot Chrome windows while keeping the game loop running at 60 FPS. |
| `--instances` | `4` | Number of concurrent bot instances to run. |
| `--bots-per-ip` | `2` | Maximum number of bots sharing each proxy IP simultaneously. |
| `--proxy-file` | `"Webshare 10 proxies(4).txt"` | Path to the proxy list file (`host:port:user:pass`). If missing, connects directly. |
| `--ip-rotate-seconds` | `300.0` | Seconds before each bot rotates to a new proxy IP (`300` = 5 minutes ± jitter). Set to `0` to disable rotation. |
| `--nick` | `"CircleBot"` | Base nickname for the bots (`CircleBot1`, `CircleBot2`, ...). |
| `--loops` | `4` | Number of rounds (spawn -> circle -> press `K` to self-destruct) each bot plays. |
| `--duration` | `5.0` | Base duration (in seconds) to move in a circle each round before pressing `K`. |
| `--step` | `0.35` | Base delay (in seconds) between 90-degree turns while circling. |
| `--keep-open` | `3.0` | Seconds to keep the browser window open after completing all loops before closing. |

---

## Troubleshooting

- **Resetting the Static Asset Cache**:
  If Powerline.io updates its frontend bundle, delete `/tmp/powerline_http_cache_seed` and `/tmp/powerline_flagged_ips.txt` so the bot re-seeds a fresh cache on the next launch:
  ```bash
  rm -rf /tmp/powerline_http_cache_seed /tmp/powerline_flagged_ips.txt
  ```
- **Cloudflare Turnstile on a Proxy IP**:
  If a proxy IP has been heavily used and triggers a Turnstile challenge for more than 5 seconds, the bot automatically marks that IP in `/tmp/powerline_flagged_ips.txt` (for 15 minutes) and switches to the next clean proxy IP in your file.
