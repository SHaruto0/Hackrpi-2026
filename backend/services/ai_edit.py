"""Propose map edits. Does not apply them. POST /api/ai/edit -> {ops, summary, reply, model}."""
import json
import math
import os
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
_NEAR = 3.0
_TYPES = ("stairs", "ramp", "elevator")


class EditBody(BaseModel):
    instruction: str
    level: int
    building: dict
    selection: list | None = None
    history: list | None = None


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pts(seq):
    return [(_num(p[0]), _num(p[1])) for p in seq or []
            if isinstance(p, (list, tuple)) and len(p) >= 2 and _num(p[0]) is not None and _num(p[1]) is not None]


def _centroid(pts):
    return [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)] if pts else None


def _r2(p):
    return [round(p[0], 2), round(p[1], 2)] if p and None not in p else None


def _dist(px, py, x1, y1, x2, y2):
    dx, dy, l2 = x2 - x1, y2 - y1, (x2 - x1) ** 2 + (y2 - y1) ** 2
    if l2 < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_near(c, other):
    mid = ((other[0] + other[2]) / 2, (other[1] + other[3]) / 2)
    return min(_dist(c[0], c[1], *other), _dist(c[2], c[3], *other), _dist(mid[0], mid[1], *c)) <= _NEAR


def _closest_on_poly(pts, qx, qy):
    best, best_d = None, 1e18
    for i, (x1, y1) in enumerate(pts or []):
        x2, y2 = pts[(i + 1) % len(pts)]
        dx, dy, l2 = x2 - x1, y2 - y1, (x2 - x1) ** 2 + (y2 - y1) ** 2
        if l2 < 1e-12:
            px, py = x1, y1
        else:
            t = max(0.0, min(1.0, ((qx - x1) * dx + (qy - y1) * dy) / l2))
            px, py = x1 + t * dx, y1 + t * dy
        d = math.hypot(qx - px, qy - py)
        if d < best_d:
            best, best_d = (px, py), d
    return best


def _toward(pts, center, other):
    """Point inside the room, closer to the other room than the centroid is."""
    edge = _closest_on_poly(pts, other[0], other[1])
    if not edge:
        return center
    return ((center[0] + edge[0]) / 2, (center[1] + edge[1]) / 2)


def _near_poly(seg, pts):
    if not pts:
        return False
    if any(_dist(p[0], p[1], *seg) <= _NEAR for p in pts):
        return True
    samples = ((seg[0], seg[1]), (seg[2], seg[3]), ((seg[0] + seg[2]) / 2, (seg[1] + seg[3]) / 2))
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        if any(_dist(s[0], s[1], a[0], a[1], b[0], b[1]) <= _NEAR for s in samples):
            return True
    return False


def _floor(building, level):
    for fl in (building or {}).get("floors") or []:
        try:
            if int(fl.get("level")) == int(level):
                return fl
        except (TypeError, ValueError):
            continue
    return None


def _floors(building):
    out = []
    for fl in (building or {}).get("floors") or []:
        try:
            out.append((int(fl.get("level")), fl))
        except (TypeError, ValueError):
            continue
    return out


def _catalog(building):
    doors, spaces, walls, verts, levels = set(), set(), set(), set(), set()
    for lv, fl in _floors(building):
        levels.add(lv)
        for key, bucket in (("doors", doors), ("spaces", spaces), ("walls", walls)):
            for obj in fl.get(key) or []:
                if obj.get("id") is not None:
                    bucket.add(str(obj.get("id")))
    for v in (building or {}).get("vertical_connections") or []:
        if v.get("id") is not None:
            verts.add(str(v.get("id")))
    return doors, spaces, walls, verts, levels


def _label_num(label):
    token = str(label or "").lower().split(" ")[0]
    return token if re.fullmatch(r"\d{3}[a-z]?", token) else None


def _where_fn(box):
    def where(x, y):
        fx = (x - box["min_x"]) / max(box["max_x"] - box["min_x"], 1e-6)
        fy = (y - box["min_y"]) / max(box["max_y"] - box["min_y"], 1e-6)
        v = "top" if fy < 1 / 3 else ("bottom" if fy > 2 / 3 else "middle")
        h = "left" if fx < 1 / 3 else ("right" if fx > 2 / 3 else "center")
        return f"{v}-{h}"
    return where


