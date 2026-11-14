"""CamillaDSP (v4) configs for a 2.1 PiDSP system.

The running pipeline:

  capture (S/PDIF, 2 ch)
    -> preamp, room-correction FIR per input channel (stage B)
    -> mixer: L -> left outputs, R -> right outputs, (L + R) / 2 -> sub outputs
    -> left/right: LR4 high-pass, delay, trim
    -> sub: LR4 low-pass, delay, trim/polarity, volume (fader Aux1)
  playback (USB card, e.g. 8 ch on a CM6206)

The sub's Volume filter listens to fader Aux1. PiDSP sets Aux1 from the
Bluesound's volume, so the sub follows the volume set after CamillaDSP on the
amplifier. The FIR sits before the mixer, so the sub gets corrected bass too:
stage B fits each FIR on "that main + sub" as heard at the seat.

Measurement configs read from an ALSA loopback instead, where the measurement
player writes its sweeps:

  measure_raw()     stage A: left, right and sub each on their own input, no
                    filtering, so their raw responses can be measured
  build(..., measure_side="left")
                    stage B: the full plan, with the sub fed only the measured
                    side at full level (what it gets from mono bass)
"""

import copy

import yaml

# Hardware defaults for the reference setup. The CM6206 enumerates as
# "ICUSBAUDIO7D" (USB 0d8c:0102): 8-ch S16_LE playback, 2-ch capture with the
# S/PDIF input selected by the "PCM Capture Source" mixer control. Still to
# confirm: which half of the 3.5 mm centre/sub jack carries the sub, so the sub
# is sent to both centre and LFE (USB order FL FR FC LFE ...).
DEFAULT_HARDWARE = {
    "samplerate": 48000,
    "chunksize": 2048,
    "capture": {"type": "Alsa", "device": "hw:CARD=ICUSBAUDIO7D,DEV=0", "channels": 2, "format": "S16_LE"},
    "playback": {"type": "Alsa", "device": "hw:CARD=ICUSBAUDIO7D,DEV=0", "channels": 8, "format": "S16_LE"},
    "outputs": {"left": [0], "right": [1], "sub": [2, 3]},
    # The TV's S/PDIF clock and the DAC's clock are independent: let CamillaDSP
    # track the drift with an async resampler.
    "rate_adjust": True,
}

# Where the measurement player's sweeps come in (snd-aloop).
LOOPBACK_CAPTURE = {"type": "Alsa", "device": "hw:CARD=Loopback,DEV=1,SUBDEV=0", "format": "S32_LE"}
# The player writes here; the loopback hands it to LOOPBACK_CAPTURE.
LOOPBACK_PLAYER = "hw:CARD=Loopback,DEV=0,SUBDEV=0"

SUB_FADER = "Aux1"
SUB_VOLUME_RAMP_MS = 200


def _hardware(hardware):
    hw = copy.deepcopy(DEFAULT_HARDWARE)
    for key, value in (hardware or {}).items():
        if isinstance(value, dict) and isinstance(hw.get(key), dict):
            hw[key].update(value)
        else:
            hw[key] = value
    return hw


def _devices(hw, capture=None, channels=2):
    cap = dict(capture or hw["capture"])
    cap["channels"] = channels
    devices = {
        "samplerate": hw["samplerate"],
        "chunksize": hw["chunksize"],
        "capture": cap,
        "playback": dict(hw["playback"]),
    }
    loopback = "Loopback" in str(cap.get("device", ""))
    if hw["rate_adjust"]:
        devices["enable_rate_adjust"] = True
        # A loopback capture is tuned directly; anything else needs resampling.
        if not loopback:
            devices["resampler"] = {"type": "AsyncSinc", "profile": "Balanced"}
    return devices


def _mixer(hw, sources):
    """sources: {"left": [(in_ch, gain_db)], "right": [...], "sub": [...]}."""
    mapping = []
    for role in ("left", "right", "sub"):
        for dest in hw["outputs"][role]:
            mapping.append({"dest": dest,
                            "sources": [{"channel": ch, "gain": round(g, 2), "inverted": False}
                                        for ch, g in sources[role]]})
    return {"channels": {"in": sources["_in"], "out": hw["playback"]["channels"]},
            "mapping": sorted(mapping, key=lambda m: m["dest"])}


def measure_raw(hardware=None, title="PiDSP measurement: raw channels"):
    """Stage A: three loopback inputs straight to left, right and sub."""
    hw = _hardware(hardware)
    return {
        "title": title,
        "description": "Unfiltered left, right and sub, for measuring their raw responses.",
        "devices": _devices(hw, LOOPBACK_CAPTURE, channels=3),
        "mixers": {"raw_2_1": _mixer(hw, {"_in": 3, "left": [(0, 0.0)], "right": [(1, 0.0)], "sub": [(2, 0.0)]})},
        "pipeline": [{"type": "Mixer", "name": "raw_2_1"}],
    }


