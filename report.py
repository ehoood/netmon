#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py - Build a self-contained HTML report from netmon_log.csv and
(optionally) send it to Telegram as a document plus a text summary.

Zero dependencies (stdlib only). Charts are inline SVG, so the file opens
offline on a phone straight from the Telegram attachment.

Language, plan speed, peak windows and Telegram credentials all come from
netmon.conf; CLI flags override them.

Usage:
    ./report.py                                  # HTML report using netmon.conf
    ./report.py --plan 500 --html report.html
    ./report.py --telegram                       # build and send
    ./report.py --telegram --dry-run             # build and preview, do not send
    ./report.py --lang en                        # override the configured language
"""

import argparse
import csv
import html
import os
import statistics as st

import netmon_config as cfgmod
from i18n import t, direction

DEFAULT_CSV = cfgmod.CSV_PATH

# Coherent, colorblind-distinguishable palette (good/warn/bad differ in luminance too).
C_GOOD, C_WARN, C_BAD = "#2a9d8f", "#e9c46a", "#e76f51"
C_INK, C_MUTED, C_LINE = "#1d3557", "#6b7280", "#cbd5e1"


def fnum(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k); hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def col(rows, key):
    out = []
    for r in rows:
        if r.get("status") != "ok":
            continue
        v = fnum(r.get(key))
        if v is not None:
            out.append(v)
    return out


def current_instrument(rows):
    """The measurement server the most recent rows used, and how far back it goes.

    Speed figures from two different servers are not comparable - the same line
    can read 585 Mbps against one and 847 against another - so a median taken
    across a server change describes neither. Returns (name, rows_from_it).
    """
    ok = [r for r in rows if r.get("status") == "ok" and r.get("server_name")]
    if not ok:
        return None, rows
    name = ok[-1]["server_name"]
    keep = []
    for r in reversed(rows):
        if r.get("status") == "ok" and r.get("server_name") and r["server_name"] != name:
            break
        keep.append(r)
    return name, list(reversed(keep))


def summarize(rows, plan, peak=(18, 24), offpeak=(2, 7), per_instrument=True):
    peak_hours = set(range(peak[0], peak[1]))
    off_hours = set(range(offpeak[0], offpeak[1]))

    # Restrict to one instrument by default. Older rows are not discarded - they
    # stay in the log and remain valid on their own terms - but mixing them into
    # one median hides a change of instrument behind what looks like a change in
    # the line.
    all_rows = rows
    instrument, rows = (current_instrument(rows) if per_instrument else (None, rows))
    dropped = len(all_rows) - len(rows)

    ok = [r for r in rows if r.get("status") == "ok"]
    dl, ul = col(ok, "download_mbps"), col(ok, "upload_mbps")
    s = {
        "total": len(rows), "ok": len(ok), "fail": len(rows) - len(ok),
        "start": rows[0].get("timestamp_iso", "?") if rows else "?",
        "end": rows[-1].get("timestamp_iso", "?") if rows else "?",
        "isp": ", ".join(sorted({r.get("isp") for r in ok if r.get("isp")})) or "—",
        "dl_med": st.median(dl) if dl else 0, "dl_p10": pct(dl, 10) if dl else 0,
        "ul_med": st.median(ul) if ul else 0,
        "ping_med": st.median(col(ok, "ping_idle_ms")) if col(ok, "ping_idle_ms") else 0,
        "bb_med": st.median(col(ok, "bufferbloat_ms")) if col(ok, "bufferbloat_ms") else 0,
        "loss_avg": st.mean(col(ok, "packet_loss_pct")) if col(ok, "packet_loss_pct") else 0,
        "plan": plan, "peak": peak, "offpeak": offpeak,
        "instrument": instrument or "—",
        "excluded": dropped,          # earlier rows, from a different server
    }
    # by-hour averages
    byh = {h: {"dl": [], "bb": [], "loss": []} for h in range(24)}
    for r in ok:
        h = fnum(r.get("hour_of_day"))
        if h is None:
            continue
        h = int(h)
        for k, key in (("dl", "download_mbps"), ("bb", "bufferbloat_ms"), ("loss", "packet_loss_pct")):
            v = fnum(r.get(key))
            if v is not None:
                byh[h][k].append(v)
    s["byhour"] = {h: {k: (st.mean(v) if v else None) for k, v in d.items()} for h, d in byh.items()}
    # peak vs off-peak
    peak_vals = [fnum(r.get("download_mbps")) for r in ok
                 if fnum(r.get("hour_of_day")) in peak_hours and fnum(r.get("download_mbps"))]
    off_vals = [fnum(r.get("download_mbps")) for r in ok
                if fnum(r.get("hour_of_day")) in off_hours and fnum(r.get("download_mbps"))]
    s["peak_avg"] = st.mean(peak_vals) if peak_vals else None
    s["off_avg"] = st.mean(off_vals) if off_vals else None
    s["drop_pct"] = (100 * (1 - s["peak_avg"] / s["off_avg"])) if (peak_vals and off_vals and s["off_avg"]) else None
    # worst events
    lows = sorted([(fnum(r.get("download_mbps")), r) for r in ok if fnum(r.get("download_mbps")) is not None],
                  key=lambda t_: t_[0])
    s["worst"] = lows[:6]
    return s


def verdict(s, lang):
    """Return (emoji, headline, color)."""
    d = s["drop_pct"]
    if d is None:
        return "⚪", t(lang, "verdict_nodata"), C_MUTED
    if d >= 40:
        return "🔴", t(lang, "verdict_bad", d), C_BAD
    if d >= 20:
        return "🟠", t(lang, "verdict_warn", d), C_WARN
    return "🟢", t(lang, "verdict_good", abs(d)), C_GOOD


# ---------------------------------------------------------------- SVG charts
def svg_bars(series, plan=None, unit="Mbps", ref_label="", good_high=True, threshold=None):
    """series: dict hour->value. Bars colored by threshold. Returns SVG string."""
    W, H = 720, 240
    ml, mr, mt, mb = 44, 12, 16, 26
    pw, ph = W - ml - mr, H - mt - mb
    vals = [v for v in series.values() if v is not None]
    vmax = max(vals + ([plan] if plan else []) + ([threshold] if threshold else [1]))
    vmax = vmax * 1.1 if vmax else 1
    bw = pw / 24.0
    out = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" direction="ltr" '
           'style="direction:ltr" font-family="system-ui,Arial" font-size="10">' % (W, H)]
    # y gridlines
    for i in range(5):
        y = mt + ph - ph * i / 4
        val = vmax * i / 4
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>' % (ml, y, W - mr, y, C_LINE))
        out.append('<text x="%d" y="%.1f" fill="%s" text-anchor="end">%.0f</text>' % (ml - 4, y + 3, C_MUTED, val))
    # plan reference line
    if plan:
        yp = mt + ph - ph * plan / vmax
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.5" stroke-dasharray="5 4"/>' % (ml, yp, W - mr, yp, C_INK))
        out.append('<text x="%d" y="%.1f" fill="%s" text-anchor="start">%s %.0f</text>' % (W - mr - 78, yp - 3, C_INK, html.escape(ref_label or "plan"), plan))
    # bars
    for h in range(24):
        v = series.get(h)
        x = ml + h * bw
        if v is None:
            continue
        bh = ph * v / vmax
        y = mt + ph - bh
        if threshold is not None:
            color = (C_GOOD if v >= threshold else C_BAD) if good_high else (C_BAD if v >= threshold else C_GOOD)
        else:
            color = C_GOOD
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="1.5" fill="%s"><title>%02d:00 — %.0f %s</title></rect>'
                   % (x + bw * 0.12, y, bw * 0.76, bh, color, h, v, unit))
        if h % 3 == 0:
            out.append('<text x="%.1f" y="%d" fill="%s" text-anchor="middle">%02d</text>' % (x + bw / 2, H - 8, C_MUTED, h))
    out.append('</svg>')
    return "".join(out)


def card(label, value, sub="", color=C_INK):
    return ('<div class="card"><div class="cv" style="color:%s">%s</div>'
            '<div class="cl">%s</div><div class="cs">%s</div></div>') % (color, value, html.escape(label), html.escape(sub))


def build_html(s, lang="en"):
    emoji, head, vcolor = verdict(s, lang)
    plan = s["plan"]
    dl_pct = t(lang, "of_plan", 100 * s["dl_med"] / plan) if plan else ""
    dl_worst_pct = t(lang, "low10", 100 * s["dl_p10"] / plan) if plan else ""
    dl_color = C_GOOD if (not plan or s["dl_med"] >= 0.8 * plan) else (C_WARN if s["dl_med"] >= 0.5 * plan else C_BAD)
    bb_color = C_GOOD if s["bb_med"] < 30 else (C_WARN if s["bb_med"] < 80 else C_BAD)
    loss_color = C_GOOD if s["loss_avg"] < 0.5 else (C_WARN if s["loss_avg"] < 2 else C_BAD)

    dl_series = {h: d["dl"] for h, d in s["byhour"].items()}
    bb_series = {h: d["bb"] for h, d in s["byhour"].items()}
    threshold = 0.6 * plan if plan else None
    rtl = direction(lang) == "rtl"

    worst_rows = "".join(
        "<tr><td>%s</td><td style='color:%s'>%.0f Mbps</td><td>%s ms</td><td>%s%%</td></tr>" % (
            html.escape(r.get("timestamp_iso", "")), C_BAD, v,
            r.get("ping_idle_ms", "—"), r.get("packet_loss_pct", "0"))
        for v, r in s["worst"])

    pv = ""
    if s["off_avg"] is not None and s["peak_avg"] is not None:
        pv = ("<div class='pv'><span>%s</span><span>%s</span></div>") % (
            t(lang, "night_label", s["offpeak"][0], s["offpeak"][1], s["off_avg"]),
            t(lang, "evening_label", s["peak"][0], s["peak"][1], vcolor, s["peak_avg"]))

    return """<!doctype html><html lang="%(lang)s" dir="%(dir)s"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title><style>
