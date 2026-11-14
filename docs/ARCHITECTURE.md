# PiDSP architecture (draft)

This is a living document. Decisions marked **(proposed)** are open for change.

## Components

| Component | Choice | Notes |
|---|---|---|
| OS | Raspberry Pi OS Lite, 64-bit **(proposed)** | Headless. Same base moOde uses. |
| DSP engine | CamillaDSP | Runs as a systemd service and is controlled over its websocket API. |
| Backend | Python (FastAPI) **(proposed)** | Measurement, analysis, filter generation and CamillaDSP control (`pycamilladsp`). NumPy/SciPy for DSP. The DSP core comes from omarchy-camilladsp (see below). |
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

## Signal chain for the reference setup

**Source:** Apple TV 4K (2nd gen). It is the only source, and it has HDMI output only. Lip sync is handled at the end by the Apple TV's own *Wireless Audio Sync* (calibrated with an iPhone), which measures the whole chain's delay. PiDSP therefore doesn't have to keep latency low, but the latency must stay **fixed** after calibration. Changing filter length or phase type means recalibrating.

**Chain (agreed):**

```
Apple TV ──HDMI──► TCL C8K ──optical (PCM stereo)──► Pi / CM6206 S/PDIF in
                                                    │ CamillaDSP
                                       ┌────────────┴────────────┐
                              analog L/R out              analog sub out
                                       ▼                         ▼
                          Powernode 2i analog in            B&W DB4S line in
                          (sub out unused, full range)
```

- The TV (TCL C8K, 2025) has an optical output. It must be set to **PCM stereo**, not Dolby/DTS bitstream. 2.1 is derived from stereo in CamillaDSP.
- Bass management on the Powernode and room EQ on the DB4S are switched off or flat. PiDSP handles the crossover, delay and EQ.
- **Volume control is the main problem.** If the Powernode sets the volume, only the mains follow it, and the sub, fed directly from the Pi, would not. Options:
  1. **Follow the Powernode's volume** **(direction agreed, details open)**: PiDSP reads the volume through the BluOS HTTP API (port 11000, long-polling `/Status`) and applies the same change to the sub channel in CamillaDSP. The Powernode remote/app stays the volume control. The same volume reading can drive loudness compensation, as the Omarchy plugin's loudness-tracker does with the desktop volume.
  2. Keep the Powernode at a fixed level and set the volume in CamillaDSP from the PiDSP web UI. This is simpler but less convenient.
- **Alternatives to the optical input**, kept in case the TV's optical output is a problem:
  - The Pi as an **AirPlay 2 receiver** (shairport-sync, as in moOde). The Apple TV streams straight to the Pi and compensates for the delay itself. This needs testing for video use.
  - An HDMI audio extractor between the Apple TV and the TV.

**To verify:**
- S/PDIF capture from the CM6206 under ALSA, at 48 kHz.
- Analog channel mapping for the sub output.
- The TCL's optical output setting for PCM.
- The BluOS volume API behaviour.

## Prior work: omarchy-camilladsp

