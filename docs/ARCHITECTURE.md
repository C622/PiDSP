# PiDSP architecture (draft)

This is a living document. Decisions marked **(proposed)** are open for change.

## Components

| Component | Choice | Notes |
|---|---|---|
| OS | Raspberry Pi OS Lite, 64-bit **(proposed)** | Headless. Same base moOde uses. |
| DSP engine | CamillaDSP | Runs as a systemd service and is controlled over its websocket API. |
| Backend | Python (FastAPI) **(proposed)** | Measurement, analysis, filter generation and CamillaDSP control (`pycamilladsp`). NumPy/SciPy for DSP. |
| Frontend | Lightweight SPA, served by the backend **(proposed)** | Measurement wizard and plots. |
| Image build | `pi-gen` in GitHub Actions **(proposed)** | Outputs a flashable `.img.xz` attached to GitHub Releases. |
| Updates | GitHub Releases API | The web UI checks for the latest release, downloads it, swaps it in and restarts the service. Rollback keeps the previous version. |

## Measurement pipeline

1. **Sweep**: exponential (log) sine sweep, 20 Hz to 20 kHz, 48 kHz sample rate, with a fade-in/out and a pre/post silence.
2. **Playback/record**: playback goes through the output device (bypassing correction filters) while the UMIK-1 records. The UMIK-1 is a separate USB device with its own clock, so:
   - all channels are measured in **one continuous capture** (or against a shared acoustic timing reference) so that *relative* delays between channels are valid;
   - clock drift is small over a short sweep, but should be checked.
3. **Calibration**: apply the UMIK-1 calibration file (unique per serial number, downloaded from miniDSP) and its sensitivity factor.
4. **Deconvolution**: the inverse-sweep method (Farina) produces the impulse response. Harmonic distortion components separate out in time and are windowed away.
5. **Multiple positions** (optional): average several mic positions around the listening position to give a more robust correction.

## Filter generation

- **Time alignment**: find each channel's arrival time from its impulse response (onset / cross-correlation) and delay the earlier channels to match the latest. For the sub, choose delay and polarity so its summation with the mains around the crossover is maximal (by measuring sub + mains together and sweeping delay).
- **Crossover**: Linkwitz-Riley (LR4 by default) between mains and sub. The frequency is chosen from the measured low-frequency extension of the mains (DALI Rubicon LCR), typically 60 to 100 Hz.
- **Room correction**:
  - correct mainly below the Schroeder frequency (around 300 to 500 Hz in a typical room), and only gently above it;
  - prefer cutting peaks over boosting dips, and cap the maximum boost;
  - frequency-dependent windowing / psychoacoustic smoothing before inversion;
  - user-selectable target curve (e.g. Harman-like gentle tilt);
  - output is a minimum-phase or mixed-phase FIR, 48 kHz, 16k–64k taps, saved as `.wav` for CamillaDSP's `Conv` filter.
- **Latency**: FIR length and phase type set the latency. This matters for TV use (lip sync), so offer a "low-latency" mode with shorter, minimum-phase filters.

## Signal chain: open question for the reference setup

The Bluesound Powernode 2i HDMI is an integrated streamer and amp with its own sub output and bass management. For PiDSP to control the crossover and time alignment, both the mains and the sub must pass through the Pi. Options:

| Option | Chain | Pros | Cons |
|---|---|---|---|
| A | TV → (optical) → Pi → analog L/R → Powernode analog in (full-range, sub out off); Pi → analog sub out → DB4S | PiDSP controls everything | Extra AD/DA step in the Powernode. Streaming on the Powernode itself bypasses the Pi. |
| B | Powernode → sub out → Pi → DB4S (sub only) | Simple, keeps the Powernode as the source | No correction of the mains. Limited time alignment. |
| C | Source → Pi → digital out → Powernode (optical) | Single digital path | Needs a card with S/PDIF out. The Powernode still applies its own sub crossover. |

The DB4S also has its own app-based room EQ and DSP. That should be set flat/bypassed while PiDSP is in control.

To verify on the CM6206: S/PDIF input capture under ALSA, multichannel analog output mapping, and the supported sample rates (48 kHz).

## Repository layout (planned)

```
backend/     Python app: measurement, analysis, filters, CamillaDSP control, updater
web/         Frontend
image/       pi-gen stage(s) and config for the flashable image
deploy/      Dev scripts: deploy to the Pi over SSH, systemd units
docs/        Documentation
tests/       Unit tests (incl. synthetic impulse-response fixtures)
```
