"""Stage B: room correction FIRs from left+sub and right+sub measurements.

Each microphone position is measured through the bass plan, one side at a
time, with the sub fed from that side at full level (as mono bass reaches it
in normal use). The positions are averaged per side, and each side gets a
minimum-phase FIR (no added latency) that brings the average towards the
target. The FIR sits before the 2.1 split, so it corrects the main speaker
and the sub's share of that side together.
"""

from __future__ import annotations

import numpy as np

from . import calibration as cal

TARGETS = ("flat", "warm", "room")
SIDES = ("left", "right")
FIR_TAPS = 8192
MIN_POSITIONS = 1
RECOMMENDED_POSITIONS = 5


def deviation_db(curve: np.ndarray, target: np.ndarray, lo: float = 40.0, hi: float = 16000.0) -> float:
    """How far a smoothed curve strays from the target in lo..hi: the 90th
    percentile of the absolute difference after matching levels at 300-3000 Hz."""
    band = (cal.GRID >= lo) & (cal.GRID <= hi)
    mid = (cal.GRID >= 300) & (cal.GRID <= 3000)
    d = np.asarray(curve) - np.asarray(target)
    d = d - np.mean(d[mid])
    return float(np.percentile(np.abs(d[band]), 90))


def build(positions: list[dict], target: str = "room", taps: int = FIR_TAPS) -> dict:
    """Correction filters from position measurements.

    positions: one dict per microphone position with "channels" [left+sub, right+sub],
    each as calibration.analyze() returns a channel.
    Returns {"target", "preamp_db", "firs": {side: float32 taps}, "sides": {side: {...}}}
    where each side has the knee, deviation before/after and dB curves on GRID.
    """
    if len(positions) < MIN_POSITIONS:
        raise cal.MeasurementError("Measure at least one microphone position first.")
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    channels = cal.combine(positions)
    goal = cal.target_curve(target)
    firs, sides, corrections = {}, {}, []
    for side, ch in zip(SIDES, channels):
        fir, knee, corr = cal.fir_correction(ch, goal, taps=taps)
        measured = cal.smooth(ch["mean"], 6)
        predicted = measured + corr
        firs[side] = fir
        corrections.append(corr)
        sides[side] = {
            "knee_hz": round(float(knee), 1),
            "deviation_before_db": round(deviation_db(measured, goal), 2),
            "deviation_after_db": round(deviation_db(predicted, goal), 2),
            "measured": measured, "correction": corr, "predicted": predicted,
        }
    return {"target": target, "positions": len(positions), "preamp_db": cal.preamp_for(corrections),
            "firs": firs, "sides": sides, "target_curve": goal}


def curves_for_ui(result: dict, points: int = 200) -> dict:
    """Downsampled curves, levels matched to the target at 300-3000 Hz."""
    idx = np.unique(np.linspace(0, len(cal.GRID) - 1, points).astype(int))
    mid = (cal.GRID >= 300) & (cal.GRID <= 3000)
    goal = result["target_curve"]
    out = {"freq": np.round(cal.GRID[idx], 1).tolist(), "target": np.round(goal[idx], 2).tolist()}
    for side, s in result["sides"].items():
        for key in ("measured", "predicted"):
            c = s[key] - np.mean((s[key] - goal)[mid])
            out[f"{side}_{key}"] = np.round(c[idx], 2).tolist()
    return out
