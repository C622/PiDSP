"""Presets: bundles of measurements and the filter built from them; positions
measured in any order, replaced or removed one at a time."""

import json

import pytest
from fastapi.testclient import TestClient

from pidsp.measure.jobs import JobRunner
from pidsp.store import PresetError, Store
from pidsp.web import app as webapp

from test_measure import FakeCamilla, rig, wait_job, fake_room  # noqa: F401
from test_web import tree  # noqa: F401  (fixture)


def test_measurements_from_before_presets_become_the_first_preset(tmp_path):
    root = tmp_path / "state"
    (root / "bass").mkdir(parents=True)
    (root / "bass/latest.json").write_text(json.dumps({"plan": {"crossover_hz": 80}, "measured": "then"}))
    (root / "room").mkdir()
    (root / "room/pos1.json").write_text(json.dumps({"index": 1, "plan": {"crossover_hz": 80}}))
    st = Store(root, tmp_path / "camilla")
    assert st.bass()["measured"] == "then" and [p["index"] for p in st.positions()] == [1]
    assert not (root / "bass").exists()
    [info] = st.presets()
    assert info["name"] == "First calibration" and info["positions"] == [1] and info["crossover_hz"] == 80


def test_presets_keep_their_measurements_apart(tmp_path):
    st = Store(tmp_path / "state", tmp_path / "camilla")
    first = st.current_preset()
    st.update_bass({"plan": {"crossover_hz": 80}, "measured": "a"})
    copy = st.create_preset("Try 70 Hz", copy_from=first)
    empty = st.create_preset("Empty")
    assert st.current_preset() == first  # creating does not switch
    assert st.preset_store(copy).bass()["measured"] == "a" and st.preset_store(empty).bass() is None
    st.select_preset(copy)
    st.update_bass({"plan": {"crossover_hz": 70}, "measured": "b"})
    assert st.preset_store(first).bass()["measured"] == "a"
    st.rename_preset(copy, "70 Hz")
    assert {p["name"] for p in st.presets()} == {"My room", "70 Hz", "Empty"}
    with pytest.raises(PresetError):
        st.delete_preset(copy)  # the current one
    st.delete_preset(empty)
    with pytest.raises(PresetError):
        st.select_preset(empty)
    with pytest.raises(PresetError):
        st.preset_store("../etc")


def client(tree, tmp_path):  # noqa: F811
    proc, sys = tree
    cam = FakeCamilla()
    st = Store(tmp_path / "state", tmp_path / "camilla")
    return TestClient(webapp.create_app(camilla=cam, store=st, jobs=JobRunner(), proc=proc, sys=sys,
                                        rigs={"room": lambda d: rig(), "selftest": lambda d: rig()})), cam, st


def measure(c, position):
    assert c.post("/api/measure/room", json={"position": position}).status_code == 202
    assert wait_job(c)["state"] == "done"


def test_positions_in_any_order_replaced_and_removed(tree, tmp_path, fake_room):  # noqa: F811
    c, cam, st = client(tree, tmp_path)
    assert c.post("/api/measure/bass", json={}).status_code == 202
    assert wait_job(c)["state"] == "done"
    for pos in (1, 2, 3):
        measure(c, pos)
    for pos in (3, 4, 5, 6):  # later, 3 again (replaced) and on to 6
        measure(c, pos)
    state = c.get("/api/room").json()
    assert [p["index"] for p in state["positions"]] == [1, 2, 3, 4, 5, 6] and state["max"] == 8
    first3 = next(p for p in state["positions"] if p["index"] == 3)["measured"]
    measure(c, 3)
    again = next(p for p in c.get("/api/room").json()["positions"] if p["index"] == 3)["measured"]
    assert again >= first3
    assert c.delete("/api/room/position/2").status_code == 204
    assert c.delete("/api/room/position/2").status_code == 404
    assert [p["index"] for p in c.get("/api/room").json()["positions"]] == [1, 3, 4, 5, 6]
    assert c.post("/api/measure/room", json={"position": 9}).status_code == 422
    b = c.post("/api/room/build", json={"target": "flat"}).json()["build"]
    assert b["positions"] == 5


def test_api_presets_and_applying_one(tree, tmp_path, fake_room):  # noqa: F811
    c, cam, st = client(tree, tmp_path)
    assert c.post("/api/measure/bass", json={}).status_code == 202
    assert wait_job(c)["state"] == "done"
    measure(c, 1)
    assert c.post("/api/room/build", json={"target": "room"}).status_code == 200
    first = c.get("/api/presets").json()["current"]
    # A copy to try another target: same measurements, its own filter.
    r = c.post("/api/presets", json={"name": "Warm", "copy_from": first}).json()
    copy = r["current"]
    assert copy != first and len(r["presets"]) == 2
    assert c.post("/api/room/build", json={"target": "warm"}).json()["build"]["target"] == "warm"
    assert c.post("/api/room/apply").status_code == 200
    s = c.get("/api/presets").json()
    assert s["applied"] == copy and s["applied_kind"] == "room"
    assert c.get("/api/status").json()["calibration"]["preset"]["name"] == "Warm"
    # The first preset still has its own build; apply it straight from the list.
    r = c.post(f"/api/presets/{first}/apply")
    assert r.status_code == 200 and r.json()["applied"] == first
    assert st.active()["room"]["target"] == "room" and cam.path.endswith("active.yml")
    assert c.get("/api/presets").json()["current"] == copy  # applying does not change what is open
    # The check refuses while another preset than the open one is applied.
    assert "Another preset" in c.post("/api/measure/check", json={}).json()["detail"]
    # A new empty preset: nothing measured; an empty preset cannot be applied.
    r = c.post("/api/presets", json={"name": "Sofa moved"}).json()
    new = r["current"]
    assert c.get("/api/bass?selftest=false").json()["result"] is None
    assert c.post(f"/api/presets/{new}/apply").status_code == 409
    assert c.put(f"/api/presets/{first}", json={"name": "Room target"}).status_code == 200
    assert st.active()["preset"]["name"] == "Room target"
    assert c.delete(f"/api/presets/{new}").status_code == 409  # open
    assert c.post(f"/api/presets/{first}/select").status_code == 200
    assert c.delete(f"/api/presets/{new}").status_code == 200
    assert c.post("/api/presets/nope/select").status_code == 404
