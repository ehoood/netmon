#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netmon_config.py - Shared configuration for every netmon component.

One file, `netmon.conf` (KEY=VALUE, '#' comments), holds everything: Telegram
credentials, the plan you pay for, how often to measure, alert thresholds and
the weekly-report schedule. The installer writes it, the bot rewrites it live
(`/setplan`, `/setinterval`, ...), and every script reads it.

Zero dependencies (stdlib only) - a fresh Linux box needs nothing but Python 3.

Precedence for any value:  CLI flag  >  netmon.conf  >  environment  >  default.

Usage as a module:
    import netmon_config as cfgmod
    cfg = cfgmod.load()
    cfgmod.set_values(cfg, {"PLAN_DOWN_MBPS": "500"})   # persists to disk
    cfgmod.apply_schedule(cfg)                          # rewrite the cron block

Usage from the shell:
    ./netmon_config.py --show                  # print effective config
    ./netmon_config.py --set PLAN_DOWN_MBPS=500 --set INTERVAL_MINUTES=30
    ./netmon_config.py --apply-schedule        # regenerate the managed cron block
"""

import os
import re
import subprocess
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(DIR, "netmon.conf")
PAUSE_FLAG = os.path.join(DIR, "PAUSED")
CSV_PATH = os.path.join(DIR, "netmon_log.csv")
HTML_PATH = os.path.join(DIR, "report.html")
CRON_LOG = os.path.join(DIR, "netmon.cron.log")
STATE_PATH = os.path.join(DIR, ".netmon_state")

# Markers around the crontab lines we own. Anything between them is ours to
# rewrite; anything outside is the user's and is never touched.
CRON_BEGIN = "# >>> netmon (managed - do not edit between markers) >>>"
CRON_END = "# <<< netmon (managed) <<<"

# key -> (default, one-line help shown by /config and the installer)
SCHEMA = {
    "LANG":                 ("en",      "UI language for reports and bot replies: en | he"),
    "PLAN_DOWN_MBPS":       ("100",     "Download speed your ISP sells you, Mbps"),
    "PLAN_UP_MBPS":         ("",        "Upload speed your ISP sells you, Mbps (optional)"),
    "INTERVAL_MINUTES":     ("60",      "Minutes between automatic measurements"),
    "PING_HOST":            ("1.1.1.1", "Host for the independent baseline ping"),
    "BOT_TOKEN":            ("",        "Telegram bot token from @BotFather"),
    "CHAT_ID":              ("",        "Telegram chat id that may command the bot"),
    "ALERTS_ENABLED":       ("1",       "Send an instant Telegram alert on a bad measurement: 1 | 0"),
    "ALERT_THRESHOLD_PCT":  ("50",      "Alert when download falls below this % of the plan"),
    "ALERT_COOLDOWN_MIN":   ("120",     "Minimum minutes between two alerts"),
    "REPORT_ENABLED":       ("1",       "Send the periodic HTML report to Telegram: 1 | 0"),
    "REPORT_DOW":           ("0",       "Report day of week for cron: 0=Sunday .. 6=Saturday"),
    "REPORT_HOUR":          ("8",       "Report hour, 0-23"),
    "PEAK_START":           ("18",      "Peak window start hour (inclusive)"),
    "PEAK_END":             ("24",      "Peak window end hour (exclusive)"),
    "OFFPEAK_START":        ("2",       "Quiet reference window start hour (inclusive)"),
    "OFFPEAK_END":          ("7",       "Quiet reference window end hour (exclusive)"),
}

SECRET_KEYS = {"BOT_TOKEN"}


# --------------------------------------------------------------- load / save
def load(path=CONF_PATH):
    """Return the effective config: defaults <- environment <- file."""
    cfg = {k: v for k, (v, _) in SCHEMA.items()}
    # Environment is a convenience for containers; the file still wins.
    for k in SCHEMA:
        env = os.environ.get("NETMON_" + k)
        if env is not None:
            cfg[k] = env
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["BOT_TOKEN"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["CHAT_ID"] = os.environ["TELEGRAM_CHAT_ID"]
    cfg.update(read_file(path))
    cfg["_path"] = path
    return cfg


def read_file(path=CONF_PATH):
    """Parse a KEY=VALUE file. Unknown keys are kept, so upgrades never lose data."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def set_values(cfg, updates, path=None):
    """Merge `updates` into cfg and rewrite the config file, preserving comments."""
    path = path or cfg.get("_path") or CONF_PATH
    cfg.update({k: str(v) for k, v in updates.items()})

    existing = read_file(path)
    existing.update({k: str(v) for k, v in updates.items()})
    lines = ["# netmon configuration - edit by hand or from the Telegram bot (/config).",
             "# Regenerated whenever a value changes; comments below are rewritten.",
             ""]
    for key, (_, help_text) in SCHEMA.items():
        lines.append("# %s" % help_text)
        lines.append("%s=%s" % (key, existing.get(key, SCHEMA[key][0])))
        lines.append("")
    extra = {k: v for k, v in existing.items() if k not in SCHEMA and not k.startswith("_")}
    if extra:
        lines.append("# Keys not known to this version of netmon, preserved verbatim:")
        for k in sorted(extra):
            lines.append("%s=%s" % (k, extra[k]))
        lines.append("")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)
    os.chmod(path, 0o600)          # the file holds a bot token
    return cfg


