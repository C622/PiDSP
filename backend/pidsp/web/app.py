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
import re
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, bluos, system
from ..camilla.client import CamillaClient, CamillaError
from ..dsp import bass as bass_dsp, room
from ..dsp.calibration import MeasurementError
from ..guard import BitstreamGuard
from ..measure import runner
from ..measure.jobs import Busy, JobRunner
from ..store import ACTIVE_CONFIG, COMPARE_CONFIG, Store
from ..volume import SUB_LEVEL_RANGE, VolumeFollower

STATIC = Path(__file__).parent / "static"
# Root helper for the few privileged actions (see system/pidsp-admin).
ADMIN = os.environ.get("PIDSP_ADMIN", "/usr/local/sbin/pidsp-admin")
log = logging.getLogger("pidsp.web")


async def recover_from_interrupted_measurement(cam: CamillaClient, st: Store, jr: JobRunner,
                                               attempts: int = 15, delay: float = 2.0) -> str | None:
    """If CamillaDSP is on the measurement config (after a crash or power cut
    mid-measurement), go back to the applied setup, or stop. The same for the
    A/B comparison config: after a restart PiDSP plays the applied setup.
    Returns what was done."""
    for _ in range(attempts):
        try:
            path = await cam.call("GetConfigFilePath")
            break
        except Exception:  # CamillaDSP not up yet
            await asyncio.sleep(delay)
    else:
        return None
    if jr.running() or not path or Path(path).name not in (runner.MEASURE_CONFIG, COMPARE_CONFIG):
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


class CrossoverChoice(BaseModel):
    crossover_hz: int | None = None  # None: back to the automatic choice
    selftest: bool = False


class RoomMeasure(BaseModel):
    position: int = Field(ge=1, le=9)
    selftest: bool = False


class RoomBuild(BaseModel):
    target: str = "room"
    selftest: bool = False


class CheckRequest(BaseModel):
    selftest: bool = False


class Compare(BaseModel):
    room_correction: bool


class VolumeSettings(BaseModel):
    sub_level_db: float | None = Field(default=None, ge=SUB_LEVEL_RANGE[0], le=SUB_LEVEL_RANGE[1])
    follow_volume: bool | None = None
    bluos_host: str | None = Field(default=None, max_length=253)
    forget_bluos_host: bool = False


HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")


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
               proc: str = "/proc", sys: str = "/sys", player_fetch=None) -> FastAPI:
    cam = camilla or CamillaClient()
    st = store or Store()
    jr = jobs or JobRunner()
    follower = VolumeFollower(cam, st, fetch=player_fetch or bluos.fetch_status)
    guard = BitstreamGuard(cam, st, jr)
    # Rig factories, replaceable in tests: name -> callable(devices) -> Rig
    rig_for = rigs or {
        "room": lambda devices: runner.real_rig(devices["mic"]["card"]),
        "selftest": lambda devices: runner.selftest_rig(),
    }
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        tasks = [asyncio.create_task(recover_from_interrupted_measurement(cam, st, jr)),
                 asyncio.create_task(follower.run()),
                 asyncio.create_task(guard.run())]
        yield
        for t in tasks:
            t.cancel()

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
            "volume": follower.summary(),
            "guard": guard.summary(),
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
            player = await follower.refresh_player()
            if player is not None and not player.analog:
                raise HTTPException(409, f"The amplifier is on {player.input or 'another source'}, not its analog "
                                    "input, so the main speakers would stay silent. Switch it to the analog input "
                                    "in the BluOS app and measure again.")
            if player is not None and player.mute:
                raise HTTPException(409, "The amplifier is muted. Unmute it in the BluOS app and measure again.")
        return rig_for["selftest" if selftest else "room"](devices)

    def player_reference() -> dict | None:
        """The amplifier's volume now, saved with a measurement: the sub
        follows changes from this point."""
        p = follower.player
        return {"db": p.db, "volume": p.volume, "input": p.input} if p and p.db is not None else None

    async def use_reference_of(result: dict) -> None:
        ref = result.get("player")
        if ref and ref.get("db") is not None:
            st.save_settings(reference_db=ref["db"], reference_volume=ref.get("volume"))
        await follower.apply(force=True)

    # Self-test results live apart from the real ones, so running the silent
    # self-test can never replace a measurement of the room.
    st_self = Store(st.root / "selftest", st.camilla_dir)

    def data(selftest: bool) -> Store:
        return st_self if selftest else st

    @app.post("/api/measure/bass", status_code=202)
    async def measure_bass(body: BassRequest):
        rig = await ready_rig(body.selftest)
        mic_curve = None if body.selftest else st.mic_curve()
        reference = None if body.selftest else player_reference()

        def work(progress, cancel):
            with runner.Session(cam, st.config_dir, progress, cancel) as session:
                out = runner.measure_bass(session, rig, mic_curve)
            progress("Saving", 0.95)
            result = {"plan": out["plan"], "level": out["level"], "selftest": body.selftest,
                      "mic_calibrated": mic_curve is not None, "player": reference,
                      "curves": runner.curves(out["responses"], out["plan"])}
            if body.selftest:
                result["verdict"] = runner.selftest_verdict(out["plan"])
            return data(body.selftest).save_bass(result, out["responses"])

        try:
            return jr.start("selftest" if body.selftest else "bass", work)
        except Busy as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/bass")
    def bass_result(selftest: bool | None = None):
        """The real or the self-test bass result; without `selftest`, the more
        recent of the two. `other` tells whether the other one exists."""
        real, self_ = st.bass(), st_self.bass()
        if selftest is None:
            selftest = bool(self_) and (not real or self_.get("measured", "") > real.get("measured", ""))
        return {"result": self_ if selftest else real, "other": bool(real if selftest else self_)}

    @app.post("/api/bass/crossover")
    def choose_crossover(body: CrossoverChoice):
        """Re-plan the last bass measurement for another crossover; no new sweeps."""
        if jr.running():
            raise HTTPException(409, "Wait for the measurement to finish.")
        d = data(body.selftest)
        result, responses = d.bass(), d.bass_responses()
        if not result or not responses:
            raise HTTPException(409, "Measure the bass setup first.")
        allowed = [o["crossover_hz"] for o in result["plan"].get("crossover_options", []) if "excluded" not in o]
        if body.crossover_hz is not None and body.crossover_hz not in allowed:
            raise HTTPException(422, f"Choose one of {', '.join(f'{f} Hz' for f in allowed)}.")
        plan = bass_dsp.align(responses, prefer=body.crossover_hz)
        result = {**result, "plan": plan, "curves": runner.curves(responses, plan)}
        if result.get("selftest"):
            result["verdict"] = runner.selftest_verdict(plan)
        return {"result": d.update_bass(result)}

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
        await use_reference_of(result)
        return {"calibration": calibration_summary()}

    # ---- room correction ----

    def room_basis(selftest: bool) -> dict:
        """The bass measurement room correction builds on (its plan and level)."""
        bass = data(selftest).bass()
        if not bass:
            raise HTTPException(409, "Run the bass self-test first." if selftest else "Measure the bass setup first.")
        return bass

    def current_positions(bass: dict | None, selftest: bool) -> list[dict]:
        if not bass:
            return []
        return [p for p in data(selftest).positions() if p["plan"] == bass["plan"]]

    @app.get("/api/room")
    def room_state(selftest: bool = False):
        d = data(selftest)
        current = {p["index"] for p in current_positions(d.bass(), selftest)}
        return {
            "positions": [{"index": p["index"], "measured": p["measured"], "selftest": p["selftest"],
                           "warnings": p.get("warnings", []), "current": p["index"] in current}
                          for p in d.positions()],
            "recommended": room.RECOMMENDED_POSITIONS,
            "build": d.room_build(),
            "targets": list(room.TARGETS),
        }

    @app.delete("/api/room", status_code=204)
    def clear_room(selftest: bool = False):
        if jr.running():
            raise HTTPException(409, "Wait for the measurement to finish.")
        data(selftest).clear_room()

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
            data(body.selftest).save_position(body.position, m, plan, body.selftest)
            return {"position": body.position, "warnings": m["warnings"]}

        try:
            return jr.start("room-selftest" if body.selftest else "room", work)
        except Busy as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/room/build")
    async def build_room(body: RoomBuild):
        if body.target not in room.TARGETS:
            raise HTTPException(422, f"Unknown target. Choose one of {', '.join(room.TARGETS)}.")
        bass = room_basis(body.selftest)
        positions = current_positions(bass, body.selftest)
        if not positions:
            raise HTTPException(409, "Measure at least one microphone position first.")
        try:
            result = await asyncio.to_thread(room.build, positions, body.target, room.FIR_TAPS, bass["plan"])
        except MeasurementError as e:
            raise HTTPException(409, str(e)) from e
        summary = {
            "target": result["target"], "positions": result["positions"], "preamp_db": result["preamp_db"],
            # The bass plan with the sub level corrected (bass_lift_db) as part of the room correction.
            "plan": result["plan"], "bass_lift_db": result["bass_lift_db"], "selftest": body.selftest,
            "sides": {side: {k: v for k, v in s.items() if k.endswith("_db") or k == "knee_hz"}
                      for side, s in result["sides"].items()},
            "curves": room.curves_for_ui(result),
        }
        return {"build": data(body.selftest).save_room_build(summary, result["firs"])}

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
        await use_reference_of(st.bass() or {})
        return {"calibration": calibration_summary()}

    @app.post("/api/compare")
    async def compare(body: Compare):
        """Room correction on (the applied setup) or off (the same bass setup and
        preamp without the FIRs), for comparing by ear."""
        if jr.running():
            raise HTTPException(409, "Wait for the measurement to finish.")
        if (st.active() or {}).get("kind") != "room":
            raise HTTPException(409, "Apply a room correction first.")
        path = st.config_dir / ACTIVE_CONFIG if body.room_correction else await asyncio.to_thread(st.write_compare)
        try:
            await cam.call("SetConfigFilePath", str(path))
            await cam.call("Reload")
        except (CamillaError, OSError) as e:
            raise HTTPException(502, f"CamillaDSP did not switch: {e}") from e
        await follower.apply(force=True)
        return {"room_correction": body.room_correction}

    # ---- check (stage C) ----

    @app.post("/api/measure/check", status_code=202)
    async def measure_check(body: CheckRequest):
        """Measure at the main seat through the room correction, and compare
        with the target and with position 1 (the same seat, before)."""
        d = data(body.selftest)
        build = d.room_build()
        if not build:
            raise HTTPException(409, "Build the room correction from the self-test first." if body.selftest
                                else "Build and apply the room correction first.")
        if not body.selftest:
            active = st.active() or {}
            if active.get("kind") != "room":
                raise HTTPException(409, "Apply the room correction first.")
            if build.get("built") != active.get("room", {}).get("built", build.get("built")):
                raise HTTPException(409, "The last built correction is not the one applied. Apply it, then check.")
        bass = room_basis(body.selftest)
        rig = await ready_rig(body.selftest)
        mic_curve = None if body.selftest else st.mic_curve()
        before = next((p for p in current_positions(bass, body.selftest) if p["index"] == 1), None)
        fir = await asyncio.to_thread(d.write_room_firs, "check")
        plan, level = build["plan"], bass["level"]["sweep_db"]

        def work(progress, cancel):
            with runner.Session(cam, st.config_dir, progress, cancel) as session:
                progress("Loading the corrected setup", 0.05)
                m = runner.measure_position(session, rig, plan, level, mic_curve, fir=fir,
                                            preamp_db=build["preamp_db"])
            progress("Saving", 0.97)
            result = room.check(m, build["target"], before=before, predicted=build.get("curves"))
            result.update({"selftest": body.selftest, "compared_with_position": 1 if before else None,
                           "built": build.get("built"), "warnings": m["warnings"]})
            return d.save_check(result)

        try:
            return jr.start("check-selftest" if body.selftest else "check", work)
        except Busy as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/check")
    def check_result(selftest: bool = False):
        return {"result": data(selftest).check()}

    # ---- volume ----

    @app.get("/api/volume")
    async def volume():
        await follower.refresh_player()
        await follower.apply()
        return follower.summary()

    @app.put("/api/volume")
    async def set_volume(body: VolumeSettings):
        changes = {}
        if body.sub_level_db is not None:
            changes["sub_level_db"] = round(body.sub_level_db, 1)
        if body.follow_volume is not None:
            changes["follow_volume"] = body.follow_volume
        if body.forget_bluos_host:
            changes["bluos_host"] = None
        elif body.bluos_host is not None:
            host = body.bluos_host.strip()
            if not HOST_RE.match(host):
                raise HTTPException(422, "Enter the amplifier's IP address or name, e.g. 192.168.1.50.")
            try:
                await asyncio.to_thread(follower.fetch, host)
            except bluos.BluOSError as e:
                raise HTTPException(422, f"No BluOS player answered at {host}. Check the address. ({e})") from e
            changes["bluos_host"] = host
        st.save_settings(**changes)
        await follower.refresh_player()
        await follower.apply(force=True)
        return follower.summary()

    @app.post("/api/volume/reference")
    async def set_reference():
        """Take the amplifier's current volume as the one the bass was calibrated at."""
        player = await follower.refresh_player()
        if player is None or player.db is None:
            raise HTTPException(409, follower.error or "Set the amplifier's address first.")
        st.save_settings(reference_db=player.db, reference_volume=player.volume)
        await follower.apply(force=True)
        return follower.summary()

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
