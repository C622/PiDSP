"""PiDSP web server: JSON API under /api, the single-page UI everywhere else.

Run on the Pi with:  python3 -m uvicorn pidsp.web.app:app --host 0.0.0.0 --port 80

There is no login. Like moOde, PiDSP is meant for a home network and must not
be exposed to the internet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, system
from ..camilla.client import CamillaClient, CamillaError
from ..dsp import room
from ..dsp.calibration import MeasurementError
from ..measure import runner
from ..measure.jobs import Busy, JobRunner
from ..store import ACTIVE_CONFIG, Store

STATIC = Path(__file__).parent / "static"
# Root helper for the few privileged actions (see system/pidsp-admin).
ADMIN = os.environ.get("PIDSP_ADMIN", "/usr/local/sbin/pidsp-admin")
log = logging.getLogger("pidsp.web")


async def recover_from_interrupted_measurement(cam: CamillaClient, st: Store, jr: JobRunner,
                                               attempts: int = 15, delay: float = 2.0) -> str | None:
    """If CamillaDSP is on the measurement config (after a crash or power cut
    mid-measurement), go back to the applied setup, or stop. Returns what was done."""
    for _ in range(attempts):
        try:
            path = await cam.call("GetConfigFilePath")
            break
        except Exception:  # CamillaDSP not up yet
            await asyncio.sleep(delay)
    else:
        return None
    if jr.running() or not path or Path(path).name != runner.MEASURE_CONFIG:
        return None
    active = st.config_dir / ACTIVE_CONFIG
    try:
        if active.is_file():
            await cam.call("SetConfigFilePath", str(active))
            await cam.call("Reload")
            log.warning("CamillaDSP was left on the measurement config; restored %s", active)
            return "restored"
        await cam.call("Stop")
        log.warning("CamillaDSP was left on the measurement config; stopped it")
        return "stopped"
    except Exception as e:
        log.error("could not recover from an interrupted measurement: %s", e)
        return None


class PasswordChange(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class MicUpload(BaseModel):
    filename: str = Field(max_length=255)
    text: str


class BassRequest(BaseModel):
    selftest: bool = False


class RoomMeasure(BaseModel):
    position: int = Field(ge=1, le=9)
    selftest: bool = False


class RoomBuild(BaseModel):
    target: str = "room"


def _admin(*args: str, stdin: str | None = None) -> None:
    if not Path(ADMIN).exists():
        raise HTTPException(503, "The admin helper is not installed on this system.")
    try:
        r = subprocess.run(["sudo", "-n", ADMIN, *args], input=stdin, capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(500, f"Could not run the admin helper: {e}") from e
    if r.returncode != 0:
        raise HTTPException(500, (r.stderr or r.stdout).strip() or f"admin helper exited {r.returncode}")


def create_app(camilla: CamillaClient | None = None, store: Store | None = None,
               jobs: JobRunner | None = None, rigs: dict | None = None, static_dir: Path = STATIC,
               proc: str = "/proc", sys: str = "/sys") -> FastAPI:
    cam = camilla or CamillaClient()
    st = store or Store()
    jr = jobs or JobRunner()
    # Rig factories, replaceable in tests: name -> callable(devices) -> Rig
    rig_for = rigs or {
        "room": lambda devices: runner.real_rig(devices["mic"]["card"]),
        "selftest": lambda devices: runner.selftest_rig(),
    }
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(recover_from_interrupted_measurement(cam, st, jr))
        yield
        task.cancel()

    app = FastAPI(title="PiDSP", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None, lifespan=lifespan)

    def calibration_summary():
        active = st.active()
        if not active:
            return None
        return {k: active[k] for k in ("applied", "kind", "plan", "room") if k in active}

    @app.get("/api/status")
    async def status():
        return {
            "version": __version__,
            "system": system.info(proc, sys),
            "devices": system.audio_devices(proc, sys),
            "camilladsp": await cam.status(),
            "calibration": calibration_summary(),
            "measuring": jr.running(),
        }

    @app.get("/api/levels")
    async def levels():
        return {"levels": await cam.levels()}

    # ---- microphone calibration ----

    @app.get("/api/mic")
    def mic():
        return {"calibration": st.mic()}

    @app.put("/api/mic")
    def upload_mic(body: MicUpload):
        try:
            return {"calibration": st.save_mic(body.filename, body.text)}
        except ValueError as e:
            raise HTTPException(422, f"That doesn't look like a microphone calibration file: {e}.") from e

    @app.delete("/api/mic", status_code=204)
    def delete_mic():
        st.delete_mic()

    # ---- measurements ----

    @app.get("/api/measure/job")
    def job():
        return jr.snapshot()

    @app.post("/api/measure/cancel", status_code=202)
    def cancel():
        jr.cancel()
        return jr.snapshot()

    async def ready_rig(selftest: bool):
        devices = system.audio_devices(proc, sys)
        cstat = await cam.status()
        if not cstat.get("reachable"):
            raise HTTPException(409, "CamillaDSP is not running.")
        if not devices["loopback"]["present"]:
            raise HTTPException(409, "The ALSA loopback is missing. Rerun deploy/install-camilladsp.sh.")
        if not selftest:
            if not devices["dac"]["present"]:
                raise HTTPException(409, "The sound card is not connected.")
            if not devices["mic"].get("card"):
                raise HTTPException(409, "The measurement microphone is not connected.")
        return rig_for["selftest" if selftest else "room"](devices)

    @app.post("/api/measure/bass", status_code=202)
    async def measure_bass(body: BassRequest):
        rig = await ready_rig(body.selftest)
        mic_curve = None if body.selftest else st.mic_curve()

        def work(progress, cancel):
            with runner.Session(cam, st.config_dir, progress, cancel) as session:
                out = runner.measure_bass(session, rig, mic_curve)
            progress("Saving", 0.95)
            result = {"plan": out["plan"], "level": out["level"], "selftest": body.selftest,
                      "mic_calibrated": mic_curve is not None,
                      "curves": runner.curves(out["responses"], out["plan"])}
            if body.selftest:
                result["verdict"] = runner.selftest_verdict(out["plan"])
            return st.save_bass(result, out["responses"])

        try:
            return jr.start("selftest" if body.selftest else "bass", work)
        except Busy as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/bass")
    def bass_result():
        return {"result": st.bass()}

    @app.post("/api/bass/apply")
    async def apply_bass():
        if jr.running():
            raise HTTPException(409, "Wait for the measurement to finish.")
        result = st.bass()
        if not result:
            raise HTTPException(409, "Measure the bass setup first.")
        if result.get("selftest"):
            raise HTTPException(409, "That plan comes from the self-test with simulated speakers, not your room.")
        path = await asyncio.to_thread(st.apply_bass, result["plan"])
        try:
            await cam.call("SetConfigFilePath", str(path))
            await cam.call("Reload")
        except (CamillaError, OSError) as e:
            raise HTTPException(502, f"Saved, but CamillaDSP did not load it: {e}") from e
        return {"calibration": calibration_summary()}

    # ---- room correction ----

    def room_basis(selftest: bool | None = None) -> dict:
        """The bass measurement room correction builds on (its plan and level)."""
        bass = st.bass()
        if not bass:
            raise HTTPException(409, "Measure the bass setup first.")
        if selftest is not None and bool(bass.get("selftest")) != selftest:
            raise HTTPException(409, "Run the bass self-test first." if selftest else
                                "The last bass measurement was the self-test. Measure your room's bass first.")
        return bass

    def current_positions(bass: dict | None) -> list[dict]:
        if not bass:
            return []
        return [p for p in st.positions()
                if p["plan"] == bass["plan"] and p["selftest"] == bool(bass.get("selftest"))]

    @app.get("/api/room")
    def room_state():
        bass = st.bass()
        current = {p["index"] for p in current_positions(bass)}
        return {
            "positions": [{"index": p["index"], "measured": p["measured"], "selftest": p["selftest"],
                           "warnings": p.get("warnings", []), "current": p["index"] in current}
                          for p in st.positions()],
            "recommended": room.RECOMMENDED_POSITIONS,
            "build": st.room_build(),
            "targets": list(room.TARGETS),
        }

    @app.delete("/api/room", status_code=204)
    def clear_room():
        if jr.running():
            raise HTTPException(409, "Wait for the measurement to finish.")
        st.clear_room()

    @app.post("/api/measure/room", status_code=202)
    async def measure_room(body: RoomMeasure):
        bass = room_basis(body.selftest)
        rig = await ready_rig(body.selftest)
        mic_curve = None if body.selftest else st.mic_curve()
        plan, level = bass["plan"], bass["level"]["sweep_db"]

        def work(progress, cancel):
            with runner.Session(cam, st.config_dir, progress, cancel) as session:
                progress("Loading the measurement setup", 0.05)
                m = runner.measure_position(session, rig, plan, level, mic_curve)
            progress("Saving", 0.97)
            st.save_position(body.position, m, plan, body.selftest)
            return {"position": body.position, "warnings": m["warnings"]}

        try:
            return jr.start("room-selftest" if body.selftest else "room", work)
        except Busy as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/room/build")
    async def build_room(body: RoomBuild):
        if body.target not in room.TARGETS:
            raise HTTPException(422, f"Unknown target. Choose one of {', '.join(room.TARGETS)}.")
        bass = room_basis()
        positions = current_positions(bass)
        if not positions:
            raise HTTPException(409, "Measure at least one microphone position first.")
        try:
            result = await asyncio.to_thread(room.build, positions, body.target)
        except MeasurementError as e:
            raise HTTPException(409, str(e)) from e
        summary = {
            "target": result["target"], "positions": result["positions"], "preamp_db": result["preamp_db"],
            "plan": bass["plan"], "selftest": bool(bass.get("selftest")),
            "sides": {side: {k: v for k, v in s.items() if k.endswith("_db") or k == "knee_hz"}
                      for side, s in result["sides"].items()},
            "curves": room.curves_for_ui(result),
        }
        return {"build": st.save_room_build(summary, result["firs"])}

    @app.post("/api/room/apply")
    async def apply_room():
        if jr.running():
            raise HTTPException(409, "Wait for the measurement to finish.")
        build = st.room_build()
        if not build:
            raise HTTPException(409, "Build the room correction first.")
        if build.get("selftest"):
            raise HTTPException(409, "That correction comes from the self-test with simulated speakers, not your room.")
        path = await asyncio.to_thread(st.apply_room)
        try:
            await cam.call("SetConfigFilePath", str(path))
            await cam.call("Reload")
        except (CamillaError, OSError) as e:
            raise HTTPException(502, f"Saved, but CamillaDSP did not load it: {e}") from e
        return {"calibration": calibration_summary()}

    # ---- system ----

    @app.post("/api/system/password", status_code=204)
    def change_password(body: PasswordChange):
        if any(c in body.password for c in "\r\n\0"):
            raise HTTPException(422, "The password cannot contain line breaks.")
        _admin("passwd", stdin=body.password + "\n")

    @app.post("/api/system/reboot", status_code=202)
    def reboot():
        _admin("reboot")
        return {"ok": True}

    @app.post("/api/system/poweroff", status_code=202)
    def poweroff():
        _admin("poweroff")
        return {"ok": True}

    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    return app


app = create_app()
