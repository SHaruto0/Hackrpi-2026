# Firesight handoff (paste or upload this whole file to Claude Code)
**Instructions for Claude Code:** Recreate every file in the "Source files" section at the exact path shown, then run `pip install -r requirements.txt`, `python data/make_sample.py`, `python tests/test_core.py`, and `uvicorn backend.server:app --reload --port 8000`. Save the project context section as `CLAUDE.md`. Then continue with the Remaining tasks list.

---

# Firesight: project context for Claude Code

## TL;DR
HackRPI 2026 project (Nov 7 to 8, 2026, DCC at RPI, Troy NY). Coding ends **11:00 AM Sunday**; Devpost submit by **9:00 AM Sunday**, edits until 11:00 AM.

**Firesight** turns building floor plan images into a structured, multi-floor 3D map, simulates a fire, and gives every person a live escape route that reroutes as fire and smoke spread. Next: occupants point their phone camera and see the escape path drawn over the live view with an arrow, mini-map and voice, rerouting as the fire spreads. Then firefighter/commander views.

**Pitch line:** "Wayfinding apps assume the building is safe. Firesight assumes it isn't."

## Team constraints (important)
- Laptops only. No iPhone Pro (no LiDAR), no extra hardware.
- Map source = DCC architectural floor plan images (3 floors, NOT evacuation maps) → Gemini → JSON.
- Fire data = mock simulation (cellular automaton). Real sensors are "future work".
- Laptop first, cloud later (Vultr).
- Build with Cursor/Claude Code; the SpaceXSI raffle (mechanical keyboard) needs Cursor + Grok Bot usage, so mention it on Devpost.

## What exists now (working, tested)
All paths are relative to the repo root.
```
backend/
  model.py              load + validate Gemini JSON, normalize, align floors, warnings
  grid.py               rasterize to 3D grid (floor,row,col) at 0.5 m; walls, doors, rooms, exits, stair links
  fire.py               mock fire + smoke sim (spreads through doors, smoke rises via stairs)
  routing.py            multi-source Dijkstra from all exits (scipy) -> route field; instructions text
  server.py             FastAPI API + serves the 3D viewer
  extract_floorplans.py images -> Gemini -> validated JSON file
  gemini_prompt.txt     the extraction prompt (tokens {BUILDING} {N} {ORDER})
viewer/index.html       Three.js 3D viewer (stacked floors, orbit, fire/smoke tiles, routes)
data/make_sample.py     generates data/sample_building.json (synthetic 3-floor test building)
tests/test_core.py      4 tests (routing, reroute on fire, validation, API)
requirements.txt
```

### Run
```bash
pip install -r requirements.txt
python data/make_sample.py                 # synthetic building for dev
uvicorn backend.server:app --reload --port 8000
# open http://localhost:8000
python tests/test_core.py                  # or: python -m pytest -q
```
Real building:
```bash
export GEMINI_API_KEY=...
python -m backend.extract_floorplans dcc_f1.png dcc_f2.png dcc_f3.png -o data/dcc.json
FIRESIGHT_BUILDING=data/dcc.json uvicorn backend.server:app --port 8000
```
Or POST the JSON to `/api/building` to hot-swap without restarting.

### Viewer controls
Pick a room → Start fire → Step / Play. Click any room to get a route from there (reroutes live every step). "All routes" draws every room's evacuation path. Floor filter, floor spacing slider, labels toggle. Drag rotate, scroll zoom, right-drag pan.

## Data model (Gemini output = input to the loader)
Shared frame for all floors: meters, origin top-left of the lowest floor footprint, x right, y down. Key fields:
- `floors[].level`, `alignment {offset_x, offset_y, rotation_deg}` (applied by the loader: rotate about origin, then offset)
- `floors[].outline`, `walls[] {x1,y1,x2,y2,exterior}`, `spaces[] {id,label,type,polygon,multi_floor}`, `doors[] {x,y,width,connects,exterior_exit}`
- `vertical_connections[] {id,name,type: stairs|elevator|ramp, x,y,floors}`
- every element has `confidence`
Loader behavior: drops invalid elements with warnings, renames duplicate ids, clamps door width 0.6 to 3.0 m, unknown space types become `unknown`, accepts JSON wrapped in ``` fences. Warns if no exterior exits or no stairs on multi-floor buildings.

## Key algorithms / constants
- Grid: `CELL=0.5` m, wall half-thickness 0.3 m, doors carve openings (radius = width/2 + 0.25). Cells outside the floor outline are blocked.
- Stairs: link nearest walkable cell to (x,y) on consecutive served floors; cost `STAIR_COST_M=8` per floor. **Elevators excluded from evacuation routes.**
- Routing: 8-connected, no corner cutting, fire cells impassable, smoke multiplies cost by `1 + 12*smoke`. One Dijkstra from all exits gives routes for everyone; recomputed after every fire change (~10 ms on the sample).
- Fire sim: fire spreads to 4-neighbors with p=0.30/step (seeded, deterministic); smoke diffuses (decay 0.92/cell, 2 cells/step), rises up stairs (x0.97) and weakly down (x0.35). Replace `FireSim.step` with real sensor input later by writing to `sim.fire` / `sim.smoke`.
- Room status: `fire` if any fire cell, `smoke` if max smoke > 0.35.

## API
| Method | Path | Body / params | Returns |
|---|---|---|---|
| GET | `/api/building` | | normalized model + `grid` meta + `exits` + `warnings` |
| POST | `/api/building` | raw building JSON | replaces building, resets fire |
| GET | `/api/state` | | `t`, `rooms` status, `verticals` status, `fire`/`smoke` cells per level |
| POST | `/api/fire/start` | `{room_id}` or `{level,x,y}` | |
| POST | `/api/fire/step` | `{n}` (1 to 50) | |
| POST | `/api/fire/reset` | | |
| GET | `/api/route` | `level,x,y` | `reachable, path [[level,x,y]...], distance_m, exit, max_smoke, instructions[]` |
| GET | `/api/routes/all` | | route from every room |

Grid cell (r,c) center = `(origin_x + (c+.5)*cell, origin_y + (r+.5)*cell)`. JSON keys for per-level dicts are strings ("1").

## Viewer notes (viewer/index.html)
Three.js 0.160 from jsdelivr via importmap. Plan (x,y) → world (x, levelY, y); floors stacked on world Y with `gap` spacing. Room slabs = ShapeGeometry (y flipped, rotateX -90°). Walls are BoxGeometry split around door positions. Fire/smoke = InstancedMesh tiles per floor. Route = TubeGeometry over a CurvePath of LineCurve3. Click picking uses raycast on room slabs/floor plates.

## Camera navigation (occupant phone app) - core feature to build next
A person opens Firesight on their phone, points the camera ahead, and sees the escape path drawn over the live camera view, plus voice directions. It reroutes live as the fire spreads.

### User flow
1. Scan the QR code posted in the room (or tap "I'm in room ___"). QR gives exact position AND facing direction.
2. Camera view opens. Overlay shows: big arrow toward the next waypoint, distance to the next turn, banner ("Head to WEST STAIRS, east stairs blocked"), and a mini-map in the corner with their dot + route (2D floor, same data as the 3D viewer).
3. As they walk, position updates from the camera (see localization), the arrow turns, waypoints tick off.
4. Hazard warnings: "Turn around" if facing toward fire; "Smoke ahead, stay low" if route passes smoke.
5. Fire spreads → server pushes new route → arrow and path change immediately + voice "East stairs blocked, use west stairs."
6. At stairs: "Take West Stairs down to floor 1", floor auto-switches after stair detection or tap.
7. Outside: "You're out. Go to the assembly point." + "I'm safe" button. Also an "I'm trapped" button that sends last known room.

### Showing the path on the camera
- **v1 (works on every phone, build first):** 2D overlay on the camera feed. Arrow rotation = bearing to next waypoint minus phone heading. Heading from `deviceorientationabsolute` (Android) / `webkitCompassHeading` (iOS, needs permission tap). Map coordinates are in the building frame, so add the building bearing (from OpenStreetMap, measured once) to convert to compass degrees. Draw a "ribbon" of chevrons in perspective at the bottom of the screen pointing the arrow direction so it looks like a path on the floor.
- **v2 (Android Chrome, stretch):** WebXR `immersive-ar` with `local-floor` + hit-test to place real 3D path tiles/arrows on the floor, anchored at the QR start pose. iOS Safari WebXR support is limited, so iPhones stay on v1.

### Localization (no LiDAR, no GPS)
GPS doesn't work indoors. Combine, in order of trust:
1. **QR / AprilTag anchors** (room doors, stairwell doors, hallway corners): exact pose. Each QR encodes `{building, level, x, y, heading}`.
2. **Door numbers seen by the camera:** every ~2 s send a frame to `POST /api/localize`; Gemini (or OCR) reads room numbers like "204" and snaps to that door.
3. **Image retrieval:** during setup, film each hallway, extract a frame every 0.5 s, label frames with map positions. Embed with DINOv2 (small), search with FAISS. Return top matches + confidence. Runs on laptop CPU.
4. **Motion between fixes:** WebXR tracking on Android (v2), otherwise step counting from the accelerometer + compass heading (dead reckoning).
5. **Map matching:** snap the estimate to walkable cells and only allow moves to neighboring cells (simple particle filter or nearest walkable cell on the route).
6. **Confidence rule:** if unsure, never guess. Show "Point your camera at a door number or scan the nearest QR." ("No position beats a wrong one", lesson from marko.)

### Backend additions needed
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/localize` | body: jpeg frame + last known `{level,x,y}`; returns `{level,x,y,confidence,source}` (qr/door_number/retrieval) |
| GET | `/api/qr/{code}` | resolve a QR id to `{level,x,y,heading}` |
| GET | `/api/route` | already exists; phone calls it on every position update |
| WS | `/ws/routes` | push "hazards changed, refetch route" to phones (polling every 1 to 2 s is fine for v1) |
| POST | `/api/people/{id}/status` | safe / trapped / evacuating + last position (commander view) |
| GET | `/api/qr/print` | printable sheet of QR codes per room/door for the demo |

