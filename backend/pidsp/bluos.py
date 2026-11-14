"""A small client for the BluOS player API (Bluesound Powernode and others).

BluOS players answer plain HTTP on port 11000. /Status describes what is
playing, the input, the volume (0-100 and in dB) and mute. With
?timeout=N&etag=E it long-polls: the request returns as soon as anything
changes, or after N seconds, so following the volume costs one request per
change instead of polling every second.

Only reads are used. PiDSP never changes the player's settings.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass

PORT = 11000


class BluOSError(Exception):
    pass


@dataclass
class PlayerStatus:
    volume: int | None        # 0-100, as in the BluOS app
    db: float | None          # the same volume in dB (0 dB = full)
    mute: bool
    input: str                # what the app shows, e.g. "Analog Input 1"
    input_type: str           # e.g. "analog-1", "hdmi-arc", "" when streaming
    service: str              # e.g. "Capture" for an input, "Spotify", ...
    state: str                # "stream", "play", "pause", "stop", ...
    etag: str

    @property
    def analog(self) -> bool:
        """Is the player on an analog input (where PiDSP's output comes in)?"""
        return self.input_type.startswith("analog") or (
            self.service == "Capture" and "analog" in self.input.lower())

    def to_dict(self) -> dict:
        return {**asdict(self), "analog": self.analog}


def parse_status(xml_text: str) -> PlayerStatus:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise BluOSError(f"unreadable answer from the player: {e}") from e

    def text(tag: str, default: str = "") -> str:
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else default

    def number(tag: str, kind=float):
        try:
            return kind(text(tag))
        except ValueError:
            return None

    return PlayerStatus(
        volume=number("volume", int),
        db=number("db"),
        mute=text("mute", "0") == "1",
        input=text("title1") or text("name"),
        input_type=text("inputTypeIndex"),
        service=text("service"),
        state=text("state"),
        etag=root.get("etag", ""),
    )


def fetch_status(host: str, etag: str | None = None, wait: int | None = None,
                 timeout: float = 5.0) -> PlayerStatus:
    """The player's /Status. With etag and wait (seconds), long-poll: return
    when anything differs from that etag, or after `wait` seconds."""
    query = {}
    if wait:
        query["timeout"] = str(int(wait))
        if etag:
            query["etag"] = etag
    url = f"http://{host}:{PORT}/Status" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    try:
        with urllib.request.urlopen(url, timeout=timeout + (wait or 0)) as r:
            return parse_status(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise BluOSError(f"the player at {host} did not answer: {e}") from e


def fetch_name(host: str, timeout: float = 5.0) -> str:
    """The player's name, from /SyncStatus (e.g. "POWERNODE 2i")."""
    try:
        with urllib.request.urlopen(f"http://{host}:{PORT}/SyncStatus", timeout=timeout) as r:
            root = ET.fromstring(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, TimeoutError, ET.ParseError) as e:
        raise BluOSError(f"the player at {host} did not answer: {e}") from e
    return root.get("name") or root.get("modelName") or host
