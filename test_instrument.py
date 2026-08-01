"""A change of measurement server must not read as a change in the line."""
import sys, csv
sys.path.insert(0, "/home/ehoood/netmon")
import report

FAILS = []
def check(name, got, want):
    if got != want: FAILS.append(name)
    print("%-54s %s" % (name, "PASS" if got == want else "FAIL got=%r want=%r" % (got, want)))

def row(server, dl, h=12, status="ok"):
    return {"status": status, "server_name": server, "download_mbps": str(dl),
            "upload_mbps": "500", "hour_of_day": str(h), "timestamp_iso": "2026-07-31T12:00:00"}

# Old slow instrument, then a new one after calibration.
rows = [row("Partner", 585) for _ in range(100)] + [row("Pro0101", 847) for _ in range(15)]

s = report.summarize(rows, plan=900)
check("median reflects the current instrument only", round(s["dl_med"]), 847)
check("  names the instrument", s["instrument"], "Pro0101")
check("  reports what it set aside", s["excluded"], 100)
check("  counts only its own samples", s["ok"], 15)

s_all = report.summarize(rows, plan=900, per_instrument=False)
check("opting out still mixes everything", round(s_all["dl_med"]), 585)
check("  and sets nothing aside", s_all["excluded"], 0)

# A single instrument throughout must be untouched.
one = [row("Partner", 600) for _ in range(50)]
s1 = report.summarize(one, plan=900)
check("one instrument: nothing excluded", s1["excluded"], 0)
check("  median unchanged", round(s1["dl_med"]), 600)

# Failed rows carry no server name; they must not split the run.
mixed = [row("Pro0101", 800), row(None, 0, status="error"), row("Pro0101", 850)]
s2 = report.summarize(mixed, plan=900)
check("an error row does not break the instrument run", s2["excluded"], 0)
check("  failures still counted", s2["fail"], 1)

# No usable rows at all.
s3 = report.summarize([], plan=900)
check("empty log does not crash", s3["instrument"], "—")

print("\n%s" % ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
