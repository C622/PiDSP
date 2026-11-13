"""Tests for pidsp.camilla.config."""

import yaml

from pidsp.camilla import config

PLAN = {
    "crossover_hz": 80,
    "crossover_order": 4,
    "channels": {
        "left": {"delay_ms": 11.55, "trim_db": 0.0, "inverted": False},
        "right": {"delay_ms": 10.95, "trim_db": -0.8, "inverted": False},
        "sub": {"delay_ms": 0.0, "trim_db": 1.5, "inverted": True},
    },
}


def steps_for(cfg, channel):
    """Filters on an output channel, after the mixer."""
    names = [s.get("name") for s in cfg["pipeline"]]
    after = cfg["pipeline"][names.index("to_2_1") + 1:] if "to_2_1" in names else cfg["pipeline"]
    return [n for s in after if s["type"] == "Filter" and channel in (s.get("channels") or []) for n in s["names"]]


def test_running_config_is_consistent():
    cfg = config.build(PLAN, fir={"left": "/var/lib/pidsp/fir_l.wav", "right": "/var/lib/pidsp/fir_r.wav"},
                       preamp_db=-6.5)
    assert config.problems(cfg) == []
    # Round-trips through YAML unchanged.
    assert yaml.safe_load(config.to_yaml(cfg)) == cfg


def test_signal_flow():
    cfg = config.build(PLAN, fir={"left": "l.wav", "right": "r.wav"}, preamp_db=-6.5)
    names = [s.get("name") or s["names"] for s in cfg["pipeline"]]
    # FIR before the mixer, crossover after it.
    assert names.index("to_2_1") > names.index(["fir_left"])
    assert steps_for(cfg, 0)[0] == "xo_highpass"
    assert steps_for(cfg, 2)[0] == "xo_lowpass" and steps_for(cfg, 3)[0] == "xo_lowpass"
    assert "delay_left" in steps_for(cfg, 0) and "delay_sub" not in cfg["filters"]
    assert cfg["filters"]["trim_sub"]["parameters"]["inverted"] is True
    assert cfg["filters"]["sub_volume"]["parameters"]["fader"] == "Aux1"
    assert "sub_volume" not in steps_for(cfg, 0)
    sub_sources = [m["sources"] for m in cfg["mixers"]["to_2_1"]["mapping"] if m["dest"] == 3][0]
    assert [(s["channel"], s["gain"]) for s in sub_sources] == [(0, -6.02), (1, -6.02)]
    # S/PDIF in and the DAC have separate clocks.
    assert cfg["devices"]["resampler"]["type"] == "AsyncSinc"


def test_measure_side_feeds_sub_full_level_from_loopback():
    cfg = config.build(PLAN, measure_side="right")
    assert config.problems(cfg) == []
    sub_sources = [m["sources"] for m in cfg["mixers"]["to_2_1"]["mapping"] if m["dest"] == 2][0]
    assert [(s["channel"], s["gain"]) for s in sub_sources] == [(1, 0.0)]
    assert "Loopback" in cfg["devices"]["capture"]["device"]
    assert "resampler" not in cfg["devices"]


def test_measure_raw():
    cfg = config.measure_raw()
    assert config.problems(cfg) == []
    assert cfg["devices"]["capture"]["channels"] == 3
    dests = {m["dest"]: m["sources"][0]["channel"] for m in cfg["mixers"]["raw_2_1"]["mapping"]}
    assert dests == {0: 0, 1: 1, 2: 2, 3: 2}


def test_hardware_override():
    cfg = config.build(PLAN, hardware={"playback": {"channels": 2}, "outputs": {"left": [0], "right": [1], "sub": []}})
    # Two playback channels and no sub output is still consistent.
    assert config.problems(cfg) == []


def test_problems_catches_mistakes():
    cfg = config.build(PLAN)
    cfg["devices"]["playback"]["channels"] = 2
    cfg["pipeline"].append({"type": "Filter", "channels": [9], "names": ["nope"]})
    msgs = config.problems(cfg)
    assert any("nope" in m for m in msgs)
    assert any("channel 9" in m for m in msgs)
