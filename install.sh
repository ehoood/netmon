#!/usr/bin/env bash
#
# install.sh - Set up netmon on any Linux machine.
#
#   git clone https://github.com/ehoood/netmon.git && cd netmon
#   ./install.sh
#
# It asks you a handful of questions (with hints), writes netmon.conf, installs
# the speedtest CLI, schedules the measurements in cron and - if systemd is
# available - installs the Telegram bot as a service that survives reboots.
#
# Non-interactive (CI, re-provisioning): set any of these and add --yes
#   LANG_CODE=he PLAN_DOWN=500 PLAN_UP=800 INTERVAL=60 PING_HOST=1.1.1.1 \
#   BOT_TOKEN=123:AA... CHAT_ID=12345 ./install.sh --yes
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$DIR/netmon.conf"
PY="$(command -v python3 || true)"
ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

c_b()  { printf '\033[1m%s\033[0m\n' "$*"; }
c_ok() { printf '\033[32m%s\033[0m\n' "$*"; }
c_wn() { printf '\033[33m%s\033[0m\n' "$*"; }
c_er() { printf '\033[31m%s\033[0m\n' "$*"; }

interactive() { [ "$ASSUME_YES" = 0 ] && [ -t 0 ]; }

# ask VAR_NAME "question" "default"  -> echoes the answer
ask() {
  local q="$1" def="${2:-}" ans=""
  if interactive; then
    if [ -n "$def" ]; then read -r -p "$q [$def]: " ans; else read -r -p "$q: " ans; fi
  fi
  echo "${ans:-$def}"
}

echo
c_b "netmon installer"
echo "  directory: $DIR"

# --- 0. Prerequisites --------------------------------------------------------
if [ -z "$PY" ]; then
  c_er "python3 is required but was not found. Install it and re-run:"
  echo "     Debian/Ubuntu/Raspberry Pi OS:  sudo apt install -y python3"
  echo "     Fedora/RHEL:                    sudo dnf install -y python3"
  exit 1
fi
if ! command -v crontab >/dev/null 2>&1; then
  c_wn "  'crontab' not found - automatic measurements need it."
  echo "     Debian/Ubuntu:  sudo apt install -y cron && sudo systemctl enable --now cron"
  echo "     Fedora/RHEL:    sudo dnf install -y cronie && sudo systemctl enable --now crond"