Also add `next_waypoint` and `turn` hints to the route response: distance to next turn, turn direction (left/right/straight/stairs down/up), so the phone doesn't recompute.

### Phone app files to create
```
viewer/phone.html     camera view (getUserMedia, facingMode: "environment"), arrow + chevron ribbon overlay (canvas),
                      mini-map (2D canvas of the current floor + route), banner, voice, I'm safe / trapped buttons
backend/localize.py   QR lookup, door-number reading (Gemini), DINOv2 + FAISS retrieval, confidence
data/frames/          labeled reference frames (setup step)
scripts/build_index.py  extract frames from walkthrough videos, embed, save FAISS index + labels
```
Phones need HTTPS for camera + compass: run behind an HTTPS tunnel (e.g. cloudflared or ngrok) or deploy on Vultr with a cert.

### Demo for camera nav
Judge scans the QR taped to the demo table ("Room 204, floor 2"). Their phone shows the camera with an arrow and path toward the west stairs. We start a fire near the east stairs on the laptop; the judge's arrow swings and the voice says the stairs are blocked. Point the camera at a door number: position snaps and the panel shows "located by door number 205".

## Remaining tasks (priority order)
**Tier 1 (do first)**
1. Run extraction on the 3 real DCC floor plan images; inspect `data/dcc.raw.json` and warnings; fix by hand. Gemini coordinates will be rough. Check: stairwells line up across floors, every space has a door, exterior exits marked.
2. Map editor (React or plain canvas) to fix walls/doors/labels/exits and set scale (click a door = 0.9 m). Save back to JSON. Hand-drawn fallback map by hour 4 so routing work is never blocked.
3. Optional CV assist for geometry (OpenCV: perspective warp, adaptive threshold walls, connected components rooms), with Gemini only for labels/semantics. Gemini is good at meaning, weak at precise coordinates.
**Tier 2**
4. **Camera navigation v1** (see "Camera navigation" section): `phone.html` with camera + arrow/chevron path overlay + mini-map, QR anchors, compass heading, route polling, turn hints in the route API.
5. Voice: ElevenLabs TTS. Occupants: calm, simple ("Exit the room, turn left"). Firefighters: terse radio style ("Room 204. West stairs, floor 2, third door left"). Cache common phrases.
6. Camera localization: `/api/localize` with door-number reading (Gemini) first, then DINOv2 + FAISS retrieval; confidence check with QR fallback.
7. Stretch: camera navigation v2 with WebXR AR path on the floor (Android Chrome) + continuous tracking, map-matched with a particle filter.
**Tier 3 (pick 1 to 2)**
8. Firefighter mode: route *to* a room, mark rooms searched, commander dashboard (who's safe/trapped from self check-ins).
9. Snowflake Cortex: building knowledge RAG ("where's the gas shutoff?") + commander briefings.
10. Evacuation simulation: many agents + bottleneck heatmap (ML/Data Science track).
11. Persistence: MongoDB Atlas (buildings, rooms, people), Tiger Data (fire/smoke/sensor time series, positions). Deploy on Vultr.

**Cut order if behind:** 7 → 10 → 9 → 6 (fall back to QR/tap) → 8. Never cut 1, 2, routing, fire sim, 3D viewer.

## Checkpoints
| Hour | Must be true |
|---|---|
| 4 | Real (or hand-fixed) DCC map loads; route on screen |
| 10 | Fire spreads, routes reroute live in 3D |
| 16 | Phone page opens via QR, camera view shows arrow/path + mini-map + voice, reroutes live |
| 20 | Feature freeze, polish + demo only |

## Demo script (2 min)
1. "Here are DCC's floor plans." Gemini turns them into this 3D building (rotate it).
2. Judge scans a QR → their phone shows the way out from their "room".
3. Start a fire near the east stairs → smoke rises floor by floor → every route flips to the west stairs live.
4. Voice reads directions. Elevator is labeled not for evacuation.
5. Close: limits + future (real sensors, alarm panel integration, firefighter mode).

## Prize tracks to target
Grand ($1,000 / $750), In the Clouds (Sony headphones), ML/Data Science ($400), Gemini API, ElevenLabs (earbuds), MongoDB Atlas (M5Stack IoT kit), Tiger Data (Stream Deck Mini), Vultr, Snowflake API, SpaceXSI raffle (keyboard), Ethical AI ($300, if we add explainable routing + privacy). Check the per-team track limit on Devpost. Judging: practicality, creativity, technical difficulty, effort.

## Prior art to differentiate from
**marko** (BigRed//Hacks 2026 winner): AR indoor wayfinding from floor plans + LiDAR scan, used rented GPUs for visual localization (SuperPoint/LightGlue/NetVLAD), Dijkstra on 0.5 m grid, compass + OSM building bearing. Their lessons: lighting changes break camera localization (54% matched at night); "no position beats a wrong one"; fuse signals. Our difference: dynamic hazards (fire/smoke change the map live), multi-floor smoke physics, evacuation-first, works without LiDAR or GPUs.

## Honest limits (say these to judges)
- Fire data is simulated; production plugs into alarm panels/IoT sensors.
- Gemini maps need human review (minutes per floor, done once before any emergency).
- Cameras fail in heavy smoke; production would fuse IMU, Wi-Fi/BLE/UWB.

## Conventions
- Python 3.11+, FastAPI, numpy/scipy. Keep backend pure and testable; add tests to `tests/test_core.py` for new logic.
- Viewer is a single static HTML file; keep it dependency-free beyond the Three.js CDN.
- Never commit API keys; use env vars (`GEMINI_API_KEY`, `GEMINI_MODEL`, `FIRESIGHT_BUILDING`).
- Writing style for UI copy and docs: short, plain, no em dashes.


---

# Source files

## `requirements.txt`

```text
fastapi>=0.110
uvicorn[standard]>=0.29
numpy>=1.26
scipy>=1.11
google-genai>=1.0
httpx>=0.27
```

## `backend/__init__.py`

```python
"""Firesight backend package."""
```

## `backend/model.py`

````python
"""Load and validate the building JSON (Gemini output or hand-edited).

Normalizes everything into one shared coordinate frame (meters), applies each
floor's alignment, fixes duplicate ids, and collects warnings instead of
crashing on imperfect AI output.
"""
import json
import math
import re

SPACE_TYPES = {
    "classroom", "office", "hallway", "lobby", "restroom", "stairwell", "elevator",
    "mechanical", "lecture_hall", "storage", "unknown",
}
VERTICAL_TYPES = {"stairs", "elevator", "ramp"}
MIN_WALL_LEN = 0.05


class ModelError(Exception):
    pass


def _num(value, default=None):
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _point(p):
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        x, y = _num(p[0]), _num(p[1])
        if x is not None and y is not None:
            return [x, y]
    return None


def _transform(x, y, align):
    """Rotate about the origin, then offset (Floor 1 frame)."""
    a = math.radians(align["rotation_deg"])
    xr = x * math.cos(a) - y * math.sin(a)
    yr = x * math.sin(a) + y * math.cos(a)
    return [xr + align["offset_x"], yr + align["offset_y"]]


def parse_json_text(text):
    """Accept raw JSON or JSON wrapped in ```json fences."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelError(f"Invalid JSON: {e}") from e


def load_building(raw):
    """raw: dict (parsed JSON). Returns a normalized dict with 'warnings'."""
    if not isinstance(raw, dict):
        raise ModelError("Top-level JSON must be an object")
    warnings = []
    used_ids = set()

    def unique_id(proposed, fallback):
        base = str(proposed) if proposed not in (None, "") else fallback
        uid, n = base, 2
        while uid in used_ids:
            uid = f"{base}-{n}"
            n += 1
        if uid != base:
            warnings.append(f"Duplicate id '{base}' renamed to '{uid}'")
        used_ids.add(uid)
        return uid

    floors_in = raw.get("floors")
    if not isinstance(floors_in, list) or not floors_in:
        raise ModelError("'floors' must be a non-empty list")

    floors = []
    seen_levels = set()
    for fi, f in enumerate(floors_in):
        level = f.get("level")
        try:
            level = int(level)
        except (TypeError, ValueError):
            raise ModelError(f"Floor #{fi} has invalid 'level': {level!r}")
        if level in seen_levels:
            raise ModelError(f"Duplicate floor level {level}")
        seen_levels.add(level)

        al = f.get("alignment") or {}
        align = {
            "offset_x": _num(al.get("offset_x"), 0.0),
            "offset_y": _num(al.get("offset_y"), 0.0),
            "rotation_deg": _num(al.get("rotation_deg"), 0.0),
        }

        def xf(p, align=align):
            return _transform(p[0], p[1], align)

        outline = [xf(p) for p in (_point(q) for q in f.get("outline") or []) if p]
        if outline and len(outline) < 3:
            warnings.append(f"Floor {level}: outline has < 3 points, ignored")
            outline = []

        walls = []
        for i, w in enumerate(f.get("walls") or []):
            x1, y1, x2, y2 = (_num(w.get(k)) for k in ("x1", "y1", "x2", "y2"))
            if None in (x1, y1, x2, y2):
                warnings.append(f"Floor {level}: wall #{i} missing coordinates, dropped")
                continue
            a, b = xf([x1, y1]), xf([x2, y2])
            if math.dist(a, b) < MIN_WALL_LEN:
                continue
            walls.append({
                "id": unique_id(w.get("id"), f"F{level}-W{i}"),
                "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1],
                "exterior": bool(w.get("exterior", False)),
                "confidence": _num(w.get("confidence"), 1.0),
            })

        spaces = []
        for i, s in enumerate(f.get("spaces") or []):
            poly = [xf(p) for p in (_point(q) for q in s.get("polygon") or []) if p]
            if len(poly) < 3:
                warnings.append(f"Floor {level}: space {s.get('id', i)} has < 3 points, dropped")
                continue
            stype = str(s.get("type", "unknown")).lower()
            if stype not in SPACE_TYPES:
                warnings.append(f"Floor {level}: unknown space type '{stype}', using 'unknown'")
                stype = "unknown"
            sid = unique_id(s.get("id"), f"F{level}-S{i}")
            spaces.append({
                "id": sid,
                "label": str(s.get("label") or sid),
                "type": stype,
                "polygon": poly,
                "multi_floor": bool(s.get("multi_floor", False)),
                "confidence": _num(s.get("confidence"), 1.0),
            })

        doors = []
        for i, d in enumerate(f.get("doors") or []):
            x, y = _num(d.get("x")), _num(d.get("y"))
            if x is None or y is None:
                warnings.append(f"Floor {level}: door #{i} missing coordinates, dropped")
                continue
            p = xf([x, y])
            width = min(max(_num(d.get("width"), 0.9), 0.6), 3.0)
            doors.append({
                "id": unique_id(d.get("id"), f"F{level}-D{i}"),
                "x": p[0], "y": p[1], "width": width,
                "connects": [str(c) for c in (d.get("connects") or [])],
                "exterior_exit": bool(d.get("exterior_exit", False)),
                "confidence": _num(d.get("confidence"), 1.0),
            })

        if not walls and not spaces:
            warnings.append(f"Floor {level}: no walls or spaces found")

        floors.append({
            "level": level, "alignment": align, "outline": outline,
            "walls": walls, "spaces": spaces, "doors": doors,
            "notes": str(f.get("notes") or ""),
        })

    floors.sort(key=lambda fl: fl["level"])
    levels = [fl["level"] for fl in floors]

    verticals = []
    for i, v in enumerate(raw.get("vertical_connections") or []):
        x, y = _num(v.get("x")), _num(v.get("y"))
        vtype = str(v.get("type", "stairs")).lower()
        if vtype not in VERTICAL_TYPES:
            vtype = "stairs"
        vfloors = []
        for lv in v.get("floors") or []:
            try:
                lv = int(lv)
            except (TypeError, ValueError):
                continue
            if lv in seen_levels and lv not in vfloors:
                vfloors.append(lv)
        vfloors.sort()
        if x is None or y is None or len(vfloors) < 2:
            warnings.append(f"Vertical connection {v.get('id', i)} invalid (needs x, y, 2+ known floors), dropped")
            continue
        vid = unique_id(v.get("id"), f"V{i}")
        verticals.append({
            "id": vid, "name": str(v.get("name") or vid), "type": vtype,
            "x": x, "y": y, "floors": vfloors,
            "confidence": _num(v.get("confidence"), 1.0),
        })

    if len(floors) > 1 and not any(v["type"] in ("stairs", "ramp") for v in verticals):
        warnings.append("Multiple floors but no stairs/ramps: upper floors cannot reach exits")
    if not any(d["exterior_exit"] for fl in floors for d in fl["doors"]):
        warnings.append("No exterior exits found: routing will fail until you mark exterior_exit doors")

    low = sum(1 for fl in floors for k in ("walls", "spaces", "doors")
              for e in fl[k] if e["confidence"] < 0.5)
    if low:
        warnings.append(f"{low} elements have confidence < 0.5, review them")

    return {
        "building": str(raw.get("building") or "Building"),
        "units": "meters",
        "scale_method": str(raw.get("scale_method") or ""),
        "floor_height_m": _num(raw.get("floor_height_m"), 4.0),
        "levels": levels,
        "floors": floors,
        "vertical_connections": verticals,
        "multi_floor_spaces": raw.get("multi_floor_spaces") or [],
        "global_notes": str(raw.get("global_notes") or ""),
        "warnings": warnings,
    }


def load_building_file(path):
    with open(path, encoding="utf-8") as fh:
        return load_building(parse_json_text(fh.read()))
````

## `backend/grid.py`

```python
"""Rasterize the building into a 3D occupancy grid (floor, row, col).

All floors share one frame, so a stairwell at (x, y) lands on the same cell
on every floor. Cell (r, c) center = (origin_x + (c + .5) * cell,
origin_y + (r + .5) * cell).
"""
import math
import numpy as np

CELL = 0.5            # meters per cell
WALL_HALF = 0.3       # wall half-thickness for rasterizing (m)
DOOR_PAD = 0.25       # extra clearance around a door opening (m)
STAIR_COST_M = 8.0    # meters-equivalent cost per floor of stairs


def point_in_poly(X, Y, poly):
    inside = np.zeros(X.shape, dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        denom = (yj - yi) if yj != yi else 1e-12
        cond = ((yi > Y) != (yj > Y)) & (X < (xj - xi) * (Y - yi) / denom + xi)
        inside ^= cond
        j = i
    return inside


def seg_dist(X, Y, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return np.hypot(X - x1, Y - y1)
    t = np.clip(((X - x1) * dx + (Y - y1) * dy) / L2, 0.0, 1.0)
    return np.hypot(X - (x1 + t * dx), Y - (y1 + t * dy))


class BuildingGrid:
    def __init__(self, model, cell=CELL, pad=1.0):
        self.model = model
        self.cell = cell
        self.levels = list(model["levels"])
        self.level_index = {lv: i for i, lv in enumerate(self.levels)}
        self.warnings = []

        xs, ys = [], []
        for fl in model["floors"]:
            for p in fl["outline"]:
                xs.append(p[0]); ys.append(p[1])
            for w in fl["walls"]:
                xs += [w["x1"], w["x2"]]; ys += [w["y1"], w["y2"]]
            for s in fl["spaces"]:
                for p in s["polygon"]:
                    xs.append(p[0]); ys.append(p[1])
            for d in fl["doors"]:
                xs.append(d["x"]); ys.append(d["y"])
        for v in model["vertical_connections"]:
            xs.append(v["x"]); ys.append(v["y"])
        if not xs:
            raise ValueError("Building has no geometry")

        self.origin_x = min(xs) - pad
        self.origin_y = min(ys) - pad
        self.cols = int(math.ceil((max(xs) - min(xs) + 2 * pad) / cell))
        self.rows = int(math.ceil((max(ys) - min(ys) + 2 * pad) / cell))
        F = len(self.levels)
        X = self.origin_x + (np.arange(self.cols) + 0.5) * cell
        Y = self.origin_y + (np.arange(self.rows) + 0.5) * cell
        self.X, self.Y = np.meshgrid(X, Y)

        self.walkable = np.zeros((F, self.rows, self.cols), dtype=bool)
        self.room = np.full((F, self.rows, self.cols), -1, dtype=np.int32)
        self.rooms = []        # list of space dicts (+ 'level', 'index')
        self.room_by_id = {}

        for fl in model["floors"]:
            fi = self.level_index[fl["level"]]
            if fl["outline"]:
                inside = point_in_poly(self.X, self.Y, fl["outline"])
            else:
                inside = np.zeros_like(self.X, dtype=bool)
                for s in fl["spaces"]:
                    inside |= point_in_poly(self.X, self.Y, s["polygon"])
                if not inside.any():
                    inside[:] = True
            wall = np.zeros_like(inside)
            for w in fl["walls"]:
                wall |= seg_dist(self.X, self.Y, w["x1"], w["y1"], w["x2"], w["y2"]) <= WALL_HALF
            walk = inside & ~wall
            for d in fl["doors"]:
                r = d["width"] / 2 + DOOR_PAD
                walk |= (np.hypot(self.X - d["x"], self.Y - d["y"]) <= r) & inside
            self.walkable[fi] = walk

            for s in fl["spaces"]:
                mask = point_in_poly(self.X, self.Y, s["polygon"]) & inside
                idx = len(self.rooms)
                self.room[fi][mask] = idx
                info = dict(s, level=fl["level"], index=idx)
                info["centroid"] = self._centroid(s["polygon"])
                self.rooms.append(info)
                self.room_by_id[s["id"]] = info

        # exits
        self.exits = []   # list of (fi, r, c, door_id)
        for fl in model["floors"]:
            fi = self.level_index[fl["level"]]
            for d in fl["doors"]:
                if d["exterior_exit"]:
                    cell_ = self.nearest_walkable(fi, d["x"], d["y"], max_dist=2.0)
                    if cell_ is None:
                        self.warnings.append(f"Exit {d['id']} not reachable from any walkable cell")
                    else:
                        self.exits.append((fi, cell_[0], cell_[1], d["id"]))

        # vertical links: list of (cellA, cellB, cost_m, vc)
        self.links = []
        self.vertical_cells = {}   # vc id -> {level: (fi, r, c)}
        for v in model["vertical_connections"]:
            cells = {}
            for lv in v["floors"]:
                fi = self.level_index[lv]
                c = self.nearest_walkable(fi, v["x"], v["y"], max_dist=3.0)
                if c is None:
                    self.warnings.append(f"{v['id']} has no walkable cell near it on floor {lv}")
                else:
                    cells[lv] = (fi, c[0], c[1])
            self.vertical_cells[v["id"]] = cells
            lvs = sorted(cells)
            for a, b in zip(lvs, lvs[1:]):
                span = self.level_index[b] - self.level_index[a]
                self.links.append((cells[a], cells[b], STAIR_COST_M * span, v))

    @staticmethod
    def _centroid(poly):
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        return [sum(xs) / len(xs), sum(ys) / len(ys)]

    # --- coordinate helpers -------------------------------------------------
    def cell_of(self, x, y):
        c = int((x - self.origin_x) // self.cell)
        r = int((y - self.origin_y) // self.cell)
        return r, c

    def center_of(self, r, c):
        return (self.origin_x + (c + 0.5) * self.cell,
                self.origin_y + (r + 0.5) * self.cell)

    def nearest_walkable(self, fi, x, y, max_dist=3.0, mask=None):
        walk = self.walkable[fi] if mask is None else (self.walkable[fi] & mask)
        r, c = self.cell_of(x, y)
        if 0 <= r < self.rows and 0 <= c < self.cols and walk[r, c]:
            return (r, c)
        d = np.hypot(self.X - x, self.Y - y)
        d = np.where(walk, d, np.inf)
        k = int(np.argmin(d))
        if not math.isfinite(d.flat[k]) or d.flat[k] > max_dist:
            return None
        return divmod(k, self.cols)

    def flat(self, fi, r, c):
        return (fi * self.rows + r) * self.cols + c

    def unflat(self, idx):
        fi, rem = divmod(int(idx), self.rows * self.cols)
        r, c = divmod(rem, self.cols)
        return fi, r, c

    def meta(self):
        return {
            "origin_x": self.origin_x, "origin_y": self.origin_y,
            "cell": self.cell, "rows": self.rows, "cols": self.cols,
            "levels": self.levels,
        }
```

## `backend/fire.py`

```python
"""Mock fire + smoke simulation on the building grid (cellular automaton).

- Fire spreads to walkable 4-neighbors with probability P_SPREAD per step
  (walls block it, open doors let it through).
- Smoke is 0..1, sourced from fire, diffuses with decay, and RISES through
  stairs/ramps/elevators (strongly up, weakly down).
Swap `step()` for real sensor data later: just write into self.fire / self.smoke.
"""
import numpy as np

P_SPREAD = 0.30
SMOKE_DECAY = 0.92       # per cell of travel
SMOKE_ITERS = 2          # smoke travels ~2 cells per step
SMOKE_UP = 0.97          # transfer factor up a stairwell
SMOKE_DOWN = 0.35        # transfer factor down a stairwell


def _neighbor_max(a):
    """Max of 4-neighbors within each floor. a: (F, R, C)."""
    out = np.zeros_like(a)
    out[:, 1:, :] = np.maximum(out[:, 1:, :], a[:, :-1, :])
    out[:, :-1, :] = np.maximum(out[:, :-1, :], a[:, 1:, :])
    out[:, :, 1:] = np.maximum(out[:, :, 1:], a[:, :, :-1])
    out[:, :, :-1] = np.maximum(out[:, :, :-1], a[:, :, 1:])
    return out


class FireSim:
    def __init__(self, grid, seed=7):
        self.grid = grid
        self.seed = seed
        self.reset()

    def reset(self):
        g = self.grid
        shape = g.walkable.shape
        self.fire = np.zeros(shape, dtype=bool)
        self.smoke = np.zeros(shape, dtype=np.float32)
        self.t = 0
        self.rng = np.random.default_rng(self.seed)
        self.origins = []

    def ignite_point(self, fi, x, y, radius=1.0):
        g = self.grid
        cell = g.nearest_walkable(fi, x, y, max_dist=5.0)
        if cell is None:
            return False
        cx, cy = g.center_of(*cell)
        patch = (np.hypot(g.X - cx, g.Y - cy) <= radius) & g.walkable[fi]
        self.fire[fi] |= patch
        self.smoke[fi][patch] = 1.0
        self.origins.append({"level": g.levels[fi], "x": cx, "y": cy})
        return True

    def ignite_room(self, room_id):
        g = self.grid
        info = g.room_by_id.get(room_id)
        if info is None:
            raise KeyError(room_id)
        fi = g.level_index[info["level"]]
        in_room = g.room[fi] == info["index"]
        cell = g.nearest_walkable(fi, *info["centroid"], max_dist=50.0, mask=in_room)
        if cell is None:
            return False
        return self.ignite_point(fi, *g.center_of(*cell))

    def step(self, n=1):
        g = self.grid
        walk = g.walkable
        for _ in range(n):
            nb = _neighbor_max(self.fire.astype(np.uint8)).astype(bool)
            cand = nb & walk & ~self.fire
            self.fire |= cand & (self.rng.random(cand.shape) < P_SPREAD)

            s = np.maximum(self.smoke, self.fire.astype(np.float32))
            for _ in range(SMOKE_ITERS):
                s = np.maximum(s, SMOKE_DECAY * _neighbor_max(s))
                s *= walk
                for (fa, ra, ca), (fb, rb, cb), _cost, _v in g.links:
                    lo, hi = s[fa, ra, ca], s[fb, rb, cb]   # a is the lower floor
                    s[fb, rb, cb] = max(hi, lo * SMOKE_UP)
                    s[fa, ra, ca] = max(lo, hi * SMOKE_DOWN)
            self.smoke = s.astype(np.float32)
            self.t += 1

    def room_status(self):
        """Per-room summary: 'fire' | 'smoke' | 'clear'."""
        g = self.grid
        out = {}
        for info in g.rooms:
            fi = g.level_index[info["level"]]
            m = g.room[fi] == info["index"]
            if not m.any():
                out[info["id"]] = "clear"
            elif self.fire[fi][m].any():
                out[info["id"]] = "fire"
            elif self.smoke[fi][m].max() > 0.35:
                out[info["id"]] = "smoke"
            else:
                out[info["id"]] = "clear"
        return out

    def vertical_status(self):
        g = self.grid
        out = {}
        for vid, cells in g.vertical_cells.items():
            worst = "clear"
            for fi, r, c in cells.values():
                if self.fire[fi, r, c]:
                    worst = "fire"; break
                if self.smoke[fi, r, c] > 0.35:
                    worst = "smoke"
            out[vid] = worst
        return out

    def cells_payload(self, smoke_min=0.15):
        g = self.grid
        fire, smoke = {}, {}
        for fi, lv in enumerate(g.levels):
            fr, fc = np.nonzero(self.fire[fi])
            fire[lv] = np.stack([fr, fc], axis=1).tolist()
            sm = (self.smoke[fi] >= smoke_min) & ~self.fire[fi]
            sr, sc = np.nonzero(sm)
            vals = np.round(self.smoke[fi][sm], 2)
            smoke[lv] = [[int(r), int(c), float(v)] for r, c, v in zip(sr, sc, vals)]
        return fire, smoke
```

## `backend/routing.py`

```python
"""Evacuation routing.

One multi-source Dijkstra from ALL exits builds a "next step toward safety"
field for every cell in the building, so any number of people get a route
instantly. Recompute it whenever hazards change.

Costs (meters-equivalent):
  - fire cells are impassable
  - smoke multiplies cost by (1 + SMOKE_PENALTY * smoke)
  - stairs/ramps cost STAIR_COST_M per floor; elevators are excluded in a fire
"""
import math
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

SMOKE_PENALTY = 12.0
NO_PRED = -9999

DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]


class RouteField:
    def __init__(self, grid, fire=None, smoke=None, use_elevators=False):
        self.grid = grid
        g = grid
        F, R, C = g.walkable.shape
        fire = np.zeros_like(g.walkable) if fire is None else fire
        smoke = np.zeros(g.walkable.shape, np.float32) if smoke is None else smoke
        self.passable = g.walkable & ~fire
        self.smoke = smoke
        idx = np.arange(F * R * C).reshape(F, R, C)

        rows, cols, wts = [], [], []
        P = self.passable
        for dr, dc in DIRS:
            r0, r1 = max(0, -dr), R - max(0, dr)
            c0, c1 = max(0, -dc), C - max(0, dc)
            a = P[:, r0:r1, c0:c1]
            b = P[:, r0 + dr:r1 + dr, c0 + dc:c1 + dc]
            ok = a & b
            if dr and dc:  # no corner cutting
                ok &= P[:, r0 + dr:r1 + dr, c0:c1] & P[:, r0:r1, c0 + dc:c1 + dc]
            step = g.cell * (math.sqrt(2) if dr and dc else 1.0)
            sa = smoke[:, r0:r1, c0:c1][ok]
            sb = smoke[:, r0 + dr:r1 + dr, c0 + dc:c1 + dc][ok]
            rows.append(idx[:, r0:r1, c0:c1][ok])
            cols.append(idx[:, r0 + dr:r1 + dr, c0 + dc:c1 + dc][ok])
            wts.append(step * (1.0 + SMOKE_PENALTY * (sa + sb) / 2.0))

        self.link_names = {}
        for (fa, ra, ca), (fb, rb, cb), cost, v in g.links:
            if v["type"] == "elevator" and not use_elevators:
                continue
            if not (P[fa, ra, ca] and P[fb, rb, cb]):
                continue
            a, b = g.flat(fa, ra, ca), g.flat(fb, rb, cb)
            if (a, b) in self.link_names:   # avoid duplicate edges (csr sums them)
                continue
            s = (smoke[fa, ra, ca] + smoke[fb, rb, cb]) / 2.0
            w = cost * (1.0 + SMOKE_PENALTY * s)
            rows += [np.array([a]), np.array([b])]
            cols += [np.array([b]), np.array([a])]
            wts += [np.array([w]), np.array([w])]
            self.link_names[(a, b)] = self.link_names[(b, a)] = v["name"]

        n = F * R * C
        graph = coo_matrix(
            (np.concatenate(wts), (np.concatenate(rows), np.concatenate(cols))),
            shape=(n, n),
        ).tocsr()

        sources = [g.flat(fi, r, c) for fi, r, c, _ in g.exits if P[fi, r, c]]
        self.exit_ids = {g.flat(fi, r, c): did for fi, r, c, did in g.exits}
        if sources:
            self.dist, self.pred, _ = dijkstra(
                graph, directed=True, indices=sources,
                return_predecessors=True, min_only=True,
            )
        else:
            self.dist = np.full(n, np.inf)
            self.pred = np.full(n, NO_PRED)

    # ------------------------------------------------------------------
    def route_from(self, fi, x, y):
        g = self.grid
        start = g.nearest_walkable(fi, x, y, max_dist=3.0, mask=self.passable[fi])
        if start is None:
            on_fire = False
            r, c = g.cell_of(x, y)
            if 0 <= r < g.rows and 0 <= c < g.cols:
                on_fire = bool(g.walkable[fi, r, c] and not self.passable[fi, r, c])
            return {"reachable": False, "reason": "in fire" if on_fire else "not on a walkable area",
                    "path": [], "distance_m": None, "instructions": []}
        node = g.flat(fi, *start)
        if not math.isfinite(self.dist[node]):
            return {"reachable": False, "reason": "no safe route to any exit",
                    "path": [], "distance_m": None, "instructions": []}
        nodes = [node]
        while self.pred[node] != NO_PRED and len(nodes) < 1_000_000:
            node = int(self.pred[node])
            nodes.append(node)
        cells = [g.unflat(k) for k in nodes]
        return {
            "reachable": True,
            "reason": "",
            "path": self._simplify(cells),
            "distance_m": round(self._length(cells), 1),
            "max_smoke": round(float(max(self.smoke[c] for c in cells)), 2),
            "exit": self.exit_ids.get(nodes[-1]),
            "instructions": self._instructions(cells, nodes),
        }

    def _length(self, cells):
        g = self.grid
        total = 0.0
        for (fa, ra, ca), (fb, rb, cb) in zip(cells, cells[1:]):
            if fa == fb:   # stair travel is reported in the instructions instead
                total += g.cell * math.hypot(ra - rb, ca - cb)
        return total

    def _simplify(self, cells):
        """Drop collinear points; returns [[level, x, y], ...]."""
        g = self.grid
        if not cells:
            return []
        keep = [cells[0]]
        for prev, cur, nxt in zip(cells, cells[1:], cells[2:]):
            if cur[0] != prev[0] or cur[0] != nxt[0]:
                keep.append(cur); continue
            d1 = (cur[1] - prev[1], cur[2] - prev[2])
            d2 = (nxt[1] - cur[1], nxt[2] - cur[2])
            if d1 != d2:
                keep.append(cur)
        if len(cells) > 1:
            keep.append(cells[-1])
        out = []
        for fi, r, c in keep:
            x, y = g.center_of(r, c)
            out.append([g.levels[fi], round(x, 2), round(y, 2)])
        return out

    def _instructions(self, cells, nodes):
        g = self.grid
        steps = []
        seg_room, seg_len = None, 0.0

        def flush():
            if seg_room is not None and seg_len >= 1.0:
                info = g.rooms[seg_room]
                name = info["label"] if info["type"] != "hallway" else f"the hallway ({info['label']})"
                steps.append(f"Walk {round(seg_len)} m through {name}")

        for i, (fi, r, c) in enumerate(cells):
            if i > 0:
                pfi, pr, pc = cells[i - 1]
                if pfi != fi:
                    flush(); seg_room, seg_len = None, 0.0
                    name = self.link_names.get((nodes[i - 1], nodes[i]), "stairs")
                    direction = "down" if fi < pfi else "up"
                    steps.append(f"Take {name} {direction} to floor {g.levels[fi]}")
                    continue
                seg_len += g.cell * math.hypot(r - pr, c - pc)
            rid = int(g.room[fi, r, c])
            if rid != -1 and rid != seg_room:
                if seg_room is not None:
                    flush()
                    seg_len = 0.0
                seg_room = rid
        flush()
        exit_id = self.exit_ids.get(nodes[-1], "the exit")
        steps.append(f"Exit the building at {exit_id}")
        return steps
```

## `backend/server.py`

```python
"""Firesight API + static 3D viewer.

Run from the project root:
    uvicorn backend.server:app --reload --port 8000
Then open http://localhost:8000

Env:
    FIRESIGHT_BUILDING  path to building JSON (default data/sample_building.json)
"""
import os
import threading

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .fire import FireSim
from .grid import BuildingGrid
from .model import ModelError, load_building, load_building_file
from .routing import RouteField

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BUILDING = os.path.join(ROOT, "data", "sample_building.json")
VIEWER_DIR = os.path.join(ROOT, "viewer")


class World:
    """Everything derived from one building JSON. Rebuilt on upload."""

    def __init__(self, model):
        self.model = model
        self.grid = BuildingGrid(model)
        self.sim = FireSim(self.grid)
        self.field = RouteField(self.grid)

    def refresh_routes(self):
        self.field = RouteField(self.grid, self.sim.fire, self.sim.smoke)


lock = threading.Lock()
world = World(load_building_file(os.environ.get("FIRESIGHT_BUILDING", DEFAULT_BUILDING)))

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
        return {"ok": True, "warnings": model["warnings"] + new_world.grid.warnings}


@app.get("/api/state")
def get_state():
    with lock:
        fire, smoke = world.sim.cells_payload()
        return {
            "t": world.sim.t,
            "origins": world.sim.origins,
            "rooms": world.sim.room_status(),
            "verticals": world.sim.vertical_status(),
            "fire": fire,
            "smoke": smoke,
        }


@app.post("/api/fire/start")
def fire_start(room_id: str = Body(None, embed=True), level: int = Body(None, embed=True),
               x: float = Body(None, embed=True), y: float = Body(None, embed=True)):
    with lock:
        if room_id:
            try:
                ok = world.sim.ignite_room(room_id)
            except KeyError:
                raise HTTPException(404, f"Unknown room {room_id}")
        elif level is not None and x is not None and y is not None:
            ok = world.sim.ignite_point(_level_index(level), x, y)
        else:
            raise HTTPException(400, "Send room_id, or level + x + y")
        if not ok:
            raise HTTPException(400, "Could not place fire there")
        world.refresh_routes()
        return {"ok": True, "t": world.sim.t}


@app.post("/api/fire/step")
def fire_step(n: int = Body(1, embed=True)):
    with lock:
        world.sim.step(max(1, min(int(n), 50)))
        world.refresh_routes()
        return {"ok": True, "t": world.sim.t}


@app.post("/api/fire/reset")
def fire_reset():
    with lock:
        world.sim.reset()
        world.refresh_routes()
        return {"ok": True}


@app.get("/api/route")
def route(level: int = Query(...), x: float = Query(...), y: float = Query(...)):
    with lock:
        return world.field.route_from(_level_index(level), x, y)


@app.get("/api/routes/all")
def routes_all():
    """Evacuation route from the center of every room (skips stairs/elevators)."""
    with lock:
        g = world.grid
        out = []
        for info in g.rooms:
            if info["type"] in ("stairwell", "elevator", "hallway"):
                continue
            fi = g.level_index[info["level"]]
            in_room = g.room[fi] == info["index"]
            cell = g.nearest_walkable(fi, *info["centroid"], max_dist=50.0, mask=in_room)
            if cell is None:
                continue
            r = world.field.route_from(fi, *g.center_of(*cell))
            out.append({"room_id": info["id"], "label": info["label"], "level": info["level"],
                        "reachable": r["reachable"], "distance_m": r["distance_m"], "path": r["path"]})
        return out


# Static viewer last so /api/* wins.
if os.path.isdir(VIEWER_DIR):
    app.mount("/", StaticFiles(directory=VIEWER_DIR, html=True), name="viewer")
```

## `backend/extract_floorplans.py`

```python
"""Send floor plan images to Gemini and save validated building JSON.

Usage (from project root):
    export GEMINI_API_KEY=...
    python -m backend.extract_floorplans floor1.png floor2.png floor3.png -o data/dcc.json
Then:
    FIRESIGHT_BUILDING=data/dcc.json uvicorn backend.server:app --port 8000

Images must be passed in floor order (first = floor 1). Output is validated with
the same loader the server uses; warnings are printed so you know what to fix.
"""
import argparse
import json
import mimetypes
import os
import sys

from .model import ModelError, load_building, parse_json_text

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_prompt.txt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+", help="floor plan images, in floor order")
    ap.add_argument("-o", "--out", default="data/building.json")
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    ap.add_argument("--building", default="Darrin Communications Center (RPI)")
    ap.add_argument("--first-level", type=int, default=1)
    args = ap.parse_args()

    from google import genai
    from google.genai import types

    with open(PROMPT_PATH, encoding="utf-8") as fh:
        prompt = fh.read()
    n = len(args.images)
    order = ", ".join(f"Image {i + 1} = Floor {args.first_level + i}" for i in range(n))
    prompt = (prompt.replace("{BUILDING}", args.building)
                    .replace("{N}", str(n))
                    .replace("{ORDER}", order))

    contents = [prompt]
    for i, path in enumerate(args.images):
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as fh:
            data = fh.read()
        contents.append(f"Image {i + 1} (Floor {args.first_level + i}):")
        contents.append(types.Part.from_bytes(data=data, mime_type=mime))

    client = genai.Client()  # reads GEMINI_API_KEY
    resp = client.models.generate_content(
        model=args.model,
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
    )
    text = resp.text or ""
    raw_path = os.path.splitext(args.out)[0] + ".raw.json"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(raw_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    try:
        raw = parse_json_text(text)
        model = load_building(raw)
    except ModelError as e:
        print(f"Gemini output failed validation: {e}\nRaw output saved to {raw_path}", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)
    print(f"Saved {args.out} ({len(model['floors'])} floors). Raw: {raw_path}")
    for w in model["warnings"]:
        print("WARNING:", w)


if __name__ == "__main__":
    main()
```

## `backend/gemini_prompt.txt`

```text
You are an expert at reading architectural floor plans. You will receive {N} floor plan
images of the same building ({BUILDING}), in order: {ORDER}.
These are architectural floor plans, not evacuation maps, so exits are not labeled for you:
infer them from doors in the exterior wall.

Convert them into ONE structured building model used for 3D rendering and fire-evacuation routing.

INSTRUCTIONS
1. For each floor, identify:
   - Walls as straight line segments (approximate curves with several short segments).
     Include interior partitions. Do NOT draw walls across door openings.
   - Spaces as polygons: classroom, office, hallway, lobby, restroom, stairwell, elevator,
     mechanical, lecture_hall, storage, unknown. Hallways/lobbies must be included as spaces.
   - Doors: center point on the wall, width, and the ids of the two spaces it connects.
   - Exterior exits: doors in the exterior wall that lead outside (exterior_exit = true).
   - Stairwells, elevators, ramps.
   - Room labels/numbers exactly as written.
2. Coordinates:
   - ONE shared coordinate system for ALL floors so they stack correctly.
   - Origin (0,0) = top-left corner of the building footprint on the lowest floor.
   - x increases to the right, y increases downward (image orientation). Units: meters.
   - Use the scale bar or dimensions if present; otherwise assume a single door is 0.9 m wide.
     Explain how you set scale in "scale_method".
3. Floor alignment:
   - Align floors using stairwells, elevators and the exterior outline. The same stairwell must
     have nearly the same (x, y) on every floor it serves. Put any extra shift you applied in
     each floor's "alignment" (use 0 if coordinates are already in the shared frame).
4. Vertical connections:
   - One entry per stairwell/elevator/ramp, at its center, listing every floor it serves.
   - A space spanning several floors (tall lecture hall, atrium) is multi_floor = true and listed
     in multi_floor_spaces with its door ids on each floor.
5. Accuracy:
   - Every space must be reachable through at least one door.
   - Do NOT invent rooms or doors you cannot see. If unsure, include it with low confidence and
     explain in "notes". Every element gets a confidence from 0 to 1.
   - Ids must be unique across the whole building (prefix with the floor, e.g. "F2-204").

OUTPUT: Return ONLY valid JSON matching this schema, no extra text.

{
  "building": "string",
  "units": "meters",
  "scale_method": "string",
  "floor_height_m": 4.0,
  "floors": [
    {
      "level": 1,
      "alignment": { "offset_x": 0, "offset_y": 0, "rotation_deg": 0 },
      "outline": [[0, 0], [40, 0], [40, 24], [0, 24]],
      "walls": [
        { "id": "F1-W1", "x1": 0, "y1": 0, "x2": 40, "y2": 0, "exterior": true, "confidence": 0.9 }
      ],
      "spaces": [
        { "id": "F1-101", "label": "101",
          "type": "classroom|office|hallway|lobby|restroom|stairwell|elevator|mechanical|lecture_hall|storage|unknown",
          "polygon": [[0, 0], [8, 0], [8, 10], [0, 10]], "multi_floor": false, "confidence": 0.8 }
      ],
      "doors": [
        { "id": "F1-D1", "x": 4, "y": 10, "width": 0.9, "connects": ["F1-101", "F1-HALL"],
          "exterior_exit": false, "confidence": 0.7 }
      ],
      "notes": "string"
    }
  ],
  "vertical_connections": [
    { "id": "STAIR-WEST", "name": "West Stairs", "type": "stairs|elevator|ramp",
      "x": 2, "y": 5, "floors": [1, 2, 3], "confidence": 0.9 }
  ],
  "multi_floor_spaces": [
    { "id": "LECTURE-HALL-A", "label": "string", "floors": [1, 2], "door_ids": ["F1-D7", "F2-D3"] }
  ],
  "global_notes": "string"
}
```

## `data/make_sample.py`

```python
"""Generate a fake 3-floor building in the exact Gemini output schema.

Use it to develop/test before the real DCC JSON exists:
    python data/make_sample.py   ->  data/sample_building.json
Layout per floor (meters): 40 x 24, central hallway y=10..14,
west stairs NW corner, east stairs SE corner, elevator on the south side.
"""
import json
import os

W, H = 40, 24


def floor(level):
    walls = [
        (0, 0, W, 0, True), (W, 0, W, H, True), (W, H, 0, H, True), (0, H, 0, 0, True),
        (0, 10, W, 10, False), (0, 14, W, 14, False),
    ]
    north = [(0, 4, "stairwell", "West Stairs"), (4, 12, "classroom", None), (12, 20, "office", None),
             (20, 28, "classroom", None), (28, 36, "classroom", None), (36, 40, "restroom", "Restroom")]
    south = [(0, 8, "classroom", None), (8, 16, "office", None), (16, 20, "elevator", "Elevator"),
             (20, 28, "classroom", None), (28, 36, "storage", None), (36, 40, "stairwell", "East Stairs")]
    if level == 3:  # a big lecture hall on the top floor
        north = [north[0], north[1], north[2], (20, 36, "lecture_hall", "Lecture Hall 3A"), north[5]]

    spaces, doors = [], []
    n = 1
    for (x0, x1, t, label), (y0, y1, ydoor) in (
        [(r, (0, 10, 10)) for r in north] + [(r, (14, 24, 14)) for r in south]
    ):
        if x0 > 0:
            walls.append((x0, y0, x0, y1, False))
        lbl = label or f"{level}{n:02d}"
        sid = f"F{level}-{lbl.replace(' ', '')}"
        n += 1
        spaces.append({"id": sid, "label": lbl, "type": t,
                       "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                       "multi_floor": False, "confidence": 0.9})
        doors.append({"id": f"F{level}-D-{lbl.replace(' ', '')}", "x": (x0 + x1) / 2, "y": ydoor,
                      "width": 1.8 if t in ("stairwell", "lecture_hall") else 0.9,
                      "connects": [sid, f"F{level}-HALL"], "exterior_exit": False, "confidence": 0.85})

    spaces.append({"id": f"F{level}-HALL", "label": f"Hall {level}", "type": "hallway",
                   "polygon": [[0, 10], [W, 10], [W, 14], [0, 14]], "multi_floor": False, "confidence": 0.95})
    if level == 1:
        doors += [
            {"id": "EXIT-WEST", "x": 0, "y": 12, "width": 1.8, "connects": ["F1-HALL"],
             "exterior_exit": True, "confidence": 0.95},
            {"id": "EXIT-EAST", "x": W, "y": 12, "width": 1.8, "connects": ["F1-HALL"],
             "exterior_exit": True, "confidence": 0.95},
        ]

    # dedupe partition walls
    uniq, seen = [], set()
    for w in walls:
        k = tuple(round(v, 3) for v in w[:4])
        if k not in seen:
            seen.add(k); uniq.append(w)

    return {
        "level": level,
        "alignment": {"offset_x": 0, "offset_y": 0, "rotation_deg": 0},
        "outline": [[0, 0], [W, 0], [W, H], [0, H]],
        "walls": [{"id": f"F{level}-W{i}", "x1": a, "y1": b, "x2": c, "y2": d,
                   "exterior": e, "confidence": 0.9} for i, (a, b, c, d, e) in enumerate(uniq)],
        "spaces": spaces,
        "doors": doors,
        "notes": "synthetic sample",
    }


building = {
    "building": "Sample Hall (synthetic)",
    "units": "meters",
    "scale_method": "synthetic",
    "floor_height_m": 4.0,
    "floors": [floor(1), floor(2), floor(3)],
    "vertical_connections": [
        {"id": "STAIR-WEST", "name": "West Stairs", "type": "stairs", "x": 2, "y": 5, "floors": [1, 2, 3], "confidence": 0.95},
        {"id": "STAIR-EAST", "name": "East Stairs", "type": "stairs", "x": 38, "y": 19, "floors": [1, 2, 3], "confidence": 0.95},
        {"id": "ELEVATOR-1", "name": "Elevator", "type": "elevator", "x": 18, "y": 19, "floors": [1, 2, 3], "confidence": 0.9},
    ],
    "multi_floor_spaces": [],
    "global_notes": "Synthetic test building, not DCC.",
}

if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_building.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(building, fh, indent=2)
    print("wrote", out)
```

## `tests/test_core.py`

````python
"""Run: python -m pytest -q   (or: python tests/test_core.py)"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from backend.model import load_building_file, load_building, parse_json_text, ModelError
from backend.grid import BuildingGrid
from backend.fire import FireSim
from backend.routing import RouteField
from backend.server import app

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sample_building.json")


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
````

## `viewer/index.html`

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Firesight 3D</title>
<style>
  :root { --bg:#0e1116; --panel:#161b22; --line:#2a313c; --text:#e6edf3; --muted:#8b949e;
          --fire:#ff4d3d; --smoke:#d9a441; --safe:#3fb950; --route:#58a6ff; }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--text);
               font: 13px/1.4 system-ui, -apple-system, Segoe UI, sans-serif; overflow:hidden; }
  #app { position:fixed; inset:0; }
  #panel { position:fixed; top:12px; left:12px; width:300px; max-height:calc(100% - 24px); overflow:auto;
           background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; z-index:2; }
  h1 { font-size:15px; margin:0 0 2px; } .sub { color:var(--muted); margin-bottom:10px; }
  .row { display:flex; gap:6px; align-items:center; margin:6px 0; flex-wrap:wrap; }
  label { color:var(--muted); font-size:12px; }
  select, button, input[type=range] { font:inherit; }
  select, button { background:#21262d; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:5px 8px; }
  button { cursor:pointer; } button:hover { border-color:#8b949e; }
  button.primary { background:#da3633; border-color:#da3633; }
  button.on { background:#1f6feb; border-color:#1f6feb; }
  select { flex:1; min-width:0; }
  .sec { border-top:1px solid var(--line); margin-top:10px; padding-top:8px; }
  .k { color:var(--muted); } .bad { color:var(--fire); } .good { color:var(--safe); }
  ol { margin:6px 0 0 18px; padding:0; } li { margin:2px 0; }
  #warn { color:var(--smoke); font-size:12px; white-space:pre-wrap; }
  #legend span { display:inline-flex; align-items:center; gap:4px; margin-right:8px; font-size:12px; color:var(--muted); }
  #legend i { width:10px; height:10px; border-radius:2px; display:inline-block; }
  .lbl { color:#c9d1d9; font-size:10px; background:rgba(14,17,22,.65); padding:1px 4px; border-radius:3px; pointer-events:none; white-space:nowrap; }
  #hint { position:fixed; bottom:12px; right:12px; color:var(--muted); font-size:12px; background:var(--panel);
          border:1px solid var(--line); border-radius:8px; padding:6px 10px; z-index:2; }
  @media (max-width: 640px) { #panel { width:calc(100% - 24px); max-height:45%; } #hint { display:none; } }
</style>
</head>
<body>
<div id="app"></div>
<div id="panel">
  <h1>Firesight</h1>
  <div class="sub" id="bname">Loading building…</div>

  <div class="row"><label>Fire in</label><select id="roomSel"></select></div>
  <div class="row">
    <button class="primary" id="btnFire">Start fire</button>
    <button id="btnStep">Step</button>
    <button id="btnPlay">Play</button>
    <button id="btnReset">Reset</button>
  </div>
  <div class="row k">Sim step: <b id="tval">0</b></div>

  <div class="sec">
    <div class="row"><label>Show floor</label><select id="floorSel"><option value="all">All floors</option></select></div>
    <div class="row"><label>Floor spacing</label><input id="gap" type="range" min="3" max="20" step="0.5" value="9"></div>
    <div class="row">
      <button id="btnLabels" class="on">Labels</button>
      <button id="btnAll">All routes</button>
    </div>
    <div id="legend" class="row">
      <span><i style="background:var(--fire)"></i>Fire</span>
      <span><i style="background:var(--smoke)"></i>Smoke</span>
      <span><i style="background:var(--safe)"></i>Exit / clear stairs</span>
      <span><i style="background:var(--route)"></i>Route</span>
    </div>
  </div>

  <div class="sec">
    <div class="k">Click any room to get the escape route from there. It reroutes live as the fire spreads.</div>
    <div id="routeInfo" style="margin-top:6px"></div>
  </div>
  <div class="sec" id="warnSec" style="display:none"><div class="k">Map warnings</div><div id="warn"></div></div>
</div>
<div id="hint">Drag to rotate · Scroll to zoom · Right-drag to pan</div>

<script type="importmap">
{ "imports": {
  "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
  "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
} }
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const API = "";            // same origin as FastAPI
const WALL_H = 1.2;        // short walls = "exploded" look
const COLORS = {
  floor: 0x1b2129, room: 0x2b3440, hallway: 0x343e4b, stairwell: 0x23432d, elevator: 0x3a3a46,
  wall: 0x8b949e, fire: 0xff4d3d, smoke: 0xd9a441, safe: 0x3fb950, route: 0x58a6ff, exit: 0x3fb950,
};

// ---------- scene ----------
const app = document.getElementById("app");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.setClearColor(0x0e1116);
app.appendChild(renderer.domElement);

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(innerWidth, innerHeight);
labelRenderer.domElement.style.position = "fixed";
labelRenderer.domElement.style.inset = "0";
labelRenderer.domElement.style.pointerEvents = "none";
app.appendChild(labelRenderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, innerWidth / innerHeight, 0.1, 2000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x223344, 1.0));
const sun = new THREE.DirectionalLight(0xffffff, 0.8);
sun.position.set(30, 60, 20);
scene.add(sun);

addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  labelRenderer.setSize(innerWidth, innerHeight);
});

// ---------- state ----------
let building = null, grid = null, levels = [];
let gap = 9;
const floorGroups = new Map();     // level -> Group
const roomMeshes = new Map();      // room id -> mesh
const hazardMeshes = new Map();    // level -> {fire, smoke}
const pickables = [];
let verticalGroup = new THREE.Group(); scene.add(verticalGroup);
let routeGroup = new THREE.Group(); scene.add(routeGroup);
let allRoutesGroup = new THREE.Group(); scene.add(allRoutesGroup);
let lastStart = null, playing = null, showAll = false, lastState = null;

const $ = (id) => document.getElementById(id);
const levelY = (lv) => levels.indexOf(lv) * gap;
const api = async (path, opts) => {
  const r = await fetch(API + path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
};
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                         body: JSON.stringify(body || {}) });

// plan (x, y) -> world (x, ?, y). Floors are stacked on world Y.
function shapeFrom(poly) {
  // ShapeGeometry lives in XY; we flip y then rotate -90° about X so plan y -> world +Z.
  return new THREE.Shape(poly.map(([x, y]) => new THREE.Vector2(x, -y)));
}
function flatGeom(poly) {
  const g = new THREE.ShapeGeometry(shapeFrom(poly));
  g.rotateX(-Math.PI / 2);
  return g;
}

// ---------- build ----------
function clearGroup(g) {
  g.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose?.(); });
  g.clear();
}

