# Powerline.io Multi-Instance Bot

Runs several Camoufox (anti-detect Firefox) browser instances that join a powerline.io room and play continuously, using Webshare proxies.

## How it works

1. **Coordinator** (`coordinator.py`): splits the bots into `--groups` groups of `--bots-per-proxy` bots, each group on its own proxy IP, and gives every bot one random nickname for the whole run. It places each bot on an agent (the one running the fewest bots per CPU core) and restarts bots whose worker exits, with the same name, on their group's current proxy. All browsers stay open until you stop it. Every `--proxy-check-interval` seconds it re-reads the proxy list (all pages) from Webshare: a group keeps its proxy while it's still listed, and when Webshare rotates it out the group moves to an unused proxy and its bots are told to switch. A proxy that can't reach the game (the browser can't launch through it, or the page stays on "Connecting..." through two reloads) is benched for 30 minutes and its group is moved the same way.
2. **Agent** (`agent.py`): starts and stops bot workers on its machine when the coordinator says so, passes them proxy changes, and reports workers that exit plus CPU/RAM health. For now the coordinator runs one agent on the same machine; agents on other machines are the next step.
3. **Workers** (`bot.py`, `bot_worker_process`): each gets an isolated `HOME` (a fresh `/tmp/camoufox_worker_N_*` folder, deleted when the worker exits), waits a staggered start delay, and launches Camoufox through its group's proxy (`--bots-per-proxy` workers share one proxy).
4. **Session** (`PowerlineBot.run_session`): dismisses consent/reconnect dialogs, handles the Turnstile check, types its nickname the first time it joins, and steers with arrow keys (biased Down/Left, never reversing 180°) while the game's menu overlay is hidden (i.e. actually playing), until the death or lobby screen reappears. It respawns (with Enter) after a random delay that is usually under 2s and occasionally up to 15s; since the game ignores input for about 2s after a death, most respawns land at about 2.5s. When the coordinator reports a new proxy IP, it reloads the game on that IP without closing the browser (in a fresh context, since a context's proxy is fixed). If it stays stuck out of the game for 30s it also reloads the page.
5. **Input** (`humanizer.py`): every click, mouse move, keypress and pause goes through a `Humanizer` (curved mouse paths, randomized key and click timing, typing with occasional corrected typos). Camoufox's built-in `humanize` is turned off so input isn't humanized twice.

## Requirements

- Linux with a display, or Windows 10/11 (either one can use `--headless`)
- Python 3.10+ (tested on 3.10 and 3.14)
- `pip install -r requirements.txt && python3 -m camoufox fetch`

## Usage

```bash
export WEBSHARE_API_KEY=your_key_here
python3 coordinator.py                       # 3 groups x 3 bots, room 2cjg
python3 coordinator.py --groups 4 --bots-per-proxy 2 --headless
```

| Option | Default | Description |
| --- | --- | --- |
| `--room` | `2cjg` | Room code (the part after `#` in the URL) |
| `--webshare-key` | `$WEBSHARE_API_KEY` | Webshare API key (required) |
| `--groups` | `3` | Number of groups, each on its own proxy IP |
| `--bots-per-proxy` | `3` | Bots in each group |
| `--stagger-interval` | `3.0` | Seconds between bots in a group connecting |
| `--proxy-check-interval` | `60` | Seconds between Webshare checks for rotated proxy IPs |
| `--headless` | off | Run browsers headless |
| `--debug-dir` | off | Save screenshots of key moments (load, spawn, death, every 30s in game) to this folder |

`python3 bot.py` with the same options still works and does the same thing.

Stop with `Ctrl+C`: the coordinator tells every agent to stop its workers, each worker closes its browser, and anything left in a worker's process group is killed (other Camoufox processes on the machine are not touched). On Windows, Ctrl+Break works the same way. If the coordinator itself crashes or is killed, its workers and their browsers shut down on their own.

## Tests

```bash
python3 -m unittest discover -s tests
```

They cover the pure logic (steering, nicknames, proxy reassignment, agent placement, respawn delays, click sampling) and need no browser or network.
