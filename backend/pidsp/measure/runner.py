"""Run measurements: load a CamillaDSP measurement config, sweep, analyse.

A Rig says where the sweeps go and where the microphone signal comes from:

- the real rig plays through CamillaDSP to the sound card and records the
  UMIK-1;
- the self-test rig plays into CamillaDSP but sends its output back into the
  ALSA loopback and "records" that, through simulated speakers made of
  CamillaDSP filters (known delays and roll-offs). It is silent, and checks
  the whole chain end to end: the plan must undo the simulated delays.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..camilla import config as cfgmod
from ..camilla.client import CamillaClient
from ..dsp import bass
from ..dsp import calibration as cal
from ..store import write_atomic
from .audio import Capture, play_and_record

# Sweep level control. The probe starts quiet; the sweep level is chosen so
# the microphone peaks around GOAL_PEAK_DB (about 80 dB SPL on a UMIK-1), and
# never above OUTPUT_CEILING_DB at the output.
PROBE_DB = -40.0
GOAL_PEAK_DB = -35.0
OUTPUT_CEILING_DB = -12.0
# The sub is probed on its own, in its own band, before any sweep plays on it.
SUB_PROBE_BAND = (25.0, 120.0)
SUB_PROBE_SECONDS = 1.5
# A probe this loud at the microphone (about 95 dB SPL) means the speaker's own
# level is far too high; stop rather than sweep.
TOO_LOUD_PROBE_DB = -20.0
REPEATS = 2

MEASURE_CONFIG = "measure.yml"

# Simulated speakers for the self-test.
SIM_DELAYS_MS = {"left": 1.0, "right": 1.6, "sub": 6.0}
SIM_MAINS_HP_HZ = 45
SIM_SUB_LP_HZ = 110


@dataclass
class Rig:
    name: str
    player: str
    capture: Capture
    mic_channels: list[int]
    hardware: dict | None = None
    decorate: Callable[[dict], dict] = field(default=lambda c: c)

    def mic(self, recording: np.ndarray) -> np.ndarray:
        return recording[:, self.mic_channels].sum(axis=1)


def real_rig(mic_card: str = "U18dB", hardware: dict | None = None) -> Rig:
    return Rig("room", cfgmod.LOOPBACK_PLAYER, Capture(f"hw:CARD={mic_card},DEV=0", 2, "S24_3LE"), [0],
               hardware=hardware)


def _simulate(cfg: dict) -> dict:
    """Add simulated speakers (roll-offs and delays) after the measurement mixer."""
    cfg = copy.deepcopy(cfg)
    f = cfg.setdefault("filters", {})
    f["sim_mains"] = {"type": "BiquadCombo", "parameters": {"type": "ButterworthHighpass", "freq": SIM_MAINS_HP_HZ, "order": 4}}
    f["sim_sub"] = {"type": "BiquadCombo", "parameters": {"type": "ButterworthLowpass", "freq": SIM_SUB_LP_HZ, "order": 4}}
    for role, ms in SIM_DELAYS_MS.items():
        f[f"sim_delay_{role}"] = {"type": "Delay", "parameters": {"delay": ms, "unit": "ms", "subsample": True}}
    cfg["pipeline"] += [
        {"type": "Filter", "channels": [0], "names": ["sim_mains", "sim_delay_left"]},
        {"type": "Filter", "channels": [1], "names": ["sim_mains", "sim_delay_right"]},
        {"type": "Filter", "channels": [2], "names": ["sim_sub", "sim_delay_sub"]},
    ]
    cfg["title"] = "PiDSP self-test: simulated 2.1 speakers"
    return cfg


def selftest_rig() -> Rig:
    hw = {"playback": {"device": "hw:CARD=Loopback,DEV=0,SUBDEV=1", "format": "S32_LE", "channels": 8},
          "rate_adjust": False}
    return Rig("selftest", cfgmod.LOOPBACK_PLAYER, Capture("hw:CARD=Loopback,DEV=1,SUBDEV=1", 8, "S32_LE"),
               [0, 1, 2], hardware=hw, decorate=_simulate)


class Cancelled(Exception):
    pass


class Session:
    """One measurement session: owns CamillaDSP for its duration and restores it after."""

    def __init__(self, camilla: CamillaClient, config_dir: Path, progress: Callable[[str, float], None],
                 cancel: threading.Event):
        self.cam = camilla
        self.config_dir = Path(config_dir)
        self.progress = progress
        self.cancel = cancel
        self._previous: tuple[str | None, str | None] | None = None

    def _call(self, command, arg=None):
        return asyncio.run(self.cam.call(command, arg))

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def __enter__(self):
        self._previous = (self._call("GetConfigFilePath"), self._call("GetState"))
        return self

    def __exit__(self, *exc):
        """Put back the config that was active before, or stop. Never leave the
        measurement config running: if going back fails in any way, stop."""
        path, state = self._previous or (None, None)
        restored = False
        if path and Path(path).name != MEASURE_CONFIG and Path(path).is_file():
            try:
                self._call("SetConfigFilePath", path)
                if state in ("Running", "Paused", "Stalled", "Starting"):
                    self._call("Reload")
                    restored = True
            except Exception:
                restored = False
        if not restored:
            try:
                self._call("Stop")
            except Exception:  # CamillaDSP gone; nothing left running to stop
                pass
        return False

    def load(self, cfg: dict, timeout: float = 10.0):
        problems = cfgmod.problems(cfg)
        if problems:
            raise cal.MeasurementError("Internal error in the measurement config: " + "; ".join(problems))
        text = cfgmod.to_yaml(cfg)
        self._call("ValidateConfig", text)
        path = self.config_dir / MEASURE_CONFIG
        write_atomic(path, text)
        self._call("SetConfigFilePath", str(path))
        self._call("Reload")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self._call("GetState")
            if state == "Running":
                time.sleep(0.3)  # let the output settle
                return
            self.check()
            time.sleep(0.2)
        reason = self._call("GetStopReason")
        raise cal.MeasurementError(f"CamillaDSP did not start the measurement config (state {state}, {reason}).")

    def sweep(self, rig: Rig, signal: np.ndarray) -> np.ndarray:
        return rig.mic(play_and_record(signal, rig.player, rig.capture, cancel=self.cancel))


def _probe(session: Session, rig: Rig, channels: int, cols: list[int], what: str, **kw) -> dict:
    band = (kw["f1"], kw["f2"]) if "f1" in kw else None
    probe, lead = cal.probe(PROBE_DB, channels=len(cols), **kw)
    played = np.zeros((len(probe), channels), dtype=np.float32)
    played[:, cols] = probe
    rec = session.sweep(rig, played)
    peak, noise = cal.probe_level(rec, probe, lead, band=band, seconds=kw.get("seconds", 1.0))
    if peak > TOO_LOUD_PROBE_DB:
        raise cal.MeasurementError(
            f"The {what} is very loud even at the quiet test level ({peak:.0f} dBFS at the microphone). "
            f"Turn {'the sub level down in its app' if what == 'sub' else 'the Powernode volume down'} "
            "and measure again.")
    level = cal.choose_level(PROBE_DB, peak, noise, goal_peak_db=GOAL_PEAK_DB, ceiling_db=OUTPUT_CEILING_DB)
    return {"probe_peak_db": round(peak, 1), "noise_db": round(noise, 1), "sweep_db": round(level, 1)}


def choose_sweep_level(session: Session, rig: Rig, channels: int) -> dict:
    """Quiet probes, the mains first and then the sub on its own, and a sweep
    level for each from what the microphone heard."""
    mains = _probe(session, rig, channels, [0, 1], "main speakers")
    out = {"probe_db": PROBE_DB, **mains}
    if channels > 2:
        session.check()
        sub = _probe(session, rig, channels, [2], "sub", f1=SUB_PROBE_BAND[0], f2=SUB_PROBE_BAND[1],
                     seconds=SUB_PROBE_SECONDS)
        out["sub"] = sub
        out["sub_sweep_db"] = sub["sweep_db"]
    return out


def measure_bass(session: Session, rig: Rig, mic_curve: np.ndarray | None = None) -> dict:
    """Stage A: left, right and sub raw, in one recording. Returns plan, curves and details."""
    p = session.progress
    p("Loading the measurement setup", 0.05)
    session.load(rig.decorate(cfgmod.measure_raw(hardware=rig.hardware)))
    session.check()
    p("Setting the level", 0.12)
    level = choose_sweep_level(session, rig, 3)
    session.check()
    p("Measuring left, right and sub", 0.25)
    levels = [level["sweep_db"], level["sweep_db"], level.get("sub_sweep_db", level["sweep_db"])]
    played, schedule = cal.excitation(channels=3, repeats=REPEATS, level_db=levels,
                                      bands=[None, None, bass.SUB_SWEEP_HZ])
    rec = session.sweep(rig, played)
    session.check()
    p("Working out the crossover and delays", 0.85)
    responses = bass.complex_responses(rec, played, schedule)
    if mic_curve is not None:
        for r in responses:
            r["h"] = r["h"] / 10 ** (np.asarray(mic_curve) / 20)
    plan = bass.align(responses)
    return {"plan": plan, "level": level, "responses": responses}


def selftest_verdict(plan: dict) -> dict:
    """Did the self-test plan undo the simulated speakers? The left/right delay
    difference is known exactly; the sub alignment must sum well."""
    ch = plan["channels"]
    expected = SIM_DELAYS_MS["right"] - SIM_DELAYS_MS["left"]
    got = ch["left"]["delay_ms"] - ch["right"]["delay_ms"]
    checks = [
        {"what": "Left/right delay", "expected": f"{expected:.2f} ms", "got": f"{got:.2f} ms",
         "ok": abs(got - expected) < 0.05},
        {"what": "Mains + sub summation", "expected": "better than -1 dB", "got": f"{plan['summation_db']:.2f} dB",
         "ok": plan["summation_db"] > -1.0},
        {"what": "Levels", "expected": "within 0.5 dB",
         "got": ", ".join(f"{r} {ch[r]['trim_db']:+.1f}" for r in ("left", "right", "sub")),
         "ok": all(abs(ch[r]["trim_db"]) < 0.5 for r in ("left", "right", "sub"))},
    ]
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def curves(responses: list[dict], plan: dict, points: int = 160) -> dict:
    """Smoothed dB curves for the UI, normalised to the mains' midband level."""
    idx = np.unique(np.linspace(0, len(cal.GRID) - 1, points).astype(int))
    mid = (cal.GRID >= 300) & (cal.GRID <= 3000)
    ref = np.mean([np.median(bass.db(responses[i]["h"])[mid]) for i in (bass.LEFT, bass.RIGHT)])

    def c(h):
        return np.round(cal.smooth(bass.db(h), 6)[idx] - ref, 2).tolist()

    sub_idx = idx[cal.GRID[idx] <= bass.SUB_SWEEP_HZ[1]]  # the sub is only swept this far
    sub = cal.smooth(bass.db(responses[bass.SUB]["h"]), 6)
    return {
        "freq": np.round(cal.GRID[idx], 1).tolist(),
        "left": c(responses[bass.LEFT]["h"]),
        "right": c(responses[bass.RIGHT]["h"]),
        "sub_freq": np.round(cal.GRID[sub_idx], 1).tolist(),
        "sub": np.round(sub[sub_idx] - ref, 2).tolist(),
        "sum_left": c(bass.predicted_sum(responses, plan, "left")),
        "sum_right": c(bass.predicted_sum(responses, plan, "right")),
    }


