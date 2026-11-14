"""The bitstream guard: never play Dolby or DTS data as noise.

A TV set to "Bitstream", "Auto" or "Dolby Digital" on its optical output sends
compressed audio packed in IEC 61937 frames. To a PCM input that is
full-scale noise, and through PiDSP it would reach the speakers as exactly
that. CamillaDSP cannot tell, and the CM6206 does not report the S/PDIF
channel status bits that would say so.

So PiDSP watches CamillaDSP's input levels. Packed bitstream looks like no
music does for long: both channels peaking at full scale in every chunk, with
steady and nearly identical RMS levels. When that persists, the guard stops
CamillaDSP at once (silence), then records the optical input directly (it is
free while CamillaDSP is stopped) and looks for the IEC 61937 sync words:

  - found: stay silent, report which format the TV sends, and look again
    every few seconds; when plain PCM comes back, start CamillaDSP again
  - not found (very loud, heavily limited music can look similar): start
    again at once, about a second of silence, and for the next while only
    react to a much longer stretch, so the same song does not trip it again

The guard only acts while CamillaDSP is running the applied listening setup,
never during a measurement.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from .camilla import config as cfgmod
from .store import ACTIVE_CONFIG, LISTENING_CONFIGS

log = logging.getLogger("pidsp.guard")

SYNC_A, SYNC_B = 0xF872, 0x4E1F   # IEC 61937 preamble words Pa, Pb
DATA_TYPES = {
    1: "Dolby Digital", 3: "MPEG audio", 4: "MPEG audio", 5: "MPEG audio", 6: "MPEG audio",
    7: "AAC", 11: "DTS", 12: "DTS", 13: "DTS", 17: "DTS", 21: "Dolby Digital Plus", 22: "Dolby TrueHD",
}

PERIOD_S = 0.5            # how often the levels are read
TRIGGER = 3               # consecutive suspicious readings before acting (1.5 s)
TRIGGER_AFTER_FALSE = 20  # the same after a false alarm (10 s) ...
COOLDOWN_S = 600          # ... for this long
RECHECK_S = 3.0           # while silenced: how often the input is checked again
SNIFF_SECONDS = 1


def detect_iec61937(raw: bytes) -> str | None:
    """The bitstream format in a stereo S16_LE recording, or None for PCM."""
    usable = len(raw) - len(raw) % 4
    x = np.frombuffer(raw[:usable], dtype="<u2").reshape(-1, 2)
    hits = np.where((x[:-1, 0] == SYNC_A) & (x[:-1, 1] == SYNC_B))[0]
    if len(hits) < 2:
        return None
    data_type = int(x[hits[0] + 1, 0]) & 0x7F
    return DATA_TYPES.get(data_type, "a compressed format")


def looks_like_bitstream(levels: dict | None) -> bool:
    """Do CamillaDSP's capture levels for one chunk look like packed bitstream?"""
    if not levels:
        return False
    peak, rms = levels.get("capture_peak") or [], levels.get("capture_rms") or []
    if len(peak) < 2 or len(rms) < 2:
        return False
    return (all(p >= -0.5 for p in peak[:2]) and all(-16.0 <= r <= -4.0 for r in rms[:2])
            and abs(rms[0] - rms[1]) < 1.0)


async def sniff_spdif(device: str = cfgmod.DEFAULT_HARDWARE["capture"]["device"],
                      seconds: int = SNIFF_SECONDS) -> tuple[bool, str | None]:
    """Record the optical input directly. Returns (recorded, format or None)."""
    for _ in range(3):  # the device may take a moment to be released
        proc = await asyncio.create_subprocess_exec(
            "arecord", "-q", "-D", device, "-f", "S16_LE", "-c", "2", "-r", "48000", "-d", str(seconds),
            "-t", "raw", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _err = await proc.communicate()
        if proc.returncode == 0 and out:
            return True, detect_iec61937(out)
        await asyncio.sleep(0.3)
    return False, None


class BitstreamGuard:
    def __init__(self, camilla, store, jobs, sniff=sniff_spdif, period: float = PERIOD_S,
                 recheck: float = RECHECK_S, clock=time.monotonic):
        self.cam, self.store, self.jobs = camilla, store, jobs
        self.sniff, self.period, self.recheck, self.clock = sniff, period, recheck, clock
        self.silenced: str | None = None      # the format the TV sends, while silenced
        self.since: float | None = None       # wall-clock time silenced
        self.false_alarms = 0
        self.cooldown_until = 0.0
        self._suspicious = 0
        self._stopped_path: str | None = None   # the listening config the guard stopped

    @property
    def active_path(self) -> str:
        return str(self.store.config_dir / ACTIVE_CONFIG)

    async def _start(self) -> None:
        await self.cam.call("SetConfigFilePath", self._stopped_path or self.active_path)
        await self.cam.call("Reload")

    async def step(self) -> None:
        """One round of watching. run() calls this forever."""
        if self.silenced:
            if self.jobs.running():
                return
            recorded, kind = await self.sniff()
            if recorded and kind is None:
                log.warning("the TV sends PCM again; starting the sound")
                self.silenced, self.since = None, None
                await self._start()
            elif kind:
                self.silenced = kind
            return
        p = await self.cam.probe()
        listening = p and p["config_path"] and p["config_path"].rsplit("/", 1)[-1] in LISTENING_CONFIGS
        if not p or p["state"] != "Running" or not listening or self.jobs.running():
            self._suspicious = 0
            return
        self._suspicious = self._suspicious + 1 if looks_like_bitstream(p["levels"]) else 0
        needed = TRIGGER_AFTER_FALSE if self.clock() < self.cooldown_until else TRIGGER
        if self._suspicious < needed:
            return
        self._suspicious = 0
        self._stopped_path = p["config_path"]
        await self.cam.call("Stop")  # silence first, then find out
        recorded, kind = await self.sniff()
        if kind:
            log.warning("the TV sends %s, not PCM: sound stopped until it sends PCM", kind)
            self.silenced, self.since = kind, time.time()
        else:
            log.info("loud input looked like bitstream but is PCM%s; sound restarted",
                     "" if recorded else " (could not record the input)")
            self.false_alarms += 1
            self.cooldown_until = self.clock() + COOLDOWN_S
            await self._start()

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except Exception as e:  # keep watching whatever happens
                log.error("bitstream guard: %s", e)
            await asyncio.sleep(self.recheck if self.silenced else self.period)

    def summary(self) -> dict:
        return {"silenced": self.silenced is not None, "format": self.silenced, "since": self.since,
                "false_alarms": self.false_alarms}