def _verticals(building):
    rows = []
    for v in (building or {}).get("vertical_connections") or []:
        floors = []
        for lv in v.get("floors") or []:
            try:
                floors.append(int(lv))
            except (TypeError, ValueError):
                pass
        pos = {}
        for key, raw in (v.get("positions") or {}).items():
            try:
                lk = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(raw, (list, tuple)) and len(raw) >= 2 and _num(raw[0]) is not None and _num(raw[1]) is not None:
                pos[str(lk)] = [round(_num(raw[0]), 2), round(_num(raw[1]), 2)]
        if not pos and _num(v.get("x")) is not None and _num(v.get("y")) is not None:
            xy = [round(_num(v.get("x")), 2), round(_num(v.get("y")), 2)]
            for lv in floors:
                pos[str(lv)] = xy
        rows.append({"id": v.get("id"), "name": v.get("name"), "type": v.get("type"),
                      "floors": floors, "positions": pos})
    return rows


def _room_records(building):
    rows = []
    for lv, fl in _floors(building):
        for s in fl.get("spaces") or []:
            if "lower tier" in str(s.get("label") or "").lower():
                continue
            num = _label_num(s.get("label"))
            pts = _pts(s.get("polygon"))
            c = _centroid(pts)
            if num and c and s.get("id") is not None:
                rows.append({"num": num, "id": str(s.get("id")), "level": lv, "c": c, "pts": pts})
    return rows


def _rooms_index(building, instruction):
    """number -> [id, level, centroid, point]. point is in-room, nearest the other named room."""
    rows = _room_records(building)
    named = []
    for num in re.findall(r"\b\d{3}[a-z]?\b", (instruction or "").lower()):
        if num not in named:
            named.append(num)
    named_rows = [r for r in rows if r["num"] in set(named)]
    spot = {}
    if len(set(named)) >= 2:
        for r in named_rows:
            others = [o for o in named_rows if o["num"] != r["num"]]
            if not others:
                continue
            other = min(others, key=lambda o: math.hypot(o["c"][0] - r["c"][0], o["c"][1] - r["c"][1]))
            spot[id(r)] = _toward(r["pts"], r["c"], other["c"])
    index = {}
    for r in rows:
        item = [r["id"], r["level"], _r2(r["c"]), _r2(spot.get(id(r), r["c"]))]
        cur = index.get(r["num"])
        index[r["num"]] = item if cur is None else (cur + [item] if isinstance(cur[0], list) else [cur, item])
    return index


def _named_polys(building, instruction):
    """Polygons of rooms the instruction names, keyed by floor, plus any label mentioned in full."""
    text = (instruction or "").lower()
    nums = set(re.findall(r"\b\d{3}[a-z]?\b", text))
    by = {}
    for lv, fl in _floors(building):
        hits = []
        for s in fl.get("spaces") or []:
            label = str(s.get("label") or "").lower()
            num = _label_num(label)
            named = num in nums or (len(label) >= 4 and re.search(rf"\b{re.escape(label)}\b", text))
            if named:
                pts = _pts(s.get("polygon"))
                if pts:
                    hits.append(pts)
        if hits:
            by[lv] = hits
    return by


