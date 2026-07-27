#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netmon.py - Single internet-quality measurement, appended to a CSV log.

Designed to run on a Raspberry Pi wired to the main Deco, once per hour via cron.
Measures download / upload / idle-latency / jitter / packet-loss and, crucially
for a *shared* connection, latency-under-load (bufferbloat) and time-of-day.

Zero Python dependencies (stdlib only). Requires an external speedtest tool:
  - Ookla official `speedtest`  (preferred - reports loaded latency / bufferbloat)
  - or python `speedtest-cli`   (fallback - no bufferbloat)
plus the standard `ping` binary.

Usage:
    ./netmon.py                         # append one row to ./netmon_log.csv
    ./netmon.py --csv /path/log.csv     # custom log location
    ./netmon.py --ping-host 8.8.8.8     # external baseline ping target
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


def measure_ookla(binary):
    """Run Ookla speedtest --format=json and normalise the fields we care about."""
    cmd = [binary, "--format=json", "--accept-license", "--accept-gdpr"]
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


def main():
    ap = argparse.ArgumentParser(description="One internet-quality measurement -> CSV")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="log file (default: netmon_log.csv beside script)")
    ap.add_argument("--ping-host", default="1.1.1.1", help="external baseline ping target")
    ap.add_argument("--no-ext-ping", action="store_true", help="skip the external baseline ping")
    args = ap.parse_args()

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

    try:
        result = measure_ookla(path) if kind == "ookla" else measure_speedtest_cli(path)
        row.update(result)
        row["status"] = "ok"
    except Exception as e:
        row["error"] = str(e)[:300]
        row["tool"] = kind

    append_row(args.csv, row)

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