// Split a wall into pieces, leaving gaps where doors sit on it.
function wallSegments(w, doors) {
  const dx = w.x2 - w.x1, dy = w.y2 - w.y1, L = Math.hypot(dx, dy);
  if (L < 1e-6) return [];
  const cuts = [];
  for (const d of doors) {
    const t = ((d.x - w.x1) * dx + (d.y - w.y1) * dy) / (L * L);
    if (t < -0.01 || t > 1.01) continue;
    const px = w.x1 + t * dx, py = w.y1 + t * dy;
    if (Math.hypot(d.x - px, d.y - py) > 0.4) continue;
    const half = d.width / 2 / L;
    cuts.push([t - half, t + half]);
  }
  cuts.sort((a, b) => a[0] - b[0]);
  const segs = []; let cur = 0;
  for (const [a, b] of cuts) { if (a > cur) segs.push([cur, a]); cur = Math.max(cur, b); }
  if (cur < 1) segs.push([cur, 1]);
  return segs.filter(([a, b]) => (b - a) * L > 0.05)
             .map(([a, b]) => [w.x1 + a * dx, w.y1 + a * dy, w.x1 + b * dx, w.y1 + b * dy]);
}

function buildBuilding() {
  floorGroups.forEach((g) => { clearGroup(g); scene.remove(g); });
  floorGroups.clear(); roomMeshes.clear(); hazardMeshes.clear(); pickables.length = 0;

  const cellGeom = new THREE.PlaneGeometry(grid.cell * 0.96, grid.cell * 0.96);
  cellGeom.rotateX(-Math.PI / 2);
  const maxCells = grid.rows * grid.cols;

  for (const fl of building.floors) {
    const g = new THREE.Group();
    g.userData.level = fl.level;

    if (fl.outline.length >= 3) {
      const plate = new THREE.Mesh(flatGeom(fl.outline),
        new THREE.MeshStandardMaterial({ color: COLORS.floor, side: THREE.DoubleSide }));
      plate.position.y = -0.02;
      plate.userData = { level: fl.level, kind: "plate" };
      g.add(plate); pickables.push(plate);
    }

    for (const s of fl.spaces) {
      const base = COLORS[s.type] ?? COLORS.room;
      const m = new THREE.Mesh(flatGeom(s.polygon),
        new THREE.MeshStandardMaterial({ color: base, side: THREE.DoubleSide }));
      m.userData = { level: fl.level, kind: "room", id: s.id, base };
      g.add(m); pickables.push(m); roomMeshes.set(s.id, m);

      const xs = s.polygon.map((p) => p[0]), ys = s.polygon.map((p) => p[1]);
      const div = document.createElement("div");
      div.className = "lbl"; div.textContent = s.label;
      const lbl = new CSS2DObject(div);
      lbl.position.set(xs.reduce((a, b) => a + b) / xs.length, 0.3, ys.reduce((a, b) => a + b) / ys.length);
      lbl.userData.isLabel = true;
      g.add(lbl);
    }

    const wallMat = new THREE.MeshStandardMaterial({ color: COLORS.wall });
    for (const w of fl.walls) {
      for (const [x1, y1, x2, y2] of wallSegments(w, fl.doors)) {   // doors become real gaps
        const dx = x2 - x1, dz = y2 - y1, len = Math.hypot(dx, dz);
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(len, WALL_H, 0.15), wallMat);
        mesh.position.set((x1 + x2) / 2, WALL_H / 2, (y1 + y2) / 2);
        mesh.rotation.y = -Math.atan2(dz, dx);
        g.add(mesh);
      }
    }

    for (const d of fl.doors.filter((d) => d.exterior_exit)) {
      const post = new THREE.Mesh(new THREE.BoxGeometry(0.5, WALL_H * 1.8, 0.5),
        new THREE.MeshStandardMaterial({ color: COLORS.exit, emissive: COLORS.exit, emissiveIntensity: 0.4 }));
      post.position.set(d.x, WALL_H * 0.9, d.y);
      g.add(post);
      const div = document.createElement("div");
      div.className = "lbl"; div.style.color = "#3fb950"; div.textContent = "EXIT";
      const l = new CSS2DObject(div); l.position.set(d.x, WALL_H * 2.1, d.y); l.userData.isLabel = true;
      g.add(l);
    }

    const fire = new THREE.InstancedMesh(cellGeom,
      new THREE.MeshBasicMaterial({ color: COLORS.fire, transparent: true, opacity: 0.9 }), maxCells);
    const smoke = new THREE.InstancedMesh(cellGeom,
      new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.55, depthWrite: false }), maxCells);
    fire.count = 0; smoke.count = 0;
    fire.position.y = 0.06; smoke.position.y = 0.05;
    fire.frustumCulled = false; smoke.frustumCulled = false;
    g.add(fire); g.add(smoke);
    hazardMeshes.set(fl.level, { fire, smoke });

    scene.add(g);
    floorGroups.set(fl.level, g);
  }
  layoutFloors();
  frameCamera();
}