def _pack_floor(building, fl, level, selection, near_polys, verts):
    sel = {str(s) for s in (selection or [])}
    spaces, doors, details, anchors, picked = [], [], [], [], []
    box = _pts(fl.get("outline"))
    bbox = ({"min_x": min(p[0] for p in box), "min_y": min(p[1] for p in box),
             "max_x": max(p[0] for p in box), "max_y": max(p[1] for p in box)} if box else None)
    where = _where_fn(bbox) if bbox else None
    for s in fl.get("spaces") or []:
        pts = _pts(s.get("polygon"))
        c = _centroid(pts)
        row = {"id": s.get("id"), "label": s.get("label"), "type": s.get("type"), "centroid": _r2(c)}
        if where and c:
            row["where"] = where(c[0], c[1])
        spaces.append(row)
        if str(s.get("id")) in sel:
            details.append({"kind": "space", **row})
            if c:
                anchors.append(tuple(c))
            if pts:
                near_polys.append(pts)
    for d in fl.get("doors") or []:
        x, y = _num(d.get("x")), _num(d.get("y"))
        row = {"id": d.get("id"), "x": None if x is None else round(x, 2), "y": None if y is None else round(y, 2),
               "exterior_exit": bool(d.get("exterior_exit"))}
        if where and x is not None and y is not None:
            row["where"] = where(x, y)
        doors.append(row)
        if str(d.get("id")) in sel and x is not None and y is not None:
            details.append({"kind": "door", **row})
            anchors.append((x, y))
    here = [v for v in verts if level in v["floors"] or str(level) in v["positions"]]
    for v in here:
        raw = v["positions"].get(str(level))
        if str(v.get("id")) in sel and raw:
            details.append({"kind": "vertical", "id": v.get("id"), "type": v.get("type"), "position": raw})
            anchors.append(tuple(raw))
    segs = []
    for w in fl.get("walls") or []:
        c = [_num(w.get(k)) for k in ("x1", "y1", "x2", "y2")]
        if None in c:
            continue
        segs.append((w.get("id"), c))
        if str(w.get("id")) in sel:
            picked.append(c)
    walls = []
    for wid, c in segs:
        near = (any(_dist(p[0], p[1], *c) <= _NEAR for p in anchors) or any(_seg_near(c, s) for s in picked)
                or any(_near_poly(c, poly) for poly in near_polys) or str(wid) in sel)
        if not near:
            continue
        item = {"id": wid, "x1": round(c[0], 2), "y1": round(c[1], 2), "x2": round(c[2], 2), "y2": round(c[3], 2)}
        walls.append(item)
        if str(wid) in sel:
            details.append({"kind": "wall", **item})
    return {"level": level, "outline_bbox": bbox, "spaces": spaces, "doors": doors,
            "verticals": here, "walls": walls, "selection": details}


def compact_context(building, level, selection):
    """One floor, for rule shortcuts. Walls only within 3 m of the selection."""
    fl = _floor(building, level) or {}
    try:
        lv = int(level)
    except (TypeError, ValueError):
        lv = level
    pack = _pack_floor(building, fl, lv, selection, [], _verticals(building))
    return {"level": pack["level"], "outline_bbox": pack["outline_bbox"], "spaces": pack["spaces"],
            "doors": pack["doors"], "vertical_connections": pack["verticals"],
            "walls_near_selection": pack["walls"], "selection": pack["selection"]}


def _model_context(building, selection, instruction, focus):
    verts = _verticals(building)
    named = _named_polys(building, instruction)
    floors, details = [], []
    for lv, fl in _floors(building):
        pack = _pack_floor(building, fl, lv, selection, list(named.get(lv) or []), verts)
        details.extend(pack.pop("selection"))
        if not pack["walls"]:
            pack.pop("walls")
        floors.append(pack)
    return {"focus_level": focus, "floors": floors, "rooms": _rooms_index(building, instruction), "selection": details}


def _width(v):
    w = _num(v)
    return w if w is not None and 0.6 <= w <= 3.0 else None


def _positions(raw, levels):
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, val in raw.items():
        try:
            lv = int(key)
        except (TypeError, ValueError):
            continue
        if lv not in levels or not isinstance(val, (list, tuple)) or len(val) < 2:
            continue
        x, y = _num(val[0]), _num(val[1])
        if x is None or y is None:
            continue
        out[str(lv)] = [x, y]
    return out or None


def _floor_list(raw, levels):
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for lv in raw:
        try:
            n = int(lv)
        except (TypeError, ValueError):
            return None
        if n not in levels:
            return None
        if n not in out:
            out.append(n)
    return out or None


def _opt_level(op, levels):
    if op.get("level") is None and "level" not in op:
        return None
    try:
        lv = int(op.get("level"))
    except (TypeError, ValueError):
        return False
    return lv if lv in levels else False


