"""Field report contract tests. Run: python tests/test_reports.py   (or: python -m pytest -q tests/test_reports.py)"""
import copy, functools, importlib, json, math, os, sys, threading
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read keys at request time. Pop before the first import so an import-time cache
# does not keep a real Fish or Cortex client, then restore for the rest of the process.
_ENV_KEYS = ("FISH_API_KEY", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_PAT", "MONGODB_URI")
_SAVED_ENV = {key: os.environ.pop(key, None) for key in _ENV_KEYS}

import httpx
import numpy as np
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from backend.core.model import load_building_file
from backend.server import World
import backend.services.reports as reports
import backend.services.store as store

for _key, _val in _SAVED_ENV.items():
    if _val is not None:
        os.environ[_key] = _val

_REAL_CLIENT = httpx.Client
_REAL_ASYNC = httpx.AsyncClient
_REAL_POST = httpx.post

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "data", "buildings", "sample_building.json")
FAKE_KEY = "test-fish-key"
KINDS = {"fire", "smoke", "clear", "searched", "blocked", "open", "victim", "note"}
# Floor 3, east side of the north row. Same spot test_core uses: exit is EXIT-EAST until the east stair is blocked.
ROUTE_LEVEL, ROUTE_X, ROUTE_Y = 3, 30.0, 5.0
NUMBER_WORDS = (
    ("one", 1), ("two", 2), ("three", 3), ("four", 4), ("five", 5),
    ("six", 6), ("seven", 7), ("eight", 8), ("nine", 9), ("ten", 10),
)

_BUILDING = None


def building():
    global _BUILDING
    if _BUILDING is None:
        _BUILDING = load_building_file(SAMPLE)
    return _BUILDING


def new_world():
    return World(load_building_file(SAMPLE))


def space(level, label):
    for fl in building()["floors"]:
        if int(fl["level"]) != int(level):
            continue
        for room in fl["spaces"]:
            if room["label"] == label:
                return room
    raise AssertionError(f"sample building has no floor {level} label {label!r}")


def vertical(name):
    for item in building()["vertical_connections"]:
        if item["name"].lower() == name.lower():
            return item
    raise AssertionError(name)


def named_targets(name):
    ids = {vertical(name)["id"]}
    for fl in building()["floors"]:
        for room in fl["spaces"]:
            if room["label"].lower() == name.lower():
                ids.add(room["id"])
    return ids


def searchable_ids():
    ids = []
    for fl in building()["floors"]:
        for room in fl["spaces"]:
            if room["type"] not in ("stairwell", "elevator", "void"):
                ids.append(room["id"])
    return ids


def west_east_exits():
    doors = [d for fl in building()["floors"] for d in fl["doors"] if d.get("exterior_exit")]
    doors.sort(key=lambda d: d["x"])
    assert len(doors) >= 2, doors
    return doors[0]["id"], doors[-1]["id"]


def room_cells(world, room_id):
    info = world.grid.room_by_id[room_id]
    fi = world.grid.level_index[info["level"]]
    mask = world.grid.room[fi] == info["index"]
    walk = mask & world.grid.walkable[fi]
    assert walk.any(), room_id
    return fi, mask, walk


def upd(kind, target_id, *, label=None, level=None, severity=None, count=None, raw=None):
    return {
        "kind": kind,
        "target_id": target_id,
        "target_label": label,
        "level": level,
        "severity": severity,
        "count": count,
        "raw": raw if raw is not None else kind,
    }


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def assert_update_shape(u):
    for key in ("kind", "target_id", "target_label", "level", "severity", "count", "raw"):
        assert key in u, u
    assert u["kind"] in KINDS, u
    assert u["target_id"] is None or isinstance(u["target_id"], str), u
    assert u["target_label"] is None or isinstance(u["target_label"], str), u
    assert u["level"] is None or is_int(u["level"]), u
    assert u["severity"] in (None, "light", "heavy"), u
    assert u["count"] is None or is_int(u["count"]), u
    assert isinstance(u["raw"], str) and u["raw"].strip(), u


def hazard_updates(found):
    assert isinstance(found, list), found
    kept = []
    for item in found:
        assert_update_shape(item)
        if item["kind"] == "note" and not item.get("target_id"):
            continue
        kept.append(item)
    return kept


def assert_one_room(text, kind, level, label, *, severity=None, count=None):
    found = hazard_updates(reports.parse(text, building()))
    room = space(level, label)
    assert len(found) == 1, (text, found)
    item = found[0]
    assert item["kind"] == kind, (text, item)
    assert item["target_id"] == room["id"], (text, item)
    assert item["level"] == level, (text, item)
    assert item["severity"] == severity, (text, item)
    if count is None:
        assert item["count"] is None, (text, item)
    else:
        assert is_int(item["count"]) and item["count"] == count, (text, item)
    assert label.lower() in str(item["target_label"]).lower(), (text, item)
    return item


