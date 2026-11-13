"""PiDSP web server: JSON API under /api, the single-page UI everywhere else.

Run on the Pi with:  python3 -m uvicorn pidsp.web.app:app --host 0.0.0.0 --port 80

There is no login. Like moOde, PiDSP is meant for a home network and must not
be exposed to the internet.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, system
from ..camilla.client import CamillaClient

STATIC = Path(__file__).parent / "static"
# Root helper for the few privileged actions (see system/pidsp-admin).
ADMIN = os.environ.get("PIDSP_ADMIN", "/usr/local/sbin/pidsp-admin")


class PasswordChange(BaseModel):
    password: str = Field(min_length=8, max_length=128)


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


def create_app(camilla: CamillaClient | None = None, static_dir: Path = STATIC,
               proc: str = "/proc", sys: str = "/sys") -> FastAPI:
    cam = camilla or CamillaClient()
    app = FastAPI(title="PiDSP", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)

    @app.get("/api/status")
    async def status():
        return {
            "version": __version__,
            "system": system.info(proc, sys),
            "devices": system.audio_devices(proc, sys),
            "camilladsp": await cam.status(),
            "calibration": None,  # nothing has been measured yet
        }

    @app.get("/api/levels")
    async def levels():
        return {"levels": await cam.levels()}

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
