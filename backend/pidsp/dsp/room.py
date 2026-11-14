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

import copy

from . import bass as bassmod
from . import calibration as cal

TARGETS = ("flat", "warm", "room")
SIDES = ("left", "right")
FIR_TAPS = 8192
# With the sub, the system plays to about 20 Hz: only a shortfall below this
# is the bottom of the system; dips above it are room modes (see
# calibration.magnitude_correction).
BASS_SEARCH_HZ = 40.0
# The sub's level is part of the correction: a shortfall (or excess) across
# the sub's whole range is tonal balance, not a dip, so it is fixed with the
# sub's gain (capped here) instead of an FIR boost.
MAX_BASS_LIFT_DB = 6.0
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


def bass_lift(channels: list[dict], goal: np.ndarray, plan: dict) -> float:
    """The sub gain change (dB) that brings the sub's range, as a whole, to the
    target: the median shortfall from 30 Hz to just below the crossover, after
    matching levels at 300-3000 Hz, averaged over both sides. The median
    ignores narrow room nulls and peaks; those are the FIR's job."""
    fc = plan["crossover_hz"]
    band = (cal.GRID >= 30) & (cal.GRID <= fc / 1.4)
    mid = (cal.GRID >= 300) & (cal.GRID <= 3000)
    short = []
    for ch in channels:
        m = cal.smooth(ch["mean"], 3)
        d = goal - m
        short.append(float(np.median((d - np.mean(d[mid]))[band])))
    lift = float(np.clip(np.mean(short), -MAX_BASS_LIFT_DB, MAX_BASS_LIFT_DB))
    # Never raise the sub above the level of its input (no clipping).
    lift = min(lift, -plan["channels"]["sub"].get("trim_db", 0.0))
    return round(lift, 1)


def lift_curve(plan: dict, lift_db: float) -> np.ndarray:
    """How a sub gain change shows in "main + sub" (dB on GRID). The LR4
    outputs are in phase, so |HP + g*LP| = |HP| + g*|LP| exactly."""
    fc = plan["crossover_hz"]
    hp = np.abs(bassmod.lr_response("highpass", fc))
    lp = np.abs(bassmod.lr_response("lowpass", fc))
    return 20 * np.log10(hp + 10 ** (lift_db / 20) * lp) - 20 * np.log10(hp + lp)


def build(positions: list[dict], target: str = "room", taps: int = FIR_TAPS, plan: dict | None = None) -> dict:
    """Correction filters from position measurements.

    positions: one dict per microphone position with "channels" [left+sub, right+sub],
    each as calibration.analyze() returns a channel.
    plan: the bass plan they were measured through. With it, the sub's level
    is corrected too (bass_lift()), and the result carries the adjusted plan.
    Returns {"target", "preamp_db", "firs": {side: float32 taps}, "sides": {side: {...}},
    "bass_lift_db", "plan"} where each side has the knee, deviation before/after
    and dB curves on GRID.
    """
    if len(positions) < MIN_POSITIONS:
        raise cal.MeasurementError("Measure at least one microphone position first.")
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    channels = cal.combine(positions)
    goal = cal.target_curve(target)
    lift, lifted = 0.0, np.zeros_like(cal.GRID)
    new_plan = None
    if plan is not None:
        lift = bass_lift(channels, goal, plan)
        lifted = lift_curve(plan, lift)
        new_plan = copy.deepcopy(plan)
        sub = new_plan["channels"]["sub"]
        sub["trim_db"] = round(sub.get("trim_db", 0.0) + lift, 2)
    firs, sides, corrections = {}, {}, []
    for side, ch in zip(SIDES, channels):
        fir, knee, corr = cal.fir_correction({**ch, "mean": ch["mean"] + lifted}, goal, taps=taps,
                                             bass_search_hz=BASS_SEARCH_HZ)
        measured = cal.smooth(ch["mean"], 6)
        predicted = measured + lifted + corr
        firs[side] = fir
        corrections.append(corr)
        sides[side] = {
            "knee_hz": round(float(knee), 1),
            "deviation_before_db": round(deviation_db(measured, goal), 2),
            "deviation_after_db": round(deviation_db(predicted, goal), 2),
            "measured": measured, "correction": corr, "predicted": predicted,
        }
    return {"target": target, "positions": len(positions), "preamp_db": cal.preamp_for(corrections),
            "firs": firs, "sides": sides, "target_curve": goal, "bass_lift_db": lift, "plan": new_plan}


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


def _curve(channel: dict) -> np.ndarray:
    """One position's channel (as analyze() returns it), power-averaged over
    its repeats and smoothed like the build's curves."""
    reps = np.array(channel["repeats"])
    return cal.smooth(10 * np.log10(np.mean(10 ** (reps / 10), axis=0)), 6)


def check(after: dict, target: str, before: dict | None = None, predicted: dict | None = None,
          points: int = 200) -> dict:
    """Stage C: the measurement through the room correction, against the target.

    after / before: position measurements ({"channels": [left, right]}) at the
    same seat, with and without the correction. predicted: the build's
    curves (curves_for_ui()), for reference. Returns deviations and curves,
    every curve level-matched to the target at 300-3000 Hz.
    """
    goal = cal.target_curve(target)
    mid = (cal.GRID >= 300) & (cal.GRID <= 3000)
    idx = np.unique(np.linspace(0, len(cal.GRID) - 1, points).astype(int))
    out = {"target": target, "sides": {},
           "curves": {"freq": np.round(cal.GRID[idx], 1).tolist(), "target": np.round(goal[idx], 2).tolist()}}
    for i, side in enumerate(SIDES):
        a = _curve(after["channels"][i])
        a = a - np.mean((a - goal)[mid])
        sd = {"deviation_after_db": round(deviation_db(a, goal), 2)}
        out["curves"][f"{side}_after"] = np.round(a[idx], 2).tolist()
        if before:
            b = _curve(before["channels"][i])
            b = b - np.mean((b - goal)[mid])
            sd["deviation_before_db"] = round(deviation_db(b, goal), 2)
            out["curves"][f"{side}_before"] = np.round(b[idx], 2).tolist()
        if predicted and f"{side}_predicted" in predicted:
            out["curves"][f"{side}_predicted"] = predicted[f"{side}_predicted"]
        out["sides"][side] = sd
    return out