[omarchy-camilladsp](https://github.com/C622/omarchy-camilladsp), by the same author, is a desktop CamillaDSP calibrator with a UMIK-1 measurement path that already works. PiDSP reuses its DSP core rather than starting again.

**Reused from `lib/calibration_dsp.py`** (pure NumPy/SciPy, already has tests on a simulated speaker and room):
- excitation: log sweeps plus a level probe, with automatic level choice
- deconvolution by regularised spectral division against the exact digital sweep. Latency drops out, and arrival times within one recording are kept, which handles the separate USB clocks of the UMIK-1 and the sound card.
- frequency-dependent windowing, noise floor and per-frequency SNR
- UMIK-1 / REW calibration file parsing
- averaging over several positions
- target curves
- cut-first PEQ fitting, minimum-phase FIR, and mixed-phase "room correction" timing FIR
- L/R alignment
- the safety budget
- refusing bad measurements (clipping, silence, noisy room)

**New in PiDSP:**
- sub channel handling
- crossover choice
- sub delay and polarity, chosen from the sub + mains sum
- ALSA playback/capture (no PipeWire on the Pi)
- volume follow via BluOS
- web UI
- updater and image

**Differences on an appliance:**
- **CamillaDSP websocket.** The plugin runs CamillaDSP without its websocket, because on a shared desktop it is unauthenticated. On PiDSP the websocket binds to `127.0.0.1` only, and the web UI (the only thing exposed on the LAN) talks to it.
- **Fixed latency.** The plugin's 50 ms room-correction latency is fine here because the Apple TV calibrates lip sync. It must just stay constant.
- **Licence.** The plugin is MIT. Copied files keep their MIT notice inside this GPL-3.0 project, which MIT permits.

## Calibration stages (as implemented in `backend/`)

| Stage | Plays | CamillaDSP config | Code |
|---|---|---|---|
| A. Bass setup | sweeps on left, right, sub in turn, in one recording | `config.measure_raw()`: 3 loopback inputs straight to the outputs, no filters | `dsp/bass.py`: `complex_responses()`, `align()` |
| B. Room correction | sweeps on input L, then input R, at 1–5 mic positions | `config.build(plan, measure_side=...)`: the full 2.1 plan, sub fed the measured side at 0 dB | `dsp/room.py`: `build()` on `calibration.analyze()`, `combine()`, `fir_correction()` (minimum phase, 8192 taps, no added latency) |
| C. Check (not built yet) | the same as B, with the FIRs active | `config.build(plan, fir=..., measure_side=...)` | `measure/runner.measure_side(fir=...)` |

The measurement runs in `measure/`:
- `audio.py` plays and records with `aplay`/`arecord`. Any ALSA overrun or underrun fails the measurement, because dropped samples would shift every later sweep.
- `runner.py` owns CamillaDSP for the duration of a measurement (`Session`). It loads `measure.yml`, and afterwards it restores the previous config, or stops CamillaDSP if that fails. On start-up, the web service also recovers from a measurement interrupted by a crash or power cut.
- **Level:** a quiet probe (−40 dBFS on the mains) sets the sweep level so that the microphone peaks around −35 dBFS, about 80 dB SPL on a UMIK-1. The output never goes above −12 dBFS.
- **Silent self-test:** a second "rig" sends CamillaDSP's output back into the loopback through simulated speakers (CamillaDSP filters with known delays and roll-offs) and records that. On the Pi it recovers the simulated 0.6 ms left/right offset exactly and sums to −0.12 dB at the crossover. Measuring the applied plan and FIRs through it matches the predictions within 0.8 dB (bass plan, 25–400 Hz) and 0.45 dB (FIRs, 30 Hz–16 kHz).

Design decisions:
- **The measurement player writes to an ALSA loopback** (`snd-aloop`), and CamillaDSP captures from it during measurement. The sweeps then go through exactly the output path used for listening.
- **The FIR goes before the mixer**, one per input channel, so the sub gets corrected bass as well. Each FIR is fitted on "that main + sub" as heard at the seat.
- **In normal use the sub gets (L + R) / 2.** For mono bass, which is most bass, that is the full signal. So in stage B, the sub is fed the measured side at 0 dB. That way the correction is fitted for the level the sub really plays at.
- **Sub delay and polarity** come from searching ±30 ms and both polarities for the best summation through the crossover band (fc/2 to 2·fc). Delays a period apart sum about as well at fc, so the tie-break is the candidate whose group delay at fc matches the mains'. Only earlier speakers are ever delayed.
- **Crossover:** the lowest of 60–120 Hz that is at least 2 × the mains' in-room −6 dB point, kept below the sub's −6 dB point / 1.25.
- **The sub's Volume filter listens to fader `Aux1`.** The BluOS follower sets Aux1 to (Powernode volume − volume at calibration).
- **Verified on the Pi:** the CM6206 is ALSA card `ICUSBAUDIO7D`, with 8-channel output and S/PDIF as a capture source. The UMIK-1 is card `U18dB` (S24_3LE, 48 kHz only), and its first ~0.3 s after opening is a start-up burst, which the recorder skips. CamillaDSP 4.1.3 accepts every generated config and loads the FIR WAVs.
- **Still to verify with sound:** which output channel carries the sub (the config feeds both centre and LFE), S/PDIF capture with a live signal, and the first real room measurement.

## Repository layout (planned)

```
backend/     Python app: measurement, analysis, filters, CamillaDSP control, updater
web/         Frontend
image/       pi-gen stage(s) and config for the flashable image
deploy/      Dev scripts: deploy to the Pi over SSH, systemd units
docs/        Documentation
tests/       Unit tests (incl. synthetic impulse-response fixtures)
```
