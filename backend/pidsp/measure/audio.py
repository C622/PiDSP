"""Play a signal and record at the same time, with aplay and arecord.

The recording runs from shortly before playback starts until shortly after it
ends. Latency does not matter: the analysis finds the sweeps in the recording
by cross-correlation.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile

FORMATS = {  # ALSA format -> (bytes per sample, full scale)
    "S16_LE": (2, 2 ** 15),
    "S24_3LE": (3, 2 ** 23),
    "S32_LE": (4, 2 ** 31),
}


class AudioError(Exception):
    pass


@dataclass
class Capture:
    device: str
    channels: int = 2
    format: str = "S24_3LE"


def decode(raw: bytes, fmt: str, channels: int) -> np.ndarray:
    """Interleaved little-endian PCM to float frames (n, channels) in [-1, 1)."""
    width, scale = FORMATS[fmt]
    usable = len(raw) - len(raw) % (width * channels)
    b = np.frombuffer(raw[:usable], dtype=np.uint8)
    if width == 3:
        b = b.reshape(-1, 3)
        x = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        x = np.where(x >= 1 << 23, x - (1 << 24), x)
    else:
        x = b.view("<i2" if width == 2 else "<i4").astype(np.int64)
    return (x.astype(np.float64) / scale).reshape(-1, channels)


def write_wav(path: Path, signal: np.ndarray, fs: int, fmt: str = "S32_LE") -> None:
    width, scale = FORMATS[fmt]
    if width == 3:
        raise ValueError("write S16_LE or S32_LE")
    x = np.clip(np.asarray(signal, dtype=np.float64), -1.0, 1.0 - 1.0 / scale)
    wavfile.write(path, fs, (x * scale).astype("<i2" if width == 2 else "<i4"))


def play_and_record(signal: np.ndarray, player: str, capture: Capture, fs: int = 48000,
                    play_format: str = "S32_LE", pre: float = 0.6, post: float = 0.7,
                    cancel: threading.Event | None = None) -> np.ndarray:
    """Play `signal` (n, channels) on ALSA device `player` while recording `capture`.

    Returns the recording as float frames (n, capture.channels). `pre` also
    skips the start-up burst some USB microphones (the UMIK-1) produce in
    their first few hundred milliseconds.

    Dropped samples on either side would shift every later sweep, so an ALSA
    overrun (recording) or underrun (playback) fails the measurement.
    """
    signal = np.atleast_2d(np.asarray(signal, dtype=np.float64).T).T
    with tempfile.TemporaryDirectory(prefix="pidsp-") as tmp:
        wav = Path(tmp) / "play.wav"
        write_wav(wav, signal, fs, play_format)
        rec = subprocess.Popen(
            ["arecord", "-q", "-D", capture.device, "-f", capture.format, "-c", str(capture.channels),
             "-r", str(fs), "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chunks: list[bytes] = []
        reader = threading.Thread(target=lambda: chunks.extend(iter(lambda: rec.stdout.read(65536), b"")),
                                  daemon=True)
        reader.start()
        try:
            time.sleep(pre)
            if rec.poll() is not None:
                raise AudioError(f"Could not record from {capture.device}: {rec.stderr.read().decode().strip()}")
            play = subprocess.Popen(["aplay", "-q", "-D", player, str(wav)], stderr=subprocess.PIPE)
            deadline = time.monotonic() + len(signal) / fs + 10
            while play.poll() is None:
                if cancel is not None and cancel.is_set():
                    play.terminate()
                    raise AudioError("Cancelled.")
                if time.monotonic() > deadline:
                    play.kill()
                    raise AudioError(f"Playback on {player} did not finish.")
                time.sleep(0.05)
            play_err = play.stderr.read().decode(errors="replace")
            if play.returncode != 0:
                raise AudioError(f"Could not play on {player}: {play_err.strip()}")
            if "underrun" in play_err:
                raise AudioError("Playback stuttered (underrun). Measure again.")
            time.sleep(post)
        finally:
            rec.terminate()
            try:
                rec.wait(timeout=2)
            except subprocess.TimeoutExpired:
                rec.kill()
            reader.join(timeout=2)
        rec_err = rec.stderr.read().decode(errors="replace")
    if "overrun" in rec_err:
        raise AudioError("The recording dropped samples (overrun). Measure again.")
    data = decode(b"".join(chunks), capture.format, capture.channels)
    if len(data) < len(signal):
        raise AudioError(f"The recording from {capture.device} is shorter than the playback.")
    return data