def _clean(op, building):
    if not isinstance(op, dict):
        return None
    k = op.get("op")
    fields = op.get("fields") if isinstance(op.get("fields"), dict) else {}
    doors, spaces, walls, verts, levels = _catalog(building)
    if k == "add_door":
        x, y, w = _num(op.get("x")), _num(op.get("y")), _width(op.get("width"))
        lv = _opt_level(op, levels)
        links = op.get("connects") if isinstance(op.get("connects"), list) else []
        if None not in (x, y, w) and lv is not False:
            row = {"op": k, "x": x, "y": y, "width": w, "exterior_exit": bool(op.get("exterior_exit", False)),
                   "connects": [str(c) for c in links]}
            if lv is not None:
                row["level"] = lv
            return row
    elif k == "set_door" and op.get("id") is not None and str(op["id"]) in doors:
        out = {}
        for key in ("x", "y", "width", "exterior_exit", "connects"):
            if key not in fields:
                continue
            if key in ("x", "y") and _num(fields[key]) is not None:
                out[key] = _num(fields[key])
            elif key == "width" and _width(fields[key]) is not None:
                out[key] = _width(fields[key])
            elif key == "exterior_exit":
                out[key] = bool(fields[key])
            elif key == "connects" and isinstance(fields[key], list):
                out[key] = [str(c) for c in fields[key]]
        if out:
            return {"op": k, "id": str(op["id"]), "fields": out}
    elif k == "move_door" and op.get("id") is not None and str(op["id"]) in doors:
        x, y = _num(op.get("x")), _num(op.get("y"))
        if x is not None and y is not None:
            return {"op": k, "id": str(op["id"]), "x": x, "y": y}
    elif k == "set_space" and op.get("id") is not None and str(op["id"]) in spaces:
        out = {key: str(fields[key]) for key in ("label", "type") if fields.get(key) is not None}
        if out:
            return {"op": k, "id": str(op["id"]), "fields": out}
    elif k == "add_space":
        lv = _opt_level(op, levels)
        poly = []
        for p in op.get("polygon") or []:
            if not isinstance(p, (list, tuple)) or len(p) < 2 or _num(p[0]) is None or _num(p[1]) is None:
                return None
            poly.append([_num(p[0]), _num(p[1])])
        if lv is not False and lv is not None and len(poly) >= 3 and op.get("label") is not None:
            return {"op": k, "id": str(op.get("id") or f"s-{lv}-{op.get('label')}"), "label": str(op.get("label")),
                    "type": str(op.get("type") or "room"), "level": lv, "polygon": poly}
    elif k == "delete" and op.get("kind") in ("door", "space", "wall", "vertical") and op.get("id") is not None:
        bucket = {"door": doors, "space": spaces, "wall": walls, "vertical": verts}[op["kind"]]
        if str(op["id"]) in bucket:
            return {"op": k, "kind": op["kind"], "id": str(op["id"])}
    elif k == "add_wall":
        c = [_num(op.get(key)) for key in ("x1", "y1", "x2", "y2")]
        lv = _opt_level(op, levels)
        if None not in c and lv is not False and math.hypot(c[2] - c[0], c[3] - c[1]) >= 0.05:
            row = {"op": k, "x1": c[0], "y1": c[1], "x2": c[2], "y2": c[3]}
            if lv is not None:
                row["level"] = lv
            return row
    elif k == "set_wall" and op.get("id") is not None and str(op["id"]) in walls:
        out = {}
        for key in ("x1", "y1", "x2", "y2"):
            if key in fields and _num(fields[key]) is not None:
                out[key] = _num(fields[key])
        if out:
            return {"op": k, "id": str(op["id"]), "fields": out}
    elif k == "add_vertical":
        typ = str(op.get("type") or "")
        floors, pos = _floor_list(op.get("floors"), levels), _positions(op.get("positions"), levels)
        if pos and floors:
            pos = {key: val for key, val in pos.items() if int(key) in floors}
        if typ in _TYPES and floors and pos:
            return {"op": k, "id": str(op.get("id") or "v-" + "-".join(str(n) for n in floors)),
                    "name": str(op.get("name") or typ), "type": typ, "floors": floors, "positions": pos}
    elif k == "set_vertical" and op.get("id") is not None and str(op["id"]) in verts:
        out = {}
        if fields.get("name") is not None:
            out["name"] = str(fields["name"])
        if str(fields.get("type") or "") in _TYPES:
            out["type"] = str(fields["type"])
        if "floors" in fields:
            floors = _floor_list(fields.get("floors"), levels)
            if not floors:
                return None
            out["floors"] = floors
        if "positions" in fields:
            pos = _positions(fields.get("positions"), levels)
            if pos and out.get("floors"):
                pos = {key: val for key, val in pos.items() if int(key) in out["floors"]}
            if not pos:
                return None
            out["positions"] = pos
        if out:
            return {"op": k, "id": str(op["id"]), "fields": out}
    elif k == "move_vertical" and op.get("id") is not None and str(op["id"]) in verts:
        try:
            x, y, lv = _num(op.get("x")), _num(op.get("y")), int(op["level"])
        except (TypeError, ValueError, KeyError):
            return None
        if x is not None and y is not None and lv in levels:
            return {"op": k, "id": str(op["id"]), "level": lv, "x": x, "y": y}
    return None