function layoutFloors() {
  for (const [lv, g] of floorGroups) g.position.y = levelY(lv);
  buildVerticals();
  if (lastState) applyState(lastState);
  refreshRoutes();
}

function buildVerticals() {
  clearGroup(verticalGroup);
  const status = lastState?.verticals || {};
  for (const v of building.vertical_connections) {
    const lo = Math.min(...v.floors), hi = Math.max(...v.floors);
    const y0 = levelY(lo), y1 = levelY(hi) + WALL_H;
    const st = status[v.id] || "clear";
    const color = v.type === "elevator" ? 0x6e7681 : st === "fire" ? COLORS.fire : st === "smoke" ? COLORS.smoke : COLORS.safe;
    const geo = new THREE.CylinderGeometry(0.35, 0.35, y1 - y0, 12);
    const mat = new THREE.MeshStandardMaterial({ color, transparent: true, opacity: v.type === "elevator" ? 0.35 : 0.8 });
    const m = new THREE.Mesh(geo, mat);
    m.position.set(v.x, (y0 + y1) / 2, v.y);
    verticalGroup.add(m);
    const div = document.createElement("div");
    div.className = "lbl";
    div.textContent = v.type === "elevator" ? `${v.name} (not for evacuation)` : v.name;
    const l = new CSS2DObject(div); l.position.set(v.x, y1 + 0.6, v.y); l.userData.isLabel = true;
    verticalGroup.add(l);
  }
  applyLabelVisibility();
}

