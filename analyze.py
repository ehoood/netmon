#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze.py - Turn a week of netmon_log.csv into a readable report.

Zero dependencies. Prints:
  * overall stats (avg / median / p10 / min / max) for the key metrics
  * a by-hour-of-day table with an ASCII bar chart of average download
  * peak-hour vs off-peak comparison (the tell-tale sign of an oversubscribed
    shared line: fine at night, collapses in the evening)
  * flagged bad events (high packet loss, high bufferbloat, low speed)

Usage:
    ./analyze.py                       # reads ./netmon_log.csv
    ./analyze.py --csv /path/log.csv
    ./analyze.py --plan 200            # compare against your 200 Mbps plan
"""

import argparse
import csv
import os
import statistics as st

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netmon_log.csv")

# Evening peak window for a residential shared line (local hours, inclusive).
PEAK_HOURS = set(range(18, 24))          # 18:00-23:59
OFFPEAK_HOURS = set(range(2, 7))         # 02:00-06:59 (quiet reference)


def fnum(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def load(csv_path):
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def col(rows, key, ok_only=True):
    out = []
    for r in rows:
        if ok_only and r.get("status") != "ok":
            continue
        v = fnum(r.get(key))
        if v is not None:
            out.append(v)
    return out


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def stat_line(label, values, unit, lower_is_better=False):
    if not values:
        return "  %-16s no data" % label
    p10 = pct(values, 10)
    p90 = pct(values, 90)
    worst = "%.1f" % (p90 if lower_is_better else p10)
    return "  %-16s avg %7.1f | med %7.1f | min %7.1f | max %7.1f | %s10/90 %s %s" % (
        label, st.mean(values), st.median(values), min(values), max(values),
        "p" , worst, unit,
    )


def bar(value, vmax, width=32):
    if vmax <= 0:
        return ""
    n = int(round(width * value / vmax))
    return "#" * n


def main():
    ap = argparse.ArgumentParser(description="Analyze netmon CSV into a weekly report")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--plan", type=float, default=None,
                    help="advertised plan Mbps, to compute %% of plan delivered")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print("CSV not found: %s" % args.csv)
        return 2

    rows = load(args.csv)
    total = len(rows)
    ok = [r for r in rows if r.get("status") == "ok"]
    fails = total - len(ok)

    print("=" * 72)
    print(" NETMON REPORT  |  %s" % args.csv)
    print("=" * 72)
    if not rows:
        print(" No samples yet.")
        return 0

    span_start = rows[0].get("timestamp_iso", "?")
    span_end = rows[-1].get("timestamp_iso", "?")
    print(" Samples: %d   (ok: %d, failed: %d, success %.0f%%)" % (
        total, len(ok), fails, 100.0 * len(ok) / total if total else 0))
    print(" Range:   %s  ->  %s" % (span_start, span_end))
    isps = {r.get("isp") for r in ok if r.get("isp")}
    if isps:
        print(" ISP:     %s" % ", ".join(sorted(isps)))
    print()

    dl = col(ok, "download_mbps")
    ul = col(ok, "upload_mbps")
    print(" OVERALL")
    print(stat_line("Download (Mbps)", dl, "Mbps"))
    print(stat_line("Upload (Mbps)", ul, "Mbps"))
    print(stat_line("Idle ping (ms)", col(ok, "ping_idle_ms"), "ms", lower_is_better=True))
    print(stat_line("Jitter (ms)", col(ok, "jitter_ms"), "ms", lower_is_better=True))
    print(stat_line("Bufferbloat (ms)", col(ok, "bufferbloat_ms"), "ms", lower_is_better=True))
    print(stat_line("Packet loss (%)", col(ok, "packet_loss_pct"), "%", lower_is_better=True))
    print(stat_line("Ext ping (ms)", col(ok, "ext_ping_avg_ms"), "ms", lower_is_better=True))

    if args.plan and dl:
        print("\n  Plan: %.0f Mbps  ->  median download is %.0f%% of plan, worst 10%% is %.0f%%"
              % (args.plan, 100 * st.median(dl) / args.plan, 100 * pct(dl, 10) / args.plan))

    # ---- by hour of day -------------------------------------------------
    print("\n BY HOUR OF DAY  (avg download, bar; ping/loss alongside)")
    by_hour = {h: [] for h in range(24)}
    for r in ok:
        h = fnum(r.get("hour_of_day"))
        if h is not None:
            by_hour[int(h)].append(r)
    vmax = max([st.mean(col(v, "download_mbps")) for v in by_hour.values()
                if col(v, "download_mbps")] or [1])
    for h in range(24):
        rs = by_hour[h]
        d = col(rs, "download_mbps")
        if not d:
            print("  %02d:00  %5s  n=0" % (h, "-"))
            continue
        avg_d = st.mean(d)
        p = col(rs, "ping_idle_ms")
        bb = col(rs, "bufferbloat_ms")
        loss = col(rs, "packet_loss_pct")
        print("  %02d:00  %6.1f  %-32s  ping %5.0f  bb %5.0f  loss %4.1f%%  n=%d" % (
            h, avg_d, bar(avg_d, vmax),
            st.mean(p) if p else 0, st.mean(bb) if bb else 0,
            st.mean(loss) if loss else 0, len(d)))

    # ---- peak vs off-peak ----------------------------------------------
    peak_dl = [fnum(r.get("download_mbps")) for r in ok
               if fnum(r.get("hour_of_day")) in PEAK_HOURS and fnum(r.get("download_mbps"))]
    off_dl = [fnum(r.get("download_mbps")) for r in ok
              if fnum(r.get("hour_of_day")) in OFFPEAK_HOURS and fnum(r.get("download_mbps"))]
    print("\n PEAK (18:00-23:59) vs OFF-PEAK (02:00-06:59)")
    if peak_dl and off_dl:
        pm, om = st.mean(peak_dl), st.mean(off_dl)
        drop = 100 * (1 - pm / om) if om else 0
        print("  off-peak avg download: %.1f Mbps (n=%d)" % (om, len(off_dl)))
        print("  peak     avg download: %.1f Mbps (n=%d)" % (pm, len(peak_dl)))
        print("  --> evening is %.0f%% %s than the quiet hours" % (
            abs(drop), "SLOWER" if drop > 0 else "faster"))
        if drop >= 40:
            print("  ==> Strong sign of an OVERSUBSCRIBED shared line at peak hours.")
        elif drop >= 20:
            print("  ==> Noticeable peak-hour congestion.")
        else:
            print("  ==> Line holds up reasonably well under peak load.")
    else:
        print("  Not enough peak/off-peak samples yet (need a few days spanning both).")

    # ---- flagged bad events --------------------------------------------
    print("\n WORST EVENTS")
    def show_worst(key, label, lower_is_better, unit, n=5):
        vals = [(fnum(r.get(key)), r) for r in ok if fnum(r.get(key)) is not None]
        if not vals:
            return
        vals.sort(key=lambda t: t[0], reverse=not lower_is_better)
        print("  %s:" % label)
        for v, r in vals[:n]:
            print("    %s  %.1f%s  (ping %s, loss %s%%)" % (
                r.get("timestamp_iso"), v, unit,
                r.get("ping_idle_ms"), r.get("packet_loss_pct") or "0"))
    show_worst("download_mbps", "Lowest download", lower_is_better=True, unit=" Mbps")
    show_worst("packet_loss_pct", "Highest packet loss", lower_is_better=False, unit="%")
    show_worst("bufferbloat_ms", "Worst bufferbloat", lower_is_better=False, unit=" ms")

    if fails:
        print("\n FAILED SAMPLES (possible full outages / saturation):")
        for r in [r for r in rows if r.get("status") != "ok"][:10]:
            print("    %s  %s" % (r.get("timestamp_iso"), (r.get("error") or "")[:80]))

    print("\n" + "=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
