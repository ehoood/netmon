#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netmon.py - Single internet-quality measurement, appended to a CSV log.

Runs on any Linux machine with a wired connection to your router - a Raspberry
Pi, an old laptop, a NAS, a VM. Cron (or the Telegram bot) calls it on a fixed
interval. It measures download / upload / idle latency / jitter / packet loss
and, crucially for a congested line, latency-under-load (bufferbloat) plus the
hour of day, so evenings can later be compared against quiet night hours.

Zero Python dependencies (stdlib only). Requires an external speedtest tool:
  - Ookla official `speedtest`  (preferred - reports loaded latency / bufferbloat)
  - or python `speedtest-cli`   (fallback - no bufferbloat)
plus the standard `ping` binary.

Settings come from netmon.conf (see netmon_config.py); CLI flags override it.
If ALERTS_ENABLED is on and Telegram credentials are configured, a measurement
that falls below ALERT_THRESHOLD_PCT of your plan - or fails outright - sends
an instant Telegram message, rate-limited by ALERT_COOLDOWN_MIN.

Which server it measures against matters more than anything else here. A
speedtest against one auto-selected server measures that server, and the path
to it, as much as it measures your line - and the nearest server is not
reliably a good one. netmon therefore CALIBRATES: it tries several nearby
servers once, keeps the fastest as SERVER_ID, and measures against that one
from then on, re-checking every CALIBRATE_DAYS. The bias is one-directional -
a congested server can only make a line look slower, never faster - so the
fastest server observed is the honest estimate of what the line can do.

Usage:
    ./netmon.py                         # append one row to ./netmon_log.csv
    ./netmon.py --csv /path/log.csv     # custom log location
    ./netmon.py --ping-host 8.8.8.8     # external baseline ping target
    ./netmon.py --no-alert              # measure without alerting
    ./netmon.py --calibrate             # re-pick the server that represents the line
    ./netmon.py --server-id 12345       # one-off measurement against a given server
