"""PiDSP's persistent state: microphone calibration, measurements, the active setup.

Layout (root defaults to /var/lib/pidsp, the service's StateDirectory):
  mic/calibration.txt   the uploaded file, as uploaded
  mic/mic.json          what was read from it
  active.json           what is applied, and when (and from which preset)
  settings.json         listener settings: sub level, volume following
  presets/current.json  the preset new measurements go to
  presets/<id>/         one preset: a bundle of measurements and the filter built from them
    preset.json           its name and when it was made
    bass/latest.json      the bass measurement: plan, curves, level
    bass/latest.npz       its complex responses, for later stages
    room/pos<N>.json      room correction measurements, one per mic position
    room/build.json       the correction built from them (curves, preamp)
    room/fir_<side>.npy   its FIR taps
    check/latest.json     the last check measurement (stage C)
    levelcheck/latest.json  the last level check
CamillaDSP configs go to <camilla_dir>/configs/.

A Store reads and writes the measurement data of the current preset, or of
the one directory it was given (data_dir: a preset, or the self-test's).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from .camilla import config as cfgmod
from .dsp import calibration as cal

STATE_DIR = Path(os.environ.get("PIDSP_STATE", "/var/lib/pidsp"))
CAMILLA_DIR = Path(os.environ.get("PIDSP_CAMILLA_DIR", "/var/lib/camilladsp"))
ACTIVE_CONFIG = "active.yml"
# The applied bass setup without the room correction FIRs (but with the same
# preamp, so a comparison is not just a level difference): for A/B listening.
COMPARE_CONFIG = "compare.yml"
LISTENING_CONFIGS = (ACTIVE_CONFIG, COMPARE_CONFIG)
MAX_MIC_FILE = 1_000_000
SETTINGS_DEFAULTS = {
    "sub_level_db": 0.0,      # the listener's sub level, on top of the calibration
    "follow_volume": True,    # move the sub with the amplifier's volume
    "bluos_host": None,       # the amplifier (BluOS player) to follow, IP or name
    "reference_db": None,     # the amplifier's volume (dB) when the bass was measured
    "reference_volume": None, # the same, 0-100
}
# The measurement data a preset holds (directories inside it).
DATA_DIRS = ("bass", "room", "check", "levelcheck")
PRESET_ID = re.compile(r"^p[0-9a-z]{1,24}$")
MAX_PRESET_NAME = 60


class PresetError(ValueError):
    pass


def write_atomic(path: Path, data: str | bytes) -> None:
    """Write via a temporary file and rename, so readers never see half a file
    and a file left by another user can still be replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Store:
    def __init__(self, root: Path = STATE_DIR, camilla_dir: Path = CAMILLA_DIR, data_dir: Path | None = None):
        self.root = Path(root)
        self.camilla_dir = Path(camilla_dir)
        self._data_dir = Path(data_dir) if data_dir is not None else None

    # ---- presets ----

    @property
    def presets_dir(self) -> Path:
        return self.root / "presets"

    @property
    def data(self) -> Path:
        """Where measurement data is read and written."""
        if self._data_dir is not None:
            return self._data_dir
        return self.presets_dir / self.current_preset()

    @property
    def preset_id(self) -> str | None:
        """The preset this store's data belongs to (None for the self-test)."""
        d = self.data
        return d.name if d.parent == self.presets_dir else None

    def _preset_path(self, pid: str) -> Path:
        if not PRESET_ID.match(pid or "") or not (self.presets_dir / pid / "preset.json").is_file():
            raise PresetError("No such preset.")
        return self.presets_dir / pid

    def _new_preset(self, name: str) -> str:
        name = (name or "").strip()[:MAX_PRESET_NAME] or "Preset"
        base = "p" + time.strftime("%Y%m%d%H%M%S").lower()
        pid, n = base, 1
        while (self.presets_dir / pid).exists():
            n += 1
            pid = f"{base}{n}"
        write_atomic(self.presets_dir / pid / "preset.json", json.dumps({"id": pid, "name": name, "created": _now()}))
        return pid

    def current_preset(self) -> str:
        """The current preset's id. The first time, the measurements kept
        from before presets existed become the first preset."""
        cur = self._json("presets/current.json") or {}
        pid = cur.get("id")
        if pid and PRESET_ID.match(pid) and (self.presets_dir / pid / "preset.json").is_file():
            return pid
        existing = self.preset_ids()
        if existing:
            pid = existing[-1]
        else:
            had_data = any((self.root / d).is_dir() for d in DATA_DIRS)
            pid = self._new_preset("First calibration" if had_data else "My room")
            for d in DATA_DIRS:
                if (self.root / d).is_dir():
                    os.replace(self.root / d, self.presets_dir / pid / d)
        write_atomic(self.presets_dir / "current.json", json.dumps({"id": pid}))
        return pid

    def preset_ids(self) -> list[str]:
        if not self.presets_dir.is_dir():
            return []
        return sorted(p.name for p in self.presets_dir.iterdir()
                      if PRESET_ID.match(p.name) and (p / "preset.json").is_file())

    def preset_store(self, pid: str) -> "Store":
        """A store bound to one preset's data."""
        return Store(self.root, self.camilla_dir, data_dir=self._preset_path(pid))

    def preset_info(self) -> dict:
        """This store's preset: name and a summary of what it holds."""
        info = {"id": self.preset_id, "name": None, "created": None}
        info.update(self._data_json("preset.json") or {})
        bass, build = self.bass(), self.room_build()
        plan = (bass or {}).get("plan")
        positions = self.positions()
        info.update({
            "bass_measured": (bass or {}).get("measured"),
            "crossover_hz": (plan or {}).get("crossover_hz"),
            "positions": sorted(p["index"] for p in positions if p.get("plan") == plan),
            "build": {k: build.get(k) for k in ("target", "built", "positions", "preamp_db", "bass_lift_db")}
            if build else None,
        })
        return info

    def presets(self) -> list[dict]:
        self.current_preset()
        return [self.preset_store(pid).preset_info() for pid in self.preset_ids()]

    def create_preset(self, name: str, copy_from: str | None = None) -> str:
        source = self._preset_path(copy_from) if copy_from else None
        pid = self._new_preset(name)
        if source:
            for d in DATA_DIRS:
                if (source / d).is_dir():
                    shutil.copytree(source / d, self.presets_dir / pid / d)
        return pid

    def select_preset(self, pid: str) -> None:
        self._preset_path(pid)
        write_atomic(self.presets_dir / "current.json", json.dumps({"id": pid}))

    def rename_preset(self, pid: str, name: str) -> None:
        path = self._preset_path(pid) / "preset.json"
        name = (name or "").strip()[:MAX_PRESET_NAME]
        if not name:
            raise PresetError("Give the preset a name.")
        info = json.loads(path.read_text())
        write_atomic(path, json.dumps({**info, "name": name}))
        active = self.active()
        if active and (active.get("preset") or {}).get("id") == pid:
            active["preset"]["name"] = name
            write_atomic(self.root / "active.json", json.dumps(active, indent=1))

    def delete_preset(self, pid: str) -> None:
        path = self._preset_path(pid)
        if pid == self.current_preset():
            raise PresetError("Open another preset before deleting this one.")
        shutil.rmtree(path)

    def _data_json(self, rel: str):
        try:
            return json.loads((self.data / rel).read_text())
        except (OSError, ValueError):
            return None

    @property
    def config_dir(self) -> Path:
        return self.camilla_dir / "configs"

    def _json(self, rel: str):
        try:
            return json.loads((self.root / rel).read_text())
        except (OSError, ValueError):
            return None

    # ---- settings ----

    def settings(self) -> dict:
        saved = self._json("settings.json") or {}
        return {**SETTINGS_DEFAULTS, **{k: v for k, v in saved.items() if k in SETTINGS_DEFAULTS}}

    def save_settings(self, **changes) -> dict:
        unknown = set(changes) - set(SETTINGS_DEFAULTS)
        if unknown:
            raise KeyError(f"unknown settings: {', '.join(sorted(unknown))}")
        merged = {**self.settings(), **changes}
        write_atomic(self.root / "settings.json", json.dumps(merged, indent=1))
        return merged

    # ---- microphone calibration ----

    def save_mic(self, filename: str, text: str) -> dict:
        if len(text.encode()) > MAX_MIC_FILE:
            raise ValueError("The file is too large to be a calibration file.")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
        try:
            parsed = cal.read_mic_calibration(f.name)
        finally:
            os.unlink(f.name)
        info = {
            "filename": Path(filename).name[:200],
            "serial": parsed["serial"] or None,
            "sensitivity_db": parsed["sensitivity"],
            "points": int(len(parsed["freq"])),
            "range_hz": [float(parsed["freq"][0]), float(parsed["freq"][-1])],
            "uploaded": _now(),
        }
        write_atomic(self.root / "mic/calibration.txt", text)
        write_atomic(self.root / "mic/mic.json", json.dumps(info, indent=1))
        return info

    def mic(self) -> dict | None:
        return self._json("mic/mic.json")

    def mic_curve(self) -> np.ndarray | None:
        path = self.root / "mic/calibration.txt"
        if not path.exists():
            return None
        return cal.mic_calibration_curve(cal.read_mic_calibration(path))

    def delete_mic(self) -> None:
        for rel in ("mic/calibration.txt", "mic/mic.json"):
            (self.root / rel).unlink(missing_ok=True)

    # ---- bass measurement ----

    def save_bass(self, result: dict, responses: list[dict]) -> dict:
        result = {**result, "measured": _now()}
        write_atomic(self.data / "bass/latest.json", json.dumps(result, indent=1))
        tmp = self.data / "bass/.latest.npz"
        np.savez_compressed(tmp, h=np.array([r["h"] for r in responses]),
                            arrival=np.array([r["arrival"] for r in responses]),
                            snr=np.array([r["snr"] for r in responses]))
        os.replace(tmp, self.data / "bass/latest.npz")
        return result

    def bass(self) -> dict | None:
        return self._data_json("bass/latest.json")

    def bass_responses(self) -> list[dict] | None:
        """The complex responses saved with the last bass measurement."""
        try:
            with np.load(self.data / "bass/latest.npz") as z:
                return [{"h": h, "arrival": float(a), "snr": s} for h, a, s in zip(z["h"], z["arrival"], z["snr"])]
        except (OSError, KeyError, ValueError):
            return None

    def update_bass(self, result: dict) -> dict:
        """Replace the last bass result (e.g. another crossover), same measurement."""
        write_atomic(self.data / "bass/latest.json", json.dumps(result, indent=1))
        return result

    # ---- room correction ----

    def save_position(self, index: int, measurement: dict, plan: dict, selftest: bool) -> dict:
        info = {"index": index, "measured": _now(), "plan": plan, "selftest": selftest, **measurement}
        write_atomic(self.data / f"room/pos{index}.json", json.dumps(info))
        return info

    def positions(self) -> list[dict]:
        out = []
        for path in sorted((self.data / "room").glob("pos*.json")):
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda p: p["index"])

    def delete_position(self, index: int) -> bool:
        path = self.data / f"room/pos{int(index)}.json"
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def clear_room(self) -> None:
        for path in (self.data / "room").glob("*"):
            path.unlink(missing_ok=True)

    def save_room_build(self, summary: dict, firs: dict) -> dict:
        summary = {**summary, "built": _now()}
        for side, taps in firs.items():
            tmp = self.data / f"room/.fir_{side}.npy"
            np.save(tmp, np.asarray(taps, dtype=np.float32))
            os.replace(tmp, self.data / f"room/fir_{side}.npy")
        write_atomic(self.data / "room/build.json", json.dumps(summary))
        return summary

    def room_build(self) -> dict | None:
        return self._data_json("room/build.json")

    # ---- the applied setup ----

    def _apply(self, cfg: dict, record: dict) -> Path:
        problems = cfgmod.problems(cfg)
        if problems:
            raise ValueError("; ".join(problems))
        path = self.config_dir / ACTIVE_CONFIG
        write_atomic(path, cfgmod.to_yaml(cfg))
        pid = self.preset_id
        if pid:
            info = self._data_json("preset.json") or {}
            record = {**record, "preset": {"id": pid, "name": info.get("name")}}
        write_atomic(self.root / "active.json", json.dumps({"applied": _now(), **record}, indent=1))
        return path

    def apply_bass(self, plan: dict, hardware: dict | None = None) -> Path:
        """Write the listening config for a bass plan. Returns its path."""
        cfg = cfgmod.build(plan, hardware=hardware, title="PiDSP: bass setup")
        return self._apply(cfg, {"kind": "bass", "plan": plan})

    def write_room_firs(self, prefix: str = "room") -> dict:
        """The last room build's FIRs as float32 WAVs CamillaDSP can load.
        Returns {side: path}."""
        filters = self.camilla_dir / "filters"
        fir = {}
        for side in ("left", "right"):
            taps = np.load(self.data / f"room/fir_{side}.npy")
            path = filters / f"{prefix}_{side}.wav"
            buf = io.BytesIO()
            wavfile.write(buf, cal.FS, taps.astype(np.float32))
            write_atomic(path, buf.getvalue())
            fir[side] = str(path)
        return fir

    def apply_room(self, hardware: dict | None = None) -> Path:
        """Write the listening config for the last room build (bass plan + FIRs)."""
        build = self.room_build()
        if not build:
            raise ValueError("Build the room correction first.")
        fir = self.write_room_firs("room")
        cfg = cfgmod.build(build["plan"], fir=fir, preamp_db=build["preamp_db"], hardware=hardware,
                           title="PiDSP: bass setup and room correction")
        return self._apply(cfg, {"kind": "room", "plan": build["plan"], "room": {
            k: build[k] for k in ("target", "positions", "preamp_db", "sides", "curves", "built") if k in build}})

    def active(self) -> dict | None:
        return self._json("active.json")

    def write_compare(self, hardware: dict | None = None) -> Path:
        """The applied room correction's bass setup without the FIRs, at the
        same preamp, for comparing by ear. Returns its path."""
        active = self.active() or {}
        if active.get("kind") != "room":
            raise ValueError("Apply a room correction first.")
        cfg = cfgmod.build(active["plan"], preamp_db=active["room"]["preamp_db"], hardware=hardware,
                           title="PiDSP: bass setup, room correction off (comparison)")
        problems = cfgmod.problems(cfg)
        if problems:
            raise ValueError("; ".join(problems))
        path = self.config_dir / COMPARE_CONFIG
        write_atomic(path, cfgmod.to_yaml(cfg))
        return path

    # ---- check (stage C) ----

    def save_check(self, result: dict) -> dict:
        result = {**result, "measured": _now()}
        write_atomic(self.data / "check/latest.json", json.dumps(result))
        return result

    def check(self) -> dict | None:
        return self._data_json("check/latest.json")

    # ---- level check ----

    def save_level_check(self, result: dict) -> dict:
        result = {**result, "measured": _now()}
        write_atomic(self.data / "levelcheck/latest.json", json.dumps(result))
        return result

    def level_check(self) -> dict | None:
        return self._data_json("levelcheck/latest.json")