# ------------------------------------------------------------------ accessors
def get_int(cfg, key, default=None):
    try:
        return int(float(str(cfg.get(key, "")).strip()))
    except (TypeError, ValueError):
        if default is not None:
            return default
        try:
            return int(float(SCHEMA[key][0]))
        except (KeyError, ValueError):
            return 0


def get_float(cfg, key):
    try:
        v = str(cfg.get(key, "")).strip()
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def get_bool(cfg, key):
    return str(cfg.get(key, "")).strip().lower() in ("1", "true", "yes", "on")


def lang(cfg):
    return "he" if str(cfg.get("LANG", "en")).lower().startswith("he") else "en"


def is_paused():
    return os.path.exists(PAUSE_FLAG)


def creds(cfg):
    return cfg.get("BOT_TOKEN", "").strip(), cfg.get("CHAT_ID", "").strip()


def mask(key, value):
    """Never print a full bot token - a screenshot of /config would leak it."""
    if key in SECRET_KEYS and value:
        return value[:6] + "..." + value[-4:] if len(value) > 12 else "set"
    return value


# ------------------------------------------------------------------ validation
def validate(key, value):
    """Return (ok, cleaned_value_or_error). Used by the bot's /set* commands."""
    value = str(value).strip()
    if key == "LANG":
        v = value.lower()
        if v not in ("en", "he"):
            return False, "language must be 'en' or 'he'"
        return True, v
    if key in ("PLAN_DOWN_MBPS", "PLAN_UP_MBPS"):
        if value == "" and key == "PLAN_UP_MBPS":
            return True, ""
        try:
            f = float(value)
        except ValueError:
            return False, "must be a number (Mbps)"
        if not 0 < f <= 100000:
            return False, "must be between 0 and 100000 Mbps"
        return True, ("%g" % f)
    if key == "INTERVAL_MINUTES":
        try:
            n = int(float(value))
        except ValueError:
            return False, "must be a whole number of minutes"
        if not 5 <= n <= 1440:
            return False, "must be between 5 and 1440 minutes"
        return True, str(n)
    if key == "ALERT_THRESHOLD_PCT":
        try:
            n = int(float(value))
        except ValueError:
            return False, "must be a percentage"
        if not 1 <= n <= 100:
            return False, "must be between 1 and 100"
        return True, str(n)
    if key == "ALERT_COOLDOWN_MIN":
        try:
            n = int(float(value))
        except ValueError:
            return False, "must be a whole number of minutes"
        return True, str(max(0, n))
    if key == "PING_HOST":
        if not re.match(r"^[A-Za-z0-9._:-]{1,255}$", value):
            return False, "must be a hostname or IP address"
        return True, value
    if key == "REPORT_DOW":
        try:
            n = int(value)
        except ValueError:
            return False, "must be 0-6 (0=Sunday)"
        if not 0 <= n <= 6:
            return False, "must be 0-6 (0=Sunday)"
        return True, str(n)
    if key in ("REPORT_HOUR", "PEAK_START", "PEAK_END", "OFFPEAK_START", "OFFPEAK_END"):
        try:
            n = int(value)
        except ValueError:
            return False, "must be an hour 0-24"
        if not 0 <= n <= 24:
            return False, "must be an hour 0-24"
        return True, str(n)
    if key in ("ALERTS_ENABLED", "REPORT_ENABLED"):
        v = value.lower()
        if v in ("1", "on", "true", "yes"):
            return True, "1"
        if v in ("0", "off", "false", "no"):
            return True, "0"
        return False, "must be on or off"
    if key == "CHAT_ID":
        if not re.match(r"^-?\d{1,20}$", value):
            return False, "must be a numeric chat id"
        return True, value
    if key == "BOT_TOKEN":
        if not re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", value):
            return False, "does not look like a BotFather token (123456:AA...)"
        return True, value
    return True, value