"""

import argparse
import csv
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

import netmon_config as cfgmod
from i18n import t

# Columns written to the CSV. Keep order stable so old logs stay readable.
FIELDS = [
    "timestamp_iso",       # local time, ISO8601
    "timestamp_epoch",     # unix seconds (easy sorting/plotting)
    "hour_of_day",         # 0-23 local, for peak-hour analysis
    "status",              # ok | error
    "download_mbps",
    "upload_mbps",
    "ping_idle_ms",        # idle latency to speedtest server
    "jitter_ms",
    "packet_loss_pct",     # from speedtest, if reported
    "dl_latency_ms",       # latency during download (loaded)
    "ul_latency_ms",       # latency during upload (loaded)
    "bufferbloat_ms",      # max(loaded) - idle  -> the key congestion signal
    "ext_ping_avg_ms",     # independent baseline ping (e.g. 1.1.1.1)
    "ext_ping_loss_pct",
    "server_name",
    "isp",
    "tool",                # ookla | speedtest-cli
    "result_url",          # shareable Ookla result, if any
    "error",
]

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netmon_log.csv")


def run(cmd, timeout):
    """Run a command, return (returncode, stdout, stderr). Never raises on failure."""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except FileNotFoundError:
        return 127, "", "command not found: %s" % cmd[0]
    except Exception as e:  # be defensive: cron jobs must never crash silently
        return 1, "", "exec error: %s" % e


def external_ping(host, count=10):
    """Independent baseline latency/loss, ISP-agnostic. Best effort."""
    rc, out, _ = run(["ping", "-c", str(count), "-i", "0.2", "-w", "20", host], timeout=40)
    if rc != 0 and not out:
        return None, None
    avg = None
    loss = None
    m = re.search(r"=\s*[\d.]+/([\d.]+)/", out)          # rtt min/avg/max/...
    if m:
        avg = float(m.group(1))
    m = re.search(r"([\d.]+)% packet loss", out)
    if m:
        loss = float(m.group(1))
    return avg, loss


def measure_ookla(binary, server_id=None):
    """Run Ookla speedtest --format=json and normalise the fields we care about."""
    cmd = [binary, "--format=json", "--accept-license", "--accept-gdpr"]
    if server_id:
        cmd.append("--server-id=%s" % server_id)
    rc, out, err = run(cmd, timeout=180)
    if rc != 0:
        # Ookla sometimes prints a JSON error object; surface stderr otherwise.
        msg = err.strip() or out.strip() or ("exit %s" % rc)
        raise RuntimeError(msg[:300])
    data = json.loads(out)

    def mbps(section):
        # "bandwidth" is bytes/second
        return round(data[section]["bandwidth"] * 8 / 1e6, 2)

    ping = data.get("ping", {})
    idle = ping.get("latency")
    dl_lat = (data.get("download", {}).get("latency") or {}).get("iqm")
    ul_lat = (data.get("upload", {}).get("latency") or {}).get("iqm")
    loaded = [x for x in (dl_lat, ul_lat) if x is not None]
    bufferbloat = round(max(loaded) - idle, 1) if (loaded and idle is not None) else ""

    return {
        "download_mbps": mbps("download"),
        "upload_mbps": mbps("upload"),
        "ping_idle_ms": round(idle, 2) if idle is not None else "",
        "jitter_ms": round(ping.get("jitter"), 2) if ping.get("jitter") is not None else "",
        "packet_loss_pct": data.get("packetLoss", ""),
        "dl_latency_ms": round(dl_lat, 2) if dl_lat is not None else "",
        "ul_latency_ms": round(ul_lat, 2) if ul_lat is not None else "",
        "bufferbloat_ms": bufferbloat,
        # Deliberately no server id column: FIELDS defines the CSV header, which
        # is written once at file creation, so growing it would make every new
        # row wider than the header of an existing log. server_name identifies
        # the server well enough for reading the log.
        "server_name": (data.get("server", {}) or {}).get("name", ""),
        "isp": data.get("isp", ""),
        "tool": "ookla",
        "result_url": (data.get("result", {}) or {}).get("url", ""),
    }


def measure_speedtest_cli(binary):
    """Fallback: python speedtest-cli --json. No loaded-latency / bufferbloat."""
    rc, out, err = run([binary, "--json", "--secure"], timeout=180)
    if rc != 0:
        raise RuntimeError((err.strip() or out.strip() or ("exit %s" % rc))[:300])
    data = json.loads(out)
    return {
        "download_mbps": round(data.get("download", 0) / 1e6, 2),
        "upload_mbps": round(data.get("upload", 0) / 1e6, 2),
        "ping_idle_ms": round(data.get("ping", 0), 2),
        "jitter_ms": "",
        "packet_loss_pct": "",
        "dl_latency_ms": "",
        "ul_latency_ms": "",
        "bufferbloat_ms": "",
        "server_name": (data.get("server", {}) or {}).get("sponsor", ""),
        "isp": (data.get("client", {}) or {}).get("isp", ""),
        "tool": "speedtest-cli",
        "result_url": data.get("share", "") or "",
    }


def list_servers(binary, limit):
    """Nearby speedtest servers, nearest first. Ookla only; [] if unavailable."""
    rc, out, err = run([binary, "-L", "--format=json"], timeout=60)
    if rc != 0:
        return []
    try:
        servers = json.loads(out).get("servers", [])
    except ValueError:
        return []
    return [(str(s.get("id")), "%s (%s)" % (s.get("name"), s.get("location")))
            for s in servers[:limit] if s.get("id") is not None]


def calibrate(binary, count, verbose=True):
    """Find which nearby server actually represents this line.

    A speedtest measures the server and the path to it as much as it measures
    your connection, and the nearest server is not reliably a good one. The
    error is one-directional: a congested server can only make the line look
    SLOWER, never faster, because no server can deliver more than the link
    carries. So the fastest server observed is the closest estimate of what the
    line can really do - and the one worth measuring against from then on.

    Returns (best_id, best_name, results) where results is a list of
    (id, name, download_mbps or None).
    """
    servers = list_servers(binary, count)
    if not servers:
        return None, None, []

    results = []
    for sid, name in servers:
        try:
            r = measure_ookla(binary, server_id=sid)
            results.append((sid, name, r["download_mbps"]))
        except Exception as e:                       # one bad server must not abort
            results.append((sid, name, None))
            if verbose:
                print("  %-34s failed: %s" % (name, str(e)[:80]), file=sys.stderr)
            continue
        if verbose:
            print("  %-34s %7.1f Mbps down" % (name, r["download_mbps"]))

    ok = [r for r in results if r[2] is not None]
    if not ok:
        return None, None, results
    best = max(ok, key=lambda r: r[2])
    return best[0], best[1], results


def maybe_calibrate(cfg, binary):
    """Calibrate on first use and every CALIBRATE_DAYS after. Best effort.

    Returns a note for the caller to print, or "". Never raises: a failed
    calibration must not cost the scheduled measurement.
    """
    days = cfgmod.get_int(cfg, "CALIBRATE_DAYS")
    if not days:
        return ""
    server = str(cfg.get("SERVER_ID", "")).strip()
    try:
        last = float(cfgmod.read_state().get("LAST_CALIBRATION_EPOCH", 0))
    except (TypeError, ValueError):
        last = 0
    age_days = (time.time() - last) / 86400.0
    if server and age_days < days:
        return ""

    count = cfgmod.get_int(cfg, "CALIBRATE_SERVERS")
    try:
        best_id, best_name, results = calibrate(binary, count, verbose=False)
    except Exception as e:
        return "calibration failed: %s" % str(e)[:120]
    # Record the attempt either way, so a provider with one reachable server
    # does not retry the whole sweep on every single run.
    cfgmod.write_state({"LAST_CALIBRATION_EPOCH": int(time.time())})
    if not best_id:
        return "calibration found no usable server; keeping the current setting"
    cfgmod.set_values(cfg, {"SERVER_ID": best_id})
    tried = ", ".join("%s=%s" % (n, "fail" if d is None else "%.0f" % d)
                      for _, n, d in results)
    return "calibrated: measuring against %s (%s)" % (best_name, tried)


def pick_speedtest():
    """Return (kind, path). Prefer Ookla; fall back to speedtest-cli."""
    for name in ("speedtest",):
        p = shutil.which(name)
        if p:
            # Confirm it's Ookla, not the Debian 'speedtest-cli' symlinked as speedtest.
            rc, out, err = run([p, "--version"], timeout=15)
            blob = (out + err).lower()
            if "ookla" in blob:
                return "ookla", p
    for name in ("speedtest-cli",):
        p = shutil.which(name)
        if p:
            return "speedtest-cli", p
    # A plain 'speedtest' that wasn't Ookla is still usable as speedtest-cli.
    p = shutil.which("speedtest")
    if p:
        return "speedtest-cli", p
    return None, None


def append_row(csv_path, row):
    new_file = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    # Ensure every field exists so the CSV stays rectangular.
    full = {k: row.get(k, "") for k in FIELDS}
    with open(csv_path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(full)


def maybe_alert(cfg, row):
    """Instant Telegram warning for a bad or failed measurement. Best effort."""
    if not cfgmod.get_bool(cfg, "ALERTS_ENABLED"):
        return
    token, chat = cfgmod.creds(cfg)
    if not token or not chat:
        return

    lang = cfgmod.lang(cfg)
    plan = cfgmod.get_float(cfg, "PLAN_DOWN_MBPS")
    threshold_pct = cfgmod.get_int(cfg, "ALERT_THRESHOLD_PCT")

    if row["status"] == "ok":
        dl = row.get("download_mbps")
        if not plan or dl in (None, ""):
            return
        share = 100.0 * float(dl) / plan
        if share >= threshold_pct:
            return
        text = t(lang, "alert_slow", float(dl), share, plan,
                 row.get("ping_idle_ms", "—"), row.get("bufferbloat_ms", "—"),
                 row.get("packet_loss_pct", "0"))
    else:
        text = t(lang, "alert_failed", (row.get("error") or "unknown error")[:300])

    # Rate limit: a line that is down would otherwise alert on every run.
    cooldown = cfgmod.get_int(cfg, "ALERT_COOLDOWN_MIN") * 60
    try:
        last = float(cfgmod.read_state().get("LAST_ALERT_EPOCH", 0))
    except (TypeError, ValueError):
        last = 0
    now = time.time()
    if cooldown and now - last < cooldown:
        return

    try:
        import telegram_send as tg
        tg.send_message(token, chat, text)
        cfgmod.write_state({"LAST_ALERT_EPOCH": int(now)})
    except Exception as e:                      # never let an alert break the cron run
        print("alert not sent: %s" % e, file=sys.stderr)


def main():
    cfg = cfgmod.load()
    ap = argparse.ArgumentParser(description="One internet-quality measurement -> CSV")
    ap.add_argument("--csv", default=cfgmod.CSV_PATH, help="log file (default: netmon_log.csv beside script)")
    ap.add_argument("--ping-host", default=cfg.get("PING_HOST") or "1.1.1.1",
                    help="external baseline ping target")
    ap.add_argument("--no-ext-ping", action="store_true", help="skip the external baseline ping")
    ap.add_argument("--no-alert", action="store_true", help="do not send a Telegram alert on a bad result")
    ap.add_argument("--server-id", default=None,
                    help="measure against this speedtest server (overrides SERVER_ID)")
    ap.add_argument("--calibrate", nargs="?", const=0, type=int, metavar="N",
                    help="test N nearby servers, keep the fastest as SERVER_ID, and exit")
    args = ap.parse_args()

    if args.calibrate is not None:
        kind, path = pick_speedtest()
        if kind != "ookla":
            print("calibration needs the Ookla speedtest CLI", file=sys.stderr)
            return 2
        count = args.calibrate or cfgmod.get_int(cfg, "CALIBRATE_SERVERS")
        print("Testing %d nearby servers - this transfers a few GB and takes a few minutes."
              % count)
        best_id, best_name, results = calibrate(path, count)
        if not best_id:
            print("No server produced a usable result; SERVER_ID left unchanged.",
                  file=sys.stderr)
            return 1
        cfgmod.set_values(cfg, {"SERVER_ID": best_id})
        cfgmod.write_state({"LAST_CALIBRATION_EPOCH": int(time.time())})
        spread = [d for _, _, d in results if d is not None]
        print("\nMeasuring against %s (id %s) from now on." % (best_name, best_id))
        if len(spread) > 1 and min(spread) and max(spread) / min(spread) >= 1.25:
            print("Servers disagreed by %.0f%% (%.0f - %.0f Mbps) - which is exactly why\n"
                  "this matters: the auto-pick could have been any of them."
                  % ((max(spread) / min(spread) - 1) * 100, min(spread), max(spread)))
        return 0

    now = datetime.datetime.now().astimezone()
    row = {
        "timestamp_iso": now.isoformat(timespec="seconds"),
        "timestamp_epoch": int(now.timestamp()),
        "hour_of_day": now.hour,
        "status": "error",
        "error": "",
    }

    # External baseline ping first (independent of the speedtest server / provider).
    if not args.no_ext_ping:
        avg, loss = external_ping(args.ping_host)
        row["ext_ping_avg_ms"] = "" if avg is None else avg
        row["ext_ping_loss_pct"] = "" if loss is None else loss

    kind, path = pick_speedtest()
    if not kind:
        row["error"] = "no speedtest tool found (install Ookla speedtest or speedtest-cli)"
        append_row(args.csv, row)
        print(row["error"], file=sys.stderr)
        return 2

    # Learn which server represents this line before trusting a number from it.
    # An explicit --server-id is the caller overriding that judgement, so leave
    # the stored setting alone.
    note = maybe_calibrate(cfg, path) if (kind == "ookla" and not args.server_id) else ""
    if note:
        print(note)
    server_id = args.server_id or str(cfg.get("SERVER_ID", "")).strip() or None

    try:
        result = (measure_ookla(path, server_id=server_id) if kind == "ookla"
                  else measure_speedtest_cli(path))
        row.update(result)
        row["status"] = "ok"
    except Exception as e:
        row["error"] = str(e)[:300]
        row["tool"] = kind

    append_row(args.csv, row)
    if not args.no_alert:
        maybe_alert(cfg, row)

    if row["status"] == "ok":
        print("%s  down=%s Mbps  up=%s Mbps  ping=%s ms  bufferbloat=%s ms  loss=%s%%" % (
            row["timestamp_iso"], row.get("download_mbps"), row.get("upload_mbps"),
            row.get("ping_idle_ms"), row.get("bufferbloat_ms"), row.get("packet_loss_pct"),
        ))
        return 0
    else:
        print("%s  MEASUREMENT FAILED: %s" % (row["timestamp_iso"], row["error"]), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