*{box-sizing:border-box} body{margin:0;font-family:system-ui,Arial,sans-serif;color:%(ink)s;background:#f4f6f8;line-height:1.5}
.wrap{max-width:780px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0 2px} .meta{color:%(muted)s;font-size:13px;margin-bottom:14px}
.banner{background:#fff;border-%(side)s:6px solid %(vcolor)s;border-radius:10px;padding:14px 16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.banner .e{font-size:26px} .banner .h{font-size:15px;font-weight:600;margin-top:4px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:18px}
.card{background:#fff;border-radius:10px;padding:12px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.cv{font-size:22px;font-weight:700} .cl{font-size:12px;color:%(muted)s;margin-top:2px} .cs{font-size:11px;color:%(muted)s}
.chart{background:#fff;border-radius:10px;padding:14px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.chart h2{font-size:14px;margin:0 0 6px} .chart .cap{font-size:12px;color:%(muted)s;margin-top:6px}
.pv{display:flex;gap:18px;font-size:13px;margin-top:8px;flex-wrap:wrap}
.tw{overflow-x:auto}
table{width:100%%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);font-size:13px}
th,td{padding:8px 10px;text-align:%(side)s;border-bottom:1px solid #eee} th{background:#fafafa;color:%(muted)s;font-weight:600}
.foot{color:%(muted)s;font-size:12px;margin-top:16px}
</style></head><body><div class="wrap">
<h1>%(title)s</h1>
<div class="meta">%(meta)s</div>
<div class="banner"><span class="e">%(emoji)s</span><div class="h">%(head)s</div>%(pv)s</div>
<div class="cards">
%(c_dl)s%(c_ul)s%(c_ping)s%(c_bb)s%(c_loss)s%(c_rate)s
</div>
<div class="chart"><h2>%(chart_dl_title)s</h2>%(chart_dl)s
<div class="cap">%(chart_dl_cap)s</div></div>
<div class="chart"><h2>%(chart_bb_title)s</h2>%(chart_bb)s
<div class="cap">%(chart_bb_cap)s</div></div>
<h2 style="font-size:14px;margin:18px 0 8px">%(worst_title)s</h2>
<div class="tw"><table><tr><th>%(col_time)s</th><th>%(col_dl)s</th><th>%(col_ping)s</th><th>%(col_loss)s</th></tr>%(worst)s</table></div>
<div class="foot">%(footer)s</div>
</div></body></html>""" % {
        "lang": lang, "dir": direction(lang), "side": "right" if rtl else "left",
        "ink": C_INK, "muted": C_MUTED, "vcolor": vcolor,
        "title": html.escape(t(lang, "report_title")),
        "meta": html.escape(t(lang, "report_meta", s["start"], s["end"], s["isp"], s["ok"],
                              100 * s["ok"] / s["total"] if s["total"] else 0))
                # Say which server the figures come from whenever the log holds
                # rows from another one, so a jump in the numbers is not read as
                # a change in the line.
                + ("<br>" + html.escape(t(lang, "report_instrument",
                                          s["instrument"], s["excluded"]))
                   if s.get("excluded") else ""),
        "emoji": emoji, "head": html.escape(head), "pv": pv,
        "c_dl": card(t(lang, "card_dl"), "%.0f" % s["dl_med"], dl_pct, dl_color),
        "c_ul": card(t(lang, "card_ul"), "%.0f" % s["ul_med"], "Mbps", C_INK),
        "c_ping": card(t(lang, "card_ping"), "%.0f" % s["ping_med"], "ms", C_INK),
        "c_bb": card(t(lang, "card_bb"), "%.0f" % s["bb_med"], "ms " + t(lang, "median"), bb_color),
        "c_loss": card(t(lang, "card_loss"), "%.1f%%" % s["loss_avg"], t(lang, "average"), loss_color),
        "c_rate": card(t(lang, "card_dl_low"), "%.0f" % s["dl_p10"], dl_worst_pct, C_MUTED),
        "chart_dl_title": html.escape(t(lang, "chart_dl_title")),
        "chart_dl_cap": html.escape(t(lang, "chart_dl_cap")),
        "chart_bb_title": html.escape(t(lang, "chart_bb_title")),
        "chart_bb_cap": html.escape(t(lang, "chart_bb_cap")),
        "chart_dl": svg_bars(dl_series, plan=plan, unit="Mbps", ref_label=t(lang, "plan_label"),
                             good_high=True, threshold=threshold),
        "chart_bb": svg_bars(bb_series, plan=None, unit="ms", good_high=False, threshold=80),
        "worst_title": html.escape(t(lang, "worst_title")),
        "col_time": html.escape(t(lang, "col_time")), "col_dl": html.escape(t(lang, "col_download")),
        "col_ping": html.escape(t(lang, "col_ping")), "col_loss": html.escape(t(lang, "col_loss")),
        "worst": worst_rows,
        "footer": html.escape(t(lang, "report_footer", s["total"], s["fail"])),
    }


def text_summary(s, lang="en"):
    emoji, head, _ = verdict(s, lang)
    plan = s["plan"]
    lines = [
        "%s <b>%s</b>" % (emoji, t(lang, "summary_title")),
        head,
        "",
        t(lang, "summary_dl", s["dl_med"], (" (" + t(lang, "of_plan", 100 * s["dl_med"] / plan) + ")") if plan else ""),
        t(lang, "summary_ul_ping", s["ul_med"], s["ping_med"]),
        t(lang, "summary_bb_loss", s["bb_med"], s["loss_avg"]),
    ]
    if s["off_avg"] is not None and s["peak_avg"] is not None:
        lines.append(t(lang, "summary_night_day", s["off_avg"], s["peak_avg"]))
    lines.append(t(lang, "summary_count", s["ok"], 100 * s["ok"] / s["total"] if s["total"] else 0))
    lines.append(t(lang, "summary_attached"))
    return "\n".join(lines)


def build(cfg, csv_path, html_path, plan=None, lang=None):
    """Shared entry point for the CLI and the bot. Returns (summary_text, html_path)."""
    lang = lang or cfgmod.lang(cfg)
    plan = plan if plan is not None else cfgmod.get_float(cfg, "PLAN_DOWN_MBPS")
    s = summarize(load(csv_path), plan,
                  peak=(cfgmod.get_int(cfg, "PEAK_START"), cfgmod.get_int(cfg, "PEAK_END")),
                  offpeak=(cfgmod.get_int(cfg, "OFFPEAK_START"), cfgmod.get_int(cfg, "OFFPEAK_END")))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(s, lang))
    return text_summary(s, lang), html_path


def main():
    cfg = cfgmod.load()
    ap = argparse.ArgumentParser(description="Build HTML report + optional Telegram send")
    ap.add_argument("--csv", default=cfgmod.CSV_PATH)
    ap.add_argument("--plan", type=float, default=None, help="plan download Mbps (default: from netmon.conf)")
    ap.add_argument("--lang", choices=("en", "he"), default=None)
    ap.add_argument("--html", default=cfgmod.HTML_PATH)
    ap.add_argument("--telegram", action="store_true", help="send report + summary to Telegram")
    ap.add_argument("--config", default=cfgmod.CONF_PATH)
    ap.add_argument("--dry-run", action="store_true", help="with --telegram: build and preview, do not send")
    args = ap.parse_args()

    if args.config != cfgmod.CONF_PATH:
        cfg = cfgmod.load(args.config)
    if not os.path.exists(args.csv):
        print("CSV not found: %s" % args.csv); return 2

    lang = args.lang or cfgmod.lang(cfg)
    summary, path = build(cfg, args.csv, args.html, plan=args.plan, lang=lang)
    print("Wrote HTML report: %s" % path)

    if args.telegram:
        import telegram_send as tg
        token, chat = cfgmod.creds(cfg)
        if args.dry_run:
            print("[dry-run] token=%s chat=%s" % ("SET" if token else "MISSING", chat or "MISSING"))
            print("[dry-run] message:\n" + summary)
            return 0
        if not token or not chat:
            print("ERROR: missing BOT_TOKEN/CHAT_ID in %s" % args.config); return 2
        tg.send_message(token, chat, summary)
        tg.send_document(token, chat, path, caption=t(lang, "report_caption"))
        print("Sent to Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
