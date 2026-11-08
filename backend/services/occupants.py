"""Occupant API: QR anchors, live nav hints, and check-ins.

Wire-up (before the static `/` mount in server.py):

    from .occupants import make_router as occupants_router
    app.include_router(occupants_router(lambda: world, lock))
"""
import json
import math
import os
import threading
import time
from html import escape
from urllib.parse import quote

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

_QR_JS = "https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"
_QR_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "qr.json")
_SKIP_TYPES = ("stairwell", "elevator", "void")
_NEXT_M = 1.0
_TURN_DEG = 30.0
_FIRE_M = 5.0
_SMOKE = 0.35
_STATUSES = ("safe", "trapped", "evacuating")

# In-memory check-ins. Own lock, separate from the world lock.
people = {}
people_lock = threading.Lock()
_people_hydrated = False
_hydrate_lock = threading.Lock()


def _hydrate_people() -> None:
    """Fill `people` from Mongo once. Disabled Mongo leaves the dict unchanged."""
    global _people_hydrated
    if _people_hydrated:
        return
    with _hydrate_lock:
        if _people_hydrated:
            return
        try:
            from . import store
            rows = store.load_people()
        except Exception:
            rows = []
        _people_hydrated = True
    if not rows:
        return
    with people_lock:
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = row.get("person_id")
            if pid in (None, "") or pid in people:
                continue
            people[pid] = dict(row)


class PersonStatus(BaseModel):
    status: str
    level: int | None = None
    x: float | None = None
    y: float | None = None
    room_id: str | None = None


def _bearing(x1, y1, x2, y2):
    """Plan bearing in degrees: 0 = up (-y), clockwise positive. None if no distance."""
    dx, dy = x2 - x1, y2 - y1
    if dx * dx + dy * dy < 1e-12:
        return None
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def _turn_name(heading, heading_after):
    """Clockwise change is a right turn. |delta| < 30 deg is straight."""
    if heading is None or heading_after is None:
        return "straight"
    delta = (heading_after - heading + 180.0) % 360.0 - 180.0
    if abs(delta) < _TURN_DEG:
        return "straight"
    return "right" if delta > 0.0 else "left"


def nav_hints(path, level, x, y, plan_north_deg):
    """Next waypoint, distance, turn, and headings for one position.

    `next` is the first path point at least 1 m away on this level, or the
    first point on another level if the path changes floor before that.
    `turn` is the maneuver at that point.
    """
    try:
        north = float(plan_north_deg)
    except (TypeError, ValueError):
        north = 0.0
    x, y = float(x), float(y)
    empty = {
        "next": None,
        "distance_to_next_m": 0.0,
        "turn": "arrive",
        "heading_plan_deg": 0.0,
        "heading_compass_deg": north % 360.0,
    }
    chosen = None  # (index, level, x, y, dist)
    for i, pt in enumerate(path or []):
        if not isinstance(pt, (list, tuple)) or len(pt) < 3:
            continue
        try:
            pl, px, py = pt[0], float(pt[1]), float(pt[2])
        except (TypeError, ValueError):
            continue
        dist = math.hypot(px - x, py - y)
        if pl != level or dist >= _NEXT_M:
            chosen = (i, pl, px, py, dist)
            break
    if chosen is None:
        return empty

    i, pl, px, py, dist = chosen
    heading = _bearing(x, y, px, py)
    heading_plan = 0.0 if heading is None else heading
    if pl != level:
        turn = "stairs_up" if pl > level else "stairs_down"
    elif i + 1 >= len(path):
        turn = "arrive"
    else:
        fol = path[i + 1]
        if not isinstance(fol, (list, tuple)) or len(fol) < 3:
            turn = "arrive"
        else:
            try:
                fx, fy = float(fol[1]), float(fol[2])
            except (TypeError, ValueError):
                turn = "arrive"
            else:
                turn = _turn_name(heading, _bearing(px, py, fx, fy))
    try:
        pl_out = int(pl) if float(pl) == int(pl) else pl
    except (TypeError, ValueError):
        pl_out = pl
    return {
        "next": {"level": pl_out, "x": px, "y": py},
        "distance_to_next_m": dist,
        "turn": turn,
        "heading_plan_deg": heading_plan,
        "heading_compass_deg": (north + heading_plan) % 360.0,
    }