fi
chmod +x "$DIR"/*.py "$DIR"/install.sh 2>/dev/null || true

# --- 1. Link-speed sanity check (this machine must not be the bottleneck) ----
IFACE="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'dev \K\S+' || echo eth0)"
echo "  primary interface: $IFACE"
if command -v ethtool >/dev/null 2>&1; then
  SPEED="$(ethtool "$IFACE" 2>/dev/null | grep -i 'Speed:' | awk '{print $2}' || true)"
  echo "  link speed: ${SPEED:-unknown}"
  case "$SPEED" in
    10Mb*|100Mb*)
      c_wn "  WARNING: a ${SPEED} link caps every measurement. If your plan is faster,"
      echo "     this machine - not your internet - is what the report will show."
      echo "     Use a gigabit machine or a USB3 gigabit adapter." ;;
  esac
else
  echo "  (install 'ethtool' to see the link speed: sudo apt install ethtool)"
fi
case "$(cat /sys/class/net/"$IFACE"/type 2>/dev/null || echo)" in
  1) [ -d "/sys/class/net/$IFACE/wireless" ] && c_wn "  WARNING: $IFACE looks like Wi-Fi. Measure over Ethernet if you can - Wi-Fi adds its own losses and you would be blaming your ISP for them." ;;
esac

# --- 2. speedtest CLI --------------------------------------------------------
have_ookla() { command -v speedtest >/dev/null 2>&1 && speedtest --version 2>&1 | grep -qi ookla; }
if have_ookla; then
  c_ok "  Ookla speedtest already installed."
else
  echo "  Installing the Ookla speedtest CLI (gives bufferbloat / latency-under-load) ..."
  if command -v apt-get >/dev/null 2>&1; then
    curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | sudo bash >/dev/null 2>&1 || true
    sudo apt-get install -y speedtest >/dev/null 2>&1 || {
      c_wn "  Ookla package unavailable; falling back to speedtest-cli (no bufferbloat)."
      sudo apt-get install -y speedtest-cli >/dev/null 2>&1 || pip3 install --user speedtest-cli >/dev/null 2>&1 || true
    }
  elif command -v dnf >/dev/null 2>&1; then
    curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.rpm.sh | sudo bash >/dev/null 2>&1 || true
    sudo dnf install -y speedtest >/dev/null 2>&1 || sudo dnf install -y speedtest-cli >/dev/null 2>&1 || true
  else
    c_wn "  Unknown package manager; trying pip speedtest-cli."
    pip3 install --user speedtest-cli >/dev/null 2>&1 || true
  fi
fi
have_ookla && speedtest --accept-license --accept-gdpr --format=json >/dev/null 2>&1 || true
if ! command -v speedtest >/dev/null 2>&1 && ! command -v speedtest-cli >/dev/null 2>&1; then
  c_er "  No speedtest tool could be installed. Install one manually, then re-run."
  echo "     https://www.speedtest.net/apps/cli"
  exit 1
fi

# --- 3. Questions ------------------------------------------------------------
echo
c_b "Settings"
echo "  Everything below is stored in netmon.conf and can be changed later,"
echo "  by editing that file or straight from Telegram (/config, /setplan, ...)."
echo

LANG_CODE="$(ask "  Language for reports and bot replies (en/he)" "${LANG_CODE:-en}")"
echo
echo "  Your plan is what the report compares against - put in the numbers your"
echo "  ISP sells you, not what you measured."
PLAN_DOWN="$(ask "  Download speed you pay for, Mbps" "${PLAN_DOWN:-100}")"
PLAN_UP="$(ask "  Upload speed you pay for, Mbps (optional)" "${PLAN_UP:-}")"
echo
echo "  How often to measure. Every 60 min is a good default: enough resolution"
echo "  to see evening congestion, light enough not to disturb the household."
echo "  Each test moves a few hundred MB - watch out on metered connections."
INTERVAL="$(ask "  Minutes between measurements (5-1440)" "${INTERVAL:-60}")"
PING_HOST="$(ask "  Host for the baseline ping" "${PING_HOST:-1.1.1.1}")"

echo
c_b "Telegram"
cat <<'HINT'
  The bot sends you reports and instant alerts, and lets you control netmon
  from your phone. Two things are needed:

  1) A BOT TOKEN
     - Open Telegram and message @BotFather
     - Send /newbot, pick a name, then a username ending in "bot"
     - It replies with a token like 8123456789:AAE...  <- that is the token

  2) YOUR CHAT ID  (which chat the bot is allowed to talk to)
     - Message your new bot once (anything, e.g. "hi")
     - This installer can then detect the id automatically.

  Leave the token empty to skip Telegram entirely; netmon still measures and
  you can add credentials to netmon.conf later.
HINT
echo
BOT_TOKEN="$(ask "  Bot token" "${BOT_TOKEN:-}")"
CHAT_ID="${CHAT_ID:-}"

# --- 4. Write the config -----------------------------------------------------
set_conf() { "$PY" "$DIR/netmon_config.py" --set "$1=$2" >/dev/null; }
[ -f "$CONF" ] || : > "$CONF"
chmod 600 "$CONF"

set_conf LANG "$LANG_CODE"
set_conf PLAN_DOWN_MBPS "$PLAN_DOWN"
[ -n "$PLAN_UP" ] && set_conf PLAN_UP_MBPS "$PLAN_UP"
set_conf INTERVAL_MINUTES "$INTERVAL"
set_conf PING_HOST "$PING_HOST"

if [ -n "$BOT_TOKEN" ]; then
  if set_conf BOT_TOKEN "$BOT_TOKEN"; then :; else
    c_er "  That token does not look like a BotFather token; skipping Telegram."
    BOT_TOKEN=""
  fi
fi

if [ -n "$BOT_TOKEN" ] && [ -z "$CHAT_ID" ]; then
  if interactive; then
    echo
    echo "  Now send any message to your bot in Telegram."
    printf '  Waiting up to 120s for it '
    CHAT_ID="$("$PY" "$DIR/netmon_bot.py" --detect-chat --seconds 120 || true)"
    if [ -n "$CHAT_ID" ]; then
      c_ok "detected chat id: $CHAT_ID"
    else
      c_wn "no message received."
      CHAT_ID="$(ask "  Enter your chat id manually (or leave empty to do it later)" "")"
    fi
  fi
fi
[ -n "$CHAT_ID" ] && set_conf CHAT_ID "$CHAT_ID"

# --- 5. Schedule -------------------------------------------------------------
echo
c_b "Scheduling"
if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
  REPORT_DOW="$(ask "  Periodic report - day of week (0=Sun..6=Sat, 'off' to disable)" "${REPORT_DOW:-0}")"
  if [ "$REPORT_DOW" = "off" ]; then
    set_conf REPORT_ENABLED 0
  else
    REPORT_HOUR="$(ask "  Periodic report - hour (0-23)" "${REPORT_HOUR:-8}")"
    set_conf REPORT_ENABLED 1
    set_conf REPORT_DOW "$REPORT_DOW"
    set_conf REPORT_HOUR "$REPORT_HOUR"
  fi
else
  set_conf REPORT_ENABLED 0
fi

if "$PY" "$DIR/netmon_config.py" --apply-schedule; then
  c_ok "  cron updated."
else
  c_wn "  Could not write to cron - schedule the measurement yourself:"
  echo "     */$INTERVAL * * * * $DIR/netmon.py"
