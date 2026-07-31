#!/usr/bin/env bash
#
# diagnose.sh - One-shot internet diagnosis: where does a speed problem sit?
#
# Collects the evidence needed to place a bottleneck:
#   * local NIC link speed + error counters (is the machine / cabling healthy?)
#   * speed across SEVERAL servers          (your line, or one slow server?)
#   * download vs upload asymmetry          (one-directional cap = shaping)
#   * path and private hops (mtr)           (is local distribution gear the limit?)
#   * latency / jitter / loss / DNS
# Then prints a verdict COMPUTED FROM THE NUMBERS and saves a shareable report.
#
# The multi-server test is the point. A speedtest against one auto-selected
# server measures that server as much as it measures your line, and the nearest
# server is not always a good one - a single slow server can look exactly like
# a capped connection until you test a second one.
#
# Run:   chmod +x diagnose.sh && ./diagnose.sh
# Link speed and error counters need root:  sudo ./diagnose.sh
#
set -uo pipefail

# ethtool lives in /usr/sbin, which is absent from a non-login shell's PATH.
# Without this the script reports "ethtool not available" on a box that has it.
PATH="$PATH:/usr/sbin:/sbin"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$DIR/diag_${STAMP}.txt"
TARGETS=(1.1.1.1 8.8.8.8)

# Mirror everything to screen AND to the report file.
exec > >(tee "$OUT") 2>&1

hr(){ printf '%s\n' "------------------------------------------------------------------------"; }
sec(){ echo; hr; echo " $1"; hr; }

echo "netmon diagnose  |  $(date -Is)"
echo "report file: $OUT"

# --------------------------------------------------------------- 1. system
sec "1. SYSTEM & INTERFACE"
[ -r /proc/device-tree/model ] && echo "Device : $(tr -d '\0' </proc/device-tree/model)"
echo "Kernel : $(uname -srm)"
echo "Cores  : $(nproc)   Load: $(cat /proc/loadavg)"
IFACE="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'dev \K\S+' | head -1)"
GW="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'via \K\S+' | head -1)"
LOCALIP="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' | head -1)"
echo "Iface  : ${IFACE:-unknown}   IP: ${LOCALIP:-?}   Gateway: ${GW:-?}"

if command -v ethtool >/dev/null 2>&1 && [ -n "${IFACE:-}" ]; then
  echo "-- ethtool $IFACE --"
  # Reading link settings needs CAP_NET_ADMIN; as a plain user ethtool answers
  # "Operation not permitted" and the Speed/Duplex lines silently go missing.
  [ "$(id -u)" -ne 0 ] && echo "  (not root: link speed & error counters need 'sudo ./diagnose.sh')"
  ethtool "$IFACE" 2>/dev/null | grep -Ei 'Speed|Duplex|Link detected' | sed 's/^/  /'
  SPEED="$(ethtool "$IFACE" 2>/dev/null | grep -i 'Speed:' | grep -oP '[0-9]+' | head -1)"
  echo "-- link error counters (should all be 0) --"
  ethtool -S "$IFACE" 2>/dev/null | grep -iE 'err|crc|drop|discard|fail|collision' \
    | grep -vE ': 0$' | sed 's/^/  /' || true
  ethtool -S "$IFACE" 2>/dev/null | grep -iE 'err|crc|drop|discard|fail|collision' \
    | grep -qE ': [1-9]' && echo "  !! non-zero link errors above -> suspect cable/port" \
    || echo "  (no non-zero error counters -> physical link looks clean)"
else
  echo "  ethtool not available (install: sudo apt install ethtool) or iface unknown"
  SPEED=""
fi
echo "-- ip -s link (rx/tx errors & drops) --"
ip -s link show "${IFACE:-}" 2>/dev/null | sed 's/^/  /' || true

# --------------------------------------------------------------- 2. path
sec "2. PATH TO THE INTERNET (private hops = gear between you and the ISP)"
PATHTOOL=""
command -v mtr >/dev/null 2>&1 && PATHTOOL=mtr
[ -z "$PATHTOOL" ] && command -v traceroute >/dev/null 2>&1 && PATHTOOL=traceroute
for t in "${TARGETS[@]}"; do
  echo "-- path to $t --"
  if [ "$PATHTOOL" = mtr ]; then
    mtr -r -c 20 -b "$t" 2>/dev/null | sed 's/^/  /'
  elif [ "$PATHTOOL" = traceroute ]; then
    traceroute -q2 -w2 "$t" 2>/dev/null | sed 's/^/  /'
  else
    echo "  (install mtr for best results: sudo apt install mtr-tiny)"
    break
  fi
