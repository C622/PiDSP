"""Sub level and volume following: the BluOS client, the fader maths, the API."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from pidsp import bluos
from pidsp.measure.jobs import JobRunner
from pidsp.store import Store
from pidsp.volume import VolumeFollower, sub_gain
from pidsp.web import app as webapp

from test_measure import FakeCamilla, fake_room, rig  # noqa: F401  (fixture)
from test_web import tree  # noqa: F401  (fixture)

# A Powernode's /Status on its analog input (trimmed).
STATUS_ANALOG = """<?xml version="1.0" encoding="UTF-8"?>
<status etag="41a354607ecf37b38b7f0f36fc2fb68c"><canSeek>0</canSeek><db>-9.3</db>
<inputId>input0</inputId><inputTypeIndex>analog-1</inputTypeIndex><mute>0</mute>
<service>Capture</service><serviceType>AudioInputs</serviceType><state>stream</state>
<title1>Analog Input 1</title1><volume>62</volume></status>"""


def status(db=-9.3, volume=62, mute=False, input_type="analog-1", title="Analog Input 1", etag="e1"):
    return bluos.PlayerStatus(volume=volume, db=db, mute=mute, input=title, input_type=input_type,
                              service="Capture", state="stream", etag=etag)


def test_parse_powernode_status():
    s = bluos.parse_status(STATUS_ANALOG)
    assert (s.volume, s.db, s.mute, s.input, s.analog) == (62, -9.3, False, "Analog Input 1", True)
    assert s.etag == "41a354607ecf37b38b7f0f36fc2fb68c"
    arc = bluos.parse_status(STATUS_ANALOG.replace("analog-1", "hdmi-arc").replace("Analog Input 1", "TV"))
    assert not arc.analog and arc.input == "TV"
    with pytest.raises(bluos.BluOSError):
        bluos.parse_status("<not xml")


SETTINGS = {"sub_level_db": 0.0, "follow_volume": True, "reference_db": -9.3}


def test_sub_follows_the_amplifier_from_the_reference():
    g = sub_gain(SETTINGS, status(db=-15.3), trim_db=-13.1)
    assert g["following"] and g["fader_db"] == pytest.approx(-6.0) and not g["mute"]
    g = sub_gain({**SETTINGS, "sub_level_db": 3.0}, status(db=-9.3), trim_db=-13.1)
    assert g["fader_db"] == pytest.approx(3.0)


def test_sub_fader_never_raises_the_sub_above_the_input_level():
    g = sub_gain({**SETTINGS, "sub_level_db": 6.0}, status(db=0.0), trim_db=-13.1)
    assert g["wanted_db"] == pytest.approx(15.3) and g["fader_db"] == pytest.approx(13.1) and g["clamped"]
    assert sub_gain({**SETTINGS, "sub_level_db": 4.0}, status(), trim_db=1.5)["fader_db"] == 0.0


def test_sub_mutes_with_the_amplifier_or_when_it_leaves_the_analog_input():
    assert sub_gain(SETTINGS, status(mute=True), -13)["mute"]
    g = sub_gain(SETTINGS, status(input_type="hdmi-arc", title="TV"), -13)
    assert g["mute"] and "TV" in g["mute_reason"]
    # Not following: never muted, only the listener's level.
    g = sub_gain({**SETTINGS, "follow_volume": False, "sub_level_db": -2.0}, status(mute=True), -13)
    assert not g["mute"] and g["fader_db"] == -2.0 and not g["following"]
    # No reference yet, or no player: the sub level alone.
    assert sub_gain({**SETTINGS, "reference_db": None}, status(db=-20), -13)["fader_db"] == 0.0
    assert sub_gain(SETTINGS, None, -13)["fader_db"] == 0.0


def test_follower_sets_the_fader_only_when_it_changes(tmp_path):
    cam = FakeCamilla()
    st = Store(tmp_path / "state", tmp_path / "camilla")
    st.save_settings(bluos_host="amp", reference_db=-9.3)
    players = iter([status(db=-12.3), status(db=-12.3, etag="e2"), status(db=-9.3, mute=True, etag="e3")])
    f = VolumeFollower(cam, st, fetch=lambda host, etag=None, wait=None: next(players))

    async def go():
        for _ in range(3):
            f.player = f.fetch("amp")
            await f.apply()
    asyncio.run(go())
    faders = [c for c in cam.calls if isinstance(c, tuple) and c[0].startswith("SetFader")]
    assert faders == [("SetFaderVolume", [1, -3.0]), ("SetFaderMute", [1, False]),
                      ("SetFaderVolume", [1, 0.0]), ("SetFaderMute", [1, True])]


def make_app(tree, tmp_path, fetch):  # noqa: F811
    proc, sys = tree
    cam = FakeCamilla()
    st = Store(tmp_path / "state", tmp_path / "camilla")
    app = webapp.create_app(camilla=cam, store=st, jobs=JobRunner(), proc=proc, sys=sys,
                            rigs={"room": lambda d: rig(), "selftest": lambda d: rig()}, player_fetch=fetch)
    return TestClient(app), cam, st


def test_api_volume_settings(tree, tmp_path):  # noqa: F811
    def fetch(host, etag=None, wait=None, timeout=5.0):
        if host != "192.0.2.10":
            raise bluos.BluOSError("no answer")
        return status(db=-12.3)
    c, cam, st = make_app(tree, tmp_path, fetch)
    assert c.get("/api/volume").json()["player"] is None
    assert c.put("/api/volume", json={"bluos_host": "192.0.2.99"}).status_code == 422  # nobody there
    assert c.put("/api/volume", json={"bluos_host": "bad host!"}).status_code == 422
    r = c.put("/api/volume", json={"bluos_host": "192.0.2.10"})
    assert r.status_code == 200 and r.json()["player"]["volume"] == 62
    # No reference yet: the sub level alone. Then take the current volume as the reference.
    r = c.put("/api/volume", json={"sub_level_db": 4.5})
    assert r.json()["sub"]["fader_db"] == 0.0  # no calibration applied: limit is 0 dB
    st.save_settings(reference_db=-6.3)
    assert c.get("/api/volume").json()["sub"]["offset_db"] == pytest.approx(-6.0)
    r = c.post("/api/volume/reference")
    assert r.json()["reference"]["db"] == -12.3 and r.json()["sub"]["offset_db"] == 0.0
    assert c.put("/api/volume", json={"sub_level_db": 50}).status_code == 422
    assert c.put("/api/volume", json={"forget_bluos_host": True}).json()["bluos_host"] is None
    assert ("SetFaderVolume", [1, 0.0]) in cam.calls
    assert "volume" in c.get("/api/status").json()


def test_measuring_refuses_when_the_amplifier_is_on_another_input(tree, tmp_path, fake_room):  # noqa: F811
    current = {"s": status(input_type="hdmi-arc", title="TV")}
    c, cam, st = make_app(tree, tmp_path, lambda host, etag=None, wait=None, timeout=5.0: current["s"])
    st.save_settings(bluos_host="amp")
    r = c.post("/api/measure/bass", json={})
    assert r.status_code == 409 and "TV" in r.json()["detail"]
    current["s"] = status(mute=True)
    assert "muted" in c.post("/api/measure/bass", json={}).json()["detail"]
    # The self-test plays nothing through the amplifier: allowed.
    assert c.post("/api/measure/bass", json={"selftest": True}).status_code == 202
    from test_measure import wait_job
    assert wait_job(c)["state"] == "done"
