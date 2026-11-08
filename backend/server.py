"""Firesight API + static 3D viewer.

Run from the project root:
    uvicorn backend.server:app --reload --port 8000
Then open http://localhost:8000

Env:
    FIRESIGHT_BUILDING  path to building JSON (default data/buildings/sample_building.json)
"""
import json
import os

import numpy as np
import re
import threading

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .core.fire import FireSim
from .core.grid import BuildingGrid
from .core.model import ModelError, load_building, load_building_file
from .core.routing import RouteField, best_room_cell
from .services import store
from .services import live

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BUILDING = os.path.join(ROOT, "data", "buildings", "sample_building.json")
VIEWER_DIR = os.path.join(ROOT, "viewer")


class World:
    """Everything derived from one building JSON. Rebuilt on upload."""

    def __init__(self, model):
        self.model = model
        self.grid = BuildingGrid(model)
        self.sim = FireSim(self.grid)
        # Field reports (firefighter voice/text): cells reported impassable, rooms searched, report log.
        self.blocked = np.zeros(self.grid.walkable.shape, dtype=bool)
        self.searched = {}      # room_id -> {"by", "t", "note"}
        self.reports = []       # applied report dicts, newest last
        self.field = RouteField(self.grid)

    def refresh_routes(self):
        """Routes avoid fire and reported blockages; smoke raises cost."""
        self.field = RouteField(self.grid, self.sim.fire | self.blocked, self.sim.smoke)


lock = threading.Lock()
def _building_path():
    """FIRESIGHT_BUILDING, also accepting old paths from before data/buildings/ existed."""
    want = os.environ.get("FIRESIGHT_BUILDING", DEFAULT_BUILDING)
    for cand in (want, os.path.join(ROOT, want),
                 os.path.join(ROOT, "data", "buildings", os.path.basename(want))):
        if os.path.isfile(cand):
            return cand
    print(f"WARNING: FIRESIGHT_BUILDING={want!r} not found, using {DEFAULT_BUILDING}. "
          f"Try data/buildings/dcc.json")
    return DEFAULT_BUILDING


world = World(load_building_file(_building_path()))

app = FastAPI(title="Firesight")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _level_index(level):
    if level not in world.grid.level_index:
        raise HTTPException(404, f"Unknown floor level {level}")
    return world.grid.level_index[level]


@app.get("/api/building")
def get_building():
    with lock:
        m = world.model
        return {**m, "grid": world.grid.meta(),
                "warnings": m["warnings"] + world.grid.warnings,
                "exits": [{"level": world.grid.levels[f], "door_id": d,
                           "x": world.grid.center_of(r, c)[0], "y": world.grid.center_of(r, c)[1]}
                          for f, r, c, d in world.grid.exits]}


def _bits(mask):
    """Bool [R, C] -> base64 of np.packbits(row-major, big bit order)."""
    import base64
    return base64.b64encode(np.packbits(mask.astype(np.uint8), axis=None).tobytes()).decode("ascii")


def _display_masks(w, res=0.15):
    """Thin, display-only wall mask at `res` m/px drawn from the real wall lines (routing keeps its 0.5 m grid).
    Cached per World. CAD maps draw each wall face 1 px; centerline maps draw the wall's real thickness."""
    if getattr(w, "_display", None) is not None:
        return w._display
    import cv2
    g = w.grid
    rows, cols = int(g.rows * g.cell / res) + 1, int(g.cols * g.cell / res) + 1
    faces = w.model.get("wall_style") == "faces"
    thick = 1 if faces else max(1, int(round(0.25 / res)))
    def px(x, y):
        return int((x - g.origin_x) / res), int((y - g.origin_y) / res)
    levels = {}
    for fl in w.model["floors"]:
        m = np.zeros((rows, cols), np.uint8)
        for wl in fl["walls"]:
            cv2.line(m, px(wl["x1"], wl["y1"]), px(wl["x2"], wl["y2"]), 1, thick)
        if not faces:   # centerline maps have walls through doorways: cut the openings
            for d in fl["doors"]:
                cv2.circle(m, px(d["x"], d["y"]), int(d["width"] / 2 / res), 0, -1)
        for sp in fl["spaces"]:
            if sp["type"] == "void":
                cv2.fillPoly(m, [np.array([px(*p) for p in sp["polygon"]], np.int32)], 1)
        levels[str(fl["level"])] = _bits(m > 0)
    w._display = {"res": res, "origin_x": g.origin_x, "origin_y": g.origin_y, "rows": rows, "cols": cols,
                  "levels": levels}
    return w._display


