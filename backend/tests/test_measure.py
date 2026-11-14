"""Measurement plumbing without hardware: audio decoding, the CamillaDSP session,
the runner (with aplay/arecord replaced by a simulated room), the store and the API."""

import threading
import time

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from pidsp.camilla import config as cfgmod
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


@pytest.fixture
def fake_room(monkeypatch):
    """Replace aplay/arecord with the simulated room from test_bass."""
    def play_and_record(sig, player, capture, cancel=None, **kw):
        sig = np.asarray(sig)
        padded = np.zeros((len(sig), 3))
        padded[:, :sig.shape[1]] = sig
        rec = np.zeros((len(sig) + 3210 + 48000, capture.channels))
        rec[:, 0] = simulate(padded)[:len(rec)]
        return rec
    monkeypatch.setattr(runner, "play_and_record", play_and_record)


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


def test_api_selftest_cannot_be_applied(tree, tmp_path, fake_room):  # noqa: F811
    proc, sys = tree
    st = Store(tmp_path / "state", tmp_path / "camilla")
    c = TestClient(webapp.create_app(camilla=FakeCamilla(), store=st, jobs=JobRunner(), proc=proc, sys=sys,
                                     rigs={"room": lambda d: rig(), "selftest": lambda d: rig()}))
    assert c.post("/api/measure/bass", json={"selftest": True}).status_code == 202
    assert wait_job(c)["state"] == "done"
    assert "verdict" in c.get("/api/bass").json()["result"]
    assert c.post("/api/bass/apply").status_code == 409


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
    # Measuring the bass again makes the old positions stale.
    assert c.post("/api/measure/bass", json={"selftest": True}).status_code == 202
    assert wait_job(c)["state"] == "done"
    assert not any(p["current"] for p in c.get("/api/room").json()["positions"])
    c.delete("/api/room")
    assert c.get("/api/room").json()["positions"] == []


def test_room_deviation():
    from pidsp.dsp import calibration as dcal, room
    flat = np.zeros_like(dcal.GRID)
    assert room.deviation_db(flat + 3.0, flat) == pytest.approx(0.0)
    bumpy = flat + 4.0 * (dcal.GRID < 100)
    assert room.deviation_db(bumpy, flat) == pytest.approx(4.0)
