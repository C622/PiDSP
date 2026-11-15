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
MAX_BASS_LIFT_DB = 10.0
# Level differences in the sub's range that the listener is told about: the
# sub turned up or down on the sub itself since it was measured, or a lift
# the correction could not give.
LEVEL_WARN_DB = 3.0
MIN_POSITIONS = 1
RECOMMENDED_POSITIONS = 5
MAX_POSITIONS = 8


def deviation_db(curve: np.ndarray, target: np.ndarray, lo: float = 40.0, hi: float = 16000.0) -> float:
    """How far a smoothed curve strays from the target in lo..hi: the 90th
    percentile of the absolute difference after matching levels at 300-3000 Hz."""
    band = (cal.GRID >= lo) & (cal.GRID <= hi)
    mid = (cal.GRID >= 300) & (cal.GRID <= 3000)
    d = np.asarray(curve) - np.asarray(target)
    d = d - np.mean(d[mid])
    return float(np.percentile(np.abs(d[band]), 90))


def _sub_band(plan: dict) -> np.ndarray:
    return (cal.GRID >= 30) & (cal.GRID <= plan["crossover_hz"] / 1.4)


MID = (cal.GRID >= 300) & (cal.GRID <= 3000)


def bass_lift(channels: list[dict], goal: np.ndarray, plan: dict) -> dict:
    """The sub gain change (dB) that brings the sub's range, as a whole, to the
    target: the median shortfall from 30 Hz to just below the crossover, after
    matching levels at 300-3000 Hz, averaged over both sides. The median
    ignores narrow room nulls and peaks; those are the FIR's job.

    Returns {"lift_db": what is applied, "wanted_db": what the target asks
    for, "limited_by": None, "cap" or "headroom"}."""
    band = _sub_band(plan)
    short = []
    for ch in channels:
        d = goal - cal.smooth(ch["mean"], 3)
        short.append(float(np.median((d - np.mean(d[MID]))[band])))
    wanted = float(np.mean(short))
    lift, limited_by = float(np.clip(wanted, -MAX_BASS_LIFT_DB, MAX_BASS_LIFT_DB)), None
    if lift != wanted:
        limited_by = "cap"
    # Never raise the sub above the level of its input (no clipping).
    headroom = -plan["channels"]["sub"].get("trim_db", 0.0)
    if lift > headroom:
        lift, limited_by = headroom, "headroom"
    return {"lift_db": round(lift, 1), "wanted_db": round(wanted, 1), "limited_by": limited_by}


def lift_warning(lift: dict, target: str) -> str | None:
    """Tell the listener when the sub could not be brought to the target."""
    short = lift["wanted_db"] - lift["lift_db"]
    if lift["limited_by"] is None or abs(short) < 1.0:
        return None
    if short > 0:
        why = ("as far as PiDSP can without the sub clipping" if lift["limited_by"] == "headroom"
               else f"by {lift['lift_db']:.0f} dB, the most the correction allows")
        return (f"The sub is too quiet for the {target.capitalize()} target: it needs {lift['wanted_db']:.0f} dB more. "
                f"PiDSP raised it {why}, so the bass stays about {short:.0f} dB under the target. "
                f"Turn the sub up about {round(short):.0f} dB in its own app (or on the sub), "
                "then measure the bass setup and the positions again.")
    return (f"The sub is too loud for the {target.capitalize()} target: it needs {-lift['wanted_db']:.0f} dB less, "
            f"and PiDSP turned it down {-lift['lift_db']:.0f} dB. Turn the sub down about {round(-short):.0f} dB "
            "in its own app (or on the sub), then measure the bass setup and the positions again.")


def bass_change_db(position: dict, responses: list[dict], plan: dict) -> float:
    """How much louder (+) or quieter (-) the sub's range measured at a
    position than the bass setup measurement predicts for that seat, relative
    to the midrange; the mean of both sides. Only meaningful at the seat the
    bass setup was measured from (position 1, the main seat). A large value
    means the sub's own level was changed after the bass setup was measured."""
    band, out = _sub_band(plan), []
    for i, side in enumerate(SIDES):
        d = _curve(position["channels"][i]) - bassmod.db(bassmod.predicted_sum(responses, plan, side))
        out.append(float(np.median(d[band]) - np.median(d[MID])))
    return round(float(np.mean(out)), 1)


def bass_change_warning(change_db: float, where: str) -> str | None:
    if abs(change_db) < LEVEL_WARN_DB:
        return None
    word = "quieter" if change_db < 0 else "louder"
    return (f"{where} the bass is {abs(change_db):.0f} dB {word} than when the bass setup was measured. "
            "Was the sub's own level changed since then (in its app or on the sub)? If so, set it back, or "
            "measure the bass setup again, and then this measurement.")


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
    new_plan, warnings, wanted = None, [], None
    if plan is not None:
        info = bass_lift(channels, goal, plan)
        lift, wanted = info["lift_db"], info["wanted_db"]
        lifted = lift_curve(plan, lift)
        w = lift_warning(info, target)
        if w:
            warnings.append(w)
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
            "firs": firs, "sides": sides, "target_curve": goal, "bass_lift_db": lift, "bass_lift_wanted_db": wanted, "plan": new_plan,
            "warnings": warnings}


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
          crossover_hz: float | None = None, points: int = 200) -> dict:
    """Stage C: the measurement through the room correction, against the target.

    after / before: position measurements ({"channels": [left, right]}) at the
    same seat, with and without the correction. predicted: the build's
    curves (curves_for_ui()), for reference; with crossover_hz and before,
    the bass level is compared with what the seat should measure now (bass_vs_predicted_db), which
    catches a sub whose own level was changed after it was measured.
    Returns deviations, curves (every curve level-matched to the target at
    300-3000 Hz) and warnings.
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
            # The seat before, plus what the build changes (sub level and FIR):
            # what this seat should measure now.
            pred, meas = np.asarray(predicted[f"{side}_predicted"]), np.asarray(predicted.get(f"{side}_measured", []))
            if crossover_hz and before and len(pred) == len(meas) == len(idx):
                f = cal.GRID[idx]
                d = a[idx] - (b[idx] + pred - meas)
                sd["bass_vs_predicted_db"] = round(float(
                    np.median(d[(f >= 30) & (f <= crossover_hz / 1.4)]) - np.median(d[(f >= 300) & (f <= 3000)])), 1)
        out["sides"][side] = sd
    changes = [s["bass_vs_predicted_db"] for s in out["sides"].values() if "bass_vs_predicted_db" in s]
    w = bass_change_warning(float(np.mean(changes)), "At the main seat") if changes else None
    out["warnings"] = [w] if w else []
    return out
