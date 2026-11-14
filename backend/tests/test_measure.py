"""Measurement plumbing without hardware: audio decoding, the CamillaDSP session,
the runner (with aplay/arecord replaced by a simulated room), the store and the API."""

import threading
import time

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from pidsp.camilla import config as cfgmod
from pidsp.dsp.calibration import MeasurementError
from pidsp.measure import audio, runner
from pidsp.measure.jobs import JobRunner
from pidsp.store import Store
from pidsp.web import app as webapp

from test_bass import simulate
from test_web import tree  # noqa: F401  (fixture)

UMIK_FILE = '"Sens Factor =-1.38dB, AGain =18dB, SERNO: 7101234"\n' + "".join(
    f"{f:.3f}\t{0.5 * np.sin(i / 7):.3f}\n" for i, f in enumerate(np.geomspace(10, 20000, 120)))


class FakeCamilla:
    def __init__(self, path=None, state="Inactive"):
        self.path, self.state, self.calls = path, state, []

    async def call(self, command, arg=None):
        self.calls.append(command if arg is None or command == "ValidateConfig" else (command, arg))
        if command == "GetConfigFilePath":
            return self.path
        if command == "GetState":
            return self.state
        if command == "SetConfigFilePath":
            self.path = arg
        elif command == "Reload":
            self.state = "Running"
        elif command == "Stop":
            self.state = "Inactive"
        elif command == "GetStopReason":
            return "None"
        return None

    async def status(self):
        return {"reachable": True, "version": "4.1.3", "state": self.state}

    async def levels(self):
        return None


def _install_room(monkeypatch, **sim):
    played_log = []

    def play_and_record(sig, player, capture, cancel=None, **kw):
        sig = np.asarray(sig)
        played_log.append(sig)
        padded = np.zeros((len(sig), 3))
        padded[:, :sig.shape[1]] = sig
        rec = np.zeros((len(sig) + 3210 + 48000, capture.channels))
        rec[:, 0] = simulate(padded, **sim)[:len(rec)]
        return rec
    monkeypatch.setattr(runner, "play_and_record", play_and_record)
    return played_log


@pytest.fixture
def fake_room(monkeypatch):
    """Replace aplay/arecord with the simulated room from test_bass."""
    return _install_room(monkeypatch)


def rig():
    return runner.Rig("room", "player", audio.Capture("mic", 2, "S24_3LE"), [0])


