import asyncio

import pytest
from fastapi.testclient import TestClient

from pidsp import system
from pidsp.camilla.client import CamillaClient
from pidsp.web import app as webapp

CARDS = """\
 0 [ICUSBAUDIO7D   ]: USB-Audio - ICUSBAUDIO7D
                      ICUSBAUDIO7D at usb-fe9c0000.xhci-1.1, full speed
 1 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 3 [Loopback       ]: Loopback - Loopback
                      Loopback 1
 4 [U18dB          ]: USB-Audio - Umik-1  Gain: 18dB
                      miniDSP Umik-1  Gain: 18dB at usb-fe9c0000.xhci-1.2, full speed
"""


class FakeCamilla:
    def __init__(self, state="Inactive"):
        self.state = state

    async def status(self):
        return {"reachable": True, "version": "4.1.3", "state": self.state, "capture_rate": 48000,
                "load": 3.2, "clock_ppm": 12.5, "config_path": None, "stop_reason": "None",
                "clipped_samples": 0}

    async def levels(self):
        return {"capture_peak": [-12.0, -13.0], "playback_peak": [-18.0, -18.5, -9.0]}


@pytest.fixture
def tree(tmp_path):
    proc, sys = tmp_path / "proc", tmp_path / "sys"
    (proc / "asound").mkdir(parents=True)
    (proc / "asound" / "cards").write_text(CARDS)
    (proc / "uptime").write_text("3725.4 100.0\n")
    (proc / "loadavg").write_text("0.10 0.20 0.30 1/100 999\n")
    (proc / "meminfo").write_text("MemTotal:        1900000 kB\nMemAvailable:    1500000 kB\n")
    (proc / "device-tree").mkdir()
    (proc / "device-tree" / "model").write_text("Raspberry Pi Compute Module 4 Rev 1.1\x00")
    (sys / "class/thermal/thermal_zone0").mkdir(parents=True)
    (sys / "class/thermal/thermal_zone0/temp").write_text("47250\n")
    for name, vid, pid, serial in [("1-1.1", "0d8c", "0102", ""), ("1-1.2", "2752", "0007", "700-1234")]:
        d = sys / "bus/usb/devices" / name
        d.mkdir(parents=True)
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        if serial:
            (d / "serial").write_text(serial + "\n")
    return str(proc), str(sys)


def client(tree, **kw):
    proc, sys = tree
    return TestClient(webapp.create_app(camilla=FakeCamilla(**kw), proc=proc, sys=sys))


def test_sound_cards_parse(tree):
    cards = system.sound_cards(tree[0] + "/asound/cards")
    assert [(c["index"], c["id"]) for c in cards] == [(0, "ICUSBAUDIO7D"), (1, "vc4hdmi0"), (3, "Loopback"), (4, "U18dB")]


def test_placeholder_mic_serial_hidden(tree):
    serial = tree[1] + "/bus/usb/devices/1-1.2/serial"
    open(serial, "w").write("000-0000\n")
    assert system.audio_devices(*tree)["mic"]["serial"] is None


def test_status(tree):
    r = client(tree).get("/api/status")
    assert r.status_code == 200
    s = r.json()
    assert s["system"]["cpu_temp_c"] == pytest.approx(47.25, abs=0.06)
    assert s["system"]["uptime_s"] == 3725
    assert s["system"]["model"] == "Raspberry Pi Compute Module 4 Rev 1.1"
    assert s["devices"]["dac"]["present"] and s["devices"]["loopback"]["present"]
    assert s["devices"]["mic"] == {"present": True, "role": "mic", "name": "miniDSP UMIK-1",
                                   "usb_id": "2752:0007", "serial": "700-1234",
                                   "card": "U18dB", "gain_db": 18}
    assert s["camilladsp"]["version"] == "4.1.3"
    assert s["calibration"] is None


def test_levels(tree):
    assert client(tree).get("/api/levels").json()["levels"]["playback_peak"][2] == -9.0


def test_ui_served(tree):
    c = client(tree)
    r = c.get("/")
    assert r.status_code == 200 and "<title>PiDSP</title>" in r.text
    assert c.get("/app.js").status_code == 200
    assert c.get("/app.css").status_code == 200


def test_password_validation(tree):
    c = client(tree)
    assert c.post("/api/system/password", json={"password": "short"}).status_code == 422
    assert c.post("/api/system/password", json={"password": "longenough\nroot:x"}).status_code == 422


def test_admin_missing(tree, monkeypatch):
    monkeypatch.setattr(webapp, "ADMIN", "/nonexistent/pidsp-admin")
    c = client(tree)
    assert c.post("/api/system/password", json={"password": "longenough"}).status_code == 503
    assert c.post("/api/system/reboot").status_code == 503


def test_admin_called(tree, monkeypatch, tmp_path):
    calls = []
    helper = tmp_path / "pidsp-admin"
    helper.write_text("")
    monkeypatch.setattr(webapp, "ADMIN", str(helper))

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, input=None, **kw):
        calls.append((cmd, input))
        return Done()

    monkeypatch.setattr(webapp.subprocess, "run", fake_run)
    c = client(tree)
    assert c.post("/api/system/password", json={"password": "a good password"}).status_code == 204
    assert c.post("/api/system/reboot").status_code == 202
    assert calls == [(["sudo", "-n", str(helper), "passwd"], "a good password\n"),
                     (["sudo", "-n", str(helper), "reboot"], None)]


def test_camilla_unreachable():
    s = asyncio.run(CamillaClient("ws://127.0.0.1:9", timeout=0.5).status())
    assert s["reachable"] is False and s["error"]