function frameCamera() {
  const minX = grid.origin_x, minZ = grid.origin_y;
  const w = grid.cols * grid.cell, d = grid.rows * grid.cell;
  const cx = minX + w / 2, cz = minZ + d / 2, cy = levelY(levels[levels.length - 1]) / 2;
  const size = Math.max(w, d, levels.length * gap);
  controls.target.set(cx, cy, cz);
  camera.position.set(cx + size * 0.9, cy + size * 0.8, cz + size * 1.1);
  camera.updateProjectionMatrix();
}

// ---------- hazards ----------
const tmpM = new THREE.Matrix4(), tmpC = new THREE.Color(), GRAY = new THREE.Color(0x777777);
const cellX = (c) => grid.origin_x + (c + 0.5) * grid.cell;
const cellZ = (r) => grid.origin_y + (r + 0.5) * grid.cell;

function applyState(state) {
  lastState = state;
  $("tval").textContent = state.t;
  for (const [id, mesh] of roomMeshes) {
    const s = state.rooms[id];
    const c = s === "fire" ? 0x5a1d1a : s === "smoke" ? 0x4a3d1c : mesh.userData.base;
    mesh.material.color.setHex(c);
  }
  for (const lv of levels) {
    const hm = hazardMeshes.get(lv); if (!hm) continue;
    const fire = state.fire[lv] || [], smoke = state.smoke[lv] || [];
    fire.forEach(([r, c], i) => { tmpM.makeTranslation(cellX(c), 0, cellZ(r)); hm.fire.setMatrixAt(i, tmpM); });
    hm.fire.count = fire.length; hm.fire.instanceMatrix.needsUpdate = true;
    smoke.forEach(([r, c, s], i) => {
      tmpM.makeTranslation(cellX(c), 0, cellZ(r)); hm.smoke.setMatrixAt(i, tmpM);
      tmpC.setHex(COLORS.smoke).lerp(GRAY, 1 - s); hm.smoke.setColorAt(i, tmpC);
    });
    hm.smoke.count = smoke.length; hm.smoke.instanceMatrix.needsUpdate = true;
    if (hm.smoke.instanceColor) hm.smoke.instanceColor.needsUpdate = true;
  }
  buildVerticals();
}