fi

# --- 6. Telegram bot service -------------------------------------------------
if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
  echo
  c_b "Telegram bot"
  if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
    INSTALL_SVC="$(ask "  Install the bot as a systemd service (starts on boot)? (y/n)" "y")"
    if [ "${INSTALL_SVC,,}" = "y" ]; then
      SVC=/etc/systemd/system/netmon-bot.service
      sudo tee "$SVC" >/dev/null <<UNIT
[Unit]
Description=netmon Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$DIR
ExecStart=$PY $DIR/netmon_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
      sudo systemctl daemon-reload
      sudo systemctl enable --now netmon-bot >/dev/null 2>&1 || sudo systemctl restart netmon-bot
      sleep 2
      if [ "$(systemctl is-active netmon-bot)" = "active" ]; then
        c_ok "  netmon-bot service is running. Send /help to your bot."
      else
        c_wn "  Service did not start. Check: sudo journalctl -u netmon-bot -n 30"
      fi
      c_wn "  NOTE: one bot token can only be polled by one process. If this token"
      echo "     is already used by another bot of yours, create a second bot in"
      echo "     @BotFather for netmon."
    fi
  else
    c_wn "  No systemd here. Start the bot yourself, e.g.:"
    echo "     nohup $PY $DIR/netmon_bot.py >> $DIR/netmon-bot.log 2>&1 &"
  fi
fi

# --- 7. First measurement ----------------------------------------------------
echo
c_b "Running one measurement now (~40s)"
"$DIR/netmon.py" --no-alert || c_wn "  The first measurement failed - see the message above."

echo
c_ok "Done."
echo
echo "  Manual commands:"
echo "    Text report:     $DIR/analyze.py"
echo "    HTML report:     $DIR/report.py"
echo "    Send to Telegram:$DIR/report.py --telegram"
echo "    Change settings: $DIR/netmon_config.py --show"
if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
echo "    From Telegram:   /help /speed /status /config /setinterval /setplan"
fi
echo
echo "  Let it collect data for about a week, then read the report. A line that is"
echo "  fine at 03:00 and collapses at 21:00 is congestion, not your equipment."
