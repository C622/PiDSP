#!/usr/bin/env bash
# Install or update the PiDSP backend and web interface on the Pi.
#
# Usage (from your dev machine, in the repo):
#   deploy/install-web.sh <ssh-target>
#
# Copies backend/pidsp to /opt/pidsp/app and runs it as pidsp-web.service on
# port 80 (http://<hostname>.local). Python dependencies come from Debian
# packages. Privileged actions (password, restart, shut down) go through
# /usr/local/sbin/pidsp-admin, the only command the service may run with sudo.
# Run deploy/install-camilladsp.sh first. Safe to re-run.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi
TARGET=$1
REPO=$(cd "$(dirname "$0")/.." && pwd)

if ! ssh "$TARGET" sudo -n true 2>/dev/null; then
  echo "sudo on $TARGET needs a password. Enable passwordless sudo first (docs/BRINGUP.md, step 2)." >&2
  exit 1
fi

# Ship the package and the system files in one tarball over SSH.
tar -C "$REPO" -czf - --exclude='__pycache__' --exclude='*.pyc' backend/pidsp system \
  | ssh "$TARGET" 'rm -rf /tmp/pidsp-install && mkdir -p /tmp/pidsp-install && tar -xzf - -C /tmp/pidsp-install'

LOGIN_USER=${TARGET%@*}
[[ $LOGIN_USER == "$TARGET" ]] && LOGIN_USER=pidsp

ssh "$TARGET" sudo LOGIN_USER="$LOGIN_USER" bash -s <<'REMOTE'
set -euo pipefail
SRC=/tmp/pidsp-install

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3-fastapi python3-uvicorn python3-websockets python3-numpy python3-scipy python3-yaml >/dev/null

id pidsp-web >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin pidsp-web
usermod -a -G audio,camilladsp pidsp-web

install -d /opt/pidsp /etc/pidsp
echo "$LOGIN_USER" > /etc/pidsp/login-user
rm -rf /opt/pidsp/app.new
install -d /opt/pidsp/app.new
cp -r "$SRC/backend/pidsp" /opt/pidsp/app.new/
rm -rf /opt/pidsp/app.old
[[ -d /opt/pidsp/app ]] && mv /opt/pidsp/app /opt/pidsp/app.old
mv /opt/pidsp/app.new /opt/pidsp/app

install -m 755 "$SRC/system/pidsp-admin" /usr/local/sbin/pidsp-admin
install -m 440 "$SRC/system/pidsp-web.sudoers" /etc/sudoers.d/020_pidsp-web
visudo -cqf /etc/sudoers.d/020_pidsp-web
install -m 644 "$SRC/system/pidsp-web.service" /etc/systemd/system/pidsp-web.service

systemctl daemon-reload
systemctl enable pidsp-web.service >/dev/null 2>&1
systemctl restart pidsp-web.service
rm -rf "$SRC"

for i in $(seq 1 20); do
  if curl -fs -o /dev/null http://127.0.0.1/api/status; then
    echo "PiDSP $(python3 -c 'import sys; sys.path.insert(0, "/opt/pidsp/app"); import pidsp; print(pidsp.__version__)') is up on port 80"
    exit 0
  fi
  sleep 0.5
done
echo "pidsp-web did not answer; recent log:" >&2
journalctl -u pidsp-web --no-pager -n 30 >&2
exit 1
REMOTE
echo "Open http://$(ssh "$TARGET" hostname).local"
