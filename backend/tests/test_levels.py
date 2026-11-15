"""Catching levels that are off: a sub too quiet for the target, and a sub
whose own level was changed after calibrating."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pidsp.dsp import calibration as cal, room
from pidsp.measure import runner
from pidsp.measure.jobs import JobRunner
from pidsp.store import Store
from pidsp.web import app as webapp

from test_measure import FakeCamilla, _install_room, rig, wait_job, fake_room  # noqa: F401
from test_web import tree  # noqa: F401  (fixture)

PLAN = {"crossover_hz": 80, "crossover_order": 4,
        "channels": {"left": {"delay_ms": 0, "trim_db": 0, "inverted": False},
                     "right": {"delay_ms": 0, "trim_db": 0, "inverted": False},
                     "sub": {"delay_ms": 0, "trim_db": -15.0, "inverted": False}}}


def channel(short_db, plan=PLAN):
    """Both sides measuring the Room target with the sub short_db too quiet."""
    curve = cal.target_curve("room") + room.lift_curve(plan, -short_db)
    ch = {"repeats": [curve, curve], "snr": np.full(len(cal.GRID), 40.0), "arrivals": [0.0, 0.0]}
    return [{"channels": [ch, ch]}]


def test_a_sub_far_too_quiet_for_the_target_is_reported():
    r = room.build(channel(16.0), "room", plan=PLAN)
    assert r["bass_lift_db"] == room.MAX_BASS_LIFT_DB and r["bass_lift_wanted_db"] > 12
    assert len(r["warnings"]) == 1 and "too quiet for the Room target" in r["warnings"][0]
    assert f"Turn the sub up about {round(r['bass_lift_wanted_db'] - 10)} dB" in r["warnings"][0]
    # Within reach: corrected silently.
    r = room.build(channel(7.5), "room", plan=PLAN)
    assert 6.0 < r["bass_lift_db"] < 8.0 and r["warnings"] == []


def test_a_lift_limited_by_headroom_says_so():
    tight = {**PLAN, "channels": {**PLAN["channels"], "sub": {**PLAN["channels"]["sub"], "trim_db": -2.0}}}
    r = room.build(channel(7.0, tight), "room", plan=tight)
    assert r["bass_lift_db"] == 2.0 and "without the sub clipping" in r["warnings"][0]


def test_a_sub_far_too_loud_is_reported_too():
    r = room.build(channel(-18.0), "room", plan=PLAN)
    assert r["bass_lift_db"] == -room.MAX_BASS_LIFT_DB and "too loud" in r["warnings"][0]


def test_position_1_compared_with_the_bass_setup_measurement():
    """Position 1 (the main seat) should measure what the bass setup
    measurement predicts through the plan; a sub turned down on the sub
    itself in between shows as a shortfall in the sub's range."""
    from pidsp.dsp import bass
    from test_bass import simulate
    played, schedule = cal.excitation(channels=3, repeats=2, level_db=-12)
    responses = bass.complex_responses(simulate(played), played, schedule)
    plan = bass.align(responses)

    def position(sub_change_db):
        chans = []
        for side in ("left", "right"):
            curve = bass.db(bass.predicted_sum(responses, plan, side)) + room.lift_curve(plan, sub_change_db)
            chans.append({"repeats": [curve, curve]})
        return {"channels": chans}
    assert abs(room.bass_change_db(position(0.0), responses, plan)) < 0.5
    assert room.bass_change_db(position(-6.0), responses, plan) < -4


def test_bass_change_warning():
    assert room.bass_change_warning(-2.0, "At the main seat") is None
    w = room.bass_change_warning(-6.2, "At the main seat")
    assert "6 dB quieter" in w and "own level" in w


CAL = {"probe_peak_db": -52.4, "sub": {"probe_peak_db": -52.9}}


def now(mains, sub):
    return {"probe_peak_db": mains, "sub": {"probe_peak_db": sub}}


