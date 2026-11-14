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
- **Crossover**: Linkwitz-Riley (LR4 by default) between mains and sub. Every standard frequency from 60 to 120 Hz that the speakers allow is planned and scored on the predicted result at the seat; the best one wins.
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
  1. **Follow the Powernode's volume** **(built, see "Runtime" below)**: PiDSP reads the volume through the BluOS HTTP API (port 11000, long-polling `/Status`) and applies the same change to the sub channel in CamillaDSP. The Powernode remote/app stays the volume control. The same volume reading can drive loudness compensation, as the Omarchy plugin's loudness-tracker does with the desktop volume.
  2. Keep the Powernode at a fixed level and set the volume in CamillaDSP from the PiDSP web UI. This is simpler but less convenient.
- **Alternatives to the optical input**, kept in case the TV's optical output is a problem:
  - The Pi as an **AirPlay 2 receiver** (shairport-sync, as in moOde). The Apple TV streams straight to the Pi and compensates for the delay itself. This needs testing for video use.
  - An HDMI audio extractor between the Apple TV and the TV.

**Verified on the reference setup:** S/PDIF capture from the CM6206 at 48 kHz with a live signal; the sub on channels 3–4 in 6-channel mode; the TCL's optical output set to PCM (it sends Dolby Digital at 640 kbps when set to Auto/Bitstream, which PiDSP now detects); the BluOS `/Status` long-poll (volume, dB, mute, input).

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
| C. Check | the same as B at the main seat, with the FIRs and preamp active | `config.build(plan, fir=..., preamp_db=..., measure_side=...)` | `room.check()`: deviation from the target before (position 1, same seat) and after |

The measurement runs in `measure/`:
- `audio.py` plays and records with `aplay`/`arecord`. Any ALSA overrun or underrun fails the measurement, because dropped samples would shift every later sweep.
- `runner.py` owns CamillaDSP for the duration of a measurement (`Session`). It loads `measure.yml`, and afterwards it restores the previous config, or stops CamillaDSP if that fails. On start-up, the web service also recovers from a measurement interrupted by a crash or power cut.
- **Level:** quiet probes at −40 dBFS, first on the mains (200 Hz–4 kHz), then on the sub alone (25–120 Hz), set each speaker's sweep level so that the microphone peaks around −35 dBFS, about 80 dB SPL on a UMIK-1. The output never goes above −12 dBFS. If a probe alone reaches −20 dBFS at the microphone (about 95 dB SPL), the measurement stops before any sweep. Nothing louder than the probe plays on the sub before its own probe.
- **Sweeps:** the mains get full-range sweeps (10 Hz–22 kHz); the sub gets a 10–300 Hz sweep of the same length, so about 2.4 times more time per bass octave. The analysis divides by the exact played signal, so per-channel levels and bands drop out. The playback is located in the recording by the mains' sweeps only.
- **Silent self-test:** a second "rig" sends CamillaDSP's output back into the loopback through simulated speakers (CamillaDSP filters with known delays and roll-offs) and records that. On the Pi it recovers the simulated 0.6 ms left/right offset exactly and sums to −0.12 dB at the crossover. Measuring the applied plan and FIRs through it matches the predictions within 0.8 dB (bass plan, 25–400 Hz) and 0.45 dB (FIRs, 30 Hz–16 kHz).

