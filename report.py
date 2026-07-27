#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py - Build a self-contained HTML report from netmon_log.csv and
(optionally) send it to Telegram as a document plus a text summary.

Zero dependencies (stdlib only). Charts are inline SVG, so the HTML file opens
offline on a phone straight from the Telegram document.

Usage:
    ./report.py --plan 200 --html report.html
    ./report.py --plan 200 --html report.html --telegram            # send via Telegram
    ./report.py --plan 200 --html report.html --telegram --dry-run  # build + preview, no send
"""

import argparse
import csv
import html
import os
import statistics as st

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netmon_log.csv")
PEAK_HOURS = set(range(18, 24))
OFFPEAK_HOURS = set(range(2, 7))

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


def summarize(rows, plan):
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
        "plan": plan,
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
    # peak vs offpeak
    peak = [fnum(r.get("download_mbps")) for r in ok if fnum(r.get("hour_of_day")) in PEAK_HOURS and fnum(r.get("download_mbps"))]
    off = [fnum(r.get("download_mbps")) for r in ok if fnum(r.get("hour_of_day")) in OFFPEAK_HOURS and fnum(r.get("download_mbps"))]
    s["peak_avg"] = st.mean(peak) if peak else None
    s["off_avg"] = st.mean(off) if off else None
    s["drop_pct"] = (100 * (1 - s["peak_avg"] / s["off_avg"])) if (peak and off and s["off_avg"]) else None
    # worst events
    lows = sorted([(fnum(r.get("download_mbps")), r) for r in ok if fnum(r.get("download_mbps")) is not None],
                  key=lambda t: t[0])
    s["worst"] = lows[:6]
    return s


def verdict(s):
    """Return (emoji, headline_he, color)."""
    d = s["drop_pct"]
    if d is None:
        return "⚪", "אין עדיין מספיק דגימות שמכסות גם ערב וגם לילה", C_MUTED
    if d >= 40:
        return "🔴", "הקו נחנק בשעות הערב — ירידה של %.0f%% לעומת הלילה. סימן מובהק לקו משותף עמוס (over-subscription)." % d, C_BAD
    if d >= 20:
        return "🟠", "עומס מורגש בשעות הערב — ירידה של %.0f%% לעומת הלילה." % d, C_WARN
    return "🟢", "הקו יציב יחסית גם בשעות העומס (ירידה של %.0f%% בלבד)." % abs(d), C_GOOD


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
        out.append('<text x="%d" y="%.1f" fill="%s" text-anchor="start">%s %.0f</text>' % (W - mr - 78, yp - 3, C_INK, ref_label or "plan", plan))
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


def build_html(s):
    emoji, head, vcolor = verdict(s)
    plan = s["plan"]
    dl_pct = ("%.0f%% מהחבילה" % (100 * s["dl_med"] / plan)) if plan else ""
    dl_worst_pct = ("שפל 10%%: %.0f%%" % (100 * s["dl_p10"] / plan)) if plan else ""
    dl_color = C_GOOD if (not plan or s["dl_med"] >= 0.8 * plan) else (C_WARN if s["dl_med"] >= 0.5 * plan else C_BAD)
    bb_color = C_GOOD if s["bb_med"] < 30 else (C_WARN if s["bb_med"] < 80 else C_BAD)
    loss_color = C_GOOD if s["loss_avg"] < 0.5 else (C_WARN if s["loss_avg"] < 2 else C_BAD)

    dl_series = {h: d["dl"] for h, d in s["byhour"].items()}
    bb_series = {h: d["bb"] for h, d in s["byhour"].items()}
    threshold = 0.6 * plan if plan else None

    worst_rows = "".join(
        "<tr><td>%s</td><td style='color:%s'>%.0f Mbps</td><td>%s ms</td><td>%s%%</td></tr>" % (
            html.escape(r.get("timestamp_iso", "")), C_BAD, v,
            r.get("ping_idle_ms", "—"), r.get("packet_loss_pct", "0"))
        for v, r in s["worst"])

    pv = ""
    if s["off_avg"] is not None and s["peak_avg"] is not None:
        pv = ("<div class='pv'><span>לילה (02–07): <b>%.0f</b> Mbps</span>"
              "<span>ערב (18–24): <b style='color:%s'>%.0f</b> Mbps</span></div>") % (
              s["off_avg"], vcolor, s["peak_avg"])

    return """<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>דוח אינטרנט שבועי</title><style>
