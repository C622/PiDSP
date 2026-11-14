"""Tests for pidsp.dsp.bass against a simulated 2.1 system in a room."""

import numpy as np
import pytest
from scipy import signal

from pidsp.dsp import bass
from pidsp.dsp import calibration as cal

FS = cal.FS


def sos(kind, freq, gain=0.0, q=0.707):
    b, a = bass.biquad(kind, freq, gain, q)
    return np.concatenate([b, a]) / a[0]


# Ported bookshelf/LCR mains: 4th-order roll-off around 50 Hz, a room mode.
MAINS = [sos("Highpass", 50, q=0.707), sos("Highpass", 50, q=0.707), sos("Peaking", 140, 4, 3.0)]
# Subwoofer: its own low-pass and DSP, an infrasonic high-pass, a room mode.
SUB = [sos("Lowpass", 140, q=0.707), sos("Lowpass", 140, q=0.707), sos("Highpass", 18, q=0.707),
       sos("Peaking", 45, 5, 4.0)]


def simulate(played, delays_ms=(2.0, 2.6, 14.0), sub_gain_db=-3.0, sub_inverted=False,
             latency=3210, noise_db=-80, seed=3):
    rng = np.random.default_rng(seed)
    out = np.zeros(len(played) + latency + FS)
    for ch in range(3):
        filt = np.array(SUB if ch == bass.SUB else MAINS)
        y = signal.sosfilt(filt, played[:, ch].astype(np.float64)) * 10 ** (-6 / 20)
        if ch == bass.SUB:
            y *= 10 ** (sub_gain_db / 20) * (-1 if sub_inverted else 1)
        d = latency + int(round(delays_ms[ch] * FS / 1000))
        out[d:d + len(y)] += y
        r = d + int(0.004 * FS)  # a nearby wall
        out[r:r + len(y)] += 0.3 * y
    out += rng.normal(0, 10 ** (noise_db / 20), len(out))
    return out


@pytest.fixture(scope="module")
def excitation():
    return cal.excitation(channels=3, repeats=2, level_db=-12)


@pytest.fixture(scope="module")
def measured(excitation):
    played, schedule = excitation
    return bass.complex_responses(simulate(played), played, schedule)


def test_lr4_sums_flat():
    total = bass.lr_response("highpass", 80) + bass.lr_response("lowpass", 80)
    assert np.allclose(np.abs(total), 1.0, atol=1e-6)
    at_fc = np.argmin(np.abs(cal.GRID - 80))
    assert bass.db(bass.lr_response("lowpass", 80))[at_fc] == pytest.approx(-6.0, abs=0.2)


def test_choose_crossover():
    assert bass.choose_crossover(40) == 80
    assert bass.choose_crossover(25) == 60
    assert bass.choose_crossover(70) == 120
    # A sub 6 dB down at 110 Hz keeps the crossover at or below 80 Hz.
    assert bass.choose_crossover(50, sub_f6_high=110) == 80


def test_relative_arrivals_survive(measured):
    left, right = measured[bass.LEFT]["arrival"], measured[bass.RIGHT]["arrival"]
    assert (right - left) * 1000 == pytest.approx(0.6, abs=0.05)


def test_mains_extension_found(measured):
    f6 = bass.low_extension(measured[bass.LEFT]["h"])
    # Two Butterworth high-passes at 50 Hz are -6 dB at 50 Hz; the nearby wall
    # and the room mode move that a little.
    assert 40 <= f6 <= 62


def test_plan_aligns_and_sums(measured):
    plan = bass.align(measured)
    ch = plan["channels"]
    # The chosen crossover is the best scoring of those the speakers allow.
    scored = [o for o in plan["crossover_options"] if "score" in o]
    best = min(o["score"] for o in scored)
    chosen = [o for o in plan["crossover_options"] if o["chosen"]]
    assert len(chosen) == 1 and chosen[0]["crossover_hz"] == plan["crossover_hz"]
    assert chosen[0]["score"] <= best + bass.SCORE_TIE_DB
    assert 60 <= plan["crossover_hz"] <= 120
    assert ch["left"]["delay_ms"] - ch["right"]["delay_ms"] == pytest.approx(0.6, abs=0.05)
    assert min(c["delay_ms"] for c in ch.values()) == 0.0
    assert plan["summation_db"] > -1.0
    # Through the crossover band, main + sub stays within a few dB of the
    # main's own midband level: no hole where they hand over.
    fc = plan["crossover_hz"]
    band = (cal.GRID >= fc / 2) & (cal.GRID <= fc * 2)
    total = bass.db(bass.predicted_sum(measured, plan, "left"))
    mid = (cal.GRID >= 300) & (cal.GRID <= 3000)
    ref = bass.db(measured[bass.LEFT]["h"])[mid].mean() + ch["left"]["trim_db"]
    assert np.min(cal.smooth(total, 3)[band]) > ref - 6


def test_sub_delay_tracks_extra_latency(excitation):
    played, schedule = excitation
    a = bass.align(bass.complex_responses(simulate(played), played, schedule), fc=80)
    b = bass.align(bass.complex_responses(simulate(played, delays_ms=(2.0, 2.6, 19.0)), played, schedule), fc=80)

    def rel(p):  # how much later the sub is played than the left main
        return p["channels"]["sub"]["delay_ms"] - p["channels"]["left"]["delay_ms"]

    assert rel(a) - rel(b) == pytest.approx(5.0, abs=0.3)
    assert a["channels"]["sub"]["inverted"] == b["channels"]["sub"]["inverted"]