@app.get("/api/grid")
def get_grid():
    """Occupancy for the 3D viewer: per level, solid cells (inside the outline but not walkable = walls/voids)
    and walkable cells as packed bit masks, plus the real stair/elevator link point on every floor."""
    with lock:
        g = world.grid
        levels = {}
        for fi, lv in enumerate(g.levels):
            levels[str(lv)] = {"solid": _bits(g.inside[fi] & ~g.walkable[fi]), "walkable": _bits(g.walkable[fi])}
        links = []
        for v in world.model["vertical_connections"]:
            cells = g.vertical_cells.get(v["id"], {})
            pts = {str(lv): list(g.center_of(r, c)) for lv, (fi, r, c) in cells.items()}
            links.append({"id": v["id"], "name": v["name"], "type": v["type"], "points": pts})
        return {**g.meta(), "encoding": "packbits-rowmajor-base64", "levels": levels, "links": links,
                "display": _display_masks(world)}


@app.post("/api/building")
def upload_building(raw: dict = Body(...)):
    """Replace the building (e.g., paste Gemini output). Resets the fire."""
    global world
    try:
        model = load_building(raw)
        new_world = World(model)
    except (ModelError, ValueError) as e:
        raise HTTPException(400, str(e))
    with lock:
        world = new_world
    store.log("building_upload", {"building": model["building"], "floors": model["levels"]})
    live.publish("building", {})
    return {"ok": True, "warnings": model["warnings"] + new_world.grid.warnings}


@app.get("/api/state")
def get_state():
    with lock:
        fire, smoke = world.sim.cells_payload()
        return {
            "t": world.sim.t,
            "mode": world.sim.mode,
            "origins": world.sim.origins,
            "rooms": world.sim.room_status(),
            "verticals": world.sim.vertical_status(),
            "fire": fire,
            "smoke": smoke,
        }


@app.post("/api/fire/start")
def fire_start(room_id: str = Body(None, embed=True), level: int = Body(None, embed=True),
               x: float = Body(None, embed=True), y: float = Body(None, embed=True),
               whole: bool = Body(False, embed=True)):
    with lock:
        if room_id:
            try:
                ok = world.sim.ignite_room(room_id, whole=bool(whole))
            except KeyError:
                raise HTTPException(404, f"Unknown room {room_id}")
        elif level is not None and x is not None and y is not None:
            ok = world.sim.ignite_point(_level_index(level), x, y)
        else:
            raise HTTPException(400, "Send room_id, or level + x + y")
        if not ok:
            raise HTTPException(400, "Could not place fire there")
        world.refresh_routes()
        t = world.sim.t
    store.log("fire_start", {"building": world.model["building"], "t": t, "room_id": room_id,
                             "level": level, "x": x, "y": y})
    live.publish("fire", {"t": world.sim.t, "mode": world.sim.mode})
    return {"ok": True, "t": t}


@app.post("/api/fire/mode")
def fire_mode(mode: str = Body(..., embed=True)):
    """"simulate": fire and smoke spread each step. "reports": nothing spreads; the map shows only what
    firefighters report (and manual ignitions)."""
    if mode not in ("simulate", "reports"):
        raise HTTPException(400, "mode must be simulate or reports")
    with lock:
        world.sim.mode = mode
    store.log("fire_mode", {"building": world.model["building"], "mode": mode})
    live.publish("fire", {"t": world.sim.t, "mode": world.sim.mode})
    return {"ok": True, "mode": mode}


@app.post("/api/fire/step")
def fire_step(n: int = Body(1, embed=True)):
    with lock:
        world.sim.step(max(1, min(int(n), 50)))
        world.refresh_routes()
        t, rooms = world.sim.t, world.sim.room_status()
    store.log("fire_step", {"building": world.model["building"], "t": t,
                            "fire_rooms": sorted(k for k, v in rooms.items() if v == "fire"),
                            "smoke_rooms": sorted(k for k, v in rooms.items() if v == "smoke")})
    live.publish("fire", {"t": world.sim.t, "mode": world.sim.mode})
    return {"ok": True, "t": t}


@app.post("/api/fire/reset")
def fire_reset():
    with lock:
        world.sim.reset()
        from .services import reports as _reports
        _reports.reset(world)          # field reports, searched rooms, blocked cells, undo log
        world.refresh_routes()
    store.log("fire_reset", {"building": world.model["building"]})
    live.publish("fire", {"t": world.sim.t, "mode": world.sim.mode})
    return {"ok": True}


@app.get("/api/route")
def route(level: int = Query(...), x: float = Query(...), y: float = Query(...)):
    with lock:
        return world.field.route_from(_level_index(level), x, y)


