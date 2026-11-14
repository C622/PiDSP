"""One background measurement at a time, with progress and cancel."""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from typing import Callable

from ..dsp.calibration import MeasurementError
from .audio import AudioError
from .runner import Cancelled

log = logging.getLogger("pidsp.jobs")


class Busy(Exception):
    pass


class JobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._job: dict | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def running(self) -> bool:
        return self._job is not None and self._job["state"] == "running"

    def start(self, kind: str, fn: Callable[[Callable[[str, float], None], threading.Event], dict]) -> dict:
        with self._lock:
            if self.running():
                raise Busy("A measurement is already running.")
            self._cancel = threading.Event()
            self._job = {"id": uuid.uuid4().hex[:8], "kind": kind, "state": "running", "step": "Starting",
                         "progress": 0.0, "error": None, "result": None, "started": time.time(), "ended": None}
            job, cancel = self._job, self._cancel

        def progress(step: str, fraction: float):
            with self._lock:
                job["step"], job["progress"] = step, round(float(fraction), 3)

        def run():
            try:
                result = fn(progress, cancel)
                state, error = "done", None
            except Cancelled:
                result, state, error = None, "cancelled", None
            except (MeasurementError, AudioError) as e:
                result, state, error = None, "failed", str(e)
            except Exception as e:  # report, don't crash the server
                log.error("measurement failed:\n%s", traceback.format_exc())
                result, state, error = None, "failed", f"Unexpected error: {e}"
            with self._lock:
                job.update(state=state, error=error, result=result, ended=time.time(),
                           progress=1.0 if state == "done" else job["progress"])

        self._thread = threading.Thread(target=run, name=f"job-{kind}", daemon=True)
        self._thread.start()
        return self.snapshot()

    def cancel(self) -> None:
        self._cancel.set()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._job) if self._job else {"state": "idle"}

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)
