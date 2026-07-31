#!/usr/bin/env bash
#
# diagnose.sh - One-shot internet diagnosis for a shared/kibbutz fiber line.
#
# Collects everything needed to decide WHERE a speed problem sits:
#   * local NIC link speed + errors  (is the Pi / cabling healthy?)
#   * download vs upload asymmetry   (a download-only cap = upstream shaping)
#   * path / internal hops (mtr)      (is the exchange the limiting hop?)
#   * multi-server speed tests        (is it one server or the whole link?)
#   * latency / jitter / loss / CPU
# Then prints a plain-language verdict and saves a shareable report file.
#
# Run on the Pi:   chmod +x diagnose.sh && ./diagnose.sh
# (some checks are better with sudo:  sudo ./diagnose.sh )
#
set -uo pipefail

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
sec "2. PATH TO THE INTERNET (internal hops = kibbutz gear)"
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
# Count private (RFC1918) hops = internal kibbutz distribution before the internet
if [ -n "$PATHTOOL" ]; then
  PRIV=$([ "$PATHTOOL" = mtr ] && mtr -r -c1 "${TARGETS[0]}" 2>/dev/null || traceroute -q1 -w1 "${TARGETS[0]}" 2>/dev/null)
  N=$(echo "$PRIV" | grep -oE '(10\.[0-9]+|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9.]*' | sort -u | wc -l)
  echo "  internal (private-IP) hops before reaching the internet: $N"
fi

# --------------------------------------------------------------- 3. latency
sec "3. LATENCY BASELINE"
for t in "${GW:-} ${TARGETS[@]}"; do
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
trap 'rm -f "$PARSER"' EXIT
cat > "$PARSER" <<'PY'
import sys, json
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
print("  ratio  : UP/DOWN = %.2f  %s" % (ul/dl if dl else 0,
      "<-- upload much higher: download is capped/shaped UPSTREAM" if dl and ul/dl>=1.25 else ""))
print("  ping   : %.1f ms  jitter %.1f ms  loss %s%%" % (
      idle or 0, p.get("jitter") or 0, d.get("packetLoss","?")))
print("  bufferbloat: %s ms" % ("%.0f"%bb if bb is not None else "?"))
u = (d.get("result",{}) or {}).get("url")
if u: print("  result : %s" % u)
PY

run_ookla() {  # $1 = optional --server-id
  local extra="$1"
  speedtest --format=json --accept-license --accept-gdpr $extra 2>/dev/null | python3 "$PARSER"
}

if [ "$SPEEDBIN" = ookla ]; then
  echo "-- default (auto-selected) server --"
  run_ookla ""
  echo "-- trying up to 3 alternate servers (is 600 consistent, or one bad server?) --"
  IDS=$(speedtest -L --format=json 2>/dev/null | python3 -c 'import sys,json;print(" ".join(str(s["id"]) for s in json.load(sys.stdin).get("servers",[])[:3]))' 2>/dev/null)
  for id in $IDS; do
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
echo "Read together with the numbers above:"
echo
echo "* If UPLOAD reaches your full plan (~900) but DOWNLOAD is stuck (~600):"
echo "    -> Your Pi, cables, Deco and router are all PROVEN healthy (they push"
echo "       900 one way). A one-directional download cap is NOT hardware."
echo "       It is DOWNLOAD SHAPING / an asymmetric rate profile UPSTREAM"
echo "       (your ONT/modem profile at the exchange). -> take it to whoever"
echo "       runs the fiber/exchange (the מרכזייה)."
echo "* If link speed is not 1000Mb/s Full, or error counters are non-zero:"
echo "    -> a LOCAL cable/port fault. Replace the cable, try another port."
echo "* If a private-IP hop in section 2 shows high latency/loss:"
echo "    -> the limiting point is internal kibbutz distribution gear."
echo "* If DOWNLOAD varies a lot between servers:"
echo "    -> partly a server/peering issue, not purely your link."
echo
echo "Full report saved to: $OUT"
echo "Send that file (and a week of netmon_log.csv) for a deeper analysis."
