#!/usr/bin/env bash
# Install CamillaDSP and the audio plumbing PiDSP needs on a Raspberry Pi.
#
# Usage (from your dev machine):
#   deploy/install-camilladsp.sh <ssh-target>
#
# Installs:
#   - CamillaDSP (pinned version, checksum verified) as /usr/local/bin/camilladsp
#   - a "camilladsp" system user (audio group) and /var/lib/camilladsp
#   - the ALSA loopback (snd-aloop, card id "Loopback"), loaded at boot; used
#     to feed measurement sweeps through CamillaDSP
#   - CM6206 mixer defaults (S/PDIF input, output at 0 dB), saved with alsactl
#   - camilladsp.service: websocket on 127.0.0.1:1234, waits for a config
# Safe to re-run. Needs passwordless sudo on the Pi (see docs/BRINGUP.md).
set -euo pipefail

if [[ $# -ne 1 ]]; then
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi
TARGET=$1

if ! ssh "$TARGET" sudo -n true 2>/dev/null; then
  echo "sudo on $TARGET needs a password. Enable passwordless sudo first (docs/BRINGUP.md, step 2)." >&2
  exit 1
fi

ssh "$TARGET" sudo bash -s <<'REMOTE'
set -euo pipefail
VERSION=4.1.3
SHA256=d9a17092923ebfe5d20a770c6b6a7eb2268f9700f999bf604b9db09f518aca5a  # camilladsp-linux-aarch64.tar.gz
CARD=ICUSBAUDIO7D   # CM6206 test card
STATE=/var/lib/camilladsp

# CamillaDSP binary
if [[ "$(/usr/local/bin/camilladsp --version 2>/dev/null)" != "CamillaDSP $VERSION"* ]]; then
  tmp=$(mktemp -d)
  curl -fsSLo "$tmp/c.tgz" \
    "https://github.com/HEnquist/camilladsp/releases/download/v$VERSION/camilladsp-linux-aarch64.tar.gz"
  echo "$SHA256  $tmp/c.tgz" | sha256sum -c --quiet
  tar -xzf "$tmp/c.tgz" -C "$tmp" camilladsp
  install -m 755 "$tmp/camilladsp" /usr/local/bin/camilladsp
  rm -rf "$tmp"
fi
echo "Installed: $(/usr/local/bin/camilladsp --version)"

# Service user and state directory
id camilladsp >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin --groups audio camilladsp
install -d -o camilladsp -g camilladsp -m 775 "$STATE" "$STATE/configs" "$STATE/filters"

# ALSA loopback
echo snd-aloop > /etc/modules-load.d/pidsp-aloop.conf
echo "options snd-aloop id=Loopback pcm_substreams=2" > /etc/modprobe.d/pidsp-aloop.conf
modprobe snd-aloop
echo "Loopback: $(grep -c "\\[Loopback" /proc/asound/cards) card(s)"

# CM6206 mixer defaults
if grep -q "\[$CARD" /proc/asound/cards; then
  amixer -q -c "$CARD" cset name='PCM Capture Source' 'IEC958 In'
  amixer -q -c "$CARD" cset name='IEC958 In Capture Switch' on
  amixer -q -c "$CARD" cset name='Speaker Playback Volume' 100%
  amixer -q -c "$CARD" cset name='Speaker Playback Switch' on
  amixer -q -c "$CARD" cset name='Line Playback Switch' off
  amixer -q -c "$CARD" cset name='Mic Playback Switch' off
  alsactl store
  echo "Mixer: $CARD set to S/PDIF in, output 0 dB"
else
  echo "Mixer: $CARD not found, skipped"
fi

# systemd service
cat > /etc/systemd/system/camilladsp.service <<'UNIT'
[Unit]
Description=CamillaDSP (PiDSP)
After=sound.target

[Service]
User=camilladsp
Group=audio
ExecStart=/usr/local/bin/camilladsp --statefile /var/lib/camilladsp/statefile.yml --wait --address 127.0.0.1 --port 1234 --loglevel info
Restart=on-failure
RestartSec=2
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=10
LimitRTPRIO=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now camilladsp.service
sleep 1
systemctl --no-pager --lines=5 status camilladsp.service || true
REMOTE
