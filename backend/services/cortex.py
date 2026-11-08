"""Snowflake Cortex commander briefing and building Q&A.

Env (never log or return these):
    SNOWFLAKE_ACCOUNT  account identifier, e.g. orgname-acct
    SNOWFLAKE_PAT      programmatic access token
    SNOWFLAKE_MODEL    default claude-sonnet-4-5

Missing account or token: GET /api/brief uses a fixed template. POST /api/ask
returns 503. A failed Cortex call falls back the same way for the brief.

Wire-up (before the static `/` mount in server.py):

    from .services.cortex import make_router as cortex_router
    app.include_router(cortex_router(lambda: world, lock))
"""
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import occupants

_DEFAULT_MODEL = "claude-sonnet-4-5"
_TIMEOUT_S = 30.0
_MAX_TOKENS = 600
_SKIP_ROUTE = {"stairwell", "elevator", "void"}
_STAIR_TYPES = {"stairs", "ramp"}
_COUNT_ORDER = ("evacuating", "trapped", "safe")
_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "where", "what", "who", "how",
    "to", "of", "in", "on", "for", "and", "or", "from", "with", "please", "show",
    "me", "do", "does", "any", "there", "which", "that", "this", "tell", "about",
    "my", "our", "can", "you", "it", "at", "be", "by", "as", "room", "rooms",
    "floor", "floors", "building",
}
_SYSTEM = (
    "You are Firesight, helping a fire commander. "
    "Use ONLY the given facts. If a fact is missing, say Unknown. "
    "Never invent people or positions. "
    "Fire data is simulated. Say so when you talk about the fire. "
    "Give the reason for each route recommendation and what it avoids. "
    "Keep the answer under 120 words."
)


class CortexError(Exception):
    """Cortex is unconfigured or the request did not yield text. Never carries secrets."""


class AskIn(BaseModel):
    question: str


def _account():
    return os.environ.get("SNOWFLAKE_ACCOUNT", "").strip()


def _token():
    return os.environ.get("SNOWFLAKE_PAT", "").strip()


def _safe_account(account):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", account or ""))


def _configured():
    return _safe_account(_account()) and bool(_token())


def _model_name():
    name = os.environ.get("SNOWFLAKE_MODEL", "").strip()
    return name or _DEFAULT_MODEL


def _people_snapshot():
    with occupants.people_lock:
        src = occupants.people
        return {
            pid: dict(row)
            for pid, row in src.items()
            if isinstance(row, dict)
        }


def _phrase(room):
    label = room.get("label") or room.get("id") or "unknown"
    level = room.get("level")
    rid = room.get("id")
    text = str(label) if level is None else f"{label} floor {level}"
    if rid and str(rid) != str(label):
        text = f"{text} [{rid}]"
    return text


def _join_phrases(rooms):
    if not rooms:
        return "none"
    return "; ".join(_phrase(room) for room in rooms)


