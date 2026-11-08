"""Tier 2 API tests. Run: python tests/test_tier2.py   (or: python -m pytest -q tests/test_tier2.py)"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Voice must see a missing key even if it reads the env at import.
_SAVED_FISH_KEY = os.environ.pop("FISH_API_KEY", None)

from fastapi.testclient import TestClient
from backend.server import app, lock
import backend.server as srv
from backend.services.occupants import make_router, nav_hints
from backend.services.voice import router as voice_router, style_text

if not any(getattr(r, "path", "") == "/api/nav" for r in app.routes):
    app.include_router(make_router(lambda: srv.world, lock))
    app.include_router(voice_router)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "data", "buildings", "sample_building.json")
client = TestClient(app)

TURNS = {"straight", "left", "right", "stairs_up", "stairs_down", "arrive"}
SKIP_QR = {"stairwell", "elevator", "void"}


def _spaces():
    with open(SAMPLE, encoding="utf-8") as fh:
        raw = json.load(fh)
    return [s for fl in raw["floors"] for s in fl["spaces"]]


def _ang_close(a, b, tol=1e-6):
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0) <= tol


def test_qr_list_excludes_stairwells():
    rows = client.get("/api/qr").json()
    assert isinstance(rows, list) and rows
    codes = {r["code"] for r in rows}
    spaces = _spaces()
    excluded = {s["id"] for s in spaces if s["type"] in SKIP_QR}
    included = {s["id"] for s in spaces if s["type"] not in SKIP_QR}
    assert excluded.isdisjoint(codes)
    assert included <= codes
    assert "F1-EastStairs" not in codes and "F1-WestStairs" not in codes
    for row in rows:
        assert {"code", "room_id", "label", "level"} <= set(row)
        assert isinstance(row["level"], int)


def test_qr_code_known_and_unknown():
    ok = client.get("/api/qr/F1-102")
    assert ok.status_code == 200
    body = ok.json()
    assert body["code"] == "F1-102"
    assert body["room_id"] == "F1-102"
    assert body["label"] == "102"
    assert body["level"] == 1
    assert body["heading_deg"] == 0
    # Room F1-102 is the north classroom x=4..12, y=0..10. Pose is inside it.
    assert 4.0 <= body["x"] <= 12.0
    assert 0.0 <= body["y"] <= 10.0
    missing = client.get("/api/qr/NOT-A-ROOM")
    assert missing.status_code == 404


def test_qr_print_html():
    base = "https://demo.example"
    res = client.get("/api/qr/print", params={"base": base})
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    html = res.text
    assert "qrcode" in html.lower()
    assert "FIRESIGHT:F1-102" in html
    assert "FIRESIGHT:F1-EastStairs" not in html
    assert "FIRESIGHT:F1-WestStairs" not in html
    assert "FIRESIGHT:F1-Elevator" not in html


def test_nav_keys_and_heading():
    assert client.post("/api/fire/reset").status_code == 200
    res = client.get("/api/nav", params={"level": 1, "x": 20, "y": 12})
    assert res.status_code == 200
    nav = res.json()
    for key in ("reachable", "path", "distance_m", "instructions", "next",
                "distance_to_next_m", "turn", "heading_plan_deg", "plan_north_deg",
                "heading_compass_deg", "hazard", "rev"):
        assert key in nav, key
    assert nav["reachable"] is True
    assert isinstance(nav["path"], list) and nav["path"]
    assert isinstance(nav["distance_m"], (int, float))
    assert isinstance(nav["instructions"], list)
    assert isinstance(nav["next"], dict)
    assert {"level", "x", "y"} <= set(nav["next"])
    assert isinstance(nav["distance_to_next_m"], (int, float))
    assert nav["distance_to_next_m"] >= 1.0
    assert nav["turn"] in TURNS
    assert isinstance(nav["heading_plan_deg"], (int, float))
    assert isinstance(nav["plan_north_deg"], (int, float))
    assert isinstance(nav["heading_compass_deg"], (int, float))
    assert 0 <= nav["heading_compass_deg"] < 360
    expect = (nav["plan_north_deg"] + nav["heading_plan_deg"]) % 360
    assert _ang_close(nav["heading_compass_deg"], expect, tol=0.05)
    assert nav["hazard"] in (None, "fire_near", "smoke_on_route")
    assert isinstance(nav["rev"], str) and nav["rev"]
    bad = client.get("/api/nav", params={"level": 99, "x": 20, "y": 12})
    assert bad.status_code in (404, 422)


def test_nav_unreachable_arrive():
    assert client.post("/api/fire/reset").status_code == 200
    nav = client.get("/api/nav", params={"level": 1, "x": -30, "y": -30}).json()
    assert nav["reachable"] is False
    assert nav["next"] is None
    assert nav["turn"] == "arrive"
    assert nav["distance_to_next_m"] == 0


def test_rev_changes_and_fire_near():
    try:
        assert client.post("/api/fire/reset").status_code == 200
        calm = client.get("/api/nav", params={"level": 1, "x": 23, "y": 12}).json()
        assert calm["hazard"] is None
        before = client.get("/api/rev").json()
        assert {"rev", "t"} <= set(before)
        assert client.post("/api/fire/start", json={"level": 1, "x": 20, "y": 12}).status_code == 200
        assert client.post("/api/fire/step", json={"n": 1}).status_code == 200
        after = client.get("/api/rev").json()
        assert after["t"] == before["t"] + 1
        assert after["rev"] != before["rev"]
        hot = client.get("/api/nav", params={"level": 1, "x": 23, "y": 12}).json()
        assert hot["hazard"] == "fire_near"
        assert hot["rev"] == after["rev"]
        assert isinstance(hot["heading_compass_deg"], (int, float))
        assert 0 <= hot["heading_compass_deg"] < 360
    finally:
        assert client.post("/api/fire/reset").status_code == 200


def test_people_status_round_trip():
    pid = "judge-1"
    ok = client.post(f"/api/people/{pid}/status", json={
        "status": "evacuating", "level": 1, "x": 20, "y": 12, "room_id": "F1-102",
    })
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    rows = client.get("/api/people").json()
    me = next(p for p in rows if p["person_id"] == pid)
    assert me["status"] == "evacuating"
    assert me["level"] == 1
    assert me["x"] == 20
    assert me["y"] == 12
    assert me["room_id"] == "F1-102"
    assert abs(float(me["updated_at"]) - time.time()) < 30
    bad = client.post(f"/api/people/{pid}/status", json={"status": "lost"})
    assert bad.status_code == 400
    again = next(p for p in client.get("/api/people").json() if p["person_id"] == pid)
    assert again["status"] == "evacuating"


def test_nav_hints_l_shape():
    # Plan frame: x right, y down. Heading 0 is up (-y), clockwise positive.
    # Up, then east, is a right turn. Up, then west, is a left turn.
    right_path = [[1, 10.0, 10.0], [1, 10.0, 4.0], [1, 16.0, 4.0]]
    right = nav_hints(right_path, 1, 10.0, 10.0, 15.0)
    assert right["turn"] == "right"
    assert right["next"]["level"] == 1
    assert abs(right["next"]["x"] - 10.0) < 1e-6
    assert abs(right["next"]["y"] - 4.0) < 1e-6
    assert abs(right["distance_to_next_m"] - 6.0) < 1e-6
    assert _ang_close(right["heading_plan_deg"], 0.0)
    assert _ang_close(right["heading_compass_deg"], 15.0)
    assert 0 <= right["heading_compass_deg"] < 360

    left_path = [[1, 10.0, 10.0], [1, 10.0, 4.0], [1, 4.0, 4.0]]
    left = nav_hints(left_path, 1, 10.0, 10.0, 0.0)
    assert left["turn"] == "left"
    assert abs(left["next"]["x"] - 10.0) < 1e-6
    assert abs(left["next"]["y"] - 4.0) < 1e-6
    assert _ang_close(left["heading_plan_deg"], 0.0)
    assert _ang_close(left["heading_compass_deg"], 0.0)


def test_voice_tts_503_without_key():
    os.environ.pop("FISH_API_KEY", None)
    try:
        res = client.get("/api/voice/tts", params={"text": "Turn left.", "style": "occupant"})
        assert res.status_code == 503
        assert "detail" in res.json()
    finally:
        if _SAVED_FISH_KEY is not None:
            os.environ["FISH_API_KEY"] = _SAVED_FISH_KEY


def test_style_text_firefighter_shorter():
    text = "Walk 12 m through the hallway (317)"
    occupant = style_text(text, "occupant")
    firefighter = style_text(text, "firefighter")
    assert isinstance(occupant, str) and isinstance(firefighter, str)
    assert firefighter.strip()
    assert len(firefighter) < len(occupant)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
