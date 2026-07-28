#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netmon_bot.py - Standalone Telegram bot: control and configure netmon by chat.

Zero dependencies (stdlib only), long-polls the Telegram Bot API. Runs as a
systemd service created by install.sh, so it comes back automatically after a
reboot or a power cut.

Commands (see /help): /speed /stats /report /status /pause /resume /config
/setplan /setinterval /setalert /setreport /setping /setlang

Only the chat id in netmon.conf may issue commands. If CHAT_ID is not set yet,
the bot answers any chat with that chat's id so you can fill it in - which is
exactly what `--detect-chat` automates during installation.

Usage:
    ./netmon_bot.py                 # run the bot (normally via systemd)
    ./netmon_bot.py --detect-chat   # print the chat id of whoever messages next
"""

import json
import os
import statistics as st
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import netmon_config as cfgmod
import report as report_mod
import telegram_send as tg
from i18n import t

API = "https://api.telegram.org/bot%s/%s"
POLL_TIMEOUT = 30          # seconds the Telegram server holds an empty getUpdates

_busy = threading.Lock()   # one measurement at a time


# --------------------------------------------------------------- API plumbing
def api(token, method, params=None, timeout=POLL_TIMEOUT + 15):
    url = API % (token, method)
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:200]
        log("HTTP %s from %s: %s" % (e.code, method, body))
        if e.code == 409:
            log("409 conflict: another process is polling this bot token.")
        return {"ok": False}
    except Exception as e:
        log("%s failed: %s" % (method, e))
        return {"ok": False}


def log(msg):
    print("[netmon-bot] %s" % msg, flush=True)


def reply(token, chat, text):
    try:
        tg.send_message(token, chat, text)
    except Exception as e:
        log("send failed: %s" % e)


# ------------------------------------------------------------------- helpers
def last_row():
    """Last CSV row as a dict, or None."""
    if not os.path.exists(cfgmod.CSV_PATH):
        return None
    import csv
    with open(cfgmod.CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else None


def all_rows():
    if not os.path.exists(cfgmod.CSV_PATH):
        return []
    import csv
    with open(cfgmod.CSV_PATH, newline="") as f:
        return list(csv.DictReader(f))


def service_state(name):
    try:
        p = subprocess.run(["systemctl", "is-active", name], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=10)
        return p.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def describe_last(row, lang):
    if not row:
        return t(lang, "no_data_yet")
    if row.get("status") == "ok":
        return "%s · %s↓/%s↑ Mbps" % (row.get("timestamp_iso", "?"),
                                      row.get("download_mbps"), row.get("upload_mbps"))
    return "%s · %s" % (row.get("timestamp_iso", "?"), (row.get("error") or "error")[:80])


def save(cfg, updates, lang, token, chat, reschedule=False):
    """Persist config changes, optionally rewriting the cron block, and confirm."""
    cfgmod.set_values(cfg, updates)
    shown = ", ".join("%s=%s" % (k, cfgmod.mask(k, v)) for k, v in sorted(updates.items()))
    reply(token, chat, t(lang, "bot_saved", shown))
    if reschedule:
        ok, msg = cfgmod.apply_schedule(cfg)
        reply(token, chat, t(lang, "bot_schedule_set", msg) if ok
              else t(lang, "bot_schedule_err", msg))


# ------------------------------------------------------------------ commands
def cmd_speed(cfg, token, chat, lang):
    if not _busy.acquire(blocking=False):
        reply(token, chat, t(lang, "bot_busy"))
        return
    try:
        reply(token, chat, t(lang, "bot_measuring"))
        p = subprocess.run([sys.executable, os.path.join(cfgmod.DIR, "netmon.py"), "--no-alert"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
        out = (p.stdout or "").strip()
        if p.returncode == 0 and "down=" in out:
            reply(token, chat, t(lang, "bot_measure_ok", out))
        else:
            why = ((p.stderr or "") + "\n" + out).strip()[:600] or "unknown error"
            reply(token, chat, t(lang, "bot_measure_fail", why))
    except subprocess.TimeoutExpired:
        reply(token, chat, t(lang, "bot_measure_fail", "timeout"))
    except Exception as e:
        reply(token, chat, t(lang, "bot_measure_fail", str(e)[:300]))
    finally:
        _busy.release()


def cmd_report(cfg, token, chat, lang):
    rows = all_rows()
    if not rows:
        reply(token, chat, t(lang, "bot_no_data"))
        return
    reply(token, chat, t(lang, "bot_building"))
    try:
        summary, path = report_mod.build(cfg, cfgmod.CSV_PATH, cfgmod.HTML_PATH, lang=lang)
        tg.send_message(token, chat, summary)
        tg.send_document(token, chat, path, caption=t(lang, "report_caption"))
    except Exception as e:
        reply(token, chat, t(lang, "bot_report_fail", str(e)[:400]))


def cmd_status(cfg, token, chat, lang):
    rows = all_rows()
    reply(token, chat, t(lang, "bot_status",
                         t(lang, "state_paused") if cfgmod.is_paused() else t(lang, "state_running"),
                         cfgmod.get_int(cfg, "INTERVAL_MINUTES"),
                         service_state("cron"),
                         describe_last(rows[-1] if rows else None, lang),
                         len(rows)))


def cmd_stats(cfg, token, chat, lang):
    rows = all_rows()
    ok = [r for r in rows if r.get("status") == "ok"]
    if not ok:
        reply(token, chat, t(lang, "bot_no_data"))
        return
    dl = report_mod.col(ok, "download_mbps")
    ul = report_mod.col(ok, "upload_mbps")
    ping = report_mod.col(ok, "ping_idle_ms")
    bb = report_mod.col(ok, "bufferbloat_ms")
    loss = report_mod.col(ok, "packet_loss_pct")
    plan = cfgmod.get_float(cfg, "PLAN_DOWN_MBPS")
    plan_note = ""
    if plan and dl:
        plan_note = " (" + t(lang, "of_plan", 100 * st.median(dl) / plan) + ")"

    s = report_mod.summarize(rows, plan,
                             peak=(cfgmod.get_int(cfg, "PEAK_START"), cfgmod.get_int(cfg, "PEAK_END")),
                             offpeak=(cfgmod.get_int(cfg, "OFFPEAK_START"), cfgmod.get_int(cfg, "OFFPEAK_END")))
    _, headline, _ = report_mod.verdict(s, lang)

    reply(token, chat, t(lang, "bot_stats", len(ok),
                         st.median(dl) if dl else 0, min(dl) if dl else 0, max(dl) if dl else 0, plan_note,
                         st.median(ul) if ul else 0,
                         st.median(ping) if ping else 0,
                         st.median(bb) if bb else 0,
                         st.mean(loss) if loss else 0,
                         len(rows) - len(ok), headline))


def cmd_config(cfg, token, chat, lang):
    lines = []
    for key in cfgmod.SCHEMA:
        value = cfgmod.mask(key, cfg.get(key, ""))
        lines.append("<code>%s</code> = %s" % (key, value if value != "" else "—"))
    reply(token, chat, t(lang, "bot_config", "\n".join(lines)))


def cmd_setplan(cfg, args, token, chat, lang):
    if not args:
        reply(token, chat, t(lang, "bot_usage", "/setplan &lt;download&gt; [upload]   (Mbps)"))
        return
    updates = {}
    ok, cleaned = cfgmod.validate("PLAN_DOWN_MBPS", args[0])
    if not ok:
        reply(token, chat, t(lang, "bot_bad_value", "PLAN_DOWN_MBPS", cleaned)); return
    updates["PLAN_DOWN_MBPS"] = cleaned
    if len(args) > 1:
        ok, cleaned = cfgmod.validate("PLAN_UP_MBPS", args[1])
        if not ok:
            reply(token, chat, t(lang, "bot_bad_value", "PLAN_UP_MBPS", cleaned)); return
        updates["PLAN_UP_MBPS"] = cleaned
    save(cfg, updates, lang, token, chat)


def cmd_setinterval(cfg, args, token, chat, lang):
    if not args:
        reply(token, chat, t(lang, "bot_usage", "/setinterval &lt;minutes&gt;   (5–1440)"))
        return
    ok, cleaned = cfgmod.validate("INTERVAL_MINUTES", args[0])
    if not ok:
        reply(token, chat, t(lang, "bot_bad_value", "INTERVAL_MINUTES", cleaned)); return
    save(cfg, {"INTERVAL_MINUTES": cleaned}, lang, token, chat, reschedule=True)


def cmd_setalert(cfg, args, token, chat, lang):
    if not args:
        reply(token, chat, t(lang, "bot_usage", "/setalert &lt;percent&gt;  |  /setalert off"))
        return
    if args[0].lower() in ("off", "0", "no", "false"):
        cfgmod.set_values(cfg, {"ALERTS_ENABLED": "0"})
        reply(token, chat, t(lang, "bot_alerts_off")); return
    ok, cleaned = cfgmod.validate("ALERT_THRESHOLD_PCT", args[0])
    if not ok:
        reply(token, chat, t(lang, "bot_bad_value", "ALERT_THRESHOLD_PCT", cleaned)); return
    save(cfg, {"ALERT_THRESHOLD_PCT": cleaned, "ALERTS_ENABLED": "1"}, lang, token, chat)


def cmd_setreport(cfg, args, token, chat, lang):
    if not args:
        reply(token, chat, t(lang, "bot_usage", "/setreport &lt;day 0-6&gt; &lt;hour 0-23&gt;  |  /setreport off"))
        return
    if args[0].lower() in ("off", "no", "false"):
        cfgmod.set_values(cfg, {"REPORT_ENABLED": "0"})
        reply(token, chat, t(lang, "bot_report_off"))
        ok, msg = cfgmod.apply_schedule(cfg)
        if not ok:
            reply(token, chat, t(lang, "bot_schedule_err", msg))
        return
    if len(args) < 2:
        reply(token, chat, t(lang, "bot_usage", "/setreport &lt;day 0-6&gt; &lt;hour 0-23&gt;"))
        return
    ok, dow = cfgmod.validate("REPORT_DOW", args[0])
    if not ok:
        reply(token, chat, t(lang, "bot_bad_value", "REPORT_DOW", dow)); return
    ok, hour = cfgmod.validate("REPORT_HOUR", args[1])
    if not ok:
        reply(token, chat, t(lang, "bot_bad_value", "REPORT_HOUR", hour)); return
    save(cfg, {"REPORT_DOW": dow, "REPORT_HOUR": hour, "REPORT_ENABLED": "1"},
         lang, token, chat, reschedule=True)


def cmd_setping(cfg, args, token, chat, lang):
    if not args:
        reply(token, chat, t(lang, "bot_usage", "/setping &lt;host or IP&gt;")); return
    ok, cleaned = cfgmod.validate("PING_HOST", args[0])
    if not ok:
        reply(token, chat, t(lang, "bot_bad_value", "PING_HOST", cleaned)); return
    save(cfg, {"PING_HOST": cleaned}, lang, token, chat)


def cmd_setlang(cfg, args, token, chat, lang):
    if not args:
        reply(token, chat, t(lang, "bot_usage", "/setlang en | he")); return
    ok, cleaned = cfgmod.validate("LANG", args[0])
    if not ok:
        reply(token, chat, t(lang, "bot_bad_value", "LANG", cleaned)); return
    cfgmod.set_values(cfg, {"LANG": cleaned})
    reply(token, chat, t(cleaned, "bot_saved", "LANG=" + cleaned))
    reply(token, chat, t(cleaned, "bot_help"))


def handle(text, token, chat):
    """Dispatch one message. Config is re-read every time so edits apply live."""
    cfg = cfgmod.load()
    lang = cfgmod.lang(cfg)
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]      # tolerate /speed@mybot in groups
    args = parts[1:]

    if cmd in ("/help", "/start"):
        reply(token, chat, t(lang, "bot_help"))
    elif cmd in ("/speed", "/netmon", "/test"):
        threading.Thread(target=cmd_speed, args=(cfg, token, chat, lang), daemon=True).start()
    elif cmd == "/report":
        threading.Thread(target=cmd_report, args=(cfg, token, chat, lang), daemon=True).start()
    elif cmd == "/status":
        cmd_status(cfg, token, chat, lang)
    elif cmd == "/stats":
        cmd_stats(cfg, token, chat, lang)
    elif cmd in ("/pause", "/monitorstop", "/stop"):
        open(cfgmod.PAUSE_FLAG, "w").close()
        reply(token, chat, t(lang, "bot_paused"))
    elif cmd in ("/resume", "/monitorstart"):
        if os.path.exists(cfgmod.PAUSE_FLAG):
            os.remove(cfgmod.PAUSE_FLAG)
        reply(token, chat, t(lang, "bot_resumed"))
    elif cmd == "/config":
        cmd_config(cfg, token, chat, lang)
    elif cmd == "/setplan":
        cmd_setplan(cfg, args, token, chat, lang)
    elif cmd == "/setinterval":
        cmd_setinterval(cfg, args, token, chat, lang)
    elif cmd == "/setalert":
        cmd_setalert(cfg, args, token, chat, lang)
    elif cmd == "/setreport":
        cmd_setreport(cfg, args, token, chat, lang)
    elif cmd == "/setping":
        cmd_setping(cfg, args, token, chat, lang)
    elif cmd == "/setlang":
        cmd_setlang(cfg, args, token, chat, lang)
    elif cmd.startswith("/"):
        reply(token, chat, t(lang, "bot_unknown"))


# ------------------------------------------------------------------ main loop
def detect_chat(token, seconds=120):
    """Print the chat id of the next message the bot receives. Used by install.sh."""
    deadline = time.time() + seconds
    offset = None
    while time.time() < deadline:
        params = {"timeout": 10}
        if offset is not None:
            params["offset"] = offset
        res = api(token, "getUpdates", params, timeout=30)
        for u in res.get("result", []) or []:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("channel_post") or {}
            chat = (msg.get("chat") or {}).get("id")
            if chat is not None:
                print(chat)
                return 0
    print("", end="")
    return 1


def run():
    cfg = cfgmod.load()
    token, allowed = cfgmod.creds(cfg)
    if not token:
        log("BOT_TOKEN is not set in %s - nothing to do." % cfgmod.CONF_PATH)
        return 2

    # Drop the backlog so a restart does not replay old commands.
    offset = None
    res = api(token, "getUpdates", {"timeout": 0}, timeout=20)
    for u in res.get("result", []) or []:
        offset = u["update_id"] + 1

    if allowed:
        reply(token, allowed, t(cfgmod.lang(cfg), "bot_online"))
    log("polling (authorized chat: %s)" % (allowed or "NOT SET - will reply with chat id"))

    while True:
        params = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            params["offset"] = offset
        res = api(token, "getUpdates", params)
        if not res.get("ok"):
            time.sleep(5)                  # backoff on network/API trouble
            continue
        for u in res.get("result", []) or []:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or {}
            text = msg.get("text")
            chat = str((msg.get("chat") or {}).get("id", ""))
            if not text or not chat:
                continue
            current = cfgmod.load().get("CHAT_ID", "").strip()
            if not current:
                # Not configured yet: help the owner discover their chat id.
                reply(token, chat, "netmon is not configured yet.\nYour chat id is: %s\n"
                                   "Put it in netmon.conf as CHAT_ID=%s and restart the bot."
                                   % (chat, chat))
                log("unconfigured; reported chat id %s" % chat)
                continue
            if chat != current:
                log("ignoring message from unauthorized chat %s" % chat)
                continue
            try:
                handle(text, token, chat)
            except Exception as e:         # a bad command must never kill the bot
                log("handler error: %s" % e)


def main():
    if "--detect-chat" in sys.argv:
        cfg = cfgmod.load()
        token = cfg.get("BOT_TOKEN", "").strip()
        if not token:
            print("", end="")
            return 2
        secs = 120
        for i, a in enumerate(sys.argv):
            if a == "--seconds" and i + 1 < len(sys.argv):
                secs = int(sys.argv[i + 1])
        return detect_chat(token, secs)
    try:
        return run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
