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
    "void",
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
    a = math.radians(align["rotation_deg"])
    xr = x * math.cos(a) - y * math.sin(a)
    yr = x * math.sin(a) + y * math.cos(a)
    return [xr + align["offset_x"], yr + align["offset_y"]]


def parse_json_text(text):
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelError(f"Invalid JSON: {e}") from e


def load_building(raw):
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
            axis = None
            ax = d.get("axis")
            if isinstance(ax, (list, tuple)) and len(ax) == 2 and None not in (_num(ax[0]), _num(ax[1])):
                o = xf([0.0, 0.0]); q = xf([_num(ax[0]), _num(ax[1])])
                n = math.hypot(q[0] - o[0], q[1] - o[1])
                if n > 1e-6:
                    axis = [(q[0] - o[0]) / n, (q[1] - o[1]) / n]
            doors.append({
                "id": unique_id(d.get("id"), f"F{level}-D{i}"),
                "x": p[0], "y": p[1], "width": width, "axis": axis,
                "connects": [str(c) for c in (d.get("connects") or [])],
                "exterior_exit": bool(d.get("exterior_exit", False)),
                "confidence": _num(d.get("confidence"), 1.0),
            })

        if not walls and not spaces:
            warnings.append(f"Floor {level}: no walls or spaces found")

        flights = []
        for fl_ in f.get("stair_flights") or []:
            try:
                poly = [xf(p) for p in (_point(q) for q in fl_.get("poly") or []) if p]
                if len(poly) != 4:
                    continue
                o = xf([0.0, 0.0]); up = xf([float(fl_["up"][0]), float(fl_["up"][1])])
                flights.append({"poly": poly, "center": xf(_point(fl_["center"])),
                                "up": [up[0] - o[0], up[1] - o[1]], "steps": int(fl_.get("steps", 10)),
                                "stair": fl_.get("stair"),
                                "goes": fl_.get("goes") if fl_.get("goes") in ("up", "down") else None,
                                "exterior": bool(fl_.get("exterior", False)),
                                "z": [float(fl_["z"][0]), float(fl_["z"][1])] if fl_.get("z") else None})
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        floors.append({
            "level": level, "alignment": align, "outline": outline,
            "walls": walls, "spaces": spaces, "doors": doors, "stair_flights": flights,
            "notes": str(f.get("notes") or ""),
        })

    floors.sort(key=lambda fl: fl["level"])
    levels = [fl["level"] for fl in floors]

    align_by_level = {fl["level"]: fl["alignment"] for fl in floors}
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
        positions = {}
        for key, pt in (v.get("positions") or {}).items():
            try:
                lv = int(key)
            except (TypeError, ValueError):
                continue
            pt = _point(pt)
            if pt and lv in vfloors:
                positions[lv] = _transform(pt[0], pt[1], align_by_level.get(lv, {}))
        verticals.append({
            "id": vid, "name": str(v.get("name") or vid), "type": vtype,
            "x": x, "y": y, "floors": vfloors, "positions": positions,
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
        "wall_style": "faces" if raw.get("wall_style") == "faces" else "centerline",
        "plan_north_deg": (_num(raw.get("plan_north_deg"), 0.0) or 0.0) % 360,
        "levels": levels,
        "floors": floors,
        "vertical_connections": verticals,
        "multi_floor_spaces": raw.get("multi_floor_spaces") or [],
        "view_tweaks": raw.get("view_tweaks") if isinstance(raw.get("view_tweaks"), dict) else {},
        "global_notes": str(raw.get("global_notes") or ""),
        "warnings": warnings,
    }


def load_building_file(path):
    with open(path, encoding="utf-8") as fh:
        return load_building(parse_json_text(fh.read()))