def _load_qr_file():
    """Optional data/qr.json. Missing or unreadable means no overrides."""
    if not os.path.isfile(_QR_PATH):
        return {}
    try:
        with open(_QR_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _listed_rooms(world):
    return [info for info in world.grid.rooms if info.get("type") not in _SKIP_TYPES]


def _room_pose(world, info):
    """Walkable cell nearest the room centroid, restricted to that room."""
    g = world.grid
    fi = g.level_index[info["level"]]
    mask = g.room[fi] == info["index"]
    reach = max(50.0, float(g.rows + g.cols) * float(g.cell))
    cx, cy = info["centroid"]
    cell = g.nearest_walkable(fi, cx, cy, max_dist=reach, mask=mask)
    if cell is None:
        return None
    x, y = g.center_of(*cell)
    return {
        "code": info["id"],
        "room_id": info["id"],
        "label": info.get("label") or info["id"],
        "level": info["level"],
        "x": x,
        "y": y,
        "heading_deg": 0,
    }


def _override_pose(world, code, entry):
    if not isinstance(entry, dict):
        return None
    if any(k not in entry for k in ("level", "x", "y")):
        return None
    try:
        level = entry["level"]
        if isinstance(level, bool) or isinstance(entry["x"], bool) or isinstance(entry["y"], bool):
            return None
        level_f = float(level)
        x, y = float(entry["x"]), float(entry["y"])
        heading = float(entry.get("heading_deg", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (level_f, x, y, heading)):
        return None
    level_out = int(level_f) if level_f == int(level_f) else level_f
    room = world.grid.room_by_id.get(code)
    if "room_id" in entry:
        room_id = entry["room_id"]
    elif room is not None:
        room_id = room["id"]
    else:
        room_id = None
    label = entry.get("label") or (room.get("label") if room else None) or code
    heading_out = int(heading) if heading == int(heading) else heading
    return {
        "code": code,
        "room_id": room_id,
        "label": label,
        "level": level_out,
        "x": x,
        "y": y,
        "heading_deg": heading_out,
    }


def qr_lookup(world, code):
    """Pose for a QR code: data/qr.json entry, otherwise a room id. None if unknown."""
    code = str(code)
    custom = _override_pose(world, code, _load_qr_file().get(code))
    if custom is not None:
        return custom
    info = world.grid.room_by_id.get(code)
    if info is None:
        return None
    return _room_pose(world, info)


def _hazard(world, fi, x, y, max_smoke):
    """fire_near beats smoke_on_route. Fire distance is to the cell center."""
    g = world.grid
    near = np.hypot(g.X - float(x), g.Y - float(y)) <= _FIRE_M
    if bool(np.any(world.sim.fire[fi] & near)):
        return "fire_near"
    try:
        smoke = float(max_smoke)
    except (TypeError, ValueError):
        smoke = 0.0
    if smoke > _SMOKE:
        return "smoke_on_route"
    return None


def _rev(world):
    return f"{id(world)}:{world.sim.t}"


def _print_html(rooms, base):
    cards = []
    jobs = []
    for i, info in enumerate(rooms):
        code = str(info["id"])
        url = f"FIRESIGHT:{code}"   # read by the mobile app's QR scanner
        label = escape(str(info.get("label") or code))
        meta = escape(f"Floor {info['level']}, {code}")
        cards.append(
            f'<section class="card"><div id="q{i}" class="qr"></div>'
            f'<p class="label">{label}</p><p class="meta">{meta}</p></section>'
        )
        jobs.append({"id": f"q{i}", "text": url})
    payload = json.dumps(jobs).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = "\n".join(cards)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Firesight QR cards</title>
<style>
  html, body {{ margin: 0; background: #fff; color: #000; font-family: Arial, sans-serif; }}
  .sheet {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; padding: 16px; background: #fff; }}
  .card {{ border: 1px solid #ccc; padding: 12px; text-align: center; background: #fff; break-inside: avoid; page-break-inside: avoid; }}
  .qr {{ display: flex; justify-content: center; min-height: 140px; }}
  .label {{ margin: 8px 0 0; font-size: 16px; font-weight: 700; }}
  .meta {{ margin: 4px 0 0; font-size: 12px; }}
  @media print {{
    html, body, .sheet, .card {{ background: #fff; color: #000; }}
    .sheet {{ grid-template-columns: repeat(3, 1fr); }}
    .card {{ break-inside: avoid; page-break-inside: avoid; }}
  }}
</style>
</head>
<body>
<main class="sheet">
{body}
</main>
<script src="{_QR_JS}"></script>
<script>
(function () {{
  var jobs = {payload};
  for (var i = 0; i < jobs.length; i++) {{
    new QRCode(document.getElementById(jobs[i].id), {{
      text: jobs[i].text, width: 140, height: 140, correctLevel: QRCode.CorrectLevel.M
    }});
  }}
}})();
</script>
</body>
</html>
"""


def make_router(get_world, lock) -> APIRouter:
    router = APIRouter()

    def _world():
        return get_world()

    @router.get("/api/qr/print", response_class=HTMLResponse)
    def qr_print(request: Request, base: str | None = Query(None)):
        root = base.strip().rstrip("/") if base and base.strip() else str(request.base_url).rstrip("/")
        with lock:
            rooms = list(_listed_rooms(_world()))
        return HTMLResponse(_print_html(rooms, root))

    @router.get("/api/qr")
    def qr_list():
        with lock:
            world = _world()
            return [
                {
                    "code": info["id"],
                    "room_id": info["id"],
                    "label": info.get("label") or info["id"],
                    "level": info["level"],
                }
                for info in _listed_rooms(world)
            ]

    @router.get("/api/qr/{code}")
    def qr_one(code: str):
        with lock:
            found = qr_lookup(_world(), code)
        if found is None:
            raise HTTPException(404, f"Unknown QR code {code}")
        return found

    @router.get("/api/nav")
    def nav(level: int = Query(...), x: float = Query(...), y: float = Query(...)):
        with lock:
            world = _world()
            if level not in world.grid.level_index:
                raise HTTPException(404, f"Unknown floor level {level}")
            fi = world.grid.level_index[level]
            route = world.field.route_from(fi, x, y)
            north = world.model["plan_north_deg"]
            hints = nav_hints(route.get("path") or [], level, x, y, north)
            if not route.get("reachable"):
                hints["next"] = None
                hints["distance_to_next_m"] = 0.0
                hints["turn"] = "arrive"
            out = dict(route)
            out.update(hints)
            out["plan_north_deg"] = north
            out["hazard"] = _hazard(world, fi, x, y, route.get("max_smoke"))
            out["rev"] = _rev(world)
            return out

    @router.get("/api/rev")
    def rev():
        with lock:
            world = _world()
            return {"rev": _rev(world), "t": world.sim.t}

    @router.post("/api/people/{person_id}/status")
    def set_status(person_id: str, body: PersonStatus):
        if body.status not in _STATUSES:
            raise HTTPException(400, "status must be safe, trapped, or evacuating")
        _hydrate_people()
        record = {
            "person_id": person_id,
            "status": body.status,
            "level": body.level,
            "x": body.x,
            "y": body.y,
            "room_id": body.room_id,
            "updated_at": time.time(),
        }
        with people_lock:
            people[person_id] = record
        from . import store   # MongoDB Atlas incident log (no-op when not configured)
        store.log("person_status", dict(record))
        store.save_person(dict(record))
        from . import live                  # commander page updates instantly
        live.publish("people", dict(record))
        return {"ok": True}

    @router.get("/api/people")
    def list_people():
        _hydrate_people()
        with people_lock:
            return [dict(row) for row in people.values()]

    return router
