"""Data + AI contract tests. No network.

Run: python tests/test_data.py
 or: python -m pytest -q tests/test_data.py

Routers are mounted here on a fresh app. Fire start/step/reset use the
existing server endpoints, which share that app's world.
"""
import atexit
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read cloud env at call time. Drop it before importing store/cortex.
_ENV_KEYS = (
    "MONGODB_URI",
    "MONGODB_DB",
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_PAT",
    "SNOWFLAKE_MODEL",
)
_SAVED_ENV = {k: os.environ.pop(k) for k in _ENV_KEYS if k in os.environ}

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.server as srv
from backend.core.model import load_building_file
from backend.server import World, lock
from backend.services.cortex import make_router as cortex_router, retrieve, situation
from backend.services.occupants import make_router as occupants_router, people, people_lock
from backend.services.store import (
    enabled as store_enabled,
    log as store_log,
    router as history_router,
    save_building as store_save_building,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "data", "buildings", "sample_building.json")
FIRE_ROOM = "F1-102"
FIRE_LABEL = "102"
_PAT = "firesight-test-pat"

_data_app = FastAPI()
_data_app.include_router(history_router)
_data_app.include_router(cortex_router(lambda: srv.world, lock))
_data_app.include_router(occupants_router(lambda: srv.world, lock))
data = TestClient(_data_app)
fire = TestClient(srv.app)


def _restore_env():
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(_SAVED_ENV)


atexit.register(_restore_env)


def _clear_cloud_env():
    for key in _ENV_KEYS:
        os.environ.pop(key, None)


def _use_sample():
    saved = srv.world
    srv.world = World(load_building_file(SAMPLE))
    return saved


def _snapshot_people():
    with people_lock:
        return {k: dict(v) for k, v in people.items()}


def _restore_people(snap):
    with people_lock:
        people.clear()
        people.update(snap)