# ------------------------------------------------------------------ scheduling
def cron_expr(minutes):
    """Turn 'every N minutes' into a crontab time spec, aligned to the clock."""
    n = max(1, int(minutes))
    if n < 60:
        step = next((d for d in (5, 10, 15, 20, 30) if d >= n), 30)
        if 60 % n == 0:
            step = n
        return "*/%d * * * *" % step
    if n == 60:
        return "0 * * * *"
    hours = max(1, round(n / 60.0))
    if hours >= 24:
        return "0 0 * * *"
    if 24 % hours == 0:
        return "0 */%d * * *" % hours
    return "0 */%d * * *" % min((h for h in (2, 3, 4, 6, 8, 12) if h >= hours), default=12)


def schedule_lines(cfg):
    """The crontab lines netmon owns, derived entirely from the config."""
    py = sys.executable or "/usr/bin/env python3"
    measure = "%s [ -f %s ] || %s %s >> %s 2>&1" % (
        cron_expr(get_int(cfg, "INTERVAL_MINUTES")), PAUSE_FLAG,
        py, os.path.join(DIR, "netmon.py"), CRON_LOG)
    lines = [measure]
    if get_bool(cfg, "REPORT_ENABLED"):
        lines.append("%d %d * * %d %s %s --telegram >> %s 2>&1" % (
            0, get_int(cfg, "REPORT_HOUR"), get_int(cfg, "REPORT_DOW"),
            py, os.path.join(DIR, "report.py"), CRON_LOG))
    return lines


def apply_schedule(cfg):
    """Rewrite only the block between our markers in the user's crontab."""
    if os.environ.get("NETMON_SKIP_CRON"):
        return True, "\n".join(schedule_lines(cfg)) + "\n(NETMON_SKIP_CRON set: crontab not touched)"
    try:
        p = subprocess.run(["crontab", "-l"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=15)
        current = p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError) as e:
        return False, "cannot read crontab: %s" % e

    kept, inside = [], False
    for line in current.splitlines():
        if line.strip() == CRON_BEGIN:
            inside = True
            continue
        if line.strip() == CRON_END:
            inside = False
            continue
        if not inside:
            # Drop stray lines from pre-marker installs so we never double-run.
            if os.path.join(DIR, "netmon.py") in line or os.path.join(DIR, "report.py") in line:
                continue
            kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()
    block = [CRON_BEGIN] + schedule_lines(cfg) + [CRON_END, ""]
    new = "\n".join(kept + [""] + block if kept else block)

    try:
        p = subprocess.run(["crontab", "-"], input=new, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, timeout=15)
        if p.returncode != 0:
            return False, (p.stderr or "crontab returned %d" % p.returncode).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, "cannot write crontab: %s" % e
    return True, "\n".join(schedule_lines(cfg))


# ----------------------------------------------------------------------- state
def read_state():
    return read_file(STATE_PATH)


def write_state(updates):
    """Small persistent scratch (last alert time). Never holds secrets."""
    st = read_state()
    st.update({k: str(v) for k, v in updates.items()})
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k in sorted(st):
            f.write("%s=%s\n" % (k, st[k]))
    os.replace(tmp, STATE_PATH)


# ------------------------------------------------------------------------ CLI
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Inspect or change netmon.conf")
    ap.add_argument("--show", action="store_true", help="print the effective configuration")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--apply-schedule", action="store_true", help="rewrite the managed cron block")
    ap.add_argument("--config", default=CONF_PATH)
    args = ap.parse_args()

    cfg = load(args.config)
    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print("bad --set %r, expected KEY=VALUE" % item, file=sys.stderr)
                return 2
            k, v = item.split("=", 1)
            k = k.strip().upper()
            if k not in SCHEMA:
                print("unknown key %r (see --show)" % k, file=sys.stderr)
                return 2
            ok, cleaned = validate(k, v)
            if not ok:
                print("%s: %s" % (k, cleaned), file=sys.stderr)
                return 2
            updates[k] = cleaned
        set_values(cfg, updates, args.config)
        print("Updated: %s" % ", ".join(sorted(updates)))

    touched_schedule = any(k.split("=", 1)[0].strip().upper() in
                           ("INTERVAL_MINUTES", "REPORT_ENABLED", "REPORT_DOW", "REPORT_HOUR")
                           for k in args.set)
    if args.apply_schedule or touched_schedule:
        ok, msg = apply_schedule(cfg)
        print(("Schedule applied:\n" if ok else "Schedule NOT applied: ") + msg)
        if not ok:
            return 1

    if args.show or not (args.set or args.apply_schedule):
        print("# %s" % args.config)
        for k in SCHEMA:
            print("%-22s %s" % (k, mask(k, cfg.get(k, ""))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
