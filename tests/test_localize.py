"""Run: python tests/test_localize.py   (or: python -m pytest -q tests/test_localize.py)"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.server as srv
from backend.services.localize import ParticleFilter, make_router
from backend.core.model import load_building_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DCC = os.path.join(ROOT, "data", "buildings", "dcc.json")

# Hall center on floor 1 (y=10..14 corridor). East is compass 90 when plan north is 0.
HALL_X, HALL_Y = 8.0, 12.0
# F1-102 sits on x=4..12, y=0..10. Its west wall is x=4; its hall door is (8, 10).
ROOM_X, ROOM_Y = 8.0, 5.0
ROOM_WALL_X = 4.0
DOOR_102 = (8.0, 10.0)


def _client():
    app = FastAPI()
    app.include_router(make_router(lambda: srv.world, srv.lock, seed=1))
    return TestClient(app)


def _pose_ok(p, session=None):
    for key in ("session", "level", "x", "y", "spread_m", "confidence", "source", "unsure", "n_steps"):
        assert key in p, p
    spread = float(p["spread_m"])
    conf = float(p["confidence"])
    assert 0.0 <= conf <= 1.0
    assert bool(p["unsure"]) == (spread > 4.0)
    expect = min(1.0, max(0.0, 1.0 - spread / 6.0))
    assert abs(conf - expect) < 1e-3
    if session is not None:
        assert p["session"] == session


def _lower_stair(grid):
    """First stairs link, lower floor cell center and the floor above it."""
    for a, b, _cost, v in grid.links:
        if v.get("type") != "stairs":
            continue
        fa, ra, ca = a
        fb, rb, cb = b
        if grid.levels[fb] > grid.levels[fa]:
            x, y = grid.center_of(ra, ca)
            dx, dy = grid.center_of(rb, cb)
            return grid.levels[fa], float(x), float(y), grid.levels[fb], float(dx), float(dy)
    raise AssertionError("sample building has no stair link")


def _rapidocr_importable():
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import rapidocr  # noqa: F401
        return True
    except ImportError:
        return False


def test_start_pose_near_start():
    c = _client()
    sid = "near-start"
    r = c.post("/api/loc/start", json={
        "session": sid, "level": 1, "x": HALL_X, "y": HALL_Y, "sigma_m": 0.5,
    })
    assert r.status_code == 200, r.text
    p = r.json()
    _pose_ok(p, sid)
    assert p["level"] == 1
    assert p["source"] == "start"
    assert p["n_steps"] == 0
    assert p["unsure"] is False
    assert p["spread_m"] < 1.5
    assert math.hypot(p["x"] - HALL_X, p["y"] - HALL_Y) < 1.0
    g = c.get("/api/loc/" + sid)
    assert g.status_code == 200
    got = g.json()
    assert got["session"] == sid
    assert math.hypot(got["x"] - p["x"], got["y"] - p["y"]) < 1e-6


def test_noisy_steps_along_corridor():
    c = _client()
    sid = "corridor"
    r = c.post("/api/loc/start", json={
        "session": sid, "level": 1, "x": HALL_X, "y": HALL_Y, "sigma_m": 0.5,
    })
    assert r.status_code == 200, r.text
    origin = r.json()
    rng = np.random.default_rng(1)
    steps = []
    for i in range(10):
        heading = 90.0 + float(rng.normal(0.0, 4.0))
        length = float(max(0.4, 0.7 * rng.normal(1.0, 0.05)))
        steps.append({"t": i * 500, "heading_deg": heading, "length_m": length})
    plan_north = float(srv.world.model["plan_north_deg"])
    x, y = float(origin["x"]), float(origin["y"])
    for s in steps:
        rad = math.radians(s["heading_deg"] - plan_north)
        x += s["length_m"] * math.sin(rad)
        y += -s["length_m"] * math.cos(rad)
    r = c.post("/api/loc/steps", json={"session": sid, "steps": steps})
    assert r.status_code == 200, r.text
    p = r.json()
    _pose_ok(p, sid)
    assert p["source"] == "steps"
    assert p["level"] == 1
    assert p["n_steps"] == 10
    err = math.hypot(p["x"] - x, p["y"] - y)
    assert err < 2.0, err
    assert p["spread_m"] < 3.0


def test_wall_blocks_crossing():
    # Ten steps west from the middle of F1-102 would end near x=3, past the wall at x=4.
    c = _client()
    sid = "into-wall"
    r = c.post("/api/loc/start", json={
        "session": sid, "level": 1, "x": ROOM_X, "y": ROOM_Y, "sigma_m": 0.5,
    })
    assert r.status_code == 200, r.text
    start = r.json()
    steps = [{"t": i * 400, "heading_deg": 270.0, "length_m": 0.5} for i in range(10)]
    r = c.post("/api/loc/steps", json={"session": sid, "steps": steps})
    assert r.status_code == 200, r.text
    p = r.json()
    _pose_ok(p, sid)
    assert p["level"] == 1
    assert p["source"] == "steps"
    assert p["x"] > ROOM_WALL_X
    assert p["x"] < float(start["x"]) - 0.4


def test_unknown_session_404():
    c = _client()
    assert c.get("/api/loc/no-such-session").status_code == 404
    steps = {"session": "no-such-session", "steps": [{"t": 0, "heading_deg": 90.0, "length_m": 0.5}]}
    assert c.post("/api/loc/steps", json=steps).status_code == 404
    assert c.post("/api/loc/stairs", json={"session": "no-such-session", "direction": "up"}).status_code == 404


def test_stairs_up_changes_level():
    c = _client()
    level, x, y, dest_level, dx, dy = _lower_stair(srv.world.grid)
    sid = "stairs-up"
    r = c.post("/api/loc/start", json={
        "session": sid, "level": level, "x": x, "y": y, "sigma_m": 0.5,
    })
    assert r.status_code == 200, r.text
    assert r.json()["level"] == level
    r = c.post("/api/loc/stairs", json={"session": sid, "direction": "up"})
    assert r.status_code == 200, r.text
    p = r.json()
    _pose_ok(p, sid)
    assert p["source"] == "stairs"
    assert p["level"] == dest_level
    assert math.hypot(p["x"] - dx, p["y"] - dy) < 3.0


def test_observe_pulls_toward_door():
    grid = srv.world.grid
    assert "F1-HALL" in grid.room_by_id, "expected the sample building"
    pf = ParticleFilter(grid, srv.world.model, np.random.default_rng(1))
    pf.start(1, 20.0, 12.0, 0.5, room_id="F1-HALL")
    before = pf.pose()
    assert float(before["spread_m"]) > 4.0
    matches = [{
        "room_id": "F1-102",
        "level": 1,
        "label": "102",
        "points": [[DOOR_102[0], DOOR_102[1]]],
        "score": 0.9,
    }]
    pf.observe(matches)
    after = pf.pose()
    def dist(p):
        return math.hypot(float(p["x"]) - DOOR_102[0], float(p["y"]) - DOOR_102[1])
    assert dist(after) < dist(before) - 1.0, (before, after)
    assert int(after["level"]) == 1
    assert after["source"] == "vision"


def test_match_rooms_finds_f3_318():
    from backend.services.vision import match_rooms
    model = load_building_file(DCC)
    tokens = [{"text": "318", "conf": 0.9, "box": [0, 0, 1, 1]}]
    hits = match_rooms(tokens, model)
    hit = next((m for m in hits if m["room_id"] == "F3-318"), None)
    assert hit is not None, hits
    assert hit["level"] == 3
    assert abs(float(hit["score"]) - 0.9) < 1e-6
    assert hit["points"] and len(hit["points"][0]) == 2


def test_normalize_token_fuzz():
    import backend.services.vision as vision
    fn = getattr(vision, "normalize_token", None)
    if fn is None:
        print("skip normalize_token (not provided)")
        return
    assert fn("RM 3l8") == "3L8"
    model = load_building_file(DCC)
    hits = vision.match_rooms([{"text": "RM 3l8", "conf": 0.9, "box": [0, 0, 1, 1]}], model)
    hit = next((m for m in hits if m["room_id"] == "F3-318"), None)
    assert hit is not None, hits
    # One character off ("3L8" vs "318") lowers the 0.9 token conf by 0.3.
    assert abs(float(hit["score"]) - 0.6) < 1e-6


def test_resample_underflow_reinits():
    # Wall hits multiply weights by 0.02. After enough hits the squares underflow
    # to 0 while the sum is still above the dead-cloud cutoff.
    grid = srv.world.grid
    pf = ParticleFilter(grid, srv.world.model, np.random.default_rng(1))
    pf.start(1, HALL_X, HALL_Y, 0.5)
    pf._capture()
    pf.w[:] = 1e-200
    assert float(pf.w.sum()) > 1e-300
    assert float(np.sum(pf.w * pf.w)) == 0.0
    pf._resample_if_needed()
    assert abs(float(pf.w.sum()) - 1.0) < 1e-9
    assert np.isfinite(pf.x).all() and np.isfinite(pf.y).all()
    pf.w[:] = 1e-200
    pf.step(90.0, 0.4)
    assert abs(float(pf.w.sum()) - 1.0) < 1e-9
    pose = pf.pose()
    assert pose["source"] == "steps"
    assert math.isfinite(pose["x"]) and math.isfinite(pose["y"])
    assert math.isfinite(pose["confidence"])


def test_observe_without_match_keeps_source():
    pf = ParticleFilter(srv.world.grid, srv.world.model, np.random.default_rng(1))
    pf.start(1, HALL_X, HALL_Y, 0.5)
    assert pf.pose()["source"] == "start"
    pf.observe([])
    assert pf.pose()["source"] == "start"
    pf.observe([{"text": "nope"}])
    assert pf.pose()["source"] == "start"


def test_session_cap_drops_oldest_and_forgets_pose():
    import backend.services.localize as loc
    import backend.services.store as store
    saved = loc.MAX_SESSIONS
    loc.MAX_SESSIONS = 2
    sids = ("s-old", "s-mid", "s-new")
    try:
        c = _client()
        for sid in sids[:2]:
            r = c.post("/api/loc/start", json={
                "session": sid, "level": 1, "x": HALL_X, "y": HALL_Y, "sigma_m": 0.5,
            })
            assert r.status_code == 200, r.text
        for sid in sids:
            store._pose_at[sid] = 1.0
        r = c.post("/api/loc/start", json={
            "session": "s-new", "level": 1, "x": HALL_X, "y": HALL_Y, "sigma_m": 0.5,
        })
        assert r.status_code == 200, r.text
        assert c.get("/api/loc/s-old").status_code == 404
        assert "s-old" not in store._pose_at
        assert "s-mid" in store._pose_at
        assert "s-new" in store._pose_at
        assert c.get("/api/loc/s-mid").status_code == 200
        assert c.get("/api/loc/s-new").status_code == 200
    finally:
        loc.MAX_SESSIONS = saved
        for sid in sids:
            store._pose_at.pop(sid, None)


def test_read_text_ocr_engine():
    if not _rapidocr_importable():
        print("skip read_text (rapidocr not importable)")
        return
    import cv2
    from backend.services.vision import read_text
    img = np.full((240, 720, 3), 255, np.uint8)
    cv2.putText(img, "318", (40, 170), cv2.FONT_HERSHEY_SIMPLEX, 5.0, (0, 0, 0), 8, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    found = read_text(buf.tobytes())
    assert isinstance(found, list) and found
    for tok in found:
        assert "text" in tok and "conf" in tok and "box" in tok
        assert 0.0 <= float(tok["conf"]) <= 1.0
        assert len(tok["box"]) == 4
    blob = " ".join(str(t["text"]) for t in found).upper().replace(" ", "")
    assert "318" in blob, found


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
