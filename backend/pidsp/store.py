"""PiDSP's persistent state: microphone calibration, measurements, the active setup.

Layout (root defaults to /var/lib/pidsp, the service's StateDirectory):
  mic/calibration.txt   the uploaded file, as uploaded
  mic/mic.json          what was read from it
  bass/latest.json      the last bass measurement: plan, curves, level
  bass/latest.npz       its complex responses, for later stages
  room/pos<N>.json      room correction measurements, one per mic position
  room/build.json       the last correction built from them (curves, preamp)
  room/fir_<side>.npy   its FIR taps
  active.json           what is applied, and when
CamillaDSP configs go to <camilla_dir>/configs/.
"""

from __future__ import annotations

import io
import json
import os
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
MAX_MIC_FILE = 1_000_000


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
    def __init__(self, root: Path = STATE_DIR, camilla_dir: Path = CAMILLA_DIR):
        self.root = Path(root)
        self.camilla_dir = Path(camilla_dir)

    @property
    def config_dir(self) -> Path:
        return self.camilla_dir / "configs"

    def _json(self, rel: str):
        try:
            return json.loads((self.root / rel).read_text())
        except (OSError, ValueError):
            return None

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
        write_atomic(self.root / "bass/latest.json", json.dumps(result, indent=1))
        tmp = self.root / "bass/.latest.npz"
        np.savez_compressed(tmp, h=np.array([r["h"] for r in responses]),
                            arrival=np.array([r["arrival"] for r in responses]),
                            snr=np.array([r["snr"] for r in responses]))
        os.replace(tmp, self.root / "bass/latest.npz")
        return result

    def bass(self) -> dict | None:
        return self._json("bass/latest.json")

    # ---- room correction ----

    def save_position(self, index: int, measurement: dict, plan: dict, selftest: bool) -> dict:
        info = {"index": index, "measured": _now(), "plan": plan, "selftest": selftest, **measurement}
        write_atomic(self.root / f"room/pos{index}.json", json.dumps(info))
        return info

    def positions(self) -> list[dict]:
        out = []
        for path in sorted((self.root / "room").glob("pos*.json")):
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda p: p["index"])

    def clear_room(self) -> None:
        for path in (self.root / "room").glob("*"):
            path.unlink(missing_ok=True)

    def save_room_build(self, summary: dict, firs: dict) -> dict:
        summary = {**summary, "built": _now()}
        for side, taps in firs.items():
            tmp = self.root / f"room/.fir_{side}.npy"
            np.save(tmp, np.asarray(taps, dtype=np.float32))
            os.replace(tmp, self.root / f"room/fir_{side}.npy")
        write_atomic(self.root / "room/build.json", json.dumps(summary))
        return summary

    def room_build(self) -> dict | None:
        return self._json("room/build.json")

    # ---- the applied setup ----

    def _apply(self, cfg: dict, record: dict) -> Path:
        problems = cfgmod.problems(cfg)
        if problems:
            raise ValueError("; ".join(problems))
        path = self.config_dir / ACTIVE_CONFIG
        write_atomic(path, cfgmod.to_yaml(cfg))
        write_atomic(self.root / "active.json", json.dumps({"applied": _now(), **record}, indent=1))
        return path

    def apply_bass(self, plan: dict, hardware: dict | None = None) -> Path:
        """Write the listening config for a bass plan. Returns its path."""
        cfg = cfgmod.build(plan, hardware=hardware, title="PiDSP: bass setup")
        return self._apply(cfg, {"kind": "bass", "plan": plan})

    def apply_room(self, hardware: dict | None = None) -> Path:
        """Write the listening config for the last room build (bass plan + FIRs)."""
        build = self.room_build()
        if not build:
            raise ValueError("Build the room correction first.")
        filters = self.camilla_dir / "filters"
        fir = {}
        for side in ("left", "right"):
            taps = np.load(self.root / f"room/fir_{side}.npy")
            path = filters / f"room_{side}.wav"
            buf = io.BytesIO()
            wavfile.write(buf, cal.FS, taps.astype(np.float32))
            write_atomic(path, buf.getvalue())
            fir[side] = str(path)
        cfg = cfgmod.build(build["plan"], fir=fir, preamp_db=build["preamp_db"], hardware=hardware,
                           title="PiDSP: bass setup and room correction")
        return self._apply(cfg, {"kind": "room", "plan": build["plan"], "room": {
            k: build[k] for k in ("target", "positions", "preamp_db", "sides", "curves")}})

    def active(self) -> dict | None:
        return self._json("active.json")
