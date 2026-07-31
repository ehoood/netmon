# AGENTS.md — orientation for AI coding agents

Read this before changing anything. It describes the invariants that are easy to
break and expensive to notice, because the failure mode of this project is a cron
job that quietly stops measuring.

## What this is

An internet-quality monitor for a single always-on Linux box. A cron job runs
`netmon.py` on an interval, each run appends one row to `netmon_log.csv`, and
`report.py` turns those rows into an HTML report. A long-polling Telegram bot
(`netmon_bot.py`) exposes measurement, reporting and *configuration* over chat.

## Hard rules

1. **Standard library only.** No pip, no npm, no lockfile. A user must be able to
   `git clone` onto a bare Raspberry Pi OS install and run `./install.sh`. If you
   want a dependency, you almost certainly want ~40 lines of stdlib instead.
2. **A cron run must never crash.** `netmon.py` catches everything and writes a
   row with `status=error` and the message. A failed measurement is data — it is
   how outages show up in the report. Never let an exception escape, and never
   let an optional feature (an alert, a Telegram send) break the measurement.
3. **The CSV grows only at the end, and only through `migrate_header()`.**
   `FIELDS` in `netmon.py` defines the column order. Never reorder or rename.
   Appending is safe *because* `append_row` calls `migrate_header()` first: the
   header is written once at file creation, so without that step a longer
   `FIELDS` would produce rows wider than an existing log's header and the extra
   values would vanish into `DictReader`'s `restkey`. The migration rewrites the
   file once, keeping every old value and leaving new columns blank; it is
   idempotent and tested against a real 107-row log.
4. **`netmon.conf` is the single source of truth.** Not env vars, not CLI
   defaults, not constants in a module. CLI flags may override for one run.
5. **Every user-facing string goes in `i18n.py`**, in both `en` and `he`. No
   literal user-facing text in `report.py` or `netmon_bot.py`. `analyze.py` is the
   deliberate exception: it is the admin's terminal view and stays English.
6. **Never commit secrets.** `netmon.conf` holds a bot token and is gitignored,
   as is `telegram-bot.js` (a user's personal, unrelated bot may live in the
   checkout). `cfgmod.mask()` exists so `/config` output can be screenshotted.

## Architecture

| Module | Owns |
|---|---|
| `netmon_config.py` | Config schema, validation, persistence, cron block, small state file |
| `i18n.py` | All translated strings, `t(lang, key, *args)` |
| `netmon.py` | One measurement, server calibration, CSV append, instant alerts |
| `report.py` | Aggregation (`summarize`), HTML/SVG, text summary, `build()` for reuse |
| `analyze.py` | Terminal diagnostics (English) |
| `telegram_send.py` | `send_message` / `send_document`, nothing else |
| `netmon_bot.py` | Long-poll loop, auth, command dispatch |
| `install.sh` | Interactive first-run setup |

Dependency direction is one-way: `netmon_config` and `i18n` are leaves;
`netmon_bot` imports `report` and `telegram_send`, never the reverse.

## Things that will bite you

- **One process per bot token.** Telegram returns `409 Conflict` if two
  processes call `getUpdates` on the same token. Do not start a second bot for
  testing with the user's live token.
- **The cron block is marker-delimited.** `apply_schedule()` rewrites only what is
  between `CRON_BEGIN` and `CRON_END` and preserves everything else in the user's
  crontab. Never write the crontab wholesale.
- **`apply_schedule()` writes the real crontab.** Export `NETMON_SKIP_CRON=1`
  in any test or you will schedule jobs on the developer's machine.
- **Intervals must be clock-aligned.** `cron_expr()` snaps to a divisor of an
  hour; `*/45` in cron fires at :00 and :45, not every 45 minutes.
- **The `PAUSED` flag is checked by the cron line itself**, not by `netmon.py`.
  It survives reboots on purpose: a deliberate pause should not silently undo
  itself after a power cut.
- **`%%` in i18n strings.** Strings are written with `%%` for a literal percent so
  they stay safe if an argument is added later; `t()` unescapes when called
  without args. Keep that convention.
- **RTL.** The Hebrew report is `dir="rtl"`, but SVG charts are forced to
  `direction:ltr` so numeric axes render correctly. Do not remove that.
- **The measurement server is the biggest source of wrong numbers.** A speedtest
  measures the server and the path to it as much as the line, and the
  auto-selected nearest server is not reliably a good one — in the field this
  produced a steady, believable 500 Mbps on a line that reached 940. Hence
  `maybe_calibrate()`: try several servers, keep the fastest as `SERVER_ID`,
  re-check every `CALIBRATE_DAYS`. The reasoning only works one way — a bad
  server can understate a line but never overstate it — so *max* is the right
  estimator here and mean is not. Do not "improve" it into an average.
- **Calibration costs bandwidth.** Each speedtest transfers ~1.6 GB (measured on
  a gigabit line). That is why it is weekly, not per-run, and why the installer
  states the cost before spending it. Anything that makes it run more often
  needs to justify the traffic.

## Testing without touching anything real

```bash
export NETMON_SKIP_CRON=1          # never write the developer's crontab

# stub speedtest: --version must say "Ookla", otherwise print the JSON shape
# netmon.py expects (ping.latency, download.bandwidth in bytes/s, ...)
PATH=/path/to/stub:$PATH ./install.sh --yes

# exercise bot commands offline
python3 - <<'EOF'
import telegram_send as tg, netmon_bot as bot
tg.send_message  = lambda *a, **k: print("MSG:", a[2][:200]) or {"ok": True}
tg.send_document = lambda *a, **k: print("DOC:", a[2]) or {"ok": True}
bot.handle("/status", "TOKEN", "12345")     # CHAT_ID must match netmon.conf
EOF
```

`report.py`, `analyze.py` and the alert path all work against a copied
`netmon_log.csv` — real data is the best fixture.

## Common tasks

**Add a config setting** — add it to `SCHEMA` in `netmon_config.py` (default plus
a one-line help string), add a `validate()` branch, expose it as a `/set…`
command, document it in the README table and `netmon.conf.example`. Unknown keys
in an existing config file are preserved, so upgrades are safe.

**Add a bot command** — a `cmd_*` function plus a branch in `handle()`. Anything
slower than a second (a measurement, a report) must run in a `threading.Thread`
so the poll loop keeps answering; guard concurrent measurements with `_busy`.
Add the strings to `i18n.py` and the line to `bot_help`.

**Add a language** — add the code to `LANGS` in `i18n.py`, translate every key
(there is no partial-language fallback beyond English), add it to the `LANG`
branch of `validate()`, and add it to `RTL` if it is right-to-left.

**Add a metric** — append to `FIELDS`, populate it in `measure_ookla()` (and
`measure_speedtest_cli()` if available there, otherwise `""`), then surface it in
`summarize()`/`analyze.py`. Old rows will have blanks; readers must cope.