// ---------- routes ----------
function pathToCurve(path) {
  const pts = path.map(([lv, x, y]) => new THREE.Vector3(x, levelY(lv) + 0.35, y));
  const curve = new THREE.CurvePath();
  for (let i = 0; i < pts.length - 1; i++) {
    if (pts[i].distanceTo(pts[i + 1]) > 1e-6) curve.add(new THREE.LineCurve3(pts[i], pts[i + 1]));
  }
  return { curve, pts };
}

function drawPath(group, path, color, radius) {
  if (!path || path.length < 2) return;
  const { curve, pts } = pathToCurve(path);
  if (!curve.curves.length) return;
  const segs = Math.max(8, curve.curves.length * 6);
  const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, segs, radius, 8, false),
    new THREE.MeshBasicMaterial({ color }));
  group.add(tube);
  return pts;
}

async function routeFrom(level, x, y) {
  lastStart = { level, x, y };
  const r = await api(`/api/route?level=${level}&x=${x.toFixed(2)}&y=${y.toFixed(2)}`);
  clearGroup(routeGroup);
  const start = new THREE.Mesh(new THREE.SphereGeometry(0.45, 16, 12), new THREE.MeshBasicMaterial({ color: 0xffffff }));
  start.position.set(x, levelY(level) + 0.45, y);
  routeGroup.add(start);
  const info = $("routeInfo");
  if (!r.reachable) {
    info.innerHTML = `<div class="bad"><b>No safe route</b>: ${r.reason}. Shelter in place and call 911.</div>`;
    return;
  }
  const pts = drawPath(routeGroup, r.path, COLORS.route, 0.22);
  if (pts) {
    const end = new THREE.Mesh(new THREE.SphereGeometry(0.5, 16, 12), new THREE.MeshBasicMaterial({ color: COLORS.safe }));
    end.position.copy(pts[pts.length - 1]);
    routeGroup.add(end);
  }
  const smokeNote = r.max_smoke > 0.35 ? `<div class="bad">Route passes through smoke. Stay low.</div>` : "";
  info.innerHTML = `<div class="good"><b>${r.distance_m} m</b> walking to <b>${r.exit}</b></div>${smokeNote}
    <ol>${r.instructions.map((s) => `<li>${s}</li>`).join("")}</ol>`;
}