def _status_counts(obj):
    """Every safe/trapped/evacuating tally found in a situation dict."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            nums = {}
            for key, val in node.items():
                name = str(key).lower()
                if name not in ("safe", "trapped", "evacuating") or isinstance(val, bool):
                    continue
                if isinstance(val, int):
                    nums[name] = val
                elif isinstance(val, list):
                    nums[name] = len(val)
            if len(nums) >= 2:
                found.append(nums)
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            if node and all(isinstance(row, dict) and "status" in row for row in node):
                nums = {"safe": 0, "trapped": 0, "evacuating": 0}
                for row in node:
                    name = str(row.get("status", "")).lower()
                    if name in nums:
                        nums[name] += 1
                found.append(nums)
            for val in node:
                walk(val)

    walk(obj)
    return found


def _headers_of(headers):
    out = {}
    if not headers:
        return out
    try:
        pairs = headers.items() if hasattr(headers, "items") else headers
        for key, val in pairs:
            out[str(key)] = str(val)
    except (TypeError, ValueError):
        return out
    return out


def _ok_response(url):
    request = httpx.Request("POST", str(url))
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "OK"}}]},
        request=request,
    )


def test_store_disabled_noop_and_history_503():
    _clear_cloud_env()
    assert store_enabled() is False
    assert store_log("fire_start", {"room_id": FIRE_ROOM, "building": "sample"}) is None
    assert store_log("loc_pose", {"session": "tester-m", "level": 1, "x": 1.0, "y": 2.0}) is None
    assert store_log("loc_pose", {"session": "tester-m", "level": 1, "x": 1.5, "y": 2.0}) is None
    assert store_save_building("sample", {"name": "sample", "floors": []}) is None

    status = data.get("/api/history/status")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["enabled"] is False
    assert "db" in body

    blocked = (
        "/api/history/events",
        "/api/history/timeline",
        "/api/history/buildings",
        "/api/history/buildings/sample",
    )
    for path in blocked:
        res = data.get(path)
        assert res.status_code == 503, (path, res.status_code, res.text)
        assert "detail" in res.json()
    assert data.get("/api/history/events", params={"kind": "fire_start", "limit": 5}).status_code == 503
    assert data.get("/api/history/timeline", params={"since": "2020-01-01T00:00:00Z"}).status_code == 503
    assert data.get("/api/history/buildings/sample", params={"version": 1}).status_code == 503


def test_brief_template_names_fire_room_and_ask_503():
    _clear_cloud_env()
    saved = _use_sample()
    try:
        started = fire.post("/api/fire/start", json={"room_id": FIRE_ROOM})
        assert started.status_code == 200, started.text
        stepped = fire.post("/api/fire/step", json={"n": 3})
        assert stepped.status_code == 200, stepped.text
        state = fire.get("/api/state").json()
        assert state["t"] == 3
        assert state["rooms"][FIRE_ROOM] == "fire"

        brief = data.get("/api/brief")
        assert brief.status_code == 200, brief.text
        body = brief.json()
        assert body["model"] == "template"
        assert isinstance(body["text"], str) and FIRE_LABEL in body["text"]
        assert "fire" in body["text"].lower()
        assert "facts" in body

        asked = data.post("/api/ask", json={"question": "Where is room 102?"})
        assert asked.status_code == 503, asked.text
        assert "detail" in asked.json()
    finally:
        fire.post("/api/fire/reset")
        srv.world = saved


def test_situation_counts_people_by_status():
    saved_world = _use_sample()
    saved_people = _snapshot_people()
    try:
        _restore_people({})
        posted = (
            ("m-safe-1", "safe"),
            ("m-safe-2", "safe"),
            ("m-trapped", "trapped"),
            ("m-evac", "evacuating"),
        )
        for person_id, status in posted:
            res = data.post(
                f"/api/people/{person_id}/status",
                json={"status": status, "level": 1, "x": 8.0, "y": 5.0, "room_id": FIRE_ROOM},
            )
            assert res.status_code == 200, (person_id, res.text)
        sit = situation(srv.world, people)
        assert isinstance(sit, dict)
        assert sit.get("t") == 0
        counts = _status_counts(sit)
        assert any(
            row.get("safe") == 2 and row.get("trapped") == 1 and row.get("evacuating") == 1
            for row in counts
        ), sit
    finally:
        _restore_people(saved_people)
        srv.world = saved_world


def test_retrieve_finds_room_number():
    world = World(load_building_file(SAMPLE))
    rooms = retrieve(world, "Where is room 102?")
    assert isinstance(rooms, list) and rooms, rooms
    hit = None
    for room in rooms:
        assert isinstance(room, dict), room
        blob = " ".join(str(room.get(key, "")) for key in ("id", "label", "room_id"))
        if FIRE_LABEL in blob or FIRE_ROOM in blob:
            hit = room
            break
    assert hit is not None, rooms
    assert hit.get("id") == FIRE_ROOM or FIRE_LABEL in str(hit.get("label", ""))


def test_ask_returns_mocked_cortex_text_with_bearer():
    _clear_cloud_env()
    captured = []

    def fake_client_post(self, url, *args, **kwargs):
        headers = _headers_of(getattr(self, "headers", None))
        headers.update(_headers_of(kwargs.get("headers")))
        captured.append(headers)
        return _ok_response(url)

    def fake_httpx_post(url, *args, **kwargs):
        captured.append(_headers_of(kwargs.get("headers")))
        return _ok_response(url)

    saved = _use_sample()
    os.environ["SNOWFLAKE_ACCOUNT"] = "orgname-acct"
    os.environ["SNOWFLAKE_PAT"] = _PAT
    os.environ["SNOWFLAKE_MODEL"] = "claude-sonnet-4-5"
    try:
        with patch.object(httpx.Client, "post", fake_client_post), patch.object(httpx, "post", fake_httpx_post):
            res = data.post("/api/ask", json={"question": "Where is room 102?"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["answer"] == "OK"
        assert isinstance(body.get("model"), str) and body["model"]
        assert isinstance(body["sources"], list)
        assert all(isinstance(item, str) for item in body["sources"])
        assert captured, "cortex did not call httpx.post or httpx.Client.post"
        assert any(
            key.lower() == "authorization" and "bearer" in val.lower() and _PAT in val
            for headers in captured
            for key, val in headers.items()
        ), captured
    finally:
        _clear_cloud_env()
        srv.world = saved


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok", name)
    finally:
        _restore_env()
