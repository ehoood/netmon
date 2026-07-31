"""Offline tests for calibration: no network, no real config touched."""
import os, sys, time, tempfile
sys.path.insert(0, "/home/ehoood/netmon")
os.environ["NETMON_SKIP_CRON"] = "1"

import netmon_config as cfgmod
import netmon

TMP = tempfile.mkdtemp()
cfgmod.STATE_PATH = os.path.join(TMP, ".state")
CONF = os.path.join(TMP, "netmon.conf")

# id -> (label, download, upload, ping)
WORLD = {}
FAILING = set()
calls = []

netmon.list_servers = lambda binary, limit: [
    (sid, v[0], "%s.example:8080" % sid) for sid, v in list(WORLD.items())[:limit]]
netmon.tcp_latency = lambda host_port, **kw: WORLD[host_port.split(".")[0]][3]

ERROR_TEXT = "server unreachable"
def fake_measure(binary, server_id=None):
    calls.append(server_id)
    if server_id in FAILING:
        raise RuntimeError(ERROR_TEXT)
    label, d, u, p = WORLD[server_id]
    return {"download_mbps": d, "upload_mbps": u, "ping_idle_ms": p}
netmon.measure_ookla = fake_measure

def world(*rows):
    WORLD.clear()
    for sid, label, d, u, p in rows:
        WORLD[sid] = (label, d, u, p)

def fresh_cfg(**over):
    cfg = {k: v for k, (v, _) in cfgmod.SCHEMA.items()}
    cfg.update(over)
    cfg["_path"] = CONF
    return cfg

def state_age(days):
    cfgmod.write_state({"LAST_CALIBRATION_EPOCH": int(time.time() - days * 86400)})

FAILS = []
def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print("%-52s %s" % (name, "PASS" if ok else "FAIL  got=%r want=%r" % (got, want)))

# The real field data that drove this design.
FIELD = (("partner",   "Partner (Tel Aviv)",  477.0, 900.0,  5.8),
         ("pelephone", "Pelephone (Ashkelon)", 792.0, 402.0,  6.8),
         ("wavelink",  "WaveLink (Khan Yunis)", 930.0, 394.0, 62.0),
         ("umniah",    "Umniah (Aqaba)",       913.0, 563.0, 112.0),
         ("pro0101",   "Pro0101 (Jerusalem)",  839.0, 880.0,  7.8))

# 1. Chooses the server that is good in BOTH directions, not the best at either.
world(*FIELD)
_, name, _ = netmon.calibrate("speedtest", 5, verbose=False)
check("picks the all-round server, not the fastest down", name, "Pro0101 (Jerusalem)")

# 2. A great download with a poor upload must not win.
world(("a", "FastDownOnly", 1000.0, 100.0, 5.0),
      ("b", "Balanced",      850.0, 850.0, 9.0))
_, name, _ = netmon.calibrate("speedtest", 2, verbose=False)
check("  a lopsided server loses to a balanced one", name, "Balanced")

# 3. ... and so must a great upload with a poor download.
world(("a", "FastUpOnly", 100.0, 1000.0, 5.0),
      ("b", "Balanced",   850.0,  850.0, 9.0))
_, name, _ = netmon.calibrate("speedtest", 2, verbose=False)
check("  and the mirror image of that", name, "Balanced")

# 4. Equal quality -> the closer one wins, so bufferbloat stays meaningful.
world(("far",  "Far",   900.0, 900.0, 120.0),
      ("near", "Near",  900.0, 900.0,   6.0))
_, name, _ = netmon.calibrate("speedtest", 2, verbose=False)
check("equal servers: the closer one wins", name, "Near")

# 5. Latency does not override a genuinely better instrument.
world(("near", "NearButWeak", 300.0, 300.0,  5.0),
      ("far",  "FarButTrue",  900.0, 900.0, 90.0))
_, name, _ = netmon.calibrate("speedtest", 2, verbose=False)
check("  but a much weaker close server still loses", name, "FarButTrue")

# 6. Pre-screen: only `count` servers are speed-tested, lowest latency first.
world(("a", "A", 500.0, 500.0, 90.0), ("b", "B", 500.0, 500.0, 5.0),
      ("c", "C", 500.0, 500.0, 40.0), ("d", "D", 500.0, 500.0, 70.0))
del calls[:]
netmon.calibrate("speedtest", 2, verbose=False)
check("speed-tests only the shortlist", sorted(calls), ["b", "c"])
check("  (cheap latency screen before spending GB)", len(calls), 2)

# 7. One failing server does not abort the sweep. With the all-rounder gone,
#    Umniah wins on worst-dimension score (98% down / 63% up) over Partner
#    (53% down / 100% up) - correct, even though it is far away.
world(*FIELD)
FAILING.clear(); FAILING.add("pro0101")
_, name, _ = netmon.calibrate("speedtest", 5, verbose=False)
check("a failing server is skipped, not fatal", name, "Umniah (Aqaba)")
FAILING.clear()