@pytest.mark.parametrize("fmt", ["S16_LE", "S24_3LE", "S32_LE"])
def test_decode(fmt):
    width, scale = audio.FORMATS[fmt]
    values = np.array([0, 1, -1, scale // 2, -scale, scale - 1], dtype=np.int64)
    raw = b"".join(int(v).to_bytes(width, "little", signed=True) for v in values)
    out = audio.decode(raw + b"\x01", fmt, 2)  # a stray byte is dropped
    assert out.shape == (3, 2)
    assert np.allclose(out.ravel(), values / scale)


def test_write_wav_roundtrip(tmp_path):
    x = np.linspace(-1, 1, 100).reshape(50, 2)
    audio.write_wav(tmp_path / "a.wav", x, 48000)
    from scipy.io import wavfile
    fs, y = wavfile.read(tmp_path / "a.wav")
    assert fs == 48000 and y.dtype == np.int32 and np.allclose(y / 2 ** 31, x, atol=1e-6)


def test_session_restores_running_config(tmp_path):
    active = tmp_path / "active.yml"
    active.write_text("x")
    cam = FakeCamilla(str(active), "Running")
    with runner.Session(cam, tmp_path, lambda *a: None, threading.Event()) as s:
        s.load(cfgmod.measure_raw())
        assert (tmp_path / runner.MEASURE_CONFIG).exists()
        assert cam.path == str(tmp_path / runner.MEASURE_CONFIG)
    assert cam.path == str(active) and cam.calls[-1] == "Reload"


def test_session_stops_when_previous_config_is_gone(tmp_path):
    cam = FakeCamilla(str(tmp_path / "deleted.yml"), "Running")
    with runner.Session(cam, tmp_path, lambda *a: None, threading.Event()) as s:
        s.load(cfgmod.measure_raw())
    assert cam.calls[-1] == "Stop" and cam.state == "Inactive"


def test_session_stops_when_nothing_was_active(tmp_path):
    cam = FakeCamilla(None, "Inactive")
    with runner.Session(cam, tmp_path, lambda *a: None, threading.Event()) as s:
        s.load(cfgmod.measure_raw())
    assert cam.calls[-1] == "Stop" and cam.state == "Inactive"


def test_session_cancel(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with runner.Session(FakeCamilla(), tmp_path, lambda *a: None, cancel) as s:
        with pytest.raises(runner.Cancelled):
            s.check()


def test_selftest_config_valid():
    cfg = runner._simulate(cfgmod.measure_raw(hardware=runner.selftest_rig().hardware))
    assert cfgmod.problems(cfg) == []
    assert yaml.safe_load(cfgmod.to_yaml(cfg)) == cfg
    assert cfg["devices"]["playback"]["device"].startswith("hw:CARD=Loopback")


def test_selftest_verdict():
    ok = {"summation_db": -0.1, "channels": {"left": {"delay_ms": 7.15, "trim_db": 0},
                                             "right": {"delay_ms": 6.55, "trim_db": 0},
                                             "sub": {"delay_ms": 0, "trim_db": 0.1}}}
    assert runner.selftest_verdict(ok)["passed"]
    bad = {**ok, "channels": {**ok["channels"], "left": {"delay_ms": 6.55, "trim_db": 0}}}
    assert not runner.selftest_verdict(bad)["passed"]


def test_measure_bass_in_simulated_room(tmp_path, fake_room):
    steps = []
    with runner.Session(FakeCamilla(), tmp_path, lambda s, f: steps.append(s), threading.Event()) as s:
        out = runner.measure_bass(s, rig())
    ch = out["plan"]["channels"]
    assert ch["left"]["delay_ms"] - ch["right"]["delay_ms"] == pytest.approx(0.6, abs=0.05)
    assert out["level"]["sweep_db"] <= runner.OUTPUT_CEILING_DB
    assert steps[0].startswith("Loading") and len(steps) >= 4
    c = runner.curves(out["responses"], out["plan"])
    assert len(c["freq"]) == len(c["left"]) == len(c["sum_right"]) > 100


def _bass_in_room(tmp_path, monkeypatch, **sim):
    played = _install_room(monkeypatch, **sim)
    with runner.Session(FakeCamilla(), tmp_path, lambda s, f: None, threading.Event()) as s:
        return runner.measure_bass(s, rig()), played


def test_sub_is_probed_alone_before_any_sweep(tmp_path, monkeypatch):
    out, played = _bass_in_room(tmp_path, monkeypatch)
    probe_db = 20 * np.log10(np.max(np.abs(played[1])))
    assert np.max(np.abs(played[0][:, 2])) == 0  # the mains probe leaves the sub silent
    assert np.max(np.abs(played[1][:, :2])) == 0  # the sub probe plays on the sub only
    assert probe_db == pytest.approx(runner.PROBE_DB, abs=0.1)
    assert "sub_sweep_db" in out["level"]


def test_a_hot_sub_gets_a_quieter_sweep_and_the_same_plan(tmp_path, monkeypatch):
    normal, _ = _bass_in_room(tmp_path, monkeypatch)
    hot, played = _bass_in_room(tmp_path / "hot", monkeypatch, sub_gain_db=12.0)
    sweeps = played[-1]
    sub_peak = 20 * np.log10(np.max(np.abs(sweeps[:, 2])))
    main_peak = 20 * np.log10(np.max(np.abs(sweeps[:, 0])))
    assert hot["level"]["sub_sweep_db"] < normal["level"]["sub_sweep_db"] - 10
    assert sub_peak == pytest.approx(hot["level"]["sub_sweep_db"], abs=0.2)
    assert main_peak == pytest.approx(hot["level"]["sweep_db"], abs=0.2)
    # The per-channel sweep levels are divided out: the trim tracks the sub's real gain.
    trim = lambda o: o["plan"]["channels"]["sub"]["trim_db"]
    assert trim(normal) - trim(hot) == pytest.approx(15.0, abs=1.0)


def test_a_far_too_loud_sub_stops_before_sweeping(tmp_path, monkeypatch):
    played = _install_room(monkeypatch, sub_gain_db=40.0)
    with pytest.raises(MeasurementError, match="sub"):
        with runner.Session(FakeCamilla(), tmp_path, lambda s, f: None, threading.Event()) as s:
            runner.measure_bass(s, rig())
    assert len(played) == 2  # only the two quiet probes played


def test_store_mic(tmp_path):
    st = Store(tmp_path / "state", tmp_path / "camilla")
    info = st.save_mic("7101234.txt", UMIK_FILE)
    assert info["serial"] == "7101234" and info["sensitivity_db"] == -1.38 and info["points"] == 120
    assert st.mic() == info
    assert st.mic_curve().shape == (480,)
    with pytest.raises(ValueError):
        st.save_mic("x.txt", "hello\nworld\n")
    st.delete_mic()
    assert st.mic() is None and st.mic_curve() is None


def test_store_apply(tmp_path):
    st = Store(tmp_path / "state", tmp_path / "camilla")
    plan = {"crossover_hz": 80, "crossover_order": 4,
            "channels": {"left": {"delay_ms": 0.6, "trim_db": 0, "inverted": False},
                         "right": {"delay_ms": 0, "trim_db": -0.5, "inverted": False},
                         "sub": {"delay_ms": 3.2, "trim_db": 1.0, "inverted": True}}}
    path = st.apply_bass(plan)
    cfg = yaml.safe_load(path.read_text())
    assert cfgmod.problems(cfg) == [] and cfg["filters"]["delay_sub"]["parameters"]["delay"] == 3.2
    assert st.active()["plan"] == plan


def wait_job(c, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get("/api/measure/job").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_api_bass_measure_and_apply(tree, tmp_path, fake_room):  # noqa: F811
    proc, sys = tree
    cam = FakeCamilla()
    st = Store(tmp_path / "state", tmp_path / "camilla")
    app = webapp.create_app(camilla=cam, store=st, jobs=JobRunner(), proc=proc, sys=sys,
                            rigs={"room": lambda d: rig(), "selftest": lambda d: rig()})
    c = TestClient(app)
    assert c.post("/api/bass/apply").status_code == 409  # nothing measured yet
    assert c.put("/api/mic", json={"filename": "cal.txt", "text": UMIK_FILE}).status_code == 200
    r = c.post("/api/measure/bass", json={})
    assert r.status_code == 202, r.text
    j = wait_job(c)
    assert j["state"] == "done", j
    res = c.get("/api/bass").json()["result"]
    assert res["mic_calibrated"] and not res["selftest"] and "curves" in res
    r = c.post("/api/bass/apply")
    assert r.status_code == 200, r.text
    assert cam.path == str(tmp_path / "camilla/configs/active.yml") and cam.state == "Running"
    assert c.get("/api/status").json()["calibration"]["kind"] == "bass"


def test_api_choose_crossover(tree, tmp_path, fake_room):  # noqa: F811
    proc, sys = tree
    st = Store(tmp_path / "state", tmp_path / "camilla")
    c = TestClient(webapp.create_app(camilla=FakeCamilla(), store=st, jobs=JobRunner(), proc=proc, sys=sys,
                                     rigs={"room": lambda d: rig(), "selftest": lambda d: rig()}))
    assert c.post("/api/bass/crossover", json={"crossover_hz": 80}).status_code == 409  # nothing measured
    assert c.post("/api/measure/bass", json={}).status_code == 202
    assert wait_job(c)["state"] == "done"
    before = c.get("/api/bass").json()["result"]
    assert before["plan"]["crossover_choice"] == "auto"
    opts = before["plan"]["crossover_options"]
    allowed = [o["crossover_hz"] for o in opts if "excluded" not in o]
    other = next(f for f in allowed if f != before["plan"]["crossover_hz"])
    r = c.post("/api/bass/crossover", json={"crossover_hz": other})
    assert r.status_code == 200, r.text
    after = r.json()["result"]
    assert after["plan"]["crossover_hz"] == other and after["plan"]["crossover_choice"] == "listener"
    assert after["measured"] == before["measured"] and after["level"] == before["level"]
    assert after["curves"]["sum_left"] != before["curves"]["sum_left"]
    assert c.get("/api/bass").json()["result"]["plan"]["crossover_hz"] == other
    excluded = [o["crossover_hz"] for o in opts if "excluded" in o]
    if excluded:
        assert c.post("/api/bass/crossover", json={"crossover_hz": excluded[0]}).status_code == 422
    assert c.post("/api/bass/crossover", json={"crossover_hz": 55}).status_code == 422
    back = c.post("/api/bass/crossover", json={"crossover_hz": None}).json()["result"]["plan"]
    assert back["crossover_choice"] == "auto" and back["crossover_hz"] == before["plan"]["crossover_hz"]


def test_api_selftest_cannot_be_applied(tree, tmp_path, fake_room):  # noqa: F811
    proc, sys = tree
    st = Store(tmp_path / "state", tmp_path / "camilla")
    c = TestClient(webapp.create_app(camilla=FakeCamilla(), store=st, jobs=JobRunner(), proc=proc, sys=sys,
                                     rigs={"room": lambda d: rig(), "selftest": lambda d: rig()}))
    assert c.post("/api/measure/bass", json={"selftest": True}).status_code == 202
    assert wait_job(c)["state"] == "done"
    assert "verdict" in c.get("/api/bass").json()["result"]
    assert c.post("/api/bass/apply").status_code == 409


def test_compare_needs_an_applied_room_correction(tree, tmp_path):  # noqa: F811
    proc, sys = tree
    c = TestClient(webapp.create_app(camilla=FakeCamilla(), store=Store(tmp_path / "s", tmp_path / "c"),
                                     jobs=JobRunner(), proc=proc, sys=sys))
    assert c.post("/api/compare", json={"room_correction": False}).status_code == 409


def test_restart_goes_back_from_the_comparison_to_the_applied_setup(tmp_path):
    import asyncio
    st = Store(tmp_path / "state", tmp_path / "camilla")
    st.config_dir.mkdir(parents=True)
    (st.config_dir / "active.yml").write_text("x")
    cam = FakeCamilla(str(st.config_dir / "compare.yml"), "Running")
    assert asyncio.run(webapp.recover_from_interrupted_measurement(cam, st, JobRunner())) == "restored"
    assert cam.path.endswith("active.yml")


def test_recover_after_interrupted_measurement(tmp_path):
    import asyncio
    st = Store(tmp_path / "state", tmp_path / "camilla")
    measure = str(st.config_dir / runner.MEASURE_CONFIG)
    cam = FakeCamilla(measure, "Running")
    assert asyncio.run(webapp.recover_from_interrupted_measurement(cam, st, JobRunner())) == "stopped"
    assert cam.state == "Inactive"
    st.config_dir.mkdir(parents=True)
    (st.config_dir / "active.yml").write_text("x")
    cam = FakeCamilla(measure, "Running")
    assert asyncio.run(webapp.recover_from_interrupted_measurement(cam, st, JobRunner())) == "restored"
    assert cam.path.endswith("active.yml") and cam.state == "Running"
    cam = FakeCamilla(str(st.config_dir / "active.yml"), "Running")
    assert asyncio.run(webapp.recover_from_interrupted_measurement(cam, st, JobRunner())) is None


def test_api_room_correction_flow(tree, tmp_path, fake_room):  # noqa: F811
    from scipy.io import wavfile
    proc, sys = tree
    cam = FakeCamilla()
    st = Store(tmp_path / "state", tmp_path / "camilla")
    c = TestClient(webapp.create_app(camilla=cam, store=st, jobs=JobRunner(), proc=proc, sys=sys,
                                     rigs={"room": lambda d: rig(), "selftest": lambda d: rig()}))
    assert c.post("/api/measure/room", json={"position": 1}).status_code == 409  # no bass yet
    assert c.post("/api/measure/bass", json={}).status_code == 202
    assert wait_job(c)["state"] == "done"
    for pos in (1, 2):
        r = c.post("/api/measure/room", json={"position": pos})
        assert r.status_code == 202, r.text
        j = wait_job(c)
        assert j["state"] == "done", j
    state = c.get("/api/room").json()
    assert [p["index"] for p in state["positions"]] == [1, 2] and all(p["current"] for p in state["positions"])
    assert c.post("/api/room/build", json={"target": "nonsense"}).status_code == 422
    r = c.post("/api/room/build", json={"target": "room"})
    assert r.status_code == 200, r.text
    b = r.json()["build"]
    assert b["positions"] == 2 and b["preamp_db"] <= 0
    assert set(b["sides"]) == {"left", "right"} and len(b["curves"]["left_predicted"]) == len(b["curves"]["freq"])
    r = c.post("/api/room/apply")
    assert r.status_code == 200, r.text
    cfg = yaml.safe_load((tmp_path / "camilla/configs/active.yml").read_text())
    assert cfgmod.problems(cfg) == []
    for side in ("left", "right"):
        path = cfg["filters"][f"fir_{side}"]["parameters"]["filename"]
        fs, taps = wavfile.read(path)
        assert fs == 48000 and taps.dtype == np.float32 and len(taps) == 8192
    assert cam.path.endswith("active.yml")
    cal = c.get("/api/status").json()["calibration"]
    assert cal["kind"] == "room" and cal["room"]["target"] == "room"
    # A/B: the same bass setup and preamp without the FIRs, then back.
    r = c.post("/api/compare", json={"room_correction": False})
    assert r.status_code == 200, r.text
    assert cam.path.endswith("compare.yml")
    cmp = yaml.safe_load((tmp_path / "camilla/configs/compare.yml").read_text())
    assert cfgmod.problems(cmp) == [] and not any(k.startswith("fir_") for k in cmp["filters"])
    assert cmp["filters"]["preamp"]["parameters"]["gain"] == cfg["filters"]["preamp"]["parameters"]["gain"]
    assert c.post("/api/compare", json={"room_correction": True}).status_code == 200
    assert cam.path.endswith("active.yml")
    # Stage C: through the correction, compared with position 1; then back to the applied setup.
    assert c.post("/api/measure/check", json={"selftest": True}).status_code == 409  # no self-test build
    r = c.post("/api/measure/check", json={})
    assert r.status_code == 202, r.text
    assert wait_job(c)["state"] == "done"
    chk = c.get("/api/check").json()["result"]
    assert chk["compared_with_position"] == 1 and set(chk["sides"]) == {"left", "right"}
    assert {"deviation_before_db", "deviation_after_db"} <= set(chk["sides"]["left"])
    assert len(chk["curves"]["left_after"]) == len(chk["curves"]["freq"]) == len(chk["curves"]["left_before"])
    assert (tmp_path / "camilla/filters/check_left.wav").exists()
    assert cam.path.endswith("active.yml") and cam.state == "Running"
    # The self-test keeps to itself: the room's bass result, positions and build stay.
    real_bass = c.get("/api/bass?selftest=false").json()["result"]
    assert c.post("/api/measure/bass", json={"selftest": True}).status_code == 202
    assert wait_job(c)["state"] == "done"
    latest = c.get("/api/bass").json()
    assert latest["result"]["selftest"] and latest["other"]
    assert c.get("/api/bass?selftest=false").json()["result"] == real_bass
    room_now = c.get("/api/room").json()
    assert all(p["current"] for p in room_now["positions"]) and room_now["build"]["built"] == b["built"]
    assert c.get("/api/room?selftest=true").json()["positions"] == []
    # Self-test room correction, built and checked apart from the real one.
    assert c.post("/api/measure/room", json={"position": 1, "selftest": True}).status_code == 202
    assert wait_job(c)["state"] == "done"
    sb = c.post("/api/room/build", json={"target": "flat", "selftest": True}).json()["build"]
    assert sb["selftest"] and c.get("/api/room").json()["build"]["built"] == b["built"]
    assert c.post("/api/measure/check", json={"selftest": True}).status_code == 202
    assert wait_job(c)["state"] == "done"
    assert c.get("/api/check?selftest=true").json()["result"]["selftest"]
    assert not c.get("/api/check").json()["result"]["selftest"]
    assert c.post("/api/room/apply").status_code == 200  # still the real build
    # Starting over clears one set only.
    c.delete("/api/room?selftest=true")
    assert c.get("/api/room?selftest=true").json()["positions"] == []
    assert len(c.get("/api/room").json()["positions"]) == 2
    c.delete("/api/room")
    assert c.get("/api/room").json()["positions"] == []


def test_room_deviation():
    from pidsp.dsp import calibration as dcal, room
    flat = np.zeros_like(dcal.GRID)
    assert room.deviation_db(flat + 3.0, flat) == pytest.approx(0.0)
    bumpy = flat + 4.0 * (dcal.GRID < 100)
    assert room.deviation_db(bumpy, flat) == pytest.approx(4.0)


def test_room_correction_trusts_a_peak_the_positions_agree_on():
    """A +8 dB mode at 125 Hz that every position sees, give or take 4 dB,
    is a real problem of the listening area: correct most of it. A 1 dB
    wiggle the positions disagree on is left mostly alone."""
    from pidsp.dsp import calibration as cal
    g = cal.GRID
    peak = 8.0 * np.exp(-0.5 * (np.log2(g / 125.0) / 0.15) ** 2)
    wiggle = 1.0 * np.exp(-0.5 * (np.log2(g / 2000.0) / 0.1) ** 2)
    rng = np.random.default_rng(1)
    positions = []
    for k in range(5):
        jitter = rng.normal(0, 4.0) * np.exp(-0.5 * (np.log2(g / 125.0) / 0.3) ** 2) \
            + rng.normal(0, 2.0) * np.exp(-0.5 * (np.log2(g / 2000.0) / 0.2) ** 2)
        curve = peak + wiggle + jitter
        ch = {"repeats": [curve + rng.normal(0, 0.2, len(g)) for _ in range(2)], "snr": np.full(len(g), 40.0),
              "arrivals": [0.0, 0.0]}
        positions.append({"channels": [ch, ch]})
    chans = cal.combine(positions)
    _, corr = cal.magnitude_correction(chans[0], np.zeros(len(g)))
    i125, i2k = int(np.argmin(abs(g - 125))), int(np.argmin(abs(g - 2000)))
    mean = cal.smooth(chans[0]["mean"], 6)
    assert corr[i125] < -0.7 * (mean[i125] - np.median(mean))  # most of the peak comes off
    assert abs(corr[i2k]) < 0.6


def test_room_measurement_names_a_silent_main_speaker(tmp_path, monkeypatch):
    """The sub plays, the main does not (amplifier on another input): say so, and which side."""
    from scipy import signal as sig
    sos = sig.butter(4, 90, "lowpass", fs=48000, output="sos")

    def only_sub(s, player, capture, cancel=None, **kw):
        s = np.asarray(s)
        rec = np.zeros((len(s) + 3210 + 48000, capture.channels))
        bass = sig.sosfilt(sos, s.sum(axis=1)) * 0.3
        rec[3210:3210 + len(s), 0] = bass
        rec[:, 0] += np.random.default_rng(0).normal(0, 1e-4, len(rec))
        return rec
    monkeypatch.setattr(runner, "play_and_record", only_sub)
    plan = {"crossover_hz": 80, "crossover_order": 4,
            "channels": {"left": {"delay_ms": 0, "trim_db": 0, "inverted": False},
                         "right": {"delay_ms": 0, "trim_db": 0, "inverted": False},
                         "sub": {"delay_ms": 0, "trim_db": 0, "inverted": False}}}
    with pytest.raises(MeasurementError, match="Left speaker and sub: Only the bass was heard"):
        with runner.Session(FakeCamilla(), tmp_path, lambda s, f: None, threading.Event()) as s:
            runner.measure_position(s, rig(), plan, -20.0)


def test_room_correction_corrects_the_sub_level_as_a_whole():
    """Bass 5 dB under the target across the sub's whole range (plus a narrow
    null) is the sub's level: fixed with its gain, not half-filled by the FIR."""
    from pidsp.dsp import calibration as cal, room
    g = cal.GRID
    plan = {"crossover_hz": 80, "crossover_order": 4,
            "channels": {"left": {"delay_ms": 0, "trim_db": 0, "inverted": False},
                         "right": {"delay_ms": 0, "trim_db": 0, "inverted": False},
                         "sub": {"delay_ms": 0, "trim_db": -10.0, "inverted": False}}}
    goal = cal.target_curve("room")
    curve = goal + room.lift_curve(plan, -5.0) \
        - 8.0 * np.exp(-0.5 * (np.log2(g / 50.0) / 0.08) ** 2)
    ch = {"repeats": [curve, curve], "snr": np.full(len(g), 40.0), "arrivals": [0.0, 0.0]}
    r = room.build([{"channels": [ch, ch]}], "room", plan=plan)
    assert r["bass_lift_db"] == pytest.approx(5.0, abs=0.6)
    assert r["plan"]["channels"]["sub"]["trim_db"] == pytest.approx(-10.0 + r["bass_lift_db"])
    assert plan["channels"]["sub"]["trim_db"] == -10.0  # the input plan is not changed
    band = (g >= 30) & (g <= 60) & (np.abs(np.log2(g / 50.0)) > 0.3)
    pred = r["sides"]["left"]["predicted"]
    off = pred - goal - np.mean((pred - goal)[(g >= 300) & (g <= 3000)])
    assert np.max(np.abs(off[band])) < 1.5
    # Capped, and never above the sub's input level.
    loud = {**plan, "channels": {**plan["channels"], "sub": {**plan["channels"]["sub"], "trim_db": -2.0}}}
    assert room.build([{"channels": [ch, ch]}], "room", plan=loud)["bass_lift_db"] <= 2.0