def assert_named(text, kind, name):
    found = hazard_updates(reports.parse(text, building()))
    allowed = named_targets(name)
    assert found, (text, allowed)
    assert all(item["kind"] == kind for item in found), (text, found)
    assert all(item["target_id"] in allowed for item in found), (text, found, allowed)
    assert all(item["severity"] is None and item["count"] is None for item in found), found
    return found


def take_state(world):
    return {
        "fire": np.array(world.sim.fire, copy=True),
        "smoke": np.array(world.sim.smoke, copy=True),
        "blocked": np.array(world.blocked, copy=True),
        "searched": copy.deepcopy(world.searched),
        "reports": copy.deepcopy(world.reports),
        "t": world.sim.t,
    }


def assert_state(world, state, *, reports=True):
    assert np.array_equal(world.sim.fire, state["fire"])
    assert np.array_equal(world.sim.smoke, state["smoke"])
    assert np.array_equal(world.blocked, state["blocked"])
    assert world.searched == state["searched"]
    assert world.sim.t == state["t"]
    if reports:
        assert world.reports == state["reports"]


def assert_stair_blocked(world, vid):
    cells = world.grid.vertical_cells[vid]
    assert len(cells) >= 2, cells
    cell = world.grid.cell
    for fi, r, c in cells.values():
        assert world.blocked[fi, r, c], (vid, fi, r, c)
        assert not world.field.passable[fi, r, c]
        rows, cols = world.blocked.shape[1:]
        saw = 0
        for rr in range(rows):
            for cc in range(cols):
                dist = math.hypot(rr - r, cc - c) * cell
                if dist <= 1.5 + 1e-6 and world.grid.walkable[fi, rr, cc]:
                    assert world.blocked[fi, rr, cc], (vid, fi, rr, cc, round(dist, 3))
                    saw += 1
        assert saw >= 2, (vid, fi, saw)


@contextmanager
def no_network_env():
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def offline(fn):
    @functools.wraps(fn)
    def wrapped():
        with no_network_env():
            return fn()
    return wrapped


@contextmanager
def capturing_log():
    calls = []

    def fake(kind, *args, **kwargs):
        calls.append((kind, args, kwargs))

    saved = []

    def swap(obj, name):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, fake)

    targets = [(store, "log")]
    for name, val in list(vars(reports).items()):
        if val is store.log:
            targets.append((reports, name))
    for obj, name in targets:
        swap(obj, name)
    try:
        yield calls
    finally:
        for obj, name, val in reversed(saved):
            setattr(obj, name, val)


def apply_logged(world, updates, by):
    before = len(world.reports)
    with capturing_log() as calls:
        changes = reports.apply(world, updates, by)
    assert isinstance(changes, list) and changes, changes
    assert all(isinstance(item, str) and item.strip() for item in changes)
    assert [item[0] for item in calls].count("field_report") == 1, calls
    assert len(world.reports) == before + 1
    rep = world.reports[-1]
    for key in ("id", "t", "by", "text", "updates", "changes"):
        assert key in rep, rep
    assert rep["id"] not in (None, "")
    assert isinstance(rep["t"], (int, float)) and not isinstance(rep["t"], bool)
    assert rep["by"] == by
    assert isinstance(rep["text"], str) and rep["text"].strip()
    assert isinstance(rep["updates"], list)
    assert sorted(item["kind"] for item in rep["updates"]) == sorted(item["kind"] for item in updates)
    assert list(rep["changes"]) == list(changes)
    return changes


def header_dict(headers):
    if not headers:
        return {}
    if isinstance(headers, dict):
        items = headers.items()
    else:
        try:
            items = list(headers.items())
        except Exception:
            items = list(headers)
    return {str(key).lower(): str(val) for key, val in items}


def _pairs(data):
    if not data:
        return []
    if isinstance(data, dict):
        return list(data.items())
    try:
        return [(key, val) for key, val in data]
    except Exception:
        return []


def _read_part(part):
    if isinstance(part, (bytes, bytearray)):
        return bytes(part)
    if isinstance(part, str):
        return None
    read = getattr(part, "read", None)
    if not callable(read):
        return None
    try:
        pos = part.tell()
    except Exception:
        pos = None
    data = read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    if pos is not None:
        try:
            part.seek(pos)
        except Exception:
            pass
    return data if isinstance(data, (bytes, bytearray)) else None


