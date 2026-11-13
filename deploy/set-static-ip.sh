#!/usr/bin/env bash
# Give the Pi's wired interface (eth0) a static IPv4 address, using NetworkManager.
#
# Usage (from your dev machine):
#   deploy/set-static-ip.sh <ssh-target> <address/prefix> <gateway> [dns]
# Example:
#   deploy/set-static-ip.sh pi@pidsp.local 192.168.1.50/24 192.168.1.1
#
# DNS defaults to the gateway. The SSH session will drop when the new address
# takes effect; reconnect on the new IP. Needs passwordless sudo on the Pi
# (see docs/BRINGUP.md).
set -euo pipefail

if [[ $# -lt 3 ]]; then
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

TARGET=$1 ADDR=$2 GW=$3 DNS=${4:-$3}

if ! ssh "$TARGET" sudo -n true 2>/dev/null; then
  echo "sudo on $TARGET needs a password. Enable passwordless sudo first (docs/BRINGUP.md, step 2)." >&2
  exit 1
fi

ssh "$TARGET" sudo bash -s -- "$ADDR" "$GW" "$DNS" <<'REMOTE'
set -euo pipefail
ADDR=$1 GW=$2 DNS=$3
CON=$(nmcli -g GENERAL.CONNECTION device show eth0)
if [[ -z "$CON" || "$CON" == "--" ]]; then
  echo "No active NetworkManager connection on eth0" >&2; exit 1
fi
echo "Configuring connection '$CON': $ADDR via $GW (DNS $DNS)"
nmcli con mod "$CON" ipv4.method manual ipv4.addresses "$ADDR" \
  ipv4.gateway "$GW" ipv4.dns "$DNS" connection.autoconnect yes
# The auto-created profile on a fresh image lives in /run (memory only); a
# modify should persist it to /etc. Refuse to continue if it did not.
UUID=$(nmcli -g connection.uuid con show "$CON")
FILE=$(nmcli -t -f UUID,FILENAME con show | awk -F: -v u="$UUID" '$1==u{print $2}')
if [[ "$FILE" == /run/* ]]; then
  echo "Profile '$CON' is still only in memory ($FILE); it would not survive a reboot." >&2
  exit 1
fi
echo "Saved to $FILE"
# Re-activate in the background so the change survives this SSH session dropping.
nohup sh -c "sleep 1; nmcli con up '$CON'" >/dev/null 2>&1 &
echo "Applied. Reconnect at ${ADDR%/*}."
REMOTE
