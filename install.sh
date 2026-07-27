#!/usr/bin/env bash
#
# install.sh - Set up the Ookla speedtest CLI and an hourly cron job on the Pi.
#
# Run it on the Raspberry Pi (Raspberry Pi OS / Debian):
#     chmod +x install.sh && ./install.sh
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/netmon_log.csv"
PING_HOST="${PING_HOST:-1.1.1.1}"
PLAN="${PLAN:-200}"          # your advertised plan in Mbps (override: PLAN=500 ./install.sh)

echo ">> netmon installer"
echo "   dir: $DIR"

# --- 0. NIC speed sanity check (the Pi must not be the bottleneck) ----------
IFACE="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'dev \K\S+' || echo eth0)"
echo ">> primary interface: $IFACE"
if command -v ethtool >/dev/null 2>&1; then
  SPEED="$(ethtool "$IFACE" 2>/dev/null | grep -i 'Speed:' | awk '{print $2}' || true)"
  echo "   link speed: ${SPEED:-unknown}"
  case "$SPEED" in
    10Mb*|100Mb*) echo "   !! WARNING: ${SPEED} NIC caps speedtest results. If your line is faster,"
                  echo "      the Pi (not the internet) is the bottleneck. Use a Gigabit Pi (4/5)"
                  echo "      or a USB3 Gigabit adapter for lines above ~95 Mbps." ;;
  esac
else
  echo "   (install 'ethtool' to see link speed:  sudo apt install ethtool)"
fi

# --- 1. Ookla speedtest CLI --------------------------------------------------
if command -v speedtest >/dev/null 2>&1 && speedtest --version 2>&1 | grep -qi ookla; then
  echo ">> Ookla speedtest already installed."
else
  echo ">> Installing Ookla speedtest CLI ..."
  if command -v apt-get >/dev/null 2>&1; then
    curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | sudo bash
    sudo apt-get install -y speedtest || {
      echo "   apt install failed; falling back to python speedtest-cli"
      sudo apt-get install -y speedtest-cli || pip3 install --user speedtest-cli
    }
  else
    echo "   apt not found; installing python speedtest-cli via pip"
    pip3 install --user speedtest-cli
  fi
fi

# Accept Ookla licence once, non-interactively, so cron runs never block.
if command -v speedtest >/dev/null 2>&1 && speedtest --version 2>&1 | grep -qi ookla; then
  speedtest --accept-license --accept-gdpr --format=json >/dev/null 2>&1 || true
fi

chmod +x "$DIR/netmon.py" "$DIR/analyze.py" "$DIR/report.py" "$DIR/telegram_send.py"

# --- 2. Telegram config ------------------------------------------------------
if [ ! -f "$DIR/telegram.conf" ] && [ -f "$DIR/telegram.conf.example" ]; then
  cp "$DIR/telegram.conf.example" "$DIR/telegram.conf"
  chmod 600 "$DIR/telegram.conf"
  echo ">> Created telegram.conf - EDIT IT and put your BOT_TOKEN + CHAT_ID there"
  echo "   to enable the weekly Telegram report."
fi
TG_READY=no
if grep -q '^BOT_TOKEN=[0-9]' "$DIR/telegram.conf" 2>/dev/null; then TG_READY=yes; fi

# --- 3. Cron: hourly measurement + weekly Telegram report --------------------
HOURLY="0 * * * * $DIR/netmon.py --csv $LOG --ping-host $PING_HOST >> $DIR/netmon.cron.log 2>&1"
WEEKLY="0 8 * * 0 $DIR/report.py --csv $LOG --plan $PLAN --html $DIR/report.html --telegram >> $DIR/netmon.cron.log 2>&1"

add_cron() {  # $1 = match string, $2 = full line, $3 = label
  if crontab -l 2>/dev/null | grep -Fq "$1"; then
    echo ">> cron ($3) already present, leaving as-is."
  else
    ( crontab -l 2>/dev/null; echo "$2" ) | crontab -
    echo ">> Added cron ($3):"; echo "   $2"
  fi
}
add_cron "$DIR/netmon.py" "$HOURLY" "hourly measurement"
add_cron "$DIR/report.py" "$WEEKLY" "weekly Telegram report (Sun 08:00, plan=$PLAN Mbps)"
if [ "$TG_READY" != yes ]; then
  echo "   NOTE: weekly report will fail until you fill in $DIR/telegram.conf"
fi

echo
echo ">> Done. Running one measurement now as a test ..."
"$DIR/netmon.py" --csv "$LOG" --ping-host "$PING_HOST" || true
echo
echo ">> Manual commands:"
echo "     Text report:      $DIR/analyze.py --csv $LOG --plan $PLAN"
echo "     HTML report:      $DIR/report.py  --csv $LOG --plan $PLAN --html $DIR/report.html"
echo "     Send to Telegram: $DIR/report.py  --csv $LOG --plan $PLAN --telegram"