*{box-sizing:border-box} body{margin:0;font-family:system-ui,Arial,sans-serif;color:%(ink)s;background:#f4f6f8;line-height:1.5}
.wrap{max-width:780px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0 2px} .meta{color:%(muted)s;font-size:13px;margin-bottom:14px}
.banner{background:#fff;border-right:6px solid %(vcolor)s;border-radius:10px;padding:14px 16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.banner .e{font-size:26px} .banner .h{font-size:15px;font-weight:600;margin-top:4px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:18px}
.card{background:#fff;border-radius:10px;padding:12px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.cv{font-size:22px;font-weight:700} .cl{font-size:12px;color:%(muted)s;margin-top:2px} .cs{font-size:11px;color:%(muted)s}
.chart{background:#fff;border-radius:10px;padding:14px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.chart h2{font-size:14px;margin:0 0 6px} .chart .cap{font-size:12px;color:%(muted)s;margin-top:6px}
.pv{display:flex;gap:18px;font-size:13px;margin-top:8px;flex-wrap:wrap}
table{width:100%%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);font-size:13px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid #eee} th{background:#fafafa;color:%(muted)s;font-weight:600}
.foot{color:%(muted)s;font-size:12px;margin-top:16px}
</style></head><body><div class="wrap">
<h1>דוח איכות אינטרנט שבועי</h1>
<div class="meta">%(start)s → %(end)s · ספק: %(isp)s · %(ok)d מדידות (%(rate).0f%% הצלחה)</div>
<div class="banner"><span class="e">%(emoji)s</span><div class="h">%(head)s</div>%(pv)s</div>
<div class="cards">
%(c_dl)s%(c_ul)s%(c_ping)s%(c_bb)s%(c_loss)s%(c_rate)s
</div>
<div class="chart"><h2>מהירות הורדה ממוצעת לפי שעה ביום</h2>%(chart_dl)s
<div class="cap">אדום = מתחת ל‑60%% מהחבילה. הקו המקווקו = מהירות החבילה. מרחפים על עמודה לפרטים.</div></div>
<div class="chart"><h2>Bufferbloat (עליית השהיה תחת עומס) לפי שעה</h2>%(chart_bb)s
<div class="cap">אדום = מעל 80ms. ערכים גבוהים בשעות הערב = הקו נחנק ואין ניהול תור/QoS.</div></div>
<h2 style="font-size:14px;margin:18px 0 8px">האירועים הגרועים ביותר</h2>
<table><tr><th>זמן</th><th>הורדה</th><th>פינג</th><th>אובדן</th></tr>%(worst)s</table>
<div class="foot">הופק אוטומטית ע"י netmon · מדידה מה‑RPi בחיבור קווי · %(total)d דגימות סה"כ (%(fail)d כשלו).</div>
</div></body></html>""" % {
        "ink": C_INK, "muted": C_MUTED, "vcolor": vcolor,
        "start": html.escape(s["start"]), "end": html.escape(s["end"]),
        "isp": html.escape(s["isp"]), "ok": s["ok"],
        "rate": 100 * s["ok"] / s["total"] if s["total"] else 0,
        "emoji": emoji, "head": html.escape(head), "pv": pv,
        "c_dl": card("הורדה (חציון)", "%.0f" % s["dl_med"], dl_pct, dl_color),
        "c_ul": card("העלאה (חציון)", "%.0f" % s["ul_med"], "Mbps", C_INK),
        "c_ping": card("פינג idle", "%.0f" % s["ping_med"], "ms", C_INK),
        "c_bb": card("Bufferbloat", "%.0f" % s["bb_med"], "ms חציון", bb_color),
        "c_loss": card("אובדן חבילות", "%.1f%%" % s["loss_avg"], "ממוצע", loss_color),
        "c_rate": card("שפל הורדה", "%.0f" % s["dl_p10"], dl_worst_pct, C_MUTED),
        "chart_dl": svg_bars(dl_series, plan=plan, unit="Mbps", ref_label="חבילה", good_high=True, threshold=threshold),
        "chart_bb": svg_bars(bb_series, plan=None, unit="ms", good_high=False, threshold=80),
        "worst": worst_rows, "total": s["total"], "fail": s["fail"],
    }


def text_summary(s):
    emoji, head, _ = verdict(s)
    plan = s["plan"]
    lines = [
        "%s <b>דוח אינטרנט שבועי</b>" % emoji,
        head,
        "",
        "הורדה (חציון): <b>%.0f Mbps</b>%s" % (s["dl_med"], (" (%.0f%% מהחבילה)" % (100 * s["dl_med"] / plan)) if plan else ""),
        "העלאה: %.0f Mbps · פינג: %.0f ms" % (s["ul_med"], s["ping_med"]),
        "Bufferbloat: %.0f ms · אובדן: %.1f%%" % (s["bb_med"], s["loss_avg"]),
    ]
    if s["off_avg"] is not None and s["peak_avg"] is not None:
        lines.append("לילה %.0f → ערב %.0f Mbps" % (s["off_avg"], s["peak_avg"]))
    lines.append("%d מדידות · %.0f%% הצלחה" % (s["ok"], 100 * s["ok"] / s["total"] if s["total"] else 0))
    lines.append("\n📎 הדוח המלא עם גרפים מצורף כקובץ.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Build HTML report + optional Telegram send")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--plan", type=float, default=None, help="advertised plan Mbps")
    ap.add_argument("--html", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.html"))
    ap.add_argument("--telegram", action="store_true", help="send report + summary to Telegram")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram.conf"))
    ap.add_argument("--dry-run", action="store_true", help="with --telegram: build and preview, do not send")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print("CSV not found: %s" % args.csv); return 2

    s = summarize(load(args.csv), args.plan)
    with open(args.html, "w", encoding="utf-8") as f:
        f.write(build_html(s))
    print("Wrote HTML report: %s" % args.html)
    summary = text_summary(s)

    if args.telegram:
        import telegram_send as tg
        cfg = tg.load_config(args.config)
        token = cfg.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat = cfg.get("CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
        if args.dry_run:
            print("[dry-run] token=%s chat=%s" % ("SET" if token else "MISSING", chat))
            print("[dry-run] message:\n" + summary)
            return 0
        if not token or not chat:
            print("ERROR: missing BOT_TOKEN/CHAT_ID in %s (or env vars)" % args.config); return 2
        tg.send_message(token, chat, summary)
        tg.send_document(token, chat, args.html, caption="דוח אינטרנט שבועי מצורף")
        print("Sent to Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
