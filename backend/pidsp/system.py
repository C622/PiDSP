"""Read-only facts about the Pi: host, network, hardware and audio devices.

All paths are parameters, so the tests can point them at a fake tree.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

# Known USB audio devices: (vendor, product) -> role and friendly name.
KNOWN_USB = {
    ("2752", "0007"): ("mic", "miniDSP UMIK-1"),
    ("2752", "0010"): ("mic", "miniDSP UMIK-2"),
    ("0d8c", "0102"): ("dac", "C-Media CM6206 (7.1 USB)"),
}
DAC_CARD = "ICUSBAUDIO7D"
LOOPBACK_CARD = "Loopback"


def _read(path: Path | str, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _addresses() -> list[str]:
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout
        return [a for a in out.split() if ":" not in a] + [a for a in out.split() if ":" in a]
    except (OSError, subprocess.SubprocessError):
        return []


def _os_name(path: str = "/etc/os-release") -> str:
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)', _read(path), re.M)
    return m.group(1) if m else ""


def _meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    out = {}
    for line in _read(path).splitlines():
        key, _, rest = line.partition(":")
        if rest.strip().endswith("kB"):
            out[key] = int(rest.split()[0]) * 1024
    return out


def info(proc: str = "/proc", sys: str = "/sys") -> dict:
    mem = _meminfo(f"{proc}/meminfo")
    disk = shutil.disk_usage("/")
    temp = _read(f"{sys}/class/thermal/thermal_zone0/temp")
    uptime = _read(f"{proc}/uptime").split()
    load = _read(f"{proc}/loadavg").split()
    return {
        "hostname": socket.gethostname(),
        "addresses": _addresses(),
        "model": _read(f"{proc}/device-tree/model").rstrip("\x00").strip(),
        "os": _os_name(),
        "kernel": os.uname().release,
        "uptime_s": int(float(uptime[0])) if uptime else None,
        "load": [float(x) for x in load[:3]] if load else None,
        "cpu_temp_c": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "memory": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable")},
        "disk": {"total": disk.total, "free": disk.free},
    }


def sound_cards(path: str = "/proc/asound/cards") -> list[dict]:
    """Parse /proc/asound/cards into [{index, id, driver, name}]."""
    cards = []
    for m in re.finditer(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s+(\S+)\s+-\s+(.*)$", _read(path), re.M):
        cards.append({"index": int(m[1]), "id": m[2], "driver": m[3], "name": m[4].strip()})
    return cards


def usb_audio(sys: str = "/sys") -> list[dict]:
    """Known USB audio devices currently plugged in."""
    found = []
    for dev in sorted(Path(sys, "bus/usb/devices").glob("*")):
        key = (_read(dev / "idVendor"), _read(dev / "idProduct"))
        if key in KNOWN_USB:
            role, name = KNOWN_USB[key]
            found.append({"role": role, "name": name, "usb_id": ":".join(key),
                          "serial": _read(dev / "serial") or None})
    return found


def audio_devices(proc: str = "/proc", sys: str = "/sys") -> dict:
    cards = sound_cards(f"{proc}/asound/cards")
    ids = {c["id"] for c in cards}
    usb = usb_audio(sys)
    mic = next((d for d in usb if d["role"] == "mic"), None)
    if mic:
        # The UMIK-1 reports a placeholder USB serial; the real one is on the
        # label and in the calibration file's name.
        if mic["serial"] in (None, "000-0000"):
            mic["serial"] = None
        card = next((c for c in cards if re.search(r"umik", c["name"], re.I)), None)
        if card:
            gain = re.search(r"Gain:\s*(\d+)\s*dB", card["name"])
            mic["card"] = card["id"]
            mic["gain_db"] = int(gain[1]) if gain else None
    return {
        "cards": cards,
        "dac": {"present": DAC_CARD in ids, "card": DAC_CARD},
        "loopback": {"present": LOOPBACK_CARD in ids},
        "mic": {"present": mic is not None, **(mic or {})},
        "usb": usb,
    }
