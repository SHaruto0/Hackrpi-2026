"""Propose map edits. Does not apply them. POST /api/ai/edit -> {ops, summary, reply, model}."""
import json
import math
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
_NEAR = 3.0


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


def _dist(px, py, x1, y1, x2, y2):
    dx, dy, l2 = x2 - x1, y2 - y1, (x2 - x1) ** 2 + (y2 - y1) ** 2
    if l2 < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_near(c, other):
    mid = ((other[0] + other[2]) / 2, (other[1] + other[3]) / 2)
    return min(_dist(c[0], c[1], *other), _dist(c[2], c[3], *other), _dist(mid[0], mid[1], *c)) <= _NEAR


def _floor(building, level):
    for fl in (building or {}).get("floors") or []:
        try:
            if int(fl.get("level")) == int(level):
                return fl
        except (TypeError, ValueError):
            continue
    return None


def compact_context(building, level, selection):
    """One floor. Walls only within 3 m of the selection, never the whole wall list."""
    fl, sel = _floor(building, level) or {}, {str(s) for s in (selection or [])}
    spaces, doors, verts, anchors, picked, details = [], [], [], [], [], []
    for s in fl.get("spaces") or []:
        pts = _pts(s.get("polygon"))
        c = [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)] if pts else None
        row = {"id": s.get("id"), "label": s.get("label"), "type": s.get("type"), "centroid": c}
        spaces.append(row)
        if str(s.get("id")) in sel:
            details.append({"kind": "space", **row})
            if c:
                anchors.append(tuple(c))
    for d in fl.get("doors") or []:
        row = {"id": d.get("id"), "x": _num(d.get("x")), "y": _num(d.get("y")), "width": _num(d.get("width")),
               "exterior_exit": bool(d.get("exterior_exit")), "connects": [str(c) for c in (d.get("connects") or [])]}
        doors.append(row)
        if str(d.get("id")) in sel:
            details.append({"kind": "door", **row})
            if row["x"] is not None and row["y"] is not None:
                anchors.append((row["x"], row["y"]))
    for v in (building or {}).get("vertical_connections") or []:
        floors, posmap = [], v.get("positions") or {}
        for lv in v.get("floors") or []:
            try:
                floors.append(int(lv))
            except (TypeError, ValueError):
                pass
        raw = posmap.get(str(level), posmap.get(level))
        pos = [_num(raw[0]), _num(raw[1])] if isinstance(raw, (list, tuple)) and len(raw) >= 2 else [_num(v.get("x")), _num(v.get("y"))]
        if int(level) not in floors and not isinstance(raw, (list, tuple)):
            continue
        row = {"id": v.get("id"), "type": v.get("type"), "position": pos}
        verts.append(row)
        if str(v.get("id")) in sel and None not in pos:
            details.append({"kind": "vertical", **row})
            anchors.append(tuple(pos))
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
        if not (any(_dist(p[0], p[1], *c) <= _NEAR for p in anchors) or any(_seg_near(c, s) for s in picked)):
            continue
        item = {"id": wid, "x1": c[0], "y1": c[1], "x2": c[2], "y2": c[3]}
        walls.append(item)
        if str(wid) in sel:
            details.append({"kind": "wall", **item})
    box = _pts(fl.get("outline"))
    bbox = ({"min_x": min(p[0] for p in box), "min_y": min(p[1] for p in box),
             "max_x": max(p[0] for p in box), "max_y": max(p[1] for p in box)} if box else None)
    return {"level": int(level), "outline_bbox": bbox, "spaces": spaces, "doors": doors,
            "vertical_connections": verts, "walls_near_selection": walls, "selection": details}


def _width(v):
    w = _num(v)
    return w if w is not None and 0.6 <= w <= 3.0 else None


def _clean(op):
    if not isinstance(op, dict):
        return None
    k, fields = op.get("op"), op.get("fields") if isinstance(op.get("fields"), dict) else {}
    if k == "add_door":
        x, y, w = _num(op.get("x")), _num(op.get("y")), _width(op.get("width"))
        links = op.get("connects") if isinstance(op.get("connects"), list) else []
        if None not in (x, y, w):
            return {"op": k, "x": x, "y": y, "width": w, "exterior_exit": bool(op.get("exterior_exit", False)),
                    "connects": [str(c) for c in links]}
    elif k == "set_door" and op.get("id"):
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
    elif k == "set_space" and op.get("id"):
        out = {key: str(fields[key]) for key in ("label", "type") if fields.get(key) is not None}
        if out:
            return {"op": k, "id": str(op["id"]), "fields": out}
    elif k == "delete" and op.get("kind") in ("door", "space", "wall") and op.get("id"):
        return {"op": k, "kind": op["kind"], "id": str(op["id"])}
    elif k == "add_wall":
        c = [_num(op.get(key)) for key in ("x1", "y1", "x2", "y2")]
        if None not in c and math.hypot(c[2] - c[0], c[3] - c[1]) >= 0.05:
            return {"op": k, "x1": c[0], "y1": c[1], "x2": c[2], "y2": c[3]}
    elif k == "move_vertical" and op.get("id") is not None:
        try:
            x, y, lv = _num(op.get("x")), _num(op.get("y")), int(op["level"])
        except (TypeError, ValueError, KeyError):
            return None
        if x is not None and y is not None:
            return {"op": k, "id": str(op["id"]), "level": lv, "x": x, "y": y}
    return None


def _generate(client, model, contents):
    from google.genai import types
    kw = dict(response_mime_type="application/json", temperature=0.1, max_output_tokens=1024)
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


def propose(instruction, level, building, selection=None, client=None, history=None):
    if not (instruction or "").strip():
        raise HTTPException(400, "instruction is required")
    if _floor(building, level) is None:
        raise HTTPException(400, f"unknown floor {level}")
    model = os.environ.get("GEMINI_EDIT_MODEL") or "gemini-3.1-flash-lite"   # fast model for interactive chat
    if client is None:
        if not os.environ.get("GEMINI_API_KEY"):
            raise HTTPException(503, "GEMINI_API_KEY is not set")
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                              http_options=types.HttpOptions(api_version="v1beta"))
    ctx = compact_context(building, level, selection)
    turns = _recent_history(history)
    hist = ("Earlier turns:\n" + "\n".join(turns) + "\n") if turns else ""
    prompt = (
        'Return JSON {"ops":[],"summary":"","reply":""} only. Do not apply. '
        "reply is a short natural-language answer. "
        "If no map edit is needed, return an empty ops list and answer in reply. "
        'Each item needs "op": '
        "add_door {x,y,width,exterior_exit,connects}; set_door {id,fields}; "
        "set_space {id,fields: label|type}; delete {kind: door|space|wall, id}; "
        "add_wall {x1,y1,x2,y2}; move_vertical {id,level,x,y}. Use ids from the context.\n"
        f"{hist}Instruction: {instruction.strip()}\nContext: {json.dumps(ctx, separators=(',', ':'))}"
    )
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
    cleaned = [c for c in (_clean(o) for o in ops) if c]
    if not reply:
        reply = summary or ("Review these changes." if cleaned else "No edits.")
    return {"ops": cleaned, "summary": summary, "reply": reply, "model": model}


@router.post("/api/ai/edit")
def ai_edit(body: EditBody):
    return propose(body.instruction, body.level, body.building, body.selection, history=body.history)