def _quick_ops(text, building, level):
    """Rule-based handling of the most common requests; returns a full response dict or None."""
    t = text.lower()
    fl = _floor(building, level)
    if fl is None:
        return None
    ops, what = [], ""
    removing = re.search(r"\b(remove|delete|get rid of)\b", t)
    m = re.search(r"\broom\s+(\d{3}[a-z]?)\b", t)
    if removing and m:
        num = m.group(1)
        for s in fl.get("spaces") or []:
            if str(s.get("label", "")).lower().split(" ")[0] == num:
                ops.append({"op": "delete", "kind": "space", "id": s["id"]})
        what = f"room {num}"
    for did in re.findall(r"\b(f\d+-[a-z]+\d+)\b", t):
        d = next((x for f_ in building.get("floors") or [] for x in f_.get("doors") or []
                  if str(x.get("id", "")).lower() == did), None)     # door ids carry their floor
        if not d:
            continue
        if re.search(r"\bnot\b.*\bexit\b|\bno longer\b.*\bexit\b", t):
            ops.append({"op": "set_door", "id": d["id"], "fields": {"exterior_exit": False}}); what = d["id"]
        elif re.search(r"\b(make|set|mark)\b.*\bexit\b", t):
            ops.append({"op": "set_door", "id": d["id"], "fields": {"exterior_exit": True}}); what = d["id"]
        elif removing:
            ops.append({"op": "delete", "kind": "door", "id": d["id"]}); what = d["id"]
    if removing and "exit" in t and not ops:
        ctx = compact_context(building, level, [])
        v = next((w for w in ("top", "bottom", "middle") if re.search(rf"\b{w}\b", t)), None)
        h = next((w for w in ("left", "right", "center") if re.search(rf"\b{w}\b", t)), None)
        for d in ctx["doors"]:
            if not d.get("exterior_exit"):
                continue
            wv, wh = (d.get("where") or "-").split("-")
            if (v is None or v == wv) and (h is None or h == wh):
                ops.append({"op": "set_door", "id": d["id"], "fields": {"exterior_exit": False}})
        what = f"exits on floor {level}" + (f" ({v or ''} {h or ''})".replace("  ", " ") if v or h else "")
    if not ops:
        return None
    return {"ops": ops, "summary": f"{len(ops)} edit(s) for {what}", "reply": f"Ready: {len(ops)} edit(s). Review and apply.",
            "model": "rules"}


def _floor_in_text(text, building, default):
    words = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
             "ground": 1, "top": None}
    t = text.lower()
    levels = sorted(int(f.get("level")) for f in (building.get("floors") or []) if f.get("level") is not None)
    m = re.search(r"\b(?:floor|level)\s*(\d+)\b", t) or re.search(r"\b(\d+)(?:st|nd|rd|th)?\s+(?:floor|level)\b", t)
    if m and int(m.group(1)) in levels:
        return int(m.group(1))
    for w, lv in words.items():
        if re.search(rf"\b{w}\s+(?:floor|level)\b", t):
            lv = levels[-1] if lv is None and levels else lv
            if lv in levels:
                return lv
    for num in re.findall(r"\b\d{3}[a-z]?\b", t):       # a room number: use the floor that has that room
        hits = [int(f["level"]) for f in building.get("floors") or [] for s in f.get("spaces") or []
                if str(s.get("label", "")).lower().split(" ")[0] == num
                and "lower tier" not in str(s.get("label", "")).lower()]
        if hits:
            return default if default in hits else hits[0]
    return default


def _generate(client, model, contents):
    from google.genai import types
    kw = dict(response_mime_type="application/json", temperature=0.1, max_output_tokens=2048)
    try:                                   # no "thinking" pass: much faster replies for small edits
        kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except Exception:
        pass
    config = types.GenerateContentConfig(**kw)
    for attempt in range(3):  # first try, then retry 503/429 twice
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            return (getattr(resp, "text", None) or "").strip()
        except Exception as e:
            if getattr(e, "code", None) not in (429, 503) or attempt == 2:
                raise
    return ""


