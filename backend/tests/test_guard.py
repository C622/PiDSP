"""The bitstream guard: IEC 61937 detection, the level heuristic, the state machine."""

import asyncio
from pathlib import Path

import numpy as np

from pidsp import guard
from pidsp.store import Store


def iec61937(data_type=1, seconds=1.0, payload_words=1280, fs=48000, seed=0):
    """A stereo S16_LE stream like a TV's Dolby Digital output: a burst every
    1536 frames (Pa, Pb, Pc, Pd, then random payload), zeros in between."""
    rng = np.random.default_rng(seed)
    frames = int(seconds * fs)
    words = np.zeros(frames * 2, dtype="<u2")
    for start in range(0, frames - 1536, 1536):
        w = start * 2
        words[w:w + 4] = [guard.SYNC_A, guard.SYNC_B, data_type, payload_words * 16]
        words[w + 4:w + 4 + payload_words] = rng.integers(0, 65536, payload_words)
    return words.tobytes()


def levels_of(raw, chunk=2048):
    """What CamillaDSP would report for each chunk of that stream."""
    x = np.frombuffer(raw, dtype="<i2").reshape(-1, 2) / 32768.0
    out = []
    for i in range(0, len(x) - chunk, chunk):
        c = x[i:i + chunk]
        out.append({"capture_peak": list(20 * np.log10(np.abs(c).max(axis=0) + 1e-12)),
                    "capture_rms": list(10 * np.log10((c ** 2).mean(axis=0) + 1e-20))})
    return out


def test_detects_dolby_digital_and_dts():
    assert guard.detect_iec61937(iec61937(1)) == "Dolby Digital"
    assert guard.detect_iec61937(iec61937(11)) == "DTS"


def test_pcm_is_not_bitstream():
    t = np.arange(48000) / 48000
    music = (0.5 * np.sin(2 * np.pi * 440 * t)[:, None] * [1, 0.8] * 32767).astype("<i2")
    assert guard.detect_iec61937(music.tobytes()) is None
    assert guard.detect_iec61937(b"") is None


def test_level_heuristic():
    chunks = levels_of(iec61937(1))
    assert all(guard.looks_like_bitstream(c) for c in chunks)
    t = np.arange(48000) / 48000
    loud = np.clip(1.4 * np.sin(2 * np.pi * 60 * t) * (1 + 0.3 * np.sin(2 * np.pi * 0.5 * t)), -1, 1)
    music = (np.stack([loud, 0.5 * loud], axis=1) * 32767).astype("<i2")
    assert not any(guard.looks_like_bitstream(c) for c in levels_of(music.tobytes()))
    assert not guard.looks_like_bitstream(None)
    assert not guard.looks_like_bitstream({"capture_peak": [-30, -30], "capture_rms": [-45, -45]})


class Cam:
    def __init__(self, store):
        self.state, self.path, self.levels, self.calls = "Running", str(store.config_dir / "active.yml"), None, []

    async def probe(self):
        return {"state": self.state, "config_path": self.path, "levels": self.levels}

    async def call(self, command, arg=None):
        self.calls.append(command)
        if command == "Stop":
            self.state = "Inactive"
        elif command == "SetConfigFilePath":
            self.path = arg
        elif command == "Reload":
            self.state = "Running"


class Jobs:
    busy = False

    def running(self):
        return self.busy


BITSTREAM = {"capture_peak": [0.0, 0.0], "capture_rms": [-8.5, -8.6]}
MUSIC = {"capture_peak": [-6.0, -7.0], "capture_rms": [-20.0, -22.0]}


def run_steps(g, n):
    async def go():
        for _ in range(n):
            await g.step()
    asyncio.run(go())


def make(tmp_path: Path, answers):
    st = Store(tmp_path / "state", tmp_path / "camilla")
    cam = Cam(st)
    it = iter(answers)

    async def sniff():
        return next(it)
    return guard.BitstreamGuard(cam, st, Jobs(), sniff=sniff, clock=lambda: 0.0), cam


def test_guard_silences_bitstream_and_resumes_on_pcm(tmp_path):
    g, cam = make(tmp_path, [(True, "Dolby Digital"), (True, "Dolby Digital"), (True, None)])
    cam.levels = BITSTREAM
    run_steps(g, guard.TRIGGER - 1)
    assert cam.calls == []                       # not yet: needs a steady stretch
    run_steps(g, 1)
    assert cam.calls == ["Stop"] and g.summary()["silenced"] and g.summary()["format"] == "Dolby Digital"
    run_steps(g, 1)                              # still bitstream: stays silent
    assert cam.state == "Inactive"
    run_steps(g, 1)                              # PCM again: back on the applied setup
    assert cam.calls[-2:] == ["SetConfigFilePath", "Reload"] and cam.state == "Running"
    assert not g.summary()["silenced"]


def test_false_alarm_restarts_and_raises_the_bar(tmp_path):
    g, cam = make(tmp_path, [(True, None)])
    cam.levels = BITSTREAM
    run_steps(g, guard.TRIGGER)
    assert cam.calls == ["Stop", "SetConfigFilePath", "Reload"] and g.false_alarms == 1
    run_steps(g, guard.TRIGGER_AFTER_FALSE - 1)
    assert cam.calls.count("Stop") == 1          # the same loud song does not trip it again quickly


def test_guard_leaves_measurements_and_normal_music_alone(tmp_path):
    g, cam = make(tmp_path, [])
    cam.levels = MUSIC
    run_steps(g, 10)
    cam.levels, g.jobs.busy = BITSTREAM, True
    run_steps(g, 10)
    g.jobs.busy, cam.path = False, str(tmp_path / "camilla/configs/measure.yml")
    run_steps(g, 10)
    assert cam.calls == []


def test_guard_watches_the_comparison_setup_too_and_restarts_it(tmp_path):
    g, cam = make(tmp_path, [(True, "DTS"), (True, None)])
    cam.path = str(tmp_path / "camilla/configs/compare.yml")
    cam.levels = BITSTREAM
    run_steps(g, guard.TRIGGER)
    assert cam.calls == ["Stop"] and g.summary()["format"] == "DTS"
    run_steps(g, 1)
    assert cam.path.endswith("compare.yml") and cam.state == "Running"