def form_map(entry):
    out = {}
    for key, val in _pairs(entry.get("data")):
        out[str(key)] = val
    for key, val in _pairs(entry.get("files")):
        if str(key) == "audio":
            continue
        if isinstance(val, tuple) and len(val) >= 2:
            out[str(key)] = val[1]
        else:
            out[str(key)] = val
    params = entry.get("params")
    if isinstance(params, dict):
        for key, val in params.items():
            out.setdefault(str(key), val)
    return out


def audio_of(entry):
    for key, val in _pairs(entry.get("files")):
        if str(key) != "audio":
            continue
        parts = val if isinstance(val, tuple) else (val,)
        for part in parts:
            data = _read_part(part)
            if data is not None:
                return data
    content = entry.get("content")
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    return None


def timeout_seconds(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    for name in ("read", "write", "pool", "connect"):
        part = getattr(value, name, None)
        if isinstance(part, (int, float)) and not isinstance(part, bool):
            return float(part)
    return None


class _Stream:
    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._response.content

    def json(self):
        return self._response.json()

    @property
    def text(self):
        return self._response.text

    def raise_for_status(self):
        return self._response.raise_for_status()


def _fakes(entries, text, duration):
    class Sync:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")
            self.headers = header_dict(kwargs.get("headers"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            return None

        def post(self, url, *args, **kwargs):
            return self.request("POST", url, *args, **kwargs)

        def request(self, method, url, *args, **kwargs):
            headers = dict(self.headers)
            headers.update(header_dict(kwargs.get("headers")))
            entries.append({
                "method": method,
                "url": str(url),
                "headers": headers,
                "data": kwargs.get("data"),
                "files": kwargs.get("files"),
                "params": kwargs.get("params"),
                "content": kwargs.get("content"),
                "timeout": kwargs.get("timeout", self.timeout),
            })
            try:
                request = httpx.Request("POST", str(url))
            except Exception:
                request = httpx.Request("POST", "https://api.fish.audio/v1/asr")
            return httpx.Response(200, json={"text": text, "duration": duration}, request=request)

        def stream(self, method, url, *args, **kwargs):
            return _Stream(self.request(method, url, *args, **kwargs))

    class Async(Sync):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *args, **kwargs):
            return super().post(url, *args, **kwargs)

        async def request(self, method, url, *args, **kwargs):
            return super().request(method, url, *args, **kwargs)

    return Sync, Async


@contextmanager
def patched_asr(text, duration):
    entries = []
    sync, async_client = _fakes(entries, text, duration)

    def fake_post(url, **kwargs):
        return sync().request("POST", url, **kwargs)

    saved = []

    def swap(obj, name, val):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    for name, val in list(vars(reports).items()):
        if val is _REAL_CLIENT:
            swap(reports, name, sync)
        elif val is _REAL_ASYNC:
            swap(reports, name, async_client)
        elif val is _REAL_POST:
            swap(reports, name, fake_post)
    swap(httpx, "Client", sync)
    swap(httpx, "AsyncClient", async_client)
    swap(httpx, "post", fake_post)
    try:
        yield entries
    finally:
        for obj, name, val in reversed(saved):
            setattr(obj, name, val)


@contextmanager
def asr_env(text, duration, key):
    with no_network_env():
        if key:
            os.environ["FISH_API_KEY"] = key
        importlib.reload(reports)
        with patched_asr(text, duration) as entries:
            yield entries


def make_client(world):
    app = FastAPI()
    app_lock = threading.Lock()
    app.include_router(reports.make_router(lambda: world, app_lock))

    @app.get("/api/route")
    def route(level: int = Query(...), x: float = Query(...), y: float = Query(...)):
        # Same query contract as the server route, reading this fresh World.
        with app_lock:
            fi = world.grid.level_index[level]
            return world.field.route_from(fi, x, y)

    return TestClient(app)


def post_report(client, text, by="Engine 1", dry_run=False):
    res = client.post("/api/reports", json={"text": text, "by": by, "dry_run": dry_run})
    assert res.status_code == 200, res.text
    body = res.json()
    for key in ("report", "changes", "confirmation"):
        assert key in body, body
    assert isinstance(body["report"], dict), body
    assert isinstance(body["changes"], list), body
    assert all(isinstance(item, str) for item in body["changes"])
    assert isinstance(body["confirmation"], str) and body["confirmation"].strip(), body
    return body


def get_route(client):
    res = client.get("/api/route", params={"level": ROUTE_LEVEL, "x": ROUTE_X, "y": ROUTE_Y})
    assert res.status_code == 200, res.text
    body = res.json()
    assert "reachable" in body and "exit" in body, body
    return body


def assert_fish_call(entries, payload):
    assert entries, "stt did not call httpx"
    for entry in entries:
        assert "api.fish.audio" in entry["url"] and "/v1/asr" in entry["url"], entry["url"]
        assert str(entry["method"]).upper() == "POST", entry
        auth = entry["headers"].get("authorization", "")
        parts = auth.split()
        assert len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == FAKE_KEY, entry["headers"]
        assert entry["headers"].get("model", "").strip() == "transcribe-1-pro", entry["headers"]
        fields = form_map(entry)
        assert str(fields.get("language", "")).lower() == "en", fields
        assert str(fields.get("ignore_timestamps", "")).lower() == "true", fields
        assert str(fields.get("tag_audio_events", "")).lower() == "false", fields
        assert audio_of(entry) == payload, entry.get("files")
        secs = timeout_seconds(entry.get("timeout"))
        assert secs is not None and abs(secs - 30.0) < 1e-6, entry.get("timeout")


@offline
def test_parse_kinds_severity_victims_and_stairs():
    cases = [
        ("Floor 1 room 102 is on fire.", "fire", 1, "102", None, None),
        ("Flames in room 103 on floor 1.", "fire", 1, "103", None, None),
        ("Room 104 on floor 1 is burning.", "fire", 1, "104", None, None),
        ("Heavy smoke in room 105 on floor 1.", "smoke", 1, "105", "heavy", None),
        ("Thick smoke in room 107 on floor 1.", "smoke", 1, "107", "heavy", None),
        ("Light smoke in room 108 on floor 1.", "smoke", 1, "108", "light", None),
        ("Some smoke in room 110 on floor 1.", "smoke", 1, "110", "light", None),
        ("Room 102 on floor 1 is all clear.", "clear", 1, "102", None, None),
        ("No fire in room 103 on floor 1.", "clear", 1, "103", None, None),
        ("Room 111 on floor 1 is clear.", "clear", 1, "111", None, None),
        ("Room 202 on floor 2 searched.", "searched", 2, "202", None, None),
        ("Checked room 203 on floor 2.", "searched", 2, "203", None, None),
        ("Primary done in room 204 on floor 2.", "searched", 2, "204", None, None),
        ("Room 205 on floor 2 is locked.", "blocked", 2, "205", None, None),
    ]
    for text, kind, level, label, severity, count in cases:
        assert_one_room(text, kind, level, label, severity=severity, count=count)

    assert_named("East stairs blocked.", "blocked", "East Stairs")
    assert_named("West stairs collapsed.", "blocked", "West Stairs")
    assert_named("East stairs impassable.", "blocked", "East Stairs")
    assert_named("Stairwell East Stairs is locked.", "blocked", "East Stairs")
    assert_named("Elevator blocked.", "blocked", "Elevator")
    assert_named("East stairs open.", "open", "East Stairs")
    assert_named("West stairs passable.", "open", "West Stairs")
    east = named_targets("East Stairs")
    stair_id = vertical("East Stairs")["id"]
    found = hazard_updates(reports.parse("STAIR-EAST blocked.", building()))
    assert found and all(item["kind"] == "blocked" and item["target_id"] in east for item in found)
    assert any(item["target_id"] == stair_id for item in found), found

    for word, count in NUMBER_WORDS:
        if count == 1:
            text = "One person trapped in room 102 on floor 1."
        else:
            text = f"{word.capitalize()} victims trapped in room 102 on floor 1."
        item = assert_one_room(text, "victim", 1, "102", count=count)
        assert item["count"] == count, (text, item)

    note = "Note the standpipe valve is outside."
    found = reports.parse(note, building())
    assert hazard_updates(found) == [], found
    assert any(item["kind"] == "note" and "standpipe" in item["raw"].lower() for item in found), found


@offline
def test_parse_level_context_and_splits():
    text = "Floor 1, room 102 searched; room 103 is on fire."
    found = hazard_updates(reports.parse(text, building()))
    assert len(found) == 2, found
    by_id = {item["target_id"]: item for item in found}
    assert by_id[space(1, "102")["id"]]["kind"] == "searched"
    assert by_id[space(1, "103")["id"]]["kind"] == "fire"
    assert all(item["level"] == 1 for item in found)

    text = "Room 104 on floor 1 searched and room 105 on floor 1 is clear."
    found = hazard_updates(reports.parse(text, building()))
    assert len(found) == 2, found
    by_id = {item["target_id"]: item for item in found}
    assert by_id[space(1, "104")["id"]]["kind"] == "searched"
    assert by_id[space(1, "105")["id"]]["kind"] == "clear"

    text = "Floor 2. Restroom is all clear. East stairs blocked."
    found = hazard_updates(reports.parse(text, building()))
    clears = [item for item in found if item["kind"] == "clear"]
    blocks = [item for item in found if item["kind"] == "blocked"]
    assert len(clears) == 1, found
    assert clears[0]["target_id"] == space(2, "Restroom")["id"]
    assert clears[0]["level"] == 2
    east_here = {vertical("East Stairs")["id"], space(2, "East Stairs")["id"]}
    other_floors = {space(1, "East Stairs")["id"], space(3, "East Stairs")["id"]}
    assert blocks, found
    assert all(item["target_id"] in east_here for item in blocks), blocks
    assert all(item["target_id"] not in other_floors for item in blocks), blocks

    reports.parse("Floor 3 room 302 is on fire.", building())
    found = hazard_updates(reports.parse("Room 102 is on fire.", building()))
    assert len(found) == 1, found
    assert found[0]["kind"] == "fire" and found[0]["target_id"] == space(1, "102")["id"]


@offline
def test_apply_fire_smoke_searched():
    world = new_world()
    fire_id = space(1, "102")["id"]
    smoke_id = space(1, "108")["id"]
    searched_id = space(2, "202")["id"]
    calls = {"n": 0}
    orig = world.refresh_routes

    def wrapped():
        calls["n"] += 1
        return orig()

    world.refresh_routes = wrapped
    updates = [
        upd("fire", fire_id, label="102", level=1, raw="room 102 on fire"),
        upd("smoke", smoke_id, label="108", level=1, severity="light", raw="light smoke in 108"),
        upd("searched", searched_id, label="202", level=2, raw="202 searched"),
    ]
    apply_logged(world, updates, "Engine 1")
    assert calls["n"] == 1
    assert world.sim.room_status()[fire_id] == "fire"
    fi, mask, _walk = room_cells(world, fire_id)
    assert world.sim.fire[fi][mask].any()
    assert not world.sim.fire[fi][~mask].any()
    for other in range(world.sim.fire.shape[0]):
        if other != fi:
            assert not world.sim.fire[other].any()
    assert world.sim.room_status()[smoke_id] == "smoke"
    rec = world.searched[searched_id]
    assert rec["by"] == "Engine 1"
    assert "note" in rec
    assert isinstance(rec["t"], (int, float)) and not isinstance(rec["t"], bool)
    assert rec["t"] > 1_000_000_000


@offline
def test_apply_smoke_severity():
    light_id = space(1, "108")["id"]
    heavy_id = space(1, "105")["id"]

    world = new_world()
    before = world.sim.smoke.copy()
    apply_logged(world, [upd("smoke", light_id, label="108", level=1, severity="light", raw="light smoke")], "Engine 1")
    fi, mask, walk = room_cells(world, light_id)
    vals = world.sim.smoke[fi][walk]
    assert float(vals.min()) >= 0.5 - 1e-3
    assert float(vals.max()) <= 0.5 + 1e-3
    assert np.array_equal(world.sim.smoke[fi][~mask], before[fi][~mask])
    for other in range(world.sim.smoke.shape[0]):
        if other != fi:
            assert np.array_equal(world.sim.smoke[other], before[other])
    assert not world.sim.fire.any()
    assert world.sim.room_status()[light_id] == "smoke"

    world = new_world()
    apply_logged(world, [upd("smoke", heavy_id, label="105", level=1, severity="heavy", raw="heavy smoke")], "Engine 1")
    _fi, _mask, walk = room_cells(world, heavy_id)
    assert float(world.sim.smoke[_fi][walk].min()) >= 0.9 - 1e-3
    assert not world.sim.fire.any()

    world = new_world()
    apply_logged(world, [upd("smoke", heavy_id, label="105", level=1, severity="heavy", raw="heavy smoke")], "Engine 1")
    apply_logged(world, [upd("smoke", heavy_id, label="105", level=1, severity="light", raw="light smoke")], "Engine 1")
    _fi, _mask, walk = room_cells(world, heavy_id)
    assert float(world.sim.smoke[_fi][walk].min()) >= 0.9 - 1e-3


@offline
def test_apply_blocked_open():
    world = new_world()
    stair = vertical("East Stairs")["id"]
    room_id = space(1, "103")["id"]
    apply_logged(world, [upd("blocked", stair, label="East Stairs", raw="east stairs blocked")], "Engine 1")
    assert_stair_blocked(world, stair)
    apply_logged(world, [upd("blocked", room_id, label="103", level=1, raw="103 blocked")], "Engine 1")
    fi, _mask, walk = room_cells(world, room_id)
    assert world.blocked[fi][walk].all()
    assert not world.sim.fire.any()
    assert float(world.sim.smoke.max()) == 0.0
    assert_stair_blocked(world, stair)

    apply_logged(world, [upd("open", room_id, label="103", level=1, raw="103 open")], "Engine 1")
    assert not world.blocked[fi][walk].any()
    assert_stair_blocked(world, stair)

    apply_logged(world, [upd("open", stair, label="East Stairs", raw="east stairs open")], "Engine 1")
    assert not world.blocked.any()
    for fi, r, c in world.grid.vertical_cells[stair].values():
        assert world.grid.walkable[fi, r, c]
        assert world.field.passable[fi, r, c]


@offline
def test_apply_clear():
    world = new_world()
    room_id = space(1, "102")["id"]
    other_id = space(1, "103")["id"]
    stair = vertical("East Stairs")["id"]
    apply_logged(world, [upd("smoke", other_id, label="103", level=1, severity="heavy", raw="heavy smoke 103")], "Engine 1")
    apply_logged(world, [upd("fire", room_id, label="102", level=1, raw="102 on fire")], "Engine 1")
    apply_logged(world, [upd("smoke", room_id, label="102", level=1, severity="heavy", raw="heavy smoke 102")], "Engine 1")
    apply_logged(world, [upd("blocked", room_id, label="102", level=1, raw="102 blocked")], "Engine 1")
    apply_logged(world, [upd("blocked", stair, label="East Stairs", raw="east stairs blocked")], "Engine 1")
    apply_logged(world, [upd("searched", room_id, label="102", level=1, raw="102 searched")], "Engine 1")
    fi, mask, walk = room_cells(world, room_id)
    assert world.sim.room_status()[room_id] == "fire"
    assert float(world.sim.smoke[fi][walk].min()) >= 0.9 - 1e-3
    assert world.blocked[fi][walk].all()

    apply_logged(world, [upd("clear", room_id, label="102", level=1, raw="102 all clear")], "Engine 1")
    assert not world.sim.fire[fi][mask].any()
    assert float(world.sim.smoke[fi][mask].max()) <= 1e-6
    assert not world.blocked[fi][walk].any()
    assert world.sim.room_status()[room_id] == "clear"
    assert world.searched[room_id]["by"] == "Engine 1"
    ofi, _omask, owalk = room_cells(world, other_id)
    assert float(world.sim.smoke[ofi][owalk].min()) >= 0.9 - 1e-3
    assert world.sim.room_status()[other_id] == "smoke"
    assert_stair_blocked(world, stair)


@offline
def test_apply_victim_and_note():
    world = new_world()
    state = take_state(world)
    room_id = space(1, "110")["id"]
    updates = [
        upd("victim", room_id, label="110", level=1, count=3, raw="three victims in 110"),
        upd("note", None, raw="standpipe valve is outside"),
    ]
    apply_logged(world, updates, "Engine 1")
    assert_state(world, state, reports=False)
    assert room_id not in world.searched
    stored = world.reports[-1]["updates"]
    victim = next(item for item in stored if item["kind"] == "victim")
    note = next(item for item in stored if item["kind"] == "note")
    assert victim["count"] == 3 and victim["target_id"] == room_id
    blob = (note.get("raw") or "") + " " + world.reports[-1]["text"]
    assert "standpipe" in blob.lower()


@offline
def test_blocked_stair_changes_route_and_undo():
    west, east = west_east_exits()
    world = new_world()
    client = make_client(world)
    before = get_route(client)
    assert before["reachable"] is True and before["exit"] == east, before
    state = take_state(world)
    with capturing_log() as calls:
        body = post_report(client, "East stairs blocked.", "Engine 1")
    assert any(item[0] == "field_report" for item in calls), calls
    assert body["changes"]
    low = body["confirmation"].lower()
    assert "east" in low or "stair" in low, body["confirmation"]
    after = get_route(client)
    assert after["reachable"] is True and after["exit"] == west, after
    vid = vertical("East Stairs")["id"]
    stair_hit = any(world.blocked[fi, r, c] for fi, r, c in world.grid.vertical_cells[vid].values())
    room_hit = False
    for level in (1, 2, 3):
        fi, _mask, walk = room_cells(world, space(level, "East Stairs")["id"])
        if world.blocked[fi][walk].any():
            room_hit = True
    assert stair_hit or room_hit

    res = client.post("/api/reports/undo")
    assert res.status_code == 200, res.text
    undo = res.json()
    assert undo.get("ok") is True, undo
    assert isinstance(undo.get("changes"), list)
    restored = get_route(client)
    assert restored["reachable"] is True and restored["exit"] == east, restored
    assert_state(world, state, reports=False)


@offline
def test_undo_restores_only_last_report():
    world = new_world()
    client = make_client(world)
    base = take_state(world)
    fire_id = space(1, "102")["id"]
    searched_id = space(1, "103")["id"]
    post_report(client, "Floor 1 room 102 is on fire.", "Engine 1")
    assert world.sim.room_status()[fire_id] == "fire"
    mid = take_state(world)
    post_report(client, "Floor 1 room 103 searched.", "Engine 1")
    assert searched_id in world.searched
    assert world.sim.room_status()[fire_id] == "fire"

    first = client.post("/api/reports/undo")
    assert first.status_code == 200, first.text
    assert first.json().get("ok") is True
    assert searched_id not in world.searched
    assert_state(world, mid, reports=False)

    second = client.post("/api/reports/undo")
    assert second.status_code == 200, second.text
    assert second.json().get("ok") is True
    assert_state(world, base, reports=False)
    assert world.sim.room_status()[fire_id] == "clear"


@offline
def test_dry_run_does_not_change_state():
    world = new_world()
    client = make_client(world)
    state = take_state(world)
    text = "Floor 1 room 102 is on fire and room 103 on floor 1 searched and east stairs blocked."
    with capturing_log() as calls:
        body = post_report(client, text, "Engine 1", dry_run=True)
    assert_state(world, state)
    assert not any(item[0] == "field_report" for item in calls), calls
    updates = body["report"].get("updates")
    assert isinstance(updates, list) and updates, body
    kinds = {item.get("kind") for item in updates}
    assert {"fire", "searched", "blocked"} <= kinds, updates
    assert any(item.get("kind") == "fire" and item.get("target_id") == space(1, "102")["id"] for item in updates)
    assert "102" in body["confirmation"]
    applied = post_report(client, "Floor 1 room 102 is on fire.", "Engine 1")
    assert applied["changes"]
    assert world.sim.room_status()[space(1, "102")["id"]] == "fire"


@offline
def test_reports_newest_first_and_searched():
    world = new_world()
    client = make_client(world)
    empty = client.get("/api/reports")
    assert empty.status_code == 200, empty.text
    assert empty.json() == []
    body = client.get("/api/reports/searched")
    assert body.status_code == 200, body.text
    listed = body.json()
    assert set(listed) == {"unsearched"}, listed
    ids = _unsearched_ids(listed)
    assert len(ids) == len(set(ids))
    assert set(ids) == set(searchable_ids())
    assert space(1, "102")["id"] in ids
    assert space(1, "Hall 1")["id"] in ids
    for level in (1, 2, 3):
        assert space(level, "East Stairs")["id"] not in ids
        assert space(level, "West Stairs")["id"] not in ids
        assert space(level, "Elevator")["id"] not in ids

    post_report(client, "Floor 1 room 102 searched.", "Engine 1")
    post_report(client, "Floor 1 room 103 searched.", "Engine 1")
    stored_ids = [rep.get("id") for rep in world.reports]
    res = client.get("/api/reports")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert [rep.get("id") for rep in world.reports] == stored_ids
    assert isinstance(rows, list) and len(rows) == 2, rows
    assert [row.get("id") for row in rows] == list(reversed(stored_ids))
    assert "103" in json.dumps(rows[0])
    assert "102" in json.dumps(rows[1])

    searched = client.get("/api/reports/searched").json()
    ids = _unsearched_ids(searched)
    room_102 = space(1, "102")["id"]
    room_103 = space(1, "103")["id"]
    assert set(searched) == {"unsearched", room_102, room_103}
    assert set(ids) == set(searchable_ids()) - {room_102, room_103}
    for room_id in (room_102, room_103):
        rec = searched[room_id]
        assert rec["by"] == "Engine 1"
        assert "note" in rec
        assert isinstance(rec["t"], (int, float)) and rec["t"] > 1_000_000_000
        assert world.searched[room_id]["by"] == "Engine 1"


def _unsearched_ids(body):
    raw = body["unsearched"]
    assert isinstance(raw, list), body
    ids = []
    for item in raw:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            ids.append(item.get("id") or item.get("room_id"))
        else:
            raise AssertionError(item)
    assert all(isinstance(item, str) and item for item in ids), ids
    return ids


@offline
def test_stt_503_without_key():
    payload = b"RIFF-fake-audio-no-key"
    with asr_env("<|bg|>ignored", 1.0, None) as entries:
        client = make_client(new_world())
        res = client.post("/api/voice/stt", files={"audio": ("clip.webm", payload, "audio/webm")})
        assert res.status_code == 503, res.text
        detail = res.json().get("detail")
        assert isinstance(detail, str) and detail.strip(), res.text
        low = detail.lower()
        assert any(word in low for word in ("fish", "key", "missing", "not set", "unconfigured")), detail
        assert entries == []
        assert FAKE_KEY not in res.text


@offline
def test_stt_mocked_auth_model_and_markers():
    payload = b"RIFF-fake-audio-bytes"
    transcript = "<|bg|>hello <|noise|>there"
    with asr_env(transcript, 2.5, FAKE_KEY) as entries:
        client = make_client(new_world())
        res = client.post("/api/voice/stt", files={"audio": ("clip.webm", payload, "audio/webm")})
        assert res.status_code == 200, res.text
        body = res.json()
        assert "text" in body and "duration" in body, body
        assert "<|" not in body["text"] and "|>" not in body["text"], body["text"]
        assert body["text"].index("hello") < body["text"].index("there")
        assert float(body["duration"]) == 2.5
        assert FAKE_KEY not in res.text
        assert_fish_call(entries, payload)


@offline
def test_reports_audio_applies_transcript():
    payload = b"RIFF-fake-report-audio"
    transcript = "Floor 1 room 102 is on fire."
    with asr_env(transcript, 1.25, FAKE_KEY) as entries:
        world = new_world()
        client = make_client(world)
        res = client.post(
            "/api/reports/audio",
            data={"by": "Engine 1"},
            files={"audio": ("clip.webm", payload, "audio/webm")},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        for key in ("report", "changes", "confirmation"):
            assert key in body, body
        assert body["report"].get("by") == "Engine 1"
        assert body["changes"]
        assert world.sim.room_status()[space(1, "102")["id"]] == "fire"
        assert_fish_call(entries, payload)


@offline
def test_undo_keeps_fire_steps_after_report():
    world = new_world()
    client = make_client(world)
    post_report(client, "Floor 1 room 102 is on fire.", "Engine 1")
    world.sim.step(3)
    post_report(client, "East stairs blocked.", "Engine 1")
    inv = world._report_undo[-1][1]
    assert isinstance(inv["blocked"], list) and inv["blocked"]
    assert inv["fire"] == [] and inv["smoke"] == []
    assert not hasattr(inv["blocked"], "shape")
    before_fire = np.array(world.sim.fire, copy=True)
    before_smoke = np.array(world.sim.smoke, copy=True)
    changed = False
    for _ in range(30):
        world.sim.step(1)
        if not np.array_equal(world.sim.fire, before_fire) or not np.array_equal(world.sim.smoke, before_smoke):
            changed = True
            break
    assert changed, "fire did not spread after the blockage report"
    fire_now = np.array(world.sim.fire, copy=True)
    smoke_now = np.array(world.sim.smoke, copy=True)
    res = client.post("/api/reports/undo")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("ok") is True, body
    assert np.array_equal(world.sim.fire, fire_now)
    assert np.array_equal(world.sim.smoke, smoke_now)
    assert not world.blocked.any()
    assert (
        not np.array_equal(world.sim.fire, before_fire)
        or not np.array_equal(world.sim.smoke, before_smoke)
    )


@offline
def test_empty_parse_returns_this_text_not_previous_report():
    world = new_world()
    client = make_client(world)
    first = post_report(client, "Floor 1 room 102 is on fire.", "Engine 1")
    phrase = "the"
    assert reports.parse(phrase, world.model) == []
    n = len(world.reports)
    with capturing_log() as calls:
        body = post_report(client, phrase, "Engine 1")
    assert not any(item[0] == "field_report" for item in calls), calls
    report = body["report"]
    assert report["updates"] == []
    assert report["text"] == phrase
    assert report.get("id") != first["report"]["id"]
    assert "102" not in report["text"]
    assert body["changes"] == []
    assert "nothing to add" in body["confirmation"].lower()
    assert len(world.reports) == n
    assert world.reports[-1]["id"] == first["report"]["id"]
    assert world.sim.room_status()[space(1, "102")["id"]] == "fire"


@offline
def test_report_caps_and_reset():
    world = new_world()
    saved_reports, saved_undo = reports._MAX_REPORTS, reports._MAX_UNDO
    reports._MAX_REPORTS = 2
    reports._MAX_UNDO = 1
    try:
        ids = []
        for raw in ("standpipe one", "standpipe two", "standpipe three"):
            reports.apply(world, [upd("note", None, raw=raw)], "Engine 1")
            ids.append(world.reports[-1]["id"])
        assert [rep["id"] for rep in world.reports] == ids[1:]
        assert len(world._report_undo) == 1
        assert world._report_undo[0][0] == ids[2]
        world.blocked[0, 0, 0] = True
        world.searched["x"] = {"by": "Engine 1", "t": 1, "note": ""}
        world.sim.ignite_room(space(1, "102")["id"])
        assert world.sim.fire.any()
        reports.reset(world)
        assert world.reports == []
        assert world.searched == {}
        assert not world.blocked.any()
        assert world._report_undo == []
        assert world.sim.fire.any()
    finally:
        reports._MAX_REPORTS = saved_reports
        reports._MAX_UNDO = saved_undo


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok", name)
    finally:
        for key, val in _SAVED_ENV.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