Design decisions:
- **The measurement player writes to an ALSA loopback** (`snd-aloop`), and CamillaDSP captures from it during measurement. The sweeps then go through exactly the output path used for listening.
- **The FIR goes before the mixer**, one per input channel, so the sub gets corrected bass as well. Each FIR is fitted on "that main + sub" as heard at the seat.
- **The sub's level is part of the room correction:** the median shortfall from 30 Hz to fc/1.4 against the target (after level matching at 300–3000 Hz, both sides) is tonal balance, not a dip, so it changes the sub's trim (capped at ±6 dB, never above the sub's input level) instead of being half-filled by the FIR. Its effect on "main + sub" is exact, because the LR4 outputs are in phase: |HP + g·LP| = |HP| + g·|LP|. The build carries the adjusted plan. On the reference room with the Room target this was +6 dB (capped) and took the predicted deviation from ±2.7/3.5 to ±1.4/1.7 dB.
- **How much of the correction is trusted:** dips are only half filled (they are often cancellations that move with the head), and the correction fades where the signal is near the noise floor. It also shrinks where the deviation is small next to the uncertainty of the average over the mic positions (spread / √n): a peak every position sees is corrected almost fully even when the positions differ by 3–5 dB, which is normal at 30–50 cm apart; a small wiggle they disagree on is left alone. (An earlier rule, 1 / (1 + (spread / 2)²), kept only 15–35 % of the correction on the reference room.)
- **In normal use the sub gets (L + R) / 2.** For mono bass, which is most bass, that is the full signal. So in stage B, the sub is fed the measured side at 0 dB. That way the correction is fitted for the level the sub really plays at.
- **Sub delay and polarity** come from searching ±30 ms and both polarities for the best summation through the crossover band (fc/2 to 2·fc). Delays a period apart sum about as well at fc, so the tie-break is the candidate nearest the delay that lines up the bass energy of mains and sub: the cross-correlation of the Hilbert envelopes of their impulse responses through the crossover filters (the LR4 outputs are in phase, so the crossover itself drops out; the speakers' roll-offs and the devices' latencies are in the measurement). On the reference room this gave 7.0 ± 0.5 ms in three measurements with the door open and closed. Two simpler measures failed there: the group delay at fc jumped between −1 and 45 ms from one crossover to the next, and the single impulse peak of the sub jumped by 11 ms when a door was closed (room-mode humps). Only earlier speakers are ever delayed.
- **Crossover:** each of 60–120 Hz between 1.25 × the mains' in-room −6 dB point and the sub's own −6 dB point gets its own sub level, delay and polarity, and a score in dB (lower is better): the bumpiness of the predicted bass at the seat (25–250 Hz, third-octave smoothed, both sides, dips counting half), plus 2 × any summation loss through the crossover band (room correction cannot fill a cancellation), plus 1.5 dB per octave the crossover sits below 2 × the mains' −6 dB point (mains strain), plus 2 dB per octave above 80 Hz (localisation: the higher the sub plays, the easier it is to hear where it stands). Scores within 0.25 dB are equal; then the one nearest 2 × the mains' −6 dB point wins. The UI shows the whole table, and the listener can pick another allowed crossover there: the plan is worked out again from the saved responses, with no new sweeps.
- **Sub level:** the trim may cut the sub by up to 30 dB (some subs, like the DB4S, only go 10 dB down in their own app, and a sub next to the seat measures much louder than the mains) or raise it by up to 6 dB.
- **The sub's Volume filter listens to fader `Aux1`.** The BluOS follower sets Aux1 to (Powernode volume − volume at calibration). Measurement configs leave the Volume filter out, so measurements always see the calibrated level.
- **Self-test results are kept apart** (`/var/lib/pidsp/selftest/`): running the silent self-test never replaces a measurement of the room.
- **Verified on the Pi:** the CM6206 is ALSA card `ICUSBAUDIO7D`, with up to 8-channel output and S/PDIF as a capture source. PiDSP opens it with 6 channels. It and the UMIK-1 are both full-speed USB devices behind a single-TT hub, and with 8 channels there is no USB bandwidth left for the microphone. 6 channels plus the microphone fit (a measurement captures from the loopback, so the optical input is released first), as do 6 channels plus the optical input. In 4-channel mode channels 3–4 do not reach the centre/sub jack. Measured: channels 3 and 4 (centre, LFE) in 6-channel mode drive the centre/sub jack. The UMIK-1 is card `U18dB` (S24_3LE, 48 kHz only), and its first ~0.3 s after opening is a start-up burst, which the recorder skips. CamillaDSP 4.1.3 accepts every generated config and loads the FIR WAVs.
- **Verified with sound (2026-11-14):** stage A and B on the reference room (5 positions); through the applied plan, position 1 matched the prediction from the bass measurement within 1.2–1.4 dB (25–250 Hz). Stage C is built but not yet run with sound.

## Runtime (pidsp-web background tasks)

- **Volume following** (`pidsp/volume.py`, `pidsp/bluos.py`): long-polls the amplifier's `/Status` (30 s) and sets the sub fader (Aux1) to *sub level + (amplifier dB now − amplifier dB at calibration)*, clamped so the sub never exceeds the input level (the negation of its calibrated trim). When following and the amplifier is muted, or not on its analog input, the sub is muted too, rather than playing on alone. The amplifier's volume is saved with every bass measurement and becomes the reference when that plan is applied. The listener's sub level (−10 to +10 dB in the UI) rides on the same fader, so changing it needs no config reload. Before every real measurement PiDSP checks that the amplifier is on its analog input and not muted.
- **Bitstream guard** (`pidsp/guard.py`): a TV set to Auto/Bitstream sends IEC 61937 packets (Dolby Digital, DTS), which a PCM path plays as full-scale noise. The CM6206 does not expose the S/PDIF channel status, so the guard reads CamillaDSP's capture levels twice a second. Packed bitstream shows both channels peaking at full scale with steady, near-equal RMS (−16 to −4 dBFS) in every chunk; after 1.5 s of that, the guard stops CamillaDSP (silence), records the optical input directly and looks for the IEC 61937 sync words (Pa = 0xF872, Pb = 0x4E1F). Found: stay silent, report the format, and look again every 3 s until PCM returns. Not found (very loud, limited music): restart at once, and for 10 minutes only react to 10 s stretches. It acts only on the applied listening config, never during a measurement. Not yet tested with a real bitstream on the Pi (the detection was tested on synthetic IEC 61937 streams built like the TV's, and 60 s of live TV audio gave no false trigger).
- **A/B comparison:** with a room correction applied, the dashboard switches between it and `compare.yml`: the same bass setup and the same preamp without the FIRs, so the comparison is not a level difference. Switching reloads CamillaDSP (a short gap). After a restart PiDSP plays the applied setup again.
- **Restart behaviour:** CamillaDSP starts the config named in its statefile at boot (the applied `active.yml`, with the fader values it had). If a measurement was interrupted, the web service restores the applied config or stops CamillaDSP.

## Repository layout (planned)

```
backend/     Python app: measurement, analysis, filters, CamillaDSP control, updater
web/         Frontend
image/       pi-gen stage(s) and config for the flashable image
deploy/      Dev scripts: deploy to the Pi over SSH, systemd units
docs/        Documentation
tests/       Unit tests (incl. synthetic impulse-response fixtures)
```