async function refreshRoutes() {
  if (lastStart) await routeFrom(lastStart.level, lastStart.x, lastStart.y).catch(console.error);
  clearGroup(allRoutesGroup);
  if (!showAll) return;
  const all = await api("/api/routes/all");
  for (const r of all) if (r.reachable) drawPath(allRoutesGroup, r.path, 0x2f81f7, 0.08);
}

// ---------- picking ----------
const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
let downAt = null;
renderer.domElement.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return; // was a drag
  mouse.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  ray.setFromCamera(mouse, camera);
  const hits = ray.intersectObjects(pickables.filter((m) => m.parent.visible), false);
  if (!hits.length) return;
  const h = hits[0];
  routeFrom(h.object.userData.level, h.point.x, h.point.z).catch((err) => alert(err.message));
});

// ---------- UI ----------
function applyLabelVisibility() {
  const on = $("btnLabels").classList.contains("on");
  for (const g of floorGroups.values()) g.traverse((o) => { if (o.userData?.isLabel) o.visible = on && g.visible; });
  verticalGroup.traverse((o) => { if (o.userData?.isLabel) o.visible = on; });
}

async function pullState() { applyState(await api("/api/state")); }
async function afterChange() { await pullState(); await refreshRoutes(); }

$("btnFire").onclick = async () => {
  try { await post("/api/fire/start", { room_id: $("roomSel").value }); await afterChange(); }
  catch (e) { alert(e.message); }
};
$("btnStep").onclick = async () => { await post("/api/fire/step", { n: 1 }); await afterChange(); };
$("btnReset").onclick = async () => {
  stopPlay(); await post("/api/fire/reset"); await afterChange();
};
function stopPlay() { if (playing) { clearInterval(playing); playing = null; $("btnPlay").textContent = "Play"; $("btnPlay").classList.remove("on"); } }
$("btnPlay").onclick = () => {
  if (playing) return stopPlay();
  $("btnPlay").textContent = "Pause"; $("btnPlay").classList.add("on");
  let busy = false;
  playing = setInterval(async () => {
    if (busy) return; busy = true;
    try { await post("/api/fire/step", { n: 1 }); await afterChange(); } finally { busy = false; }
  }, 1200);
};
$("gap").oninput = (e) => { gap = parseFloat(e.target.value); layoutFloors(); };
$("floorSel").onchange = (e) => {
  const v = e.target.value;
  for (const [lv, g] of floorGroups) g.visible = v === "all" || String(lv) === v;
  applyLabelVisibility();
};
$("btnLabels").onclick = (e) => { e.target.classList.toggle("on"); applyLabelVisibility(); };
$("btnAll").onclick = async (e) => { showAll = !showAll; e.target.classList.toggle("on", showAll); await refreshRoutes(); };

