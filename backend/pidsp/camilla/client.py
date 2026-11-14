"""Minimal client for the CamillaDSP websocket API (v4).

Each request opens a short-lived connection. The server is local
(127.0.0.1) and calls are rare, so there is no need to keep a socket open.
"""

from __future__ import annotations

import asyncio
import json

import websockets

DEFAULT_URL = "ws://127.0.0.1:1234"

# Commands whose values make up the status summary.
STATUS_COMMANDS = (
    "GetVersion",
    "GetState",
    "GetStopReason",
    "GetCaptureRate",
    "GetProcessingLoad",
    "GetRateAdjust",
    "GetConfigFilePath",
    "GetClippedSamples",
)


class CamillaError(Exception):
    pass


class CamillaClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 2.0):
        self.url = url
        self.timeout = timeout

    async def _session(self, requests: list[tuple[str, object]]) -> dict[str, object]:
        """Send requests on one connection. A failed command maps to a CamillaError value."""
        out: dict[str, object] = {}
        async with websockets.connect(self.url, open_timeout=self.timeout, close_timeout=0.5) as ws:
            for command, arg in requests:
                await ws.send(json.dumps(command if arg is None else {command: arg}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), self.timeout))
                body = reply.get(command) or {}
                if body.get("result") == "Ok":
                    out[command] = body.get("value")
                else:
                    out[command] = CamillaError(f"{command}: {body.get('result', reply)}")
        return out

    async def call(self, command: str, arg: object = None) -> object:
        value = (await self._session([(command, arg)]))[command]
        if isinstance(value, CamillaError):
            raise value
        return value

    async def status(self) -> dict:
        """Summary for the UI. Never raises; `reachable` tells whether the service answered."""
        try:
            raw = await self._session([(c, None) for c in STATUS_COMMANDS])
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
            return {"reachable": False, "error": str(e) or type(e).__name__}
        v = {k: (None if isinstance(val, CamillaError) else val) for k, val in raw.items()}
        rate_adjust = v["GetRateAdjust"]
        return {
            "reachable": True,
            "version": v["GetVersion"],
            "state": v["GetState"],
            "stop_reason": v["GetStopReason"],
            "capture_rate": v["GetCaptureRate"],
            "load": v["GetProcessingLoad"],
            "clock_ppm": None if not rate_adjust else round((rate_adjust - 1.0) * 1e6, 1),
            "config_path": v["GetConfigFilePath"],
            "clipped_samples": v["GetClippedSamples"],
        }

    async def probe(self) -> dict | None:
        """State, config path and latest levels in one connection (for watchers
        that ask often). None if CamillaDSP does not answer."""
        try:
            r = await self._session([("GetState", None), ("GetConfigFilePath", None), ("GetSignalLevels", None)])
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
            return None
        val = lambda k: None if isinstance(r.get(k), CamillaError) else r.get(k)
        return {"state": val("GetState"), "config_path": val("GetConfigFilePath"), "levels": val("GetSignalLevels")}

    async def levels(self) -> dict | None:
        """Peak and RMS levels in dBFS of the latest chunk, or None if unavailable.

        Not GetSignalLevelsSinceLast: CamillaDSP keeps "since last" per
        websocket connection, and every call here opens a new one, so that
        always came back empty."""
        try:
            return await self.call("GetSignalLevels")
        except (CamillaError, OSError, asyncio.TimeoutError, websockets.WebSocketException):
            return None