done
# Count private (RFC1918) hops: distribution gear you traverse before the internet.
# More than one or two means a shared/managed network sits between you and the ISP.
if [ -n "$PATHTOOL" ]; then
  PRIV=$([ "$PATHTOOL" = mtr ] && mtr -r -c1 "${TARGETS[0]}" 2>/dev/null || traceroute -q1 -w1 "${TARGETS[0]}" 2>/dev/null)
  N=$(echo "$PRIV" | grep -oE '(10\.[0-9]+|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9.]*' | sort -u | wc -l)
  echo "  internal (private-IP) hops before reaching the internet: $N"
fi

# --------------------------------------------------------------- 3. latency
sec "3. LATENCY BASELINE"
for t in "${GW:-}" "${TARGETS[@]}"; do
  [ -z "$t" ] && continue
  R=$(ping -c 10 -i 0.2 -w 15 "$t" 2>/dev/null | tail -2)
  echo "-- ping $t --"; echo "$R" | sed 's/^/  /'
done

# --------------------------------------------------------------- 4. speed
sec "4. SPEED TESTS  (the core: download vs upload)"
SPEEDBIN=""
if command -v speedtest >/dev/null 2>&1 && speedtest --version 2>&1 | grep -qi ookla; then
  SPEEDBIN="ookla"
elif command -v speedtest-cli >/dev/null 2>&1; then
  SPEEDBIN="cli"
fi

# Parser written to a temp file so the piped JSON stays on stdin (a here-doc
# on `python3 -` would steal stdin from the pipe).
PARSER="$(mktemp /tmp/netmon_parse.XXXXXX.py)"
# Every parsed run appends one row here; section 6 reads it to reach a verdict
# from the measurements instead of guessing.
RESULTS="$(mktemp /tmp/netmon_results.XXXXXX)"
export RESULTS
trap 'rm -f "$PARSER" "$RESULTS"' EXIT
cat > "$PARSER" <<'PY'
import os, sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("  (test failed / no JSON)"); sys.exit(0)
dl = d["download"]["bandwidth"]*8/1e6
ul = d["upload"]["bandwidth"]*8/1e6
p  = d.get("ping",{})
dll = (d.get("download",{}).get("latency") or {}).get("iqm")
ull = (d.get("upload",{}).get("latency") or {}).get("iqm")
idle = p.get("latency")
bb = (max([x for x in (dll,ull) if x is not None]) - idle) if (idle is not None and (dll or ull)) else None
srv = d.get("server",{})
print("  server : %s (%s) id=%s" % (srv.get("name"), srv.get("location"), srv.get("id")))
print("  DOWN   : %6.1f Mbps" % dl)
print("  UP     : %6.1f Mbps" % ul)
print("  ratio  : UP/DOWN = %.2f" % (ul/dl if dl else 0))
print("  ping   : %.1f ms  jitter %.1f ms  loss %s%%" % (
      idle or 0, p.get("jitter") or 0, d.get("packetLoss","?")))
print("  bufferbloat: %s ms" % ("%.0f"%bb if bb is not None else "?"))
u = (d.get("result",{}) or {}).get("url")
if u: print("  result : %s" % u)

# Hand the numbers to the verdict. No interpretation here - a single server is
# never enough to conclude anything.
res = os.environ.get("RESULTS")
if res:
    with open(res, "a") as f:
        f.write("%.1f\t%.1f\t%s\t%s (%s)\n" % (
            dl, ul, srv.get("id"), srv.get("name"), srv.get("location")))
PY

run_ookla() {  # $1 = optional --server-id
  local extra="$1"
  speedtest --format=json --accept-license --accept-gdpr $extra 2>/dev/null | python3 "$PARSER"
}