def _recent_history(history):
    lines = []
    for turn in list(history or [])[-10:]:
        if isinstance(turn, dict):
            role, text = turn.get("role"), turn.get("text")
        else:
            role, text = getattr(turn, "role", None), getattr(turn, "text", None)
        if role in ("user", "assistant") and isinstance(text, str) and text.strip():
            lines.append(role + ": " + text.strip())
    return lines


_PROMPT = (
    'Return JSON {"ops":[],"summary":"","reply":""} only. Do not apply. reply is one short sentence. '
    "If unsure, ask in reply and use ops []. Frame: meters, x grows right, y grows DOWN (small y = top). "
    "where is top/middle/bottom plus left/center/right. "
    "rooms[number] is [id, level, centroid, point], or a list of those if several rooms share the number. "
    "point is inside the room, closest to the other room named in the instruction, otherwise the centroid. "
    "Use those points in add_vertical positions for stairs between rooms. One op per item. Use ids from the context. "
    "delete kind is door, space, wall, or vertical. vertical type is stairs, ramp, or elevator.\n"
    'add_door {"op":"add_door","level":3,"x":1,"y":2,"width":0.9,"exterior_exit":false,"connects":["a"]}\n'
    'set_door {"op":"set_door","id":"F3-D25","fields":{"exterior_exit":false}}\n'
    'move_door {"op":"move_door","id":"F3-D25","x":1,"y":2}\n'
    'set_space {"op":"set_space","id":"F3-S308","fields":{"label":"308","type":"room"}}\n'
    'add_space {"op":"add_space","id":"F2-S1","label":"Closet","type":"room","level":2,"polygon":[[0,0],[2,0],[2,2]]}\n'
    'delete {"op":"delete","kind":"vertical","id":"V1"}\n'
    'add_wall {"op":"add_wall","level":2,"x1":0,"y1":0,"x2":4,"y2":0}\n'
    'set_wall {"op":"set_wall","id":"F2-W1","fields":{"x2":5,"y2":0}}\n'
    'add_vertical {"op":"add_vertical","id":"V308","name":"Stairs 308-232","type":"stairs","floors":[3,2],"positions":{"3":[1,2],"2":[1.2,2.2]}}\n'
    'set_vertical {"op":"set_vertical","id":"V308","fields":{"name":"West stairs","type":"stairs"}}\n'
    'move_vertical {"op":"move_vertical","id":"V308","level":2,"x":1,"y":2}\n'
)


def propose(instruction, level, building, selection=None, client=None, history=None):
    if not (instruction or "").strip():
        raise HTTPException(400, "instruction is required")
    if _floor(building, level) is None:
        raise HTTPException(400, f"unknown floor {level}")
    model = os.environ.get("GEMINI_EDIT_MODEL") or "gemini-3.8-flash"
    focus = _floor_in_text(instruction, building, level)
    quick = _quick_ops(instruction, building, focus)
    if quick:                                       # common commands: instant, no model call
        return quick
    if client is None:
        if not os.environ.get("GEMINI_API_KEY"):
            raise HTTPException(503, "GEMINI_API_KEY is not set")
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                              http_options=types.HttpOptions(api_version="v1beta"))
    ctx = _model_context(building, selection, instruction, focus)
    turns = _recent_history(history)
    hist = ("Earlier turns:\n" + "\n".join(turns) + "\n") if turns else ""
    prompt = f"{_PROMPT}{hist}Instruction: {instruction.strip()}\nContext: {json.dumps(ctx, separators=(',', ':'))}"
    try:
        raw = _generate(client, model, prompt)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        data = json.loads(raw)
    except Exception as e:
        raise HTTPException(502, f"Gemini edit failed: {e}") from e
    ops = data if isinstance(data, list) else (data.get("ops") or [])
    summary = "" if isinstance(data, list) else str(data.get("summary") or "")
    reply = "" if isinstance(data, list) else str(data.get("reply") or "")
    cleaned = [c for c in (_clean(o, building) for o in ops) if c]
    if not reply:
        reply = summary or ("Review these changes." if cleaned else "No edits.")
    return {"ops": cleaned, "summary": summary, "reply": reply, "model": model}


@router.post("/api/ai/edit")
def ai_edit(body: EditBody):
    return propose(body.instruction, body.level, body.building, body.selection, history=body.history)
