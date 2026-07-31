# netmon — internet quality monitor with a Telegram bot

Measure what your internet connection actually delivers, around the clock, and
get the evidence in your pocket.

netmon runs on any always-on Linux box wired to your router — a Raspberry Pi, an
old laptop, a NAS, a VM. It measures your line on a schedule, logs every result
to a CSV, sends an instant Telegram alert when the line goes bad, and pushes a
periodic HTML report with charts to your phone. You configure the whole thing
from Telegram.

It is built for the argument you have with your ISP, your landlord or your
building committee: *"the line is fine at 3 AM and unusable at 9 PM."* A week of
hourly data settles that argument.

```
📡 Measurement
2026-07-28T22:00:01  down=336 Mbps  up=877 Mbps  ping=5.5 ms  bufferbloat=1.4 ms  loss=0%
```

## What it measures

| Metric | Why it matters |
|---|---|
| Download / upload | What you actually get vs. what you pay for |
| Idle latency + jitter | Baseline responsiveness |
| **Bufferbloat** (latency under load) | Latency added *while the line is busy*. High bufferbloat is why video calls stutter while someone downloads — a speed number alone never shows it |
| Packet loss | Sign of a saturated or faulty line |
| Independent baseline ping | Sanity check that does not depend on the speedtest server |
| Hour of day | Lets the report compare evening peak against quiet night hours |

Everything is stdlib-only Python 3. No pip install, no node, no database.

## Requirements