@app.get("/api/route_to")
def route_to(level: int = Query(...), x: float = Query(...), y: float = Query(...),
             to_level: int = Query(None), to_x: float = Query(None), to_y: float = Query(None),
             to_room: str = Query(None)):
    """Safest path from a point to another point or room anywhere in the building."""
    with lock:
        g = world.grid
        if to_room:
            matches = [r for r in g.rooms if r["label"].split(" ")[0].lower() == to_room.lower()]
            matches.sort(key=lambda r: "lower tier" in r["label"])      # the room itself before its lower tier
            info = g.room_by_id.get(to_room) or (matches[0] if matches else None)
            if info is None:
                raise HTTPException(404, f"Unknown room {to_room}")
            gfi, cell = best_room_cell(world.field, info)
            if cell is None:
                raise HTTPException(400, f"Room {to_room} has no walkable floor")
            gx, gy = g.center_of(*cell)
        elif None not in (to_level, to_x, to_y):
            gfi, gx, gy = _level_index(to_level), to_x, to_y
        else:
            raise HTTPException(400, "Send to_room, or to_level + to_x + to_y")
        return world.field.route_between(_level_index(level), x, y, gfi, gx, gy)


@app.get("/api/routes/all")
def routes_all():
    """Evacuation route from the center of every room (skips stairs/elevators)."""
    with lock:
        g = world.grid
        out = []
        for info in g.rooms:
            if info["type"] in ("stairwell", "elevator", "hallway"):
                continue
            fi, cell = best_room_cell(world.field, info)     # any reachable part of the room counts
            if cell is None:
                continue
            r = world.field.route_from(fi, *g.center_of(*cell))
            out.append({"room_id": info["id"], "label": info["label"], "level": info["level"],
                        "reachable": r["reachable"], "distance_m": r["distance_m"], "path": r["path"]})
        return out


def _savable(model):
    """Normalized model -> JSON the loader accepts again (alignment is already applied)."""
    out = {k: v for k, v in model.items() if k not in ("levels", "warnings")}
    out["floors"] = [{**fl, "alignment": {"offset_x": 0, "offset_y": 0, "rotation_deg": 0}}
                     for fl in model["floors"]]
    out["vertical_connections"] = [
        {**v, "positions": {str(k): p for k, p in (v.get("positions") or {}).items()}}
        for v in model["vertical_connections"]]
    return out


@app.post("/api/building/save")
def save_building(name: str = Body(..., embed=True)):
    """Write the current building to data/buildings/<name>.json (used by the map editor)."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or name.startswith("."):
        raise HTTPException(400, "name: letters, digits, _ . - only")
    path = os.path.join(ROOT, "data", "buildings", name if name.endswith(".json") else name + ".json")
    with lock:
        data = _savable(world.model)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    store.save_building(os.path.splitext(os.path.basename(path))[0], data)   # Atlas version history (no-op if off)
    store.log("building_save", {"building": data.get("building"), "name": name})
    return {"ok": True, "path": os.path.relpath(path, ROOT)}


@app.get("/api/check")
def check():
    """Map quality issues (see backend/checks.py). Can take a few seconds on big maps."""
    from .core.checks import check_building
    with lock:
        return {"issues": check_building(world.model, world.grid)}


# Feature routers (world is replaced on upload, so pass a getter).
from .services.occupants import make_router as _occupants_router  # noqa: E402
from .services.voice import router as _voice_router  # noqa: E402

app.include_router(_occupants_router(lambda: world, lock))
app.include_router(_voice_router)

from .services.localize import make_router as _loc_router  # noqa: E402

app.include_router(_loc_router(lambda: world, lock))
app.include_router(store.router)

from .services.cortex import make_router as _cortex_router  # noqa: E402

app.include_router(_cortex_router(lambda: world, lock))

from .services.reports import make_router as _reports_router  # noqa: E402

app.include_router(_reports_router(lambda: world, lock))

from .services.ai_edit import router as _ai_router  # noqa: E402

app.include_router(_ai_router)
app.include_router(live.router)

from .services.vps import make_router as _vps_router  # noqa: E402  (phone app /ws/ar + camera positioning)

app.include_router(_vps_router(lambda: world, lock))

@app.middleware("http")
async def _no_stale_pages(request, call_next):
    """Pages and their scripts revalidate every load, so edits show up (also inside iframes)."""
    resp = await call_next(request)
    p = request.url.path
    if p.endswith((".html", ".js", "/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# Static viewer last so /api/* wins.
if os.path.isdir(VIEWER_DIR):
    app.mount("/", StaticFiles(directory=VIEWER_DIR, html=True), name="viewer")
