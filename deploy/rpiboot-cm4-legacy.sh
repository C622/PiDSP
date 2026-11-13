#!/usr/bin/env bash
# Expose a CM4's eMMC as a USB drive with the legacy MSD loader.
#
# Usage (on the computer the CM4 is plugged into, board in USB/flash mode):
#   deploy/rpiboot-cm4-legacy.sh [work-dir]
#
# Fallback for when `rpiboot` (mass-storage-gadget64, the default in current
# usbboot releases) loads boot.img and then the CM4 never reappears as a drive,
# as seen on the DFRobot PiTray mini powered from a PC USB port. usbboot
# removed the CM4 legacy loader in 2026-09 (commit f6cde432b0), so its
# bootcode4.bin is fetched from the last tag that shipped it and checked by
# git blob hash. bootcode.bin and start.elf come from the installed usbboot.
set -euo pipefail

TAG=20260603-182203
BLOB=d259910a61d1d67c82da3e8de8b366b874d2723a
DIR=${1:-${XDG_CACHE_HOME:-$HOME/.cache}/pidsp-msd-cm4}

MSD=""
for d in /usr/share/rpiboot/msd /usr/local/share/rpiboot/msd; do
  [[ -f $d/start.elf ]] && MSD=$d && break
done
if [[ -z $MSD ]]; then
  echo "usbboot's msd/ directory not found. Install rpiboot first (Arch: AUR 'rpiusbboot')." >&2
  exit 1
fi

mkdir -p "$DIR"
if [[ ! -f $DIR/bootcode4.bin ]] || [[ $(git hash-object "$DIR/bootcode4.bin") != "$BLOB" ]]; then
  curl -fLo "$DIR/bootcode4.bin" \
    "https://raw.githubusercontent.com/raspberrypi/usbboot/$TAG/firmware/2711/bootcode4.bin"
fi
got=$(git hash-object "$DIR/bootcode4.bin")
if [[ $got != "$BLOB" ]]; then
  echo "bootcode4.bin hash mismatch: got $got, expected $BLOB" >&2
  exit 1
fi
cp "$MSD/bootcode.bin" "$MSD/start.elf" "$DIR/"

echo "Starting rpiboot with the legacy loader from $DIR"
sudo rpiboot -d "$DIR"
echo "The eMMC should now show up as 'RPi-MSD- 0001'. Unmount any auto-mounted"
echo "partitions (udisksctl unmount -b /dev/sdXN) before flashing."