def build(plan, fir=None, preamp_db=0.0, hardware=None, measure_side=None, title="PiDSP"):
    """The 2.1 config for a bass plan (pidsp.dsp.bass.align()).

    fir: optional {"left": wav path, "right": wav path}, applied per input
    channel before the mixer. preamp_db: headroom for the FIR's boosts.
    measure_side: "left" or "right" for a stage B measurement config.
    """
    hw = _hardware(hardware)
    ch = plan["channels"]
    fc = plan["crossover_hz"]
    filters, pipeline = {}, []

    def add(name, kind, **params):
        filters[name] = {"type": kind, "parameters": params}
        return name

    if preamp_db:
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": [add("preamp", "Gain", gain=round(preamp_db, 2))]})
    for i, side in enumerate(("left", "right")):
        if fir and fir.get(side):
            pipeline.append({"type": "Filter", "channels": [i],
                             "names": [add(f"fir_{side}", "Conv", type="Wav", filename=str(fir[side]))]})

    if measure_side:
        src = 0 if measure_side == "left" else 1
        sub_sources = [(src, 0.0)]
    else:
        sub_sources = [(0, -6.02), (1, -6.02)]
    pipeline.append({"type": "Mixer", "name": "to_2_1"})
    mixer = _mixer(hw, {"_in": 2, "left": [(0, 0.0)], "right": [(1, 0.0)], "sub": sub_sources})

    add("xo_highpass", "BiquadCombo", type="LinkwitzRileyHighpass", freq=fc, order=plan.get("crossover_order", 4))
    add("xo_lowpass", "BiquadCombo", type="LinkwitzRileyLowpass", freq=fc, order=plan.get("crossover_order", 4))
    for role in ("left", "right", "sub"):
        spec = ch[role]
        names = ["xo_lowpass" if role == "sub" else "xo_highpass"]
        if spec.get("delay_ms"):
            names.append(add(f"delay_{role}", "Delay", delay=spec["delay_ms"], unit="ms", subsample=True))
        if spec.get("trim_db") or spec.get("inverted"):
            names.append(add(f"trim_{role}", "Gain", gain=spec.get("trim_db", 0.0),
                             inverted=bool(spec.get("inverted"))))
        if role == "sub":
            names.append(add("sub_volume", "Volume", ramp_time=SUB_VOLUME_RAMP_MS, fader=SUB_FADER))
        pipeline.append({"type": "Filter", "channels": list(hw["outputs"][role]), "names": names})

    desc = f"2.1, LR4 crossover at {fc} Hz"
    if measure_side:
        desc += f"; measuring {measure_side} + sub"
    return {
        "title": title,
        "description": desc,
        "devices": _devices(hw, LOOPBACK_CAPTURE if measure_side else None, channels=2),
        "filters": filters,
        "mixers": {"to_2_1": mixer},
        "pipeline": pipeline,
    }


def to_yaml(cfg):
    return yaml.safe_dump(cfg, sort_keys=False, default_flow_style=None, width=100)


def problems(cfg):
    """Structural checks CamillaDSP would also make: every step refers to a
    defined filter or mixer, channel counts line up through the pipeline, and
    the pipeline ends with as many channels as the playback device has.
    Returns a list of messages; empty means the config is consistent."""
    out = []
    channels = cfg["devices"]["capture"]["channels"]
    filters, mixers = cfg.get("filters", {}), cfg.get("mixers", {})
    for i, step in enumerate(cfg["pipeline"]):
        if step["type"] == "Mixer":
            mixer = mixers.get(step["name"])
            if mixer is None:
                out.append(f"step {i}: mixer {step['name']} is not defined")
                continue
            if mixer["channels"]["in"] != channels:
                out.append(f"step {i}: mixer {step['name']} takes {mixer['channels']['in']} channels, gets {channels}")
            for m in mixer["mapping"]:
                if not 0 <= m["dest"] < mixer["channels"]["out"]:
                    out.append(f"mixer {step['name']}: dest {m['dest']} out of range")
                for s in m["sources"]:
                    if not 0 <= s["channel"] < mixer["channels"]["in"]:
                        out.append(f"mixer {step['name']}: source {s['channel']} out of range")
            channels = mixer["channels"]["out"]
        elif step["type"] == "Filter":
            for name in step["names"]:
                if name not in filters:
                    out.append(f"step {i}: filter {name} is not defined")
            for c in step.get("channels") or []:
                if not 0 <= c < channels:
                    out.append(f"step {i}: channel {c} does not exist ({channels} channels here)")
    if channels != cfg["devices"]["playback"]["channels"]:
        out.append(f"pipeline ends with {channels} channels, playback has {cfg['devices']['playback']['channels']}")
    for name, f in filters.items():
        if f["type"] == "Delay" and f["parameters"]["delay"] < 0:
            out.append(f"filter {name}: negative delay")
    return out
