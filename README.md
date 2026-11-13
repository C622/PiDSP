# PiDSP

**Automated room correction, subwoofer crossover and time alignment on a Raspberry Pi.**

PiDSP turns a headless Raspberry Pi into a room-correction DSP. It uses [CamillaDSP](https://github.com/HEnquist/camilladsp) as the audio engine and a [miniDSP UMIK-1](https://www.minidsp.com/products/acoustic-measurement/umik-1) measurement microphone. The goal is to automate as much of the process as possible: measure → calculate → apply → verify, all from a web interface.

> **Status:** early planning / pre-alpha. Nothing works yet. See the [roadmap](#roadmap).

## Goals

- **Room correction**: FIR / convolution filters generated from in-room measurements, with a configurable target curve.
- **Subwoofer crossover**: automatic crossover frequency and slope selection between mains and sub.
- **Time alignment**: per-channel delay and polarity so the sub and mains arrive together at the listening position.
- **Headless**: configured from a web UI (SSH also works).
- **Easy install**: a ready-made SD/eMMC image published under GitHub Releases.
- **Self-updating**: the web UI can update PiDSP to the latest GitHub release.

Inspired by [moOde audio](https://moodeaudio.org/), which also ships a ready-to-flash Raspberry Pi image built around CamillaDSP. The measurement and filter-fitting core comes from [omarchy-camilladsp](https://github.com/C622/omarchy-camilladsp), a desktop CamillaDSP calibrator by the same author.

## How it works

```
 Source (TV / streamer)
        │  S/PDIF or analog
        ▼
 ┌──────────────────────────────┐        USB
 │  Raspberry Pi                │◄──────────────── UMIK-1 (measurement mic)
 │  ┌────────────────────────┐  │
 │  │ CamillaDSP             │  │
 │  │  • FIR room correction │  │
 │  │  • crossover           │  │
 │  │  • delay / gain        │  │
 │  └────────────────────────┘  │
 │  PiDSP web app (measure,     │
 │  calculate, apply, update)   │
 └──────────────┬───────────────┘
        USB sound card
        ┌───────┴────────┐
        ▼                ▼
   Mains (L/R)       Subwoofer
```

1. **Measure**: play logarithmic sine sweeps on each channel in turn and record them with the UMIK-1, applying its calibration file.
2. **Analyse**: deconvolve the recordings into impulse responses, then find the delay, polarity and frequency response of each channel.
3. **Calculate**: pick the crossover, delays and gains, and generate correction FIR filters against a target curve.
4. **Apply**: write the CamillaDSP config and filter `.wav` files, then hot-reload CamillaDSP.
5. **Verify**: measure again with the new filters running and show before/after graphs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## Reference hardware

The development and test setup:

| Role | Device |
|---|---|
| Computer | Raspberry Pi CM4 (32 GB eMMC) on a DFRobot PiTray mini |
| Sound card | CM6206-based USB card (budget, for testing; a better card can come later) |
| Microphone | miniDSP UMIK-1 |
| Source | Apple TV 4K (2nd gen), via the TV |
| Amplifier / streamer | Bluesound Powernode 2i HDMI |
| Front speakers | DALI Rubicon LCR (pair) |
| Subwoofer | B&W DB4S |
| Layout | 2.1 |

## Roadmap

- [ ] **0. Bring-up**: flash Raspberry Pi OS Lite (64-bit), enable SSH, install CamillaDSP, and get audio through the CM6206 and UMIK-1.
- [ ] **1. Measurement**: sweep generation, playback/record sync, UMIK-1 calibration, impulse responses.
- [ ] **2. Analysis**: frequency response, delay and polarity detection, plots.
- [ ] **3. Filter generation**: target curves, FIR correction, crossover, time alignment, CamillaDSP config output.
- [ ] **4. Web UI**: guided measurement wizard, graphs, config management, CamillaDSP status.
- [ ] **5. Updates**: in-app updater driven by GitHub Releases.
- [ ] **6. Image**: a pi-gen based image built by GitHub Actions and attached to releases.

## Development

The Pi runs headless. During development, code is deployed to it over SSH. See [docs/BRINGUP.md](docs/BRINGUP.md) to get a Pi to that point.

The DSP and config code is in `backend/` and tested on simulated speakers and rooms:

```sh
python3 -m venv .venv && .venv/bin/pip install -e "backend[dev]"
cd backend && ../.venv/bin/pytest
```

## License

[GPL-3.0](LICENSE), the same license as CamillaDSP and moOde.