// ---------- boot ----------
async function boot() {
  building = await api("/api/building");
  grid = building.grid; levels = grid.levels;
  $("bname").textContent = `${building.building} · ${levels.length} floor${levels.length > 1 ? "s" : ""}`;
  const sel = $("roomSel"); sel.innerHTML = "";
  for (const fl of building.floors) for (const s of fl.spaces) {
    if (s.type === "elevator") continue;
    const o = document.createElement("option"); o.value = s.id; o.textContent = `F${fl.level} · ${s.label}`;
    sel.appendChild(o);
  }
  for (const lv of levels) { const o = document.createElement("option"); o.value = lv; o.textContent = `Floor ${lv}`; $("floorSel").appendChild(o); }
  if (building.warnings?.length) { $("warnSec").style.display = ""; $("warn").textContent = building.warnings.join("\n"); }
  buildBuilding();
  await pullState();
}

function animate() {
  requestAnimationFrame(animate);
  const t = performance.now() / 1000;
  for (const hm of hazardMeshes.values()) hm.fire.material.opacity = 0.7 + 0.25 * Math.sin(t * 6);
  controls.update();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
}
animate();
boot().catch((e) => { $("bname").textContent = "Failed to load: " + e.message; console.error(e); });
</script>
</body>
</html>
```