# 7b. Choosing a distant server must never happen silently.
world(*FIELD)
FAILING.add("pro0101")
_, _, res = netmon.calibrate("speedtest", 5, verbose=False)
far = [r for r in res if r[1] == "Umniah (Aqaba)"][0]
check("  a far choice is flagged, not silent", bool(netmon.far_warning(far)), True)
near = [r for r in res if r[1] == "Partner (Tel Aviv)"][0]
check("  a near choice is not flagged", netmon.far_warning(near), [])
FAILING.clear()

# 8. Everything fails -> no crash, no pick.
world(("a", "A", 1.0, 1.0, 1.0))
FAILING.add("a")
bid, name, res = netmon.calibrate("speedtest", 1, verbose=False)
check("total failure returns nothing rather than raising", (bid, name), (None, None))
FAILING.clear()

# ---------------------------------------------------------------- scheduling
world(*FIELD)

# 9. First run calibrates and persists.
if os.path.exists(cfgmod.STATE_PATH): os.remove(cfgmod.STATE_PATH)
cfg = fresh_cfg()
note = netmon.maybe_calibrate(cfg, "speedtest")
check("first run calibrates", bool(note), True)
check("  persists the choice", cfg["SERVER_ID"], "pro0101")
check("  records the timestamp", "LAST_CALIBRATION_EPOCH" in cfgmod.read_state(), True)

# 10. Fresh calibration is skipped entirely.
del calls[:]
state_age(1)
cfg = fresh_cfg(SERVER_ID="pro0101", CALIBRATE_DAYS="7")
check("fresh calibration is skipped", netmon.maybe_calibrate(cfg, "speedtest"), "")
check("  no bandwidth spent", calls, [])

# 11. Stale calibration re-runs.
state_age(9)
cfg = fresh_cfg(SERVER_ID="partner", CALIBRATE_DAYS="7")
check("stale calibration re-runs", bool(netmon.maybe_calibrate(cfg, "speedtest")), True)
check("  moves off the bad server", cfg["SERVER_ID"], "pro0101")

# 12. Disabled.
del calls[:]
cfg = fresh_cfg(CALIBRATE_DAYS="0")
check("CALIBRATE_DAYS=0 disables", netmon.maybe_calibrate(cfg, "speedtest"), "")
check("  no bandwidth spent", calls, [])

# 13. All servers fail -> keep the old setting, still record the attempt.
FAILING.update(WORLD)
state_age(9)
cfg = fresh_cfg(SERVER_ID="keepme", CALIBRATE_DAYS="7")
note = netmon.maybe_calibrate(cfg, "speedtest")
check("total failure keeps the old server", cfg["SERVER_ID"], "keepme")
check("  says so instead of raising", "no server produced a usable result" in note, True)
check("  records the FAILURE, not a success",
      abs(float(cfgmod.read_state()["LAST_CALIBRATION_FAIL_EPOCH"]) - time.time()) < 5, True)

# 14. A rate limit is transient: it must not freeze the server choice for the
#     whole CALIBRATE_DAYS window the way a real "no usable server" would.
import netmon as _n
world(*FIELD)
FAILING.update(WORLD)
ERROR_TEXT = "[error] Limit reached:\n\nSpeedtest CLI. Too many requests"
cfgmod.write_state({"LAST_CALIBRATION_EPOCH": 0, "LAST_CALIBRATION_FAIL_EPOCH": 0})
cfg = fresh_cfg(SERVER_ID="keepme", CALIBRATE_DAYS="7")
note = _n.maybe_calibrate(cfg, "speedtest")
check("rate limiting is named, not guessed", "rate-limiting" in note, True)
check("  retry is hours away, not days", "retrying in 6h" in note, True)
check("  success timestamp untouched",
      float(cfgmod.read_state().get("LAST_CALIBRATION_EPOCH", 0)), 0.0)

# 15. ... and the backoff actually suppresses the next attempt.
del calls[:]
check("backoff suppresses the immediate retry", _n.maybe_calibrate(cfg, "speedtest"), "")
check("  no bandwidth spent during backoff", calls, [])

# 16. Once the backoff expires it tries again.
cfgmod.write_state({"LAST_CALIBRATION_FAIL_EPOCH": int(time.time() - 7 * 3600)})
FAILING.clear()
note = _n.maybe_calibrate(cfg, "speedtest")
check("retries after the backoff expires", cfg["SERVER_ID"], "pro0101")
check("  and clears the failure marker",
      float(cfgmod.read_state().get("LAST_CALIBRATION_FAIL_EPOCH", -1)), 0.0)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
