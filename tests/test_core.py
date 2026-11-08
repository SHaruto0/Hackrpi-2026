"""Run: python -m pytest -q   (or: python tests/test_core.py)"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from backend.core.model import load_building_file, load_building, parse_json_text, ModelError
from backend.core.grid import BuildingGrid
from backend.core.fire import FireSim
from backend.core.routing import RouteField
from backend.server import app

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "buildings", "sample_building.json")


def test_every_room_reaches_an_exit():
    g = BuildingGrid(load_building_file(SAMPLE))
    f = RouteField(g)
    for info in g.rooms:
        if info["type"] == "elevator":
            continue
        r = f.route_from(g.level_index[info["level"]], *info["centroid"])
        assert r["reachable"], info["id"]


def test_fire_in_east_stairs_reroutes_to_west():
    g = BuildingGrid(load_building_file(SAMPLE))
    before = RouteField(g).route_from(2, 30, 5)
    assert before["exit"] == "EXIT-EAST"
    sim = FireSim(g); sim.ignite_room("F1-EastStairs"); sim.step(10)
    after = RouteField(g, sim.fire, sim.smoke).route_from(2, 30, 5)
    assert after["reachable"] and after["exit"] == "EXIT-WEST"
    assert any("West Stairs" in s for s in after["instructions"])


def test_bad_json_rejected():
    try:
        load_building({"floors": []}); assert False
    except ModelError:
        pass
    assert parse_json_text("```json\n{\"a\": 1}\n```") == {"a": 1}


def test_api_flow():
    c = TestClient(app)
    assert c.get("/api/building").status_code == 200
    assert c.post("/api/fire/reset").status_code == 200
    assert c.post("/api/fire/start", json={"room_id": "F1-EastStairs"}).status_code == 200
    assert c.post("/api/fire/step", json={"n": 5}).status_code == 200
    s = c.get("/api/state").json()
    assert s["t"] == 5 and s["verticals"]["STAIR-EAST"] == "fire"
    assert c.get("/api/route", params={"level": 3, "x": 28, "y": 5}).json()["reachable"]
    assert len(c.get("/api/routes/all").json()) > 0


def test_checks_flag_broken_map():
    from backend.core.checks import check_building
    raw = json.load(open(SAMPLE, encoding="utf-8"))
    assert check_building(load_building(raw)) == []
    fl = raw["floors"][0]
    room = next(s for s in fl["spaces"] if s["type"] not in ("hallway", "lobby", "stairwell", "elevator"))
    fl["doors"] = [d for d in fl["doors"] if room["id"] not in d["connects"]]
    fl["walls"] = [w for w in fl["walls"] if w.get("exterior")]
    issues = check_building(load_building(raw))
    assert any(room["id"] in i and "no door" in i for i in issues), issues
    assert any(room["id"] in i and "boundary" in i for i in issues), issues


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
