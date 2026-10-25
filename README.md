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

## Several machines

The coordinator can spread the bots over several machines on the same network. Each machine runs an agent that connects to the coordinator; new bots go to the machine with the fewest bots per CPU thread, a machine that stays above 85% CPU for 30s hands one bot (same name and proxy) to a machine below 65%, and the bots of a machine that disconnects are restarted elsewhere. Only the coordinator needs the Webshare key.

All machines need the same code (agents with different code are refused) and a shared secret:

```bash
export POWERLINE_AGENT_KEY=some-long-random-secret   # same value on every machine

# Coordinator (here: also runs at most 1 bot itself)
python3 coordinator.py --listen 0.0.0.0 --local-max-bots 1 --headless

# Every other machine (Windows: `set POWERLINE_AGENT_KEY=...` and `python agent.py ...`)
python3 agent.py --coordinator 192.168.1.96 --headless
```

| Coordinator option | Default | Description |
| --- | --- | --- |
| `--listen` | off | Accept agents on `HOST[:PORT]` (default port 4041) |
| `--wait-agents` | `15` | Seconds to let agents connect before placing bots |
| `--local-max-bots` | no limit | Most bots this machine runs itself |
| `--no-local-agent` | off | Run no bots on the coordinator machine |

| Agent option | Default | Description |
| --- | --- | --- |
| `--coordinator` | required | Coordinator address `HOST[:PORT]` |
| `--max-bots` | no limit | Most bots this machine runs |
| `--headless` | off | Run browsers headless |
| `--name` | hostname | Name shown in the coordinator's log |
| `--debug-dir` | off | Save screenshots of key moments |

If the connection drops, an agent stops its bots and reconnects every 5s, so either side can be restarted at any time. Bot names, paused machines, machine bot limits and stopped bots are saved to `~/.config/powerline-bot/state.json` on the coordinator (`--state-file` to change it), so they survive restarts. Coordinator and agents exchange plain JSON messages (never pickle) after both prove they know the shared key. A bot that keeps failing waits longer before each restart (up to 60s), and proxy failures make the coordinator re-read Webshare's list right away, since they usually mean Webshare just rotated its IPs.

## Dashboard

The coordinator can serve a password-protected web page showing every bot (state: loading / connecting / lobby / playing / dead, machine, group, IP, rounds, last respawn time), every machine (CPU, RAM, bots), the proxies and a countdown to Webshare's next IP rotation, plus a feed of recent events. From it you can stop, start or restart a bot, stop or start all bots, pause a machine (its bots move elsewhere) or resume it, set a machine's bot limit, change the number of groups, and refresh the proxy list.

```bash
export POWERLINE_DASHBOARD_PASSWORD=choose-a-password
python3 coordinator.py --listen 0.0.0.0 --dashboard 0.0.0.0 --headless   # then open http://<coordinator>:4042
```

Log in with any user name and that password. Keep port 4042 inside your network (don't forward it on the router).

## Start automatically

`deploy/` has everything to start the system when the machines boot. With `--start-stopped` the coordinator, dashboard and agents come up but every bot waits until you press **Start all** (or start single bots) on the dashboard.

**Coordinator machine** (Linux, systemd user service):

```bash
mkdir -p ~/.config/powerline-bot ~/.config/systemd/user
cp deploy/env.example ~/.config/powerline-bot/env && chmod 600 ~/.config/powerline-bot/env
nano ~/.config/powerline-bot/env          # fill in the keys and password, keep COORDINATOR_ARGS
cp deploy/powerline-coordinator.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now powerline-coordinator
loginctl enable-linger                     # run at boot without anyone logged in
journalctl --user -u powerline-coordinator -f   # follow its log
```

**Other Linux machines**: the same with `deploy/powerline-agent.service` (`systemctl --user enable --now powerline-agent`); their env file only needs `POWERLINE_AGENT_KEY` and `AGENT_ARGS`.

**Windows**: copy `deploy/start-agent.cmd` next to `agent.py`, then in a command prompt:

```bat
setx POWERLINE_AGENT_KEY "same-secret-as-the-coordinator"
schtasks /Create /TN "Powerline agent" /SC ONLOGON /RL LIMITED /F /TR "\"%USERPROFILE%\powerline-bot\start-agent.cmd\""
schtasks /Run /TN "Powerline agent"
```

It starts when you log in and restarts the agent if it ever exits (log: `agent.log`).

## Tests

```bash
python3 -m unittest discover -s tests
```

178 tests (about 15s) that need no browser, no Webshare account and no internet: fake pages, browsers, agents and Webshare responses stand in for the real ones (`tests/fakes.py`). They cover:

- **humanizer**: timing distributions, mouse paths (always end on target, no jumps, overshoot rules), click spots inside buttons, typing (typos always corrected), idle drift, fatigue, watchdog
- **bot**: names, steering odds, status reports and throttling, proxy updates and the stop message, page loading, proxy switching, the respawn loop, exit codes, process-tree clean-up
- **coordinator**: Webshare paging and errors, rotation countdown, randomized checks of proxy assignment and machine placement, restart backoff, bad-proxy benching, machines leaving, overload moves, snapshot states, state file
- **agent and network**: message format (JSON only, size limit), every command, the serve loop, reconnects, wrong keys, stalled connections, pickle rejection
- **dashboard**: login, every bad request, security headers, every button maps to an accepted action, every command
