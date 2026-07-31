"""A provider's rate limit must not be reported to the user as an outage."""
import os, sys, tempfile
sys.path.insert(0, "/home/ehoood/netmon")
os.environ["NETMON_SKIP_CRON"] = "1"
import netmon_config as cfgmod, netmon

TMP = tempfile.mkdtemp()
cfgmod.STATE_PATH = os.path.join(TMP, ".state")

sent = []
class FakeTG:
    @staticmethod
    def send_message(token, chat, text): sent.append(text)
sys.modules["telegram_send"] = FakeTG

cfg = {k: v for k, (v, _) in cfgmod.SCHEMA.items()}
cfg.update({"ALERTS_ENABLED": "1", "BOT_TOKEN": "1:x", "CHAT_ID": "9",
            "PLAN_DOWN_MBPS": "900", "ALERT_THRESHOLD_PCT": "50",
            "ALERT_COOLDOWN_MIN": "0", "_path": os.path.join(TMP, "c")})

FAILS = []
def check(name, got, want):
    if got != want: FAILS.append(name)
    print("%-50s %s" % (name, "PASS" if got == want else "FAIL got=%r" % (got,)))

del sent[:]
netmon.maybe_alert(cfg, {"status": "error",
    "error": "[error] Limit reached:\n\nSpeedtest CLI. Too many requests received."})
check("rate limit does not alert", len(sent), 0)

del sent[:]
netmon.maybe_alert(cfg, {"status": "error", "error": "Network is unreachable"})
check("a real failure still alerts", len(sent), 1)

del sent[:]
netmon.maybe_alert(cfg, {"status": "ok", "download_mbps": "200",
                         "ping_idle_ms": "5", "bufferbloat_ms": "1",
                         "packet_loss_pct": "0"})
check("a slow measurement still alerts", len(sent), 1)

del sent[:]
netmon.maybe_alert(cfg, {"status": "ok", "download_mbps": "800",
                         "ping_idle_ms": "5", "bufferbloat_ms": "1",
                         "packet_loss_pct": "0"})
check("a healthy measurement stays quiet", len(sent), 0)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