def test_compare_levels():
    assert runner.compare_levels(now(-52.0, -53.5), CAL, 0.0)["verdict"] == "ok"
    r = runner.compare_levels(now(-52.4, -58.9), CAL, 0.0)
    assert r["verdict"] == "sub_quieter" and r["sub_change_db"] == -6.0 and "raise PiDSP's sub level by 6 dB" in r["message"]
    assert runner.compare_levels(now(-52.4, -48.0), CAL, None)["verdict"] == "sub_louder"
    # The amplifier 5 dB louder than at calibration: the mains follow it, nothing changed.
    assert runner.compare_levels(now(-47.4, -52.9), CAL, 5.0)["verdict"] == "ok"
    # The mains moved but the volume did not: the microphone is somewhere else.
    r = runner.compare_levels(now(-58.0, -59.0), CAL, 0.0)
    assert r["verdict"] == "moved" and r["sub_vs_mains_db"] == pytest.approx(-0.5)
    assert runner.compare_levels(now(-47.0, -53.0), CAL, None)["verdict"] == "unsure"


def app(tree, tmp_path):  # noqa: F811
    proc, sys = tree
    st = Store(tmp_path / "state", tmp_path / "camilla")
    return TestClient(webapp.create_app(camilla=FakeCamilla(), store=st, jobs=JobRunner(), proc=proc, sys=sys,
                                        rigs={"room": lambda d: rig(), "selftest": lambda d: rig()}))


def test_a_sub_turned_down_after_calibrating_is_caught(tree, tmp_path, monkeypatch):  # noqa: F811
    _install_room(monkeypatch)
    c = app(tree, tmp_path)
    assert c.post("/api/measure/levelcheck", json={}).status_code == 409  # nothing to compare with yet
    assert c.post("/api/measure/bass", json={}).status_code == 202
    assert wait_job(c)["state"] == "done"
    # Unchanged: the level check and position 1 agree with the calibration.
    assert c.post("/api/measure/levelcheck", json={}).status_code == 202
    assert wait_job(c)["state"] == "done"
    r = c.get("/api/levelcheck").json()["result"]
    assert r["verdict"] == "ok" and abs(r["sub_change_db"]) < 1.5, r
    # Now the sub is turned down 6 dB on the sub itself.
    _install_room(monkeypatch, sub_gain_db=-9.0)
    assert c.post("/api/measure/levelcheck", json={}).status_code == 202
    assert wait_job(c)["state"] == "done"
    r = c.get("/api/levelcheck").json()["result"]
    assert r["verdict"] == "sub_quieter" and r["sub_change_db"] == pytest.approx(-6.0, abs=1.5), r
    # Position 1 (the main seat) is compared with the bass setup measurement;
    # position 2 is somewhere else, where the bass may differ: not compared.
    for pos in (1, 2):
        assert c.post("/api/measure/room", json={"position": pos}).status_code == 202
        assert wait_job(c)["state"] == "done"
    positions = {p["index"]: p for p in Store(tmp_path / "state", tmp_path / "camilla").positions()}
    assert "bass_change_db" in positions[1] and "bass_change_db" not in positions[2]


def test_the_check_catches_a_sub_changed_after_the_build():
    """Stage C compares the bass at the main seat with what position 1 plus
    the build's changes predict: a sub turned down since shows."""
    positions = channel(0.0)
    b = room.build(positions, "room", plan=PLAN)
    predicted = room.curves_for_ui(b)
    unchanged = room.check(positions[0], "room", before=positions[0], predicted=predicted, crossover_hz=80)
    assert all(abs(sd["bass_vs_predicted_db"]) < 1.0 for sd in unchanged["sides"].values())
    assert unchanged["warnings"] == []
    quieter = channel(6.0)[0]
    chk = room.check(quieter, "room", before=positions[0], predicted=predicted, crossover_hz=80)
    assert all(sd["bass_vs_predicted_db"] < -3.5 for sd in chk["sides"].values()), chk["sides"]
    assert "quieter than when the bass setup was measured" in chk["warnings"][0]
    # Without position 1 to compare with, nothing is claimed.
    assert room.check(quieter, "room", predicted=predicted, crossover_hz=80)["warnings"] == []