def _and_join(parts):
    parts = [part for part in parts if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _rooms_with_status(world, status_map, want):
    out = []
    seen = set()
    for info in world.grid.rooms:
        if status_map.get(info["id"]) != want:
            continue
        out.append({
            "id": info["id"],
            "label": info.get("label") or info["id"],
            "level": info.get("level"),
        })
        seen.add(info["id"])
    for rid, status in status_map.items():
        if status == want and rid not in seen:
            out.append({"id": rid, "label": rid, "level": None})
    return out


def _blocked_stairs(world):
    by_id = {v["id"]: v for v in world.model.get("vertical_connections") or []}
    out = []
    for vid, status in world.sim.vertical_status().items():
        if status == "clear":
            continue
        meta = by_id.get(vid) or {}
        vtype = meta.get("type") or "stairs"
        if vtype == "elevator" or vtype not in _STAIR_TYPES:
            continue
        out.append({
            "id": vid,
            "name": meta.get("name") or vid,
            "type": vtype,
            "status": status,
            "floors": list(meta.get("floors") or []),
        })
    return out


def _exits(world):
    grid = world.grid
    out = []
    for fi, row, col, door_id in grid.exits:
        x, y = grid.center_of(row, col)
        out.append({
            "id": door_id,
            "level": grid.levels[fi],
            "x": round(float(x), 2),
            "y": round(float(y), 2),
        })
    out.sort(key=lambda item: (item["level"], str(item["id"])))
    return out


def _people_parts(world, people):
    counts = {name: 0 for name in _COUNT_ORDER}
    trapped = []
    by_id = world.grid.room_by_id
    rows = people.values() if isinstance(people, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        if status != "trapped":
            continue
        room_id = row.get("room_id")
        info = by_id.get(room_id) if room_id else None
        if info is not None:
            label = info.get("label") or room_id
            level = row.get("level")
            if level is None:
                level = info.get("level")
        else:
            label = room_id
            level = row.get("level")
        trapped.append({
            "person_id": row.get("person_id"),
            "room_id": room_id,
            "room_label": label,
            "level": level,
        })
    trapped.sort(key=lambda item: str(item.get("person_id") or ""))
    return counts, trapped


def _route_at_room(world, info):
    centroid = info.get("centroid")
    level = info.get("level")
    grid = world.grid
    if not centroid or len(centroid) < 2 or level not in grid.level_index:
        return {"reachable": False, "reason": "not on a walkable area", "exit": None, "distance_m": None}
    from ..core.routing import best_room_cell
    fi, cell = best_room_cell(world.field, info)          # any reachable part of the room counts
    if cell is None:
        return world.field.route_from(grid.level_index[level], float(centroid[0]), float(centroid[1]))
    return world.field.route_from(fi, *grid.center_of(*cell))


def _no_safe_route(world):
    out = []
    for info in world.grid.rooms:
        if info.get("type") in _SKIP_ROUTE:
            continue
        route = _route_at_room(world, info)
        if route.get("reachable"):
            continue
        out.append({
            "id": info["id"],
            "label": info.get("label") or info["id"],
            "level": info.get("level"),
            "reason": route.get("reason") or "no safe route",
        })
    return out


def situation(world, people):
    """Facts a briefing is allowed to use. No network."""
    status = world.sim.room_status()
    counts, trapped = _people_parts(world, people)
    return {
        "t": world.sim.t,
        "fire_rooms": _rooms_with_status(world, status, "fire"),
        "smoke_rooms": _rooms_with_status(world, status, "smoke"),
        "blocked_stairs": _blocked_stairs(world),
        "exits": _exits(world),
        "people_counts": counts,
        "trapped": trapped,
        "no_safe_route": _no_safe_route(world),
    }


def situation_text(sit):
    """One fact per line. Compact plain text for the model."""
    counts = sit.get("people_counts") or {}
    count_bits = []
    seen = set()
    for name in _COUNT_ORDER:
        count_bits.append(f"{counts.get(name, 0)} {name}")
        seen.add(name)
    for name in sorted(counts):
        if name not in seen:
            count_bits.append(f"{counts[name]} {name}")
    trapped_bits = []
    for person in sit.get("trapped") or []:
        who = person.get("person_id") or "unknown"
        label = person.get("room_label") or person.get("room_id") or "unknown room"
        level = person.get("level")
        place = str(label) if level is None else f"{label} floor {level}"
        trapped_bits.append(f"{who} in {place}")
    blocked_bits = [
        f"{item.get('name') or item.get('id')} ({item.get('status')})"
        for item in sit.get("blocked_stairs") or []
    ]
    exit_bits = [
        f"{item.get('id')} floor {item.get('level')}"
        for item in sit.get("exits") or []
    ]
    lines = [
        f"Step: {sit.get('t', 0)}",
        "Fire data is simulated.",
        "Fire rooms: " + _join_phrases(sit.get("fire_rooms") or []),
        "Smoke rooms: " + _join_phrases(sit.get("smoke_rooms") or []),
        "Blocked stairs: " + ("; ".join(blocked_bits) if blocked_bits else "none"),
        "Exits: " + ("; ".join(exit_bits) if exit_bits else "none"),
        "People: " + ", ".join(count_bits),
        "Trapped: " + ("; ".join(trapped_bits) if trapped_bits else "none"),
        "No safe route: " + _join_phrases(sit.get("no_safe_route") or []),
    ]
    return "\n".join(lines)


def template_brief(sit):
    """Deterministic commander briefing when Cortex is not available."""
    fires = sit.get("fire_rooms") or []
    smokes = sit.get("smoke_rooms") or []
    blocked = sit.get("blocked_stairs") or []
    exits = sit.get("exits") or []
    counts = sit.get("people_counts") or {}
    trapped = sit.get("trapped") or []
    cut_off = sit.get("no_safe_route") or []
    sentences = [f"Commander brief, step {sit.get('t', 0)}. Fire data is simulated."]
    if fires:
        sentences.append("Fire: " + _join_phrases(fires) + ".")
    else:
        sentences.append("Fire: none.")
    if smokes:
        sentences.append("Smoke: " + _join_phrases(smokes) + ".")
    else:
        sentences.append("Smoke: none.")
    if blocked:
        bits = [f"{item.get('name') or item.get('id')} ({item.get('status')})" for item in blocked]
        sentences.append("Blocked stairs: " + _and_join(bits) + ". Do not send people that way.")
    else:
        sentences.append("Blocked stairs: none.")
    if exits:
        bits = [f"{item.get('id')} on floor {item.get('level')}" for item in exits]
        sentences.append("Exits: " + _and_join(bits) + ".")
    else:
        sentences.append("Exits: none marked.")
    sentences.append(
        "People: {evacuating} evacuating, {trapped} trapped, {safe} safe.".format(
            evacuating=counts.get("evacuating", 0),
            trapped=counts.get("trapped", 0),
            safe=counts.get("safe", 0),
        )
    )
    if trapped:
        bits = []
        for person in trapped:
            who = person.get("person_id") or "unknown"
            label = person.get("room_label") or person.get("room_id") or "unknown room"
            level = person.get("level")
            if level is None:
                bits.append(f"{who} in {label}")
            else:
                bits.append(f"{who} in {label} on floor {level}")
        sentences.append("Trapped: " + _and_join(bits) + ".")
    else:
        sentences.append("Trapped: none.")
    if cut_off:
        sentences.append("No safe route from: " + _join_phrases(cut_off) + ".")
    else:
        sentences.append("Every checked room has a safe route.")
    avoid = []
    if fires:
        avoid.append("rooms on fire")
    if smokes:
        avoid.append("smoke")
    if blocked:
        avoid.append("blocked stairs")
    if exits:
        dest = "Send people toward " + _and_join(str(item.get("id")) for item in exits)
    else:
        dest = "Send people to the nearest exit"
    if avoid:
        sentences.append(dest + ", avoiding " + _and_join(avoid) + ". Elevators are not for evacuation.")
    else:
        sentences.append(dest + ". Elevators are not for evacuation.")
    return " ".join(sentences)


def _doors_by_room(world):
    out = {}
    for floor in world.model.get("floors") or []:
        for door in floor.get("doors") or []:
            door_id = door.get("id")
            if not door_id:
                continue
            for room_id in door.get("connects") or []:
                out.setdefault(str(room_id), []).append(str(door_id))
    for door_ids in out.values():
        door_ids.sort()
    return out


def _door_tokens(doors):
    out = {}
    for room_id, door_ids in doors.items():
        tokens = set()
        for door_id in door_ids:
            low = door_id.lower()
            tokens.add(low)
            tokens.update(re.findall(r"[a-z0-9]+", low))
        out[room_id] = tokens
    return out


def _exit_room_ids(world):
    ids = set()
    for floor in world.model.get("floors") or []:
        for door in floor.get("doors") or []:
            if not door.get("exterior_exit"):
                continue
            for room_id in door.get("connects") or []:
                ids.add(str(room_id))
    return ids


def _question_tokens(question):
    raw = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", (question or "").lower())
    out = []
    seen = set()
    for token in raw:
        parts = [token]
        if "-" in token:
            parts.extend(token.split("-"))
        for part in parts:
            if not part or part in seen or part in _STOP:
                continue
            if len(part) < 2 and not part.isdigit():
                continue
            seen.add(part)
            out.append(part)
    return out


def _score_room(info, tokens, door_tokens):
    label = (info.get("label") or "").lower()
    rid = (info.get("id") or "").lower()
    kind = (info.get("type") or "").lower()
    label_tokens = set(re.findall(r"[a-z0-9]+", label))
    id_tokens = set(re.findall(r"[a-z0-9]+", rid))
    score = 0
    level = info.get("level")
    for token in tokens:
        if token == label or token == rid:
            score += 6
        elif token in label_tokens or token in id_tokens:
            score += 4
        elif token == kind:
            score += 3
        elif len(token) >= 3 and (token in label or token in rid or token in kind):
            score += 2
        if token in door_tokens:
            score += 5
        if token.isdigit() and level is not None and token == str(level):
            score += 1
    return score


def _room_record(world, info, status, doors):
    route = _route_at_room(world, info)
    if route.get("reachable"):
        nearest = route.get("exit")
        length = route.get("distance_m")
    else:
        nearest = None
        length = None
    return {
        "id": info["id"],
        "label": info.get("label") or info["id"],
        "level": info.get("level"),
        "type": info.get("type"),
        "doors": list(doors.get(info["id"], [])),
        "status": status.get(info["id"], "clear"),
        "nearest_exit": nearest,
        "route_length_m": length,
    }


def retrieve(world, question, k=8):
    """Rooms relevant to a question. No network.

    Room numbers and label words rank first. Questions that mention stairs or
    exits also include those rooms, up to k.
    """
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 8
    if k <= 0:
        return []
    tokens = _question_tokens(question)
    text = (question or "").lower()
    mentions_stairs = bool(re.search(r"\bstairs?\b|\bstairwells?\b|\bstaircases?\b", text))
    mentions_exits = bool(re.search(r"\bexits?\b|\begress\b|\bway out\b", text))
    doors = _doors_by_room(world)
    door_tokens = _door_tokens(doors)
    status = world.sim.room_status()
    scored = []
    for info in world.grid.rooms:
        if info.get("type") == "void":
            continue
        score = _score_room(info, tokens, door_tokens.get(info["id"], set()))
        if score > 0:
            scored.append((score, str(info["id"]), info))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ordered = [info for _, _, info in scored]
    seen = {info["id"] for info in ordered}
    stair_rooms = sorted(
        (info for info in world.grid.rooms if info.get("type") == "stairwell"),
        key=lambda info: str(info["id"]),
    )
    by_id = world.grid.room_by_id
    exit_rooms = []
    for room_id in sorted(_exit_room_ids(world)):
        info = by_id.get(room_id)
        if info is not None and info.get("type") != "void":
            exit_rooms.append(info)
    if mentions_stairs:
        for info in stair_rooms:
            if info["id"] not in seen:
                ordered.append(info)
                seen.add(info["id"])
    if mentions_exits:
        for info in exit_rooms:
            if info["id"] not in seen:
                ordered.append(info)
                seen.add(info["id"])
    if mentions_stairs or mentions_exits:
        must_ids = set()
        if mentions_stairs:
            must_ids.update(info["id"] for info in stair_rooms)
        if mentions_exits:
            must_ids.update(info["id"] for info in exit_rooms)
        must = [info for info in ordered if info["id"] in must_ids]
        rest = [info for info in ordered if info["id"] not in must_ids]
        if len(must) >= k:
            chosen = must[:k]
        else:
            chosen = rest[: k - len(must)] + must
    else:
        chosen = ordered[:k]
    return [_room_record(world, info, status, doors) for info in chosen]


def _rooms_context(rooms):
    if not rooms:
        return "Relevant rooms: none"
    lines = ["Relevant rooms:"]
    for room in rooms:
        doors = ", ".join(room.get("doors") or []) or "none"
        nearest = room.get("nearest_exit") or "none"
        length = room.get("route_length_m")
        route = "none" if length is None else f"{length} m"
        lines.append(
            f"{room.get('id')} | {room.get('label')} | floor {room.get('level')} | "
            f"{room.get('type')} | doors {doors} | status {room.get('status')} | "
            f"nearest exit {nearest} | route {route}"
        )
    return "\n".join(lines)


def _messages_for_brief(sit, style):
    rules = _SYSTEM
    if style == "radio":
        rules += " Phrase the briefing as terse radio traffic."
    return [
        {"role": "system", "content": rules},
        {"role": "user", "content": "Write the briefing from these facts.\n" + situation_text(sit)},
    ]


def _messages_for_ask(sit, rooms, question):
    context = situation_text(sit) + "\n\n" + _rooms_context(rooms)
    return [
        {"role": "system", "content": _SYSTEM + "\n\n" + context},
        {"role": "user", "content": question},
    ]


def _cortex_chat(messages):
    """POST one non-streaming Cortex chat completion. Raises CortexError on failure."""
    account = _account()
    token = _token()
    if not _safe_account(account) or not token:
        raise CortexError("not configured")
    url = f"https://{account}.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"
    payload = {
        "model": _model_name(),
        "messages": [{"role": item["role"], "content": item["content"]} for item in messages],
        "max_completion_tokens": _MAX_TOKENS,
        "temperature": 0.2,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        raise CortexError("request failed") from None
    if not isinstance(content, str) or not content.strip():
        raise CortexError("empty response")
    return content.strip()


def make_router(get_world, lock) -> APIRouter:
    router = APIRouter()

    def _snapshot():
        people = _people_snapshot()
        with lock:
            world = get_world()
            sit = situation(world, people)
        return sit

    @router.get("/api/brief")
    def brief(style: str = Query("commander")):
        style = (style or "commander").strip().lower()
        if style not in ("commander", "radio"):
            raise HTTPException(400, "style must be commander or radio")
        sit = _snapshot()
        text = None
        model = "template"
        if _configured():
            try:
                text = _cortex_chat(_messages_for_brief(sit, style))
                model = _model_name()
            except CortexError:
                text = None
                model = "template"
        if not text:
            text = template_brief(sit)
            model = "template"
        return {"text": text, "model": model, "facts": sit}

    @router.post("/api/ask")
    def ask(body: AskIn):
        question = (body.question or "").strip()
        if not question:
            raise HTTPException(400, "question is required")
        if not _configured():
            raise HTTPException(503, "Snowflake Cortex is not configured")
        people = _people_snapshot()
        with lock:
            world = get_world()
            sit = situation(world, people)
            rooms = retrieve(world, question)
        try:
            answer = _cortex_chat(_messages_for_ask(sit, rooms, question))
        except CortexError:
            raise HTTPException(503, "Snowflake Cortex request failed")
        return {
            "answer": answer,
            "model": _model_name(),
            "sources": [room["id"] for room in rooms],
        }

    return router
