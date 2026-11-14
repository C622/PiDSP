"""Sub level and volume following.

The main speakers' volume is set on the amplifier (a Bluesound Powernode),
after PiDSP. The sub is driven by PiDSP directly, so on its own it would stay
put while the mains get louder or quieter. PiDSP reads the Powernode's volume
over the BluOS API and moves the sub by the same number of dB.

One CamillaDSP fader (Aux1, the sub's Volume filter, 200 ms ramp) carries:

  sub fader = sub level (the listener's taste, on top of the calibration)
            + (Powernode dB now - Powernode dB when the bass was measured)

so it changes live, without reloading the config or touching the latency.
When following and the Powernode is muted or leaves its analog input (where
PiDSP's output comes in), the sub is muted too, rather than playing on alone.

The fader never raises the sub above the level of the input signal (the
negation of the sub's calibrated trim), so it cannot clip CamillaDSP's output.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import bluos

SUB_FADER = 1           # Aux1 (config.SUB_FADER); 0 is Main
MIN_FADER_DB = -60.0
SUB_LEVEL_RANGE = (-20.0, 12.0)
POLL_WAIT_S = 30        # long-poll length
RETRY_S = 10            # after the player did not answer
REASSERT_S = 60         # set the fader again at least this often (CamillaDSP may have restarted)

log = logging.getLogger("pidsp.volume")


def sub_gain(settings: dict, player: bluos.PlayerStatus | None, trim_db: float | None) -> dict:
    """What the sub fader should be, and why."""
    level = float(settings.get("sub_level_db") or 0.0)
    ref = settings.get("reference_db")
    follow = bool(settings.get("follow_volume")) and player is not None and player.db is not None and ref is not None
    offset = float(player.db - ref) if follow else 0.0
    wanted = level + offset
    limit = max(0.0, -(trim_db or 0.0))
    fader = min(max(wanted, MIN_FADER_DB), limit)
    mute_reason = None
    if follow and player.mute:
        mute_reason = "the Powernode is muted"
    elif follow and not player.analog:
        mute_reason = f"the Powernode is on {player.input or 'another source'}, not its analog input"
    return {
        "fader_db": round(fader, 2), "wanted_db": round(wanted, 2), "limit_db": round(limit, 2),
        "clamped": wanted > limit + 0.01, "following": follow, "offset_db": round(offset, 2),
        "sub_level_db": level, "mute": mute_reason is not None, "mute_reason": mute_reason,
    }


class VolumeFollower:
    """Keeps the sub fader in step with the settings and the Powernode."""

    def __init__(self, camilla, store, fetch=bluos.fetch_status, wait: int = POLL_WAIT_S):
        self.cam = camilla
        self.store = store
        self.fetch = fetch
        self.wait = wait
        self.player: bluos.PlayerStatus | None = None
        self.error: str | None = None
        self.gain: dict | None = None
        self._applied: tuple | None = None
        self._applied_at = 0.0

    def _trim(self) -> float | None:
        active = self.store.active() or {}
        return ((active.get("plan") or {}).get("channels") or {}).get("sub", {}).get("trim_db")

    async def apply(self, force: bool = False) -> dict:
        """Work out the sub fader and send it to CamillaDSP if it changed."""
        self.gain = sub_gain(self.store.settings(), self.player, self._trim())
        want = (self.gain["fader_db"], self.gain["mute"])
        if force or want != self._applied or time.monotonic() - self._applied_at > REASSERT_S:
            try:
                await self.cam.call("SetFaderVolume", [SUB_FADER, want[0]])
                await self.cam.call("SetFaderMute", [SUB_FADER, want[1]])
                if want != self._applied:
                    log.info("sub fader %+.1f dB%s", want[0], f" (muted: {self.gain['mute_reason']})" if want[1] else "")
                self._applied, self._applied_at = want, time.monotonic()
            except Exception as e:  # CamillaDSP restarting; the next round sets it again
                log.debug("could not set the sub fader: %s", e)
                self._applied = None
        return self.gain

    async def refresh_player(self) -> bluos.PlayerStatus | None:
        """Read the Powernode once now (no long-poll)."""
        host = self.store.settings().get("bluos_host")
        if not host:
            self.player, self.error = None, None
            return None
        try:
            self.player = await asyncio.to_thread(self.fetch, host)
            self.error = None
        except bluos.BluOSError as e:
            self.error = str(e)
        return self.player

    async def run(self) -> None:
        etag, host_seen = None, None
        while True:
            host = self.store.settings().get("bluos_host")
            if host != host_seen:
                etag, host_seen, self.player = None, host, None
            if not host:
                self.error = None
                await self.apply()
                await asyncio.sleep(5)
                continue
            try:
                self.player = await asyncio.to_thread(self.fetch, host, etag, self.wait)
                etag, self.error = self.player.etag, None
            except bluos.BluOSError as e:
                if self.error != str(e):
                    log.warning("%s", e)
                self.error = str(e)
                etag = None
                await self.apply()  # keep the last known state; reassert the fader
                await asyncio.sleep(RETRY_S)
                continue
            await self.apply()

    def summary(self) -> dict:
        s = self.store.settings()
        return {
            "bluos_host": s.get("bluos_host"),
            "player": self.player.to_dict() if self.player else None,
            "error": self.error,
            "sub": self.gain,
            "reference": {"db": s.get("reference_db"), "volume": s.get("reference_volume")},
            "follow_volume": bool(s.get("follow_volume")),
            "sub_level_db": float(s.get("sub_level_db") or 0.0),
        }