def test_inverted_sub_detected(excitation):
    played, schedule = excitation
    a = bass.align(bass.complex_responses(simulate(played), played, schedule), fc=80)
    b = bass.align(bass.complex_responses(simulate(played, sub_inverted=True), played, schedule), fc=80)
    assert a["channels"]["sub"]["inverted"] != b["channels"]["sub"]["inverted"]
    assert a["channels"]["sub"]["delay_ms"] == pytest.approx(b["channels"]["sub"]["delay_ms"], abs=0.1)
    assert a["channels"]["left"]["delay_ms"] == pytest.approx(b["channels"]["left"]["delay_ms"], abs=0.1)


def test_sub_level_matched(measured):
    plan = bass.align(measured)
    # The sim plays the sub 3 dB quieter and its room mode adds a little;
    # the trim brings it back within a couple of dB of the mains.
    assert -2.0 <= plan["channels"]["sub"]["trim_db"] <= 5.0
    assert not plan["warnings"]


def test_silent_sub_refused(excitation):
    played, schedule = excitation
    rec = simulate(played, sub_gain_db=-200)
    with pytest.raises(cal.MeasurementError, match="sub"):
        bass.complex_responses(rec, played, schedule)


def test_crossover_candidates_respect_the_speakers(measured):
    plan = bass.align(measured)
    for o in plan["crossover_options"]:
        f = o["crossover_hz"]
        if f < bass.MAINS_MIN_RATIO * plan["mains_f6_hz"] - 0.5 or f > plan["sub_f6_hz"] / bass.SUB_MAX_RATIO + 0.5:
            assert "excluded" in o and "score" not in o
        else:
            assert {"score", "bumpiness_db", "summation_db", "strain_db"} <= o.keys()


def test_a_crossover_that_cancels_scores_worse(measured):
    """Forcing the wrong polarity at a crossover makes a hole; the score must see it."""
    plan = bass.align(measured, fc=80)
    flipped = {**plan, "channels": {**plan["channels"], "sub": {**plan["channels"]["sub"],
               "inverted": not plan["channels"]["sub"]["inverted"]}}}
    good = np.mean([bass.bumpiness_db(bass.predicted_sum(measured, plan, s)) for s in ("left", "right")])
    bad = np.mean([bass.bumpiness_db(bass.predicted_sum(measured, flipped, s)) for s in ("left", "right")])
    assert bad > good + 0.5


def test_band_limited_sub_sweep_gives_the_same_plan(excitation):
    played, schedule = excitation
    full = bass.align(bass.complex_responses(simulate(played), played, schedule))
    # A louder, bass-only sub sweep must not move the timing.
    p2, s2 = cal.excitation(channels=3, repeats=2, level_db=[-18, -18, -6], bands=[None, None, bass.SUB_SWEEP_HZ])
    lim = bass.align(bass.complex_responses(simulate(p2), p2, s2))
    assert lim["crossover_hz"] == full["crossover_hz"]
    for ch in ("left", "right", "sub"):
        assert lim["channels"][ch]["delay_ms"] == pytest.approx(full["channels"][ch]["delay_ms"], abs=0.1)
        assert lim["channels"][ch]["inverted"] == full["channels"][ch]["inverted"]
    assert lim["channels"]["sub"]["trim_db"] == pytest.approx(full["channels"]["sub"]["trim_db"], abs=0.5)


def test_localisation_penalty_grows_above_80_hz(measured):
    opts = {o["crossover_hz"]: o for o in bass.align(measured)["crossover_options"] if "score" in o}
    assert all(o["localisation_db"] == 0 for f, o in opts.items() if f <= 80)
    highs = [opts[f]["localisation_db"] for f in sorted(opts) if f > 80]
    assert highs == sorted(highs) and all(v > 0 for v in highs)


def test_listener_can_prefer_an_allowed_crossover(measured):
    auto = bass.align(measured)
    allowed = [o["crossover_hz"] for o in auto["crossover_options"] if "score" in o]
    other = next(f for f in allowed if f != auto["crossover_hz"])
    mine = bass.align(measured, prefer=other)
    assert mine["crossover_hz"] == other and mine["crossover_choice"] == "listener"
    assert [o["chosen"] for o in mine["crossover_options"]].count(True) == 1
    assert bass.align(measured, prefer=55)["crossover_hz"] == auto["crossover_hz"]  # not offered: ignored


def test_sub_timing_is_consistent_across_crossovers(measured):
    """The sim's sub is 12 ms behind the mains. Whatever the crossover, the
    plan must line its bass up with the mains, not jump by a period (in a room,
    group delay at one frequency jumps around; the expected delay does not)."""
    rel = []
    for fc in (80, 90, 100, 110, 120):
        ch = bass.align(measured, fc=fc)["channels"]
        rel.append(ch["sub"]["delay_ms"] - ch["left"]["delay_ms"])
    # Polarity + half a period sums almost the same near fc, so allow up to
    # half a period at the highest crossover (4.2 ms at 120 Hz), never a whole one.
    assert max(rel) - min(rel) < 1000 / 120 / 2 + 0.3
    assert all(-19 < r < -8 for r in rel)


def test_envelope_lag_finds_a_known_delay():
    # The same band-limited response, 7 ms apart: the lag must be -7 ms
    # (mains earlier), whatever the polarity and level of the later one.
    h = bass.lr_response("lowpass", 120) * bass.lr_response("highpass", 30)
    late = bass._delayed(h, 7.0)
    assert bass.envelope_lag_ms(h, late) == pytest.approx(-7.0, abs=0.3)
    assert bass.envelope_lag_ms(h, -0.2 * late) == pytest.approx(-7.0, abs=0.3)
    assert bass.envelope_lag_ms(late, h) == pytest.approx(7.0, abs=0.3)

