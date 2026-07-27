# HANDOFF — continue this work in a local Claude Code session

This file lets a **local** `claude` session pick up exactly where a previous
cloud session left off. Read it first, then read `README.md`.

## Context (why this exists)
- User lives in a kibbutz. Internet comes from a central exchange (מרכזייה) and
  bandwidth is **shared among many families**. User suspects the shared line is
  oversubscribed and is slow at peak hours.
- Hardware: TP-Link **Deco M5** mesh + a **wired router** upstream + a
  **Raspberry Pi**.
- Decision made with the user: the Pi is plugged **directly into the wired
  router (bypassing the Deco)**, via Ethernet. This isolates the Deco out of the
  measurement and measures the raw shared line. No WiFi involved.
- Goal: measure download/upload/ping/jitter/packet-loss AND **bufferbloat
  (latency under load)** every hour for ~a week, then a weekly HTML report auto-
  sent to the user's **existing Telegram bot**. Evidence to bring to the kibbutz
  committee: does the line collapse in the evening (peak) vs the quiet night?

## Why the Pi, not the Deco API
Deco M5 has **no official API**. Community libs (`deco`, HA `tplink_deco`) can
read clients but **cannot trigger the speed test** (that's a cloud feature).
Measuring from the Pi on Ethernet is more reliable and cleaner.

## What is already built (all in this directory, zero Python deps, stdlib only)
| file | role |
|------|------|
| `netmon.py` | one measurement -> append a row to `netmon_log.csv`. Prefers Ookla `speedtest` (gives loaded latency/bufferbloat), falls back to `speedtest-cli`. Also does an external baseline ping to 1.1.1.1. Never crashes — logs failures as rows. |
| `analyze.py` | terminal text report (overall stats, by-hour table, peak vs off-peak verdict, worst events). |
| `report.py` | self-contained **HTML report** with inline-SVG charts + optional **Telegram send** (`--telegram`). Has `--dry-run`. |
| `telegram_send.py` | reusable stdlib Telegram sender (sendMessage + sendDocument multipart). |
| `install.sh` | installs Ookla speedtest, checks NIC link speed via ethtool, sets up cron (hourly measurement + weekly Sunday 08:00 Telegram report), runs one test. Honors `PLAN=` and `PING_HOST=` env vars. |
| `telegram.conf.example` | template for `BOT_TOKEN` + `CHAT_ID`. |

Key metric to explain to the user: **bufferbloat** = loaded latency − idle
latency. High bufferbloat + big evening speed drop + packet loss = classic
oversubscribed shared line.

## What still needs to be done ON THE PI (only the user/local session can)
1. `PLAN=<real_plan_Mbps> ./install.sh`  — set the user's actual plan speed.
2. **Verify the Pi actually gets internet directly from the wired router**:
   `ping -c3 1.1.1.1`. ⚠️ If the upstream device is a bridge modem/ONT and the
   **Deco** is the one doing PPPoE/auth, the Pi plugged upstream will NOT get
   online — in that case fall back to plugging the Pi into the Deco's LAN.
3. **Check NIC link speed**: `ethtool eth0 | grep Speed`. If it's 100Mb and the
   line is faster, the Pi is the bottleneck — use a Gigabit Pi (4/5) or USB3 GbE.
4. Fill `telegram.conf` with the user's **existing bot** token + chat id.
   Test: `./report.py --plan <PLAN> --telegram --dry-run`, then real send.
5. Confirm the first measurement returns numbers (Ookla license accepted).
6. Let it run ~a week, then `./analyze.py --plan <PLAN>` /
   `./report.py --plan <PLAN>`.

## Discussed but NOT yet built (offer to the user)
- **Immediate alert**: send a Telegram message the moment a single measurement
  drops below a threshold (e.g. download < 50% of plan), not just the weekly
  report. Would slot into `netmon.py` (call `telegram_send` when a row is bad).

## Conventions
- All scripts are stdlib-only on purpose (a Pi should need nothing but Python 3
  + the `speedtest` binary + `ping`). Keep it that way.
- Text/UI in Hebrew (RTL). SVG charts forced to `direction:ltr` so numeric axes
  render correctly inside the RTL page.