if [ "$SPEEDBIN" = ookla ]; then
  echo "-- default (auto-selected) server --"
  run_ookla ""
  # The comparison that makes the whole report meaningful: is a low result your
  # line, or that one server? Skip whichever server the default run already used.
  echo "-- up to 4 alternate servers (is a low result your line, or one server?) --"
  DONE_ID="$(cut -f3 "$RESULTS" 2>/dev/null | tr '\n' ' ')"
  export DONE_ID
  IDS=$(speedtest -L --format=json 2>/dev/null | python3 -c '
import sys, json, os
done = set(os.environ.get("DONE_ID", "").split())
ids = [str(s["id"]) for s in json.load(sys.stdin).get("servers", [])]
print(" ".join(i for i in ids if i not in done))
' 2>/dev/null)
  N=0
  for id in $IDS; do
    [ "$N" -ge 4 ] && break
    N=$((N + 1))
    echo "  [server-id $id]"
    run_ookla "--server-id=$id"
  done
elif [ "$SPEEDBIN" = cli ]; then
  echo "  Ookla speedtest not found; using speedtest-cli (no bufferbloat)."
  speedtest-cli --secure 2>/dev/null | grep -Ei 'Download|Upload|Ping|Hosted' | sed 's/^/  /'
else
  echo "  No speedtest tool. Run ./install.sh first."
fi

# --------------------------------------------------------------- 5. DNS
sec "5. DNS RESOLUTION"
for host in google.com cloudflare.com github.com; do
  if command -v dig >/dev/null 2>&1; then
    t=$(dig +noall +stats "$host" 2>/dev/null | grep -oP 'Query time: \K[0-9]+')
    echo "  $host : ${t:-?} ms"
  else
    /usr/bin/time -f "  $host : %e s" getent hosts "$host" >/dev/null 2>>/dev/stdout || echo "  $host : (install dnsutils for timing)"
  fi
done

# --------------------------------------------------------------- 6. verdict
sec "6. VERDICT"

# Everything below is derived from the rows collected in section 4, plus the
# link speed from section 1 and the plan from netmon.conf when it exists.
# Nothing is asserted that the measurements do not support.
LINK_SPEED="${SPEED:-}" ; export LINK_SPEED
PLAN_DOWN="$(sed -n 's/^PLAN_DOWN_MBPS=//p' "$DIR/netmon.conf" 2>/dev/null | head -1)"
PLAN_UP="$(sed -n 's/^PLAN_UP_MBPS=//p' "$DIR/netmon.conf" 2>/dev/null | head -1)"
export PLAN_DOWN PLAN_UP

python3 - "$RESULTS" <<'PY'
import os, sys, textwrap

def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

rows = []
try:
    for line in open(sys.argv[1]):
        f = line.rstrip("\n").split("\t")
        if len(f) >= 4:
            rows.append((float(f[0]), float(f[1]), f[2], f[3]))
except OSError:
    pass

if not rows:
    print("No speed results to reason about - install a speedtest tool and re-run.")
    raise SystemExit

link = num(os.environ.get("LINK_SPEED"))
plan_d, plan_u = num(os.environ.get("PLAN_DOWN")), num(os.environ.get("PLAN_UP"))

auto = rows[0]                        # the server speedtest picked on its own
best_d = max(rows, key=lambda r: r[0])
best_u = max(rows, key=lambda r: r[1])
worst_d = min(rows, key=lambda r: r[0])

print(textwrap.fill(
    "Tested %d server(s). Best download %.0f Mbps (%s), worst %.0f Mbps (%s). "
    "Best upload %.0f Mbps (%s)."
    % (len(rows), best_d[0], best_d[3], worst_d[0], worst_d[3],
       best_u[1], best_u[3]), width=76))
print()

findings = []
# A link running at its negotiated ceiling makes every "shortfall" downstream of
# it meaningless - the plan and the ISP cannot be judged through a saturated NIC.
link_capped = bool(link and best_d[0] > link * 0.9)

# 1. Server spread. This dominates: a line cannot be capped below a rate it
#    demonstrably reached, so one fast server disproves a shaping theory.
if len(rows) == 1:
    findings.append(
        "Only ONE server was tested, which is not enough to conclude anything. A "
        "slow result here could be that server rather than your line. Re-run when "
        "more servers are reachable, or test one by hand:\n"
        "speedtest -L   then   speedtest --server-id=<id>")
elif auto[0] and best_d[0] >= auto[0] * 1.25:
    findings.append(
        "The auto-selected server (%s, %.0f Mbps) is NOT representative of your "
        "line: another server reached %.0f Mbps, %.0f%% more. A rate limit applies "
        "to the link regardless of destination, so a result this much higher rules "
        "OUT a cap at the lower figure. The low number measures that server or the "
        "path to it - not your connection. Judge your line by the BEST server, and "
        "be careful reporting the low one as a fault."
        % (auto[3], auto[0], best_d[0], (best_d[0] / auto[0] - 1) * 100))
elif worst_d[0] and best_d[0] >= worst_d[0] * 1.25:
    findings.append(
        "Download varies %.0f%% between servers (%.0f - %.0f Mbps). Part of the "
        "shortfall is server/peering, not your link."
        % ((best_d[0] / worst_d[0] - 1) * 100, worst_d[0], best_d[0]))
else:
    findings.append(
        "Download is consistent across the servers tested (%.0f - %.0f Mbps), so "
        "this reflects your line rather than one slow server."
        % (worst_d[0], best_d[0]))

# 2. Physical link. A NIC negotiated below gigabit caps everything downstream.
if link_capped:
    findings.append(
        "Your best result (%.0f Mbps) is at the ceiling of a %.0f Mb/s link. The "
        "NIC is the limit - the line may well be faster than anything measured here."
        % (best_d[0], link))
elif link and link < 1000:
    findings.append(
        "The interface negotiated %.0f Mb/s, not gigabit. Everything is capped by "
        "that. Suspect the cable or the switch port before blaming the ISP." % link)

# 3. Plan comparison, only once the best server is known - and never as a
#    complaint against the ISP while the local NIC is the thing saturating.
if plan_d:
    share = best_d[0] / plan_d * 100
    if link_capped:
        findings.append(
            "Best download is %.0f%% of your %.0f Mbps plan, but the %.0f Mb/s link "
            "is the ceiling, so that figure measures this machine, not your ISP. %s"
            % (share, plan_d, link,
               "Measuring the rest of the plan needs a faster interface."
               if link >= 1000 else
               "Fix the link (cable, port, adapter) before judging the plan."))
    elif share < 70:
        findings.append(
            "Best download is %.0f%% of your %.0f Mbps plan - a real shortfall on "
            "every server tested. This is worth raising with your provider; attach "
            "this report." % (share, plan_d))
    else:
        findings.append("Best download is %.0f%% of your %.0f Mbps plan."
                        % (share, plan_d))
if plan_u and not link_capped:
    findings.append("Best upload is %.0f%% of your %.0f Mbps plan."
                    % (best_u[1] / plan_u * 100, plan_u))

# 4. Asymmetry, judged on the best server only. On the wrong server this test
#    produces a confident and completely wrong "download is shaped" conclusion.
if best_d[0] and best_u[1] / best_d[0] >= 1.25 and not link_capped:
    findings.append(
        "Upload exceeds download by %.2fx even on your best server. If that holds "
        "across servers it points to an asymmetric rate profile upstream rather "
        "than local hardware - a faulty cable degrades BOTH directions, since "
        "1000BASE-T uses every pair at once."
        % (best_u[1] / best_d[0]))

# Wrap at print time. Findings are written as flowing text so that an injected
# server name cannot overrun the line; an explicit \n marks a line that must
# stay verbatim (command examples).
for f in findings:
    for i, para in enumerate(f.split("\n")):
        print(textwrap.fill(para, width=76,
                            initial_indent="* " if i == 0 else "    ",
                            subsequent_indent="  "))
PY

echo
echo "Also check by hand:"
echo "* Non-zero error counters or a link below 1000Mb/s in section 1 -> local"
echo "  cable or port fault. Swap the cable, try another port."
echo "* A private-IP hop in section 2 with high latency or loss -> the limit is"
echo "  distribution gear between you and your ISP, not the ISP itself."
echo "* High bufferbloat (>100ms) with otherwise fine speeds -> the line is fast"
echo "  but latency collapses under load; ask about SQM/QoS."
echo
echo "Full report saved to: $OUT"
echo "Pair it with a week of netmon_log.csv for a view over time."