- Linux with Python 3.7+, `cron`, and `ping`
- A speedtest CLI — the installer sets up [Ookla `speedtest`](https://www.speedtest.net/apps/cli)
  (preferred: it reports latency under load) and falls back to `speedtest-cli`
- **A wired connection.** Over Wi-Fi you measure your Wi-Fi, not your internet
- Optional: a Telegram account, for the bot

## Install

```bash
git clone https://github.com/ehoood/netmon.git
cd netmon
./install.sh
```

The installer asks for — with hints for each — your language, the speed your ISP
sells you, how often to measure, and your Telegram credentials. It then installs
the speedtest CLI, writes `netmon.conf`, schedules measurements in cron, installs
the bot as a systemd service, and runs one measurement so you see it work.

Non-interactive:

```bash
LANG_CODE=en PLAN_DOWN=500 PLAN_UP=800 INTERVAL=60 \
BOT_TOKEN=123456:AA... CHAT_ID=987654321 ./install.sh --yes
```

### Setting up the Telegram bot

1. **Get a token** — message [@BotFather](https://t.me/BotFather), send `/newbot`,
   pick a name and a username ending in `bot`. He replies with a token that looks
   like `8123456789:AAE...`.
2. **Get your chat id** — send any message to your new bot, and the installer
   detects the id automatically. To do it later:
   `./netmon_bot.py --detect-chat`
3. Only that chat id can command the bot.

> One bot token can only be polled by one process. If the token is already used
> by another bot of yours, create a second bot for netmon.

Skip the token at install time and netmon still measures and logs — add
credentials to `netmon.conf` whenever you want.

## Bot commands

| Command | What it does |
|---|---|
| `/speed` | Run a measurement right now (~40s) |
| `/stats` | Summary of everything collected so far |
| `/report` | Build the HTML report and send it to the chat |
| `/status` | Is monitoring running, last result, sample count |
| `/pause` · `/resume` | Stop / restart automatic measurements |
| `/config` | Show every current setting |
| `/setplan <down> [up]` | The speed your ISP sells you, Mbps |
| `/setinterval <minutes>` | Time between measurements (5–1440) |
| `/setalert <percent>` · `/setalert off` | Instant alert below this % of plan |
| `/setreport <day> <hour>` · `/setreport off` | Periodic report (day 0=Sunday) |
| `/setping <host>` | Baseline ping target |
| `/calibrate` | Re-check which speedtest server represents your line |
| `/setserver <id>` · `/setserver auto` | Measure against a specific server |
| `/setlang en\|he` | Language of reports and replies |
| `/help` | The list above |

Changing the interval or the report schedule rewrites the cron entries
immediately — no shell needed.

## Configuration

Everything lives in `netmon.conf` (see `netmon.conf.example`), readable by hand
and rewritten by the bot. It holds your bot token, so it is chmod 600 and
gitignored.

| Key | Default | Meaning |
|---|---|---|
| `LANG` | `en` | `en` or `he`; report, alerts and bot replies |
| `PLAN_DOWN_MBPS` / `PLAN_UP_MBPS` | `100` / — | What your ISP sells you |
| `INTERVAL_MINUTES` | `60` | Minutes between measurements |
| `PING_HOST` | `1.1.1.1` | Independent baseline ping target |
| `SERVER_ID` | — | Speedtest server to measure against; empty = auto-pick |
| `CALIBRATE_DAYS` | `7` | Re-check which server represents your line every N days; `0` = never |
| `CALIBRATE_SERVERS` | `6` | How many nearby servers to try when calibrating |
| `BOT_TOKEN` / `CHAT_ID` | — | Telegram credentials |
| `ALERTS_ENABLED` | `1` | Instant alert on a bad measurement |
| `ALERT_THRESHOLD_PCT` | `50` | Alert below this % of plan |
| `ALERT_COOLDOWN_MIN` | `120` | Minimum minutes between alerts |
| `REPORT_ENABLED` / `REPORT_DOW` / `REPORT_HOUR` | `1` / `0` / `8` | Periodic report, day 0=Sunday |
| `PEAK_START` / `PEAK_END` | `18` / `24` | Peak window compared in the report |
| `OFFPEAK_START` / `OFFPEAK_END` | `2` / `7` | Quiet reference window |

From the shell:

```bash
./netmon_config.py --show
./netmon_config.py --set INTERVAL_MINUTES=30      # also rewrites cron
./netmon.py --calibrate                           # re-pick the measurement server
```

### Why calibration exists

The single biggest source of wrong conclusions in speed monitoring is the
choice of server. A speedtest measures the server, and the network path to it,
as much as it measures your line — and the nearest server, which is what gets
auto-selected, is not reliably a good one. One congested server produces a
steady, believable, completely wrong number, hour after hour.

The error only runs one way: a bad server can make your line look **slower**
than it is, never faster, because no server can deliver more than the link
carries. So the fastest server observed is the closest thing to the truth about
what your connection can do.

Speed alone is not enough to choose an instrument, though. netmon also reports
latency and bufferbloat, and those only mean something over a short path —
measured across an intercontinental hop, bufferbloat describes that hop, not
your line. So calibration takes the fastest server as the reference and then,
among the servers within 15% of it, keeps the one with the lowest latency. A
server 15% slower but 100 ms closer is the better instrument: the download
figure barely moves while latency becomes meaningful again.

netmon therefore tries `CALIBRATE_SERVERS` nearby servers at install time, picks
on that basis, and measures against the winner from then on — so the trend
compares like with like instead of drifting with whichever server was picked
that hour. It re-checks every `CALIBRATE_DAYS`, because the best server changes
over time. `/calibrate` forces it, `/setserver <id>` pins one by hand, and
`/setserver auto` returns to auto-selection.

This makes netmon measure **capability** — what your line can deliver, which is
what a plan is sold on — rather than the health of one nearby server.

## Reading the results

```bash
./analyze.py            # detailed terminal report (English)
./report.py             # self-contained HTML report with charts
./report.py --telegram  # …and send it to your chat
```

The HTML report is one offline file with inline SVG charts, so it opens straight
from the Telegram attachment on a phone.

What to look for after a week:

- **Median download vs. plan.** Consistently below ~80% is worth a support call.
- **The by-hour chart.** A line that is fast at 03:00 and slow at 21:00 is
  congestion — either your ISP oversubscribed the segment, or a shared line is
  carrying more households than it can.
- **Bufferbloat above ~80 ms.** The line saturates without queue management. This
  is what makes calls and games feel broken even when the speed number looks fine.
- **Packet loss above ~1%.** Something is wrong on the line itself.
- **Failed measurements.** Logged as rows too — those are outages.

## Files

| File | Role |
|---|---|
| `netmon.py` | One measurement → a CSV row, plus instant alerts |
| `netmon_bot.py` | Standalone Telegram bot (long-polling, stdlib) |
| `netmon_config.py` | Config load/save, validation, cron scheduling |
| `report.py` | HTML report with SVG charts + Telegram send |
| `analyze.py` | Detailed terminal report |
| `telegram_send.py` | Minimal Telegram API sender |
| `i18n.py` | Every user-facing string, en + he |
| `install.sh` | Interactive setup |
| `diagnose.sh` | One-shot diagnosis: link speed & errors, multi-server tests, path hops, computed verdict |
| `AGENTS.md` | Orientation for AI coding agents working on this repo |

Runtime files that stay local and are gitignored: `netmon.conf`,
`netmon_log.csv`, `report.html`, `netmon.cron.log`, `PAUSED`.

## Troubleshooting

**Speeds far below the plan, at every hour** — check the machine is not the
bottleneck: `ethtool eth0 | grep Speed`. A 100 Mb NIC caps every result at ~95
Mbps. Use a gigabit machine or a USB3 gigabit adapter.

**Want a one-shot diagnosis right now** — run `sudo ./diagnose.sh` (root is
needed for link speed and error counters). It checks the physical link, runs
speed tests against **several servers**, traces the path, and prints a verdict
computed from those numbers, plus a report file you can send on.

**A result far below the plan is not automatically your line.** A speedtest
picks one server for you, usually the nearest, and measures that server as much
as it measures your connection — a single congested server looks exactly like a
capped line. netmon handles this for you by calibrating (see below); to
investigate by hand, `diagnose.sh` tests several servers and says which of them
represents your line.

**Nothing is being measured** — `crontab -l` should show a netmon block. Check
`netmon.cron.log`, and make sure the `PAUSED` file is not there (`/resume`).

**The bot is silent** — `systemctl status netmon-bot`, then
`journalctl -u netmon-bot -n 50`. A `409 Conflict` means another process is
polling the same token.

**Measurements fail with a license error** — run once by hand to accept it:
`speedtest --accept-license --accept-gdpr`.

## Privacy

Measurements go to Ookla's speedtest servers (that is how a speedtest works) and
results go only to your own Telegram chat. `netmon.conf` holds your bot token —
it is gitignored; do not commit it or paste `/config` output publicly (the bot
masks the token for exactly that reason).

## License

MIT. Use it, fork it, bring the charts to your ISP.
