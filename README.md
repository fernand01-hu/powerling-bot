# Powerline.io Multi-Instance Bot

Runs several Camoufox (anti-detect Firefox) browser instances that join a powerline.io room and play on a fixed duty cycle, using Webshare proxies.

> The previous Selenium/Chrome implementation is kept in `bot_legacy_archived.py` for reference; its options do not apply to `bot.py`.

## How it works

1. **Manager** (`main`): splits the bots into `--groups` groups of `--bots-per-proxy` bots, each group on its own proxy IP. Every group plays for `--active-duration` seconds and then closes its browsers for `--pause-duration` seconds. The groups are offset evenly, so with the defaults (3 groups, 120s on / 60s off) 2 groups are always playing while 1 rests. The proxy list is fetched from Webshare at most once per cycle, and crashed workers are restarted for the rest of their group's window.
2. **Workers** (`bot_worker_process`): each gets an isolated `HOME` (`/tmp/camoufox_worker_N`), waits a staggered start delay, and launches Camoufox through its assigned proxy (`--bots-per-proxy` workers share one proxy).
3. **Session** (`PowerlineBot.run_session`): dismisses consent/reconnect dialogs, handles the Turnstile check, enters a random nickname, and steers with arrow keys (biased Down/Left, never reversing 180°) until the death or lobby screen reappears, then respawns. If it stays stuck out of the game for 30s it reloads the page.
4. **Input** (`humanizer.py`): every click, mouse move, keypress and pause goes through a `Humanizer` (curved mouse paths, randomized key and click timing, typing with occasional corrected typos). Camoufox's built-in `humanize` is turned off so input isn't humanized twice.

## Requirements

- Linux with a display (or `--headless`)
- Python 3.10+
- `pip install camoufox[geoip] numpy scipy && python3 -m camoufox fetch`

## Usage

```bash
export WEBSHARE_API_KEY=your_key_here
python3 bot.py                       # 3 groups x 3 bots, room 2cjg, 120s on / 60s off per group
python3 bot.py --groups 4 --bots-per-proxy 2 --headless
```

| Option | Default | Description |
| --- | --- | --- |
| `--room` | `2cjg` | Room code (the part after `#` in the URL) |
| `--webshare-key` | `$WEBSHARE_API_KEY` | Webshare API key (required) |
| `--groups` | `3` | Number of groups, each on its own proxy IP |
| `--bots-per-proxy` | `3` | Bots in each group |
| `--stagger-interval` | `3.0` | Seconds between bots in a group connecting |
| `--active-duration` | `120` | Seconds each group plays |
| `--pause-duration` | `60` | Seconds each group rests with its browsers closed |
| `--headless` | off | Run browsers headless |

Stop with `Ctrl+C`: the manager terminates every worker, each worker closes its browser, and leftover `camoufox` processes are killed. If the manager itself crashes or is killed, its workers shut down on their own.