def measure_side(session: Session, rig: Rig, plan: dict, side: str, level_db: float,
                 mic_curve: np.ndarray | None = None, fir: dict | None = None, preamp_db: float = 0.0) -> dict:
    """Stage B: one main speaker plus the sub, through the planned crossover and
    delays (and, for stage C, the room correction FIRs and their preamp).

    Returns calibration.analyze()'s result for that side (one channel).
    """
    session.load(rig.decorate(cfgmod.build(plan, fir=fir, preamp_db=preamp_db, hardware=rig.hardware,
                                           measure_side=side)))
    session.check()
    one, schedule = cal.excitation(channels=1, repeats=REPEATS, level_db=level_db)
    played = np.zeros((len(one), 2), dtype=np.float32)
    played[:, 0 if side == "left" else 1] = one[:, 0]
    rec = session.sweep(rig, played)
    session.check()
    try:
        result = cal.analyze(rec, one, schedule)
    except cal.MeasurementError as e:
        raise cal.MeasurementError(f"{side.capitalize()} speaker and sub: {e}") from e
    if mic_curve is not None:
        cal.apply_mic_calibration(result, mic_curve)
    return result


def measure_position(session: Session, rig: Rig, plan: dict, level_db: float,
                     mic_curve: np.ndarray | None = None, fir: dict | None = None,
                     preamp_db: float = 0.0) -> dict:
    """Stage B, one microphone position: left+sub, then right+sub. With fir
    (and its preamp), stage C: the same through the room correction."""
    p = session.progress
    out = {"channels": [], "warnings": []}
    for i, side in enumerate(("left", "right")):
        p(f"Measuring {side} speaker and sub", 0.1 + 0.42 * i)
        r = measure_side(session, rig, plan, side, level_db, mic_curve, fir=fir, preamp_db=preamp_db)
        out["channels"].append(r["channels"][0])
        out["warnings"] += [w.replace("Channel 1", side.capitalize()) for w in r["warnings"]]
        out[f"{side}_peak_dbfs"] = r["peak_dbfs"]
    return out
