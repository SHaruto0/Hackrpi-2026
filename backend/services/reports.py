"""Firefighter field reports: speech or text becomes map updates.

Fish Audio ASR (never log the key):
    FISH_API_KEY   required for POST /api/voice/stt and /api/reports/audio.
                   Missing key or a failed call -> 503.

Snowflake Cortex is optional. When SNOWFLAKE_ACCOUNT and SNOWFLAKE_PAT are
set, parse() asks Cortex for the same JSON list and uses it only to fill
gaps. Rules always run. A Cortex failure leaves the rule result as-is.

Wire-up (before the static `/` mount in server.py):

    from .services.reports import make_router as reports_router
    app.include_router(reports_router(lambda: world, lock))
"""
import copy
import json
import os
import re
import time
import uuid
from collections import defaultdict

import httpx
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from . import store

_ASR_URL = "https://api.fish.audio/v1/asr"
_ASR_MODEL = "transcribe-1-pro"
_ASR_TIMEOUT = 30.0
_BLOCK_RADIUS = 1.5
_SMOKE_LIGHT = 0.5
_SMOKE_HEAVY = 0.9
_STAIR_ROOM_M = 6.0
_IGNITE_CLEAR_M = 1.05
_MAX_REPORTS = 200
_MAX_UNDO = 50
_MISSING = object()

_KINDS = ("fire", "smoke", "searched", "clear", "blocked", "open", "victim", "note")
_GRID_KINDS = {"fire", "smoke", "clear", "blocked", "open"}
_SKIP_SEARCH = {"stairwell", "elevator", "void"}
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_STOP = {
    "the", "a", "an", "in", "on", "at", "to", "of", "is", "it", "there",
    "its", "and", "or", "for", "with", "from", "by", "be",
}

_MARK = re.compile(r"<\|[^|>]*\|>")
_SENT = re.compile(r"[.!?]+|\n+")
_CLAUSE = re.compile(r"[,;]|\band\b", re.IGNORECASE)
_FLOOR = re.compile(r"\b(?:floor|level)\s+(\d+)\b")
_CODE = re.compile(r"(?<![a-z0-9])(\d{2,4}[a-z]?|\d[a-z]{2,4}\d{1,3})(?![a-z0-9])")
_COUNT = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:victims?|persons?|people|trapped)\b"
)


def _smoke_value(level):
    """Float32 smoke that stays >= the requested level (0.5 or 0.9)."""
    stored = np.float32(level)
    if float(stored) < float(level):
        stored = np.nextafter(stored, np.float32(1.0))
    return stored


class ReportIn(BaseModel):
    text: str
    by: str | None = None
    dry_run: bool = False


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _api_key():
    return os.environ.get("FISH_API_KEY", "").strip()


def _vision_tools():
    try:
        from .vision import match_label, normalize_token
    except Exception:
        return None, None
    return match_label, normalize_token


def _token(text, normalize_token):
    if normalize_token is not None:
        return normalize_token(text)
    return re.sub(r"[^A-Z0-9.]", "", str(text or "").upper())


def _unique(items):
    out = []
    seen = set()
    for item in items:
        key = item.get("id")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _vertical_aliases(name, vid):
    aliases = set()
    for raw in (name, vid):
        n = _norm(raw)
        if not n:
            continue
        aliases.add(n)
        if n.endswith(" stairs"):
            base = n[:-7].strip()
            aliases.add(n[:-1])  # singular stair
            if base:
                aliases.add(base + " stairwell")
                aliases.add(base + " staircase")
        elif n.endswith(" stair"):
            base = n[:-6].strip()
            aliases.add(n + "s")
            if base:
                aliases.add(base + " stairwell")
                aliases.add(base + " staircase")
        m = re.fullmatch(r"stair ([a-z0-9]+)", n)
        if m:
            token = m.group(1)
            aliases.add(f"{token} stairs")
            aliases.add(f"{token} stair")
            aliases.add(f"stairwell {token}")
            aliases.add(f"stairs {token}")
        if "elevator" in n:
            aliases.add("elevator")
            aliases.add("elevators")
    drop = {"stair", "stairs", "stairwell", "staircase", "room"}
    return [a for a in aliases if a not in drop and (len(a) >= 4 or any(ch.isdigit() for ch in a))]


def _space_aliases(label, space_id, space_type, code):
    aliases = set()
    label_n = _norm(label)
    if len(label_n) >= 3:
        aliases.add(label_n)
    if code and any(ch.isdigit() for ch in code) and len(code) >= 2:
        low = code.lower()
        aliases.add(low)
        aliases.add(f"room {low}")
        aliases.add(f"rm {low}")
        if space_type == "stairwell":
            aliases.add(f"stairwell {low}")
            aliases.add(f"{low} stair")
            aliases.add(f"{low} stairs")
    id_n = _norm(space_id)
    if len(id_n) >= 4:
        aliases.add(id_n)
    return [a for a in aliases if a not in {"room", "stair", "stairs", "stairwell"}]


class _Catalog:
    """Room and stair names the rule parser can point at."""

    def __init__(self, model):
        self.match_label, normalize_token = _vision_tools()
        self.spaces = []
        self.verticals = []
        self.valid_ids = set()
        self.by_code = defaultdict(list)
        self.space_alias = defaultdict(list)
        self.vert_alias = defaultdict(list)

        for floor in (model or {}).get("floors") or []:
            try:
                level = int(floor.get("level"))
            except (TypeError, ValueError):
                continue
            for space in floor.get("spaces") or []:
                sid = space.get("id")
                if not sid:
                    continue
                label = str(space.get("label") or sid)
                first = label.split()[0] if label.split() else ""
                code = _token(first, normalize_token)
                target = {
                    "id": sid,
                    "label": label,
                    "level": level,
                    "where": "room",
                    "type": space.get("type") or "unknown",
                }
                self.spaces.append(target)
                self.valid_ids.add(sid)
                if code and any(ch.isdigit() for ch in code):
                    self.by_code[code].append(target)
                for alias in _space_aliases(label, sid, target["type"], code):
                    self.space_alias[alias].append(target)

        for vert in (model or {}).get("vertical_connections") or []:
            vid = vert.get("id")
            if not vid:
                continue
            name = str(vert.get("name") or vid)
            target = {
                "id": vid,
                "label": name,
                "level": None,
                "where": "vertical",
                "type": vert.get("type") or "stairs",
            }
            self.verticals.append({"target": target, "raw": vert})
            self.valid_ids.add(vid)
            for alias in _vertical_aliases(name, vid):
                self.vert_alias[alias].append(target)

    def _fuzzy(self, token):
        if self.match_label is None or len(token) < 3:
            return []
        hits = []
        seen = set()
        for key, group in self.by_code.items():
            if self.match_label(token, key) != "fuzzy":
                continue
            for target in group:
                if target["id"] in seen:
                    continue
                seen.add(target["id"])
                hits.append(target)
        if len(hits) == 1:
            return hits
        return []

    def _by_code(self, token, level):
        hits = list(self.by_code.get(token) or [])
        if not hits:
            hits = self._fuzzy(token)
        return self._prefer_level(hits, level)

    def _prefer_level(self, targets, level):
        targets = _unique(targets)
        if level is None:
            return targets
        rooms = [t for t in targets if t["where"] == "room"]
        verts = [t for t in targets if t["where"] == "vertical"]
        on = [t for t in rooms if t["level"] == level]
        return _unique(verts + (on or rooms))

    def find(self, raw, level):
        norm = _norm(raw)
        if not norm:
            return []
        hay = f" {norm} "
        cands = []

        def add(alias, priority, targets):
            targets = _unique(targets)
            if not alias or not targets:
                return
            needle = f" {alias} "
            start = 0
            while True:
                i = hay.find(needle, start)
                if i < 0:
                    return
                cands.append((i, i + len(alias), len(alias), priority, targets))
                start = i + max(1, len(alias))

        for alias, targets in self.vert_alias.items():
            add(alias, 3, targets)
        for alias, targets in self.space_alias.items():
            add(alias, 2, targets)
        for match in _CODE.finditer(norm):
            targets = self._by_code(_token(match.group(1), _vision_tools()[1]), level)
            if targets:
                cands.append((match.start(), match.end(), match.end() - match.start(), 1, targets))

        chosen = []
        for cand in sorted(cands, key=lambda c: (c[2], c[3], -c[0]), reverse=True):
            s, e = cand[0], cand[1]
            if any(s < o[1] and e > o[0] for o in chosen):
                continue
            chosen.append(cand)

        out = []
        for cand in sorted(chosen, key=lambda c: c[0]):
            out.extend(self._prefer_level(cand[4], level))
        # A stair name wins over the stairwell rooms that share that name.
        vert_labels = {_norm(t["label"]) for t in out if t["where"] == "vertical"}
        if vert_labels:
            out = [
                t for t in out
                if t["where"] == "vertical" or _norm(t["label"]) not in vert_labels
            ]
        return _unique(out)


def _kinds(raw):
    text = _norm(raw)
    kinds = []
    if re.search(r"\b(?:all clear|no fire|cleared|clear)\b", text):
        kinds.append("clear")
    scrub = re.sub(r"\b(?:all clear|no fire)\b", " ", text)
    if re.search(r"\b(?:fires?|flames?|burning)\b", scrub):
        kinds.append("fire")
    if re.search(r"\bsmoke\b", text):
        kinds.append("smoke")
    if re.search(r"\b(?:searched|checked|primary done)\b", text):
        kinds.append("searched")
    if re.search(r"\b(?:blocked|collapsed|impassable|locked)\b", text) or re.search(r"\bnot (?:open|passable)\b", text):
        kinds.append("blocked")
    if "blocked" not in kinds and re.search(r"\b(?:open|passable)\b", text):
        kinds.append("open")
    if re.search(r"\b(?:victims?|persons?|people|trapped)\b", text):
        kinds.append("victim")
    if re.search(r"\b(?:heavy|thick)\b", text):
        severity = "heavy"
    elif re.search(r"\b(?:light|some)\b", text):
        severity = "light"
    else:
        severity = None
    count = _victim_count(text) if "victim" in kinds else None
    return kinds, severity, count


def _victim_count(text):
    for match in _COUNT.finditer(text):
        tok = match.group(1)
        number = int(tok) if tok.isdigit() else _NUM_WORDS[tok]
        if tok.isdigit() and number > 20:
            continue
        return number
    return 1


def _floor_only(raw):
    return bool(re.fullmatch(r"(?:floor|level)\s+\d+", _norm(raw)))


def _meaningful(raw):
    return any(word not in _STOP for word in _norm(raw).split())


def _nearest(i, indexes):
    if not indexes:
        return None
    return min(indexes, key=lambda j: (abs(j - i), j > i))


def _ids(targets):
    return [t["id"] for t in targets]


def _inherit(clauses):
    """Share a room across the clauses of one sentence.

    "Room 102 searched, all clear" and "fire in 318 and 204" both need it.
    """
    for clause in clauses:
        clause["orig_targets"] = list(clause["targets"])
    located = [i for i, clause in enumerate(clauses) if clause["orig_targets"]]
    for i, clause in enumerate(clauses):
        if clause["targets"] or not clause["kinds"]:
            continue
        src = _nearest(i, located)
        if src is None:
            continue
        clause["targets"] = list(clauses[src]["targets"])

    kinded = [i for i, clause in enumerate(clauses) if clause["kinds"] and clause["orig_targets"]]
    for i, clause in enumerate(clauses):
        if clause["kinds"] or not clause["targets"]:
            continue
        src_i = _nearest(i, kinded)
        if src_i is None:
            continue
        src = clauses[src_i]
        if _ids(clause["targets"]) == _ids(src["orig_targets"]):
            continue
        _copy_kinds(clause, src)

    donors = [i for i, clause in enumerate(clauses) if clause["kinds"] and not clause["orig_targets"]]
    for i, clause in enumerate(clauses):
        if clause["kinds"] or not clause["orig_targets"]:
            continue
        src_i = _nearest(i, donors)
        if src_i is None:
            continue
        _copy_kinds(clause, clauses[src_i])


def _copy_kinds(dest, src):
    dest["kinds"] = list(src["kinds"])
    if dest["severity"] is None:
        dest["severity"] = src["severity"]
    if dest["count"] is None:
        dest["count"] = src["count"]


def _emit(clauses):
    updates = []
    for clause in clauses:
        kinds = [kind for kind in _KINDS if kind in clause["kinds"]]
        targets = clause["targets"] or [None]
        if not kinds:
            if targets != [None] or _meaningful(clause["raw"]):
                if targets == [None] and not _meaningful(clause["raw"]):
                    continue
                for target in targets:
                    updates.append(_make_update("note", target, clause))
            continue
        for target in targets:
            for kind in kinds:
                updates.append(_make_update(kind, target, clause))
    return _dedupe(updates)


def _make_update(kind, target, clause):
    if target is None:
        level = clause["level"]
        target_id = None
        label = None
    elif target["where"] == "vertical":
        level = clause["level"]
        target_id = target["id"]
        label = target["label"]
    else:
        level = target["level"]
        target_id = target["id"]
        label = target["label"]
    return {
        "kind": kind,
        "target_id": target_id,
        "target_label": label,
        "level": level,
        "severity": clause["severity"] if kind == "smoke" else None,
        "count": clause["count"] if kind == "victim" else None,
        "raw": clause["raw"],
    }


def _dedupe(updates):
    out = []
    index = {}
    for update in updates:
        key = (update["kind"], update["target_id"])
        if key not in index:
            index[key] = len(out)
            out.append(update)
            continue
        prev = out[index[key]]
        if prev.get("severity") is None and update.get("severity"):
            prev["severity"] = update["severity"]
        if prev.get("count") is None and update.get("count") is not None:
            prev["count"] = update["count"]
        if not prev.get("target_label") and update.get("target_label"):
            prev["target_label"] = update["target_label"]
    return out


def _as_model(model):
    if isinstance(model, dict):
        return model
    inner = getattr(model, "model", None)
    if isinstance(inner, dict):
        return inner
    return {}


def _rule_parse(text, model):
    catalog = _Catalog(_as_model(model))
    updates = []
    level = None
    for sentence in _SENT.split(text or ""):
        clauses = []
        for piece in _CLAUSE.split(sentence):
            raw = " ".join(piece.split())
            if not raw:
                continue
            floor = _FLOOR.search(_norm(raw))
            if floor:
                level = int(floor.group(1))
            if _floor_only(raw):
                continue
            kinds, severity, count = _kinds(raw)
            clauses.append({
                "raw": raw,
                "kinds": kinds,
                "severity": severity,
                "count": count,
                "level": level,
                "targets": catalog.find(raw, level),
            })
        if not clauses:
            continue
        _inherit(clauses)
        updates.extend(_emit(clauses))
    return _dedupe(updates), catalog


def _catalog_lines(catalog, text):
    norm = _norm(text)
    lines = []
    for vert in catalog.verticals:
        target = vert["target"]
        lines.append(f"{target['id']}|{target['label']}|vertical")
    for target in catalog.spaces:
        label_n = _norm(target["label"])
        if label_n and label_n in norm:
            lines.append(f"{target['id']}|{target['label']}|level {target['level']}")
            continue
        for word in label_n.split():
            if any(ch.isdigit() for ch in word) and re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", norm):
                lines.append(f"{target['id']}|{target['label']}|level {target['level']}")
                break
        if len(lines) >= 80:
            break
    return lines[:80]


def _json_list(text):
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = data.get("updates") or data.get("report") or []
    return data if isinstance(data, list) else []


def _coerce(item, valid_ids):
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in _KINDS:
        return None
    target_id = item.get("target_id")
    if target_id is not None:
        target_id = str(target_id).strip() or None
    if target_id and target_id not in valid_ids:
        target_id = None
    severity = item.get("severity")
    if severity not in ("light", "heavy"):
        severity = None
    if kind != "smoke":
        severity = None
    count = item.get("count")
    if kind == "victim":
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 1
    else:
        count = None
    level = item.get("level")
    try:
        level = int(level) if level is not None else None
    except (TypeError, ValueError):
        level = None
    label = item.get("target_label")
    label = str(label).strip() if label else None
    raw = item.get("raw")
    raw = " ".join(str(raw).split()) if raw else ""
    return {
        "kind": kind,
        "target_id": target_id,
        "target_label": label,
        "level": level,
        "severity": severity,
        "count": count,
        "raw": raw,
    }


def _cortex_parse(text, catalog):
    """Ask Cortex for the same JSON list. Empty when it is off or it fails."""
    try:
        from . import cortex
    except Exception:
        return []
    try:
        if not cortex._configured():
            return []
    except Exception:
        return []
    lines = _catalog_lines(catalog, text)
    prompt = (
        "Parse this firefighter radio report into a JSON array only. "
        "Each item has kind, target_id, target_label, level, severity, count, raw. "
        "kind is one of fire, smoke, clear, searched, blocked, open, victim, note. "
        "severity is light, heavy, or null. count is an integer or null. "
        "target_id must be copied from the list or null. No markdown.\n"
        "Targets:\n" + "\n".join(lines) + "\nReport:\n" + text
    )
    try:
        raw = cortex._cortex_chat([
            {"role": "system", "content": "You return strict JSON only."},
            {"role": "user", "content": prompt},
        ])
    except Exception:
        return []
    out = []
    for item in _json_list(raw):
        update = _coerce(item, catalog.valid_ids)
        if update is not None:
            out.append(update)
    return out


def _merge(rule, extra):
    """Rules win. Cortex only fills a missing target, or the whole list if rules found nothing."""
    actionable = [u for u in rule if u["kind"] != "note"]
    if not actionable:
        return extra or rule
    if not extra:
        return rule
    by_kind = defaultdict(list)
    for update in extra:
        by_kind[update["kind"]].append(update)
    for update in rule:
        if update.get("target_id"):
            continue
        for cand in by_kind.get(update["kind"]) or []:
            if not cand.get("target_id"):
                continue
            update["target_id"] = cand["target_id"]
            update["target_label"] = update.get("target_label") or cand.get("target_label")
            if update.get("level") is None:
                update["level"] = cand.get("level")
            if update.get("severity") is None:
                update["severity"] = cand.get("severity")
            if update.get("count") is None:
                update["count"] = cand.get("count")
            break
    return rule


def parse(text, model):
    """Rule parse of a radio report. Optional Cortex merge when configured.

    Each update is {kind, target_id, target_label, level, severity, count, raw}.
    kind is fire, smoke, clear, searched, blocked, open, victim, or note.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return []
    rule, catalog = _rule_parse(text, model)
    try:
        extra = _cortex_parse(text, catalog)
    except Exception:
        extra = []
    return _merge(rule, extra)


def _changed_cells(before, after):
    """Previous values at cells this report changed. None when the grid shape changed."""
    if before.shape != after.shape:
        return None
    fis, rs, cs = np.nonzero(before != after)
    if len(fis) == 0:
        return []
    vals = before[fis, rs, cs].tolist()
    return [
        (int(fi), int(r), int(c), val)
        for fi, r, c, val in zip(fis.tolist(), rs.tolist(), cs.tolist(), vals)
    ]


def _searched_delta(before, after):
    """room_id -> previous record, or None when this report created the key."""
    delta = {}
    for key in set(before) | set(after):
        old = before.get(key, _MISSING)
        new = after.get(key, _MISSING)
        if old == new:
            continue
        delta[key] = None if old is _MISSING else copy.deepcopy(old)
    return delta


def _origin_key(origin):
    return (
        origin.get("level"),
        round(float(origin.get("x") or 0.0), 6),
        round(float(origin.get("y") or 0.0), 6),
    )


def _origins_delta(before, after):
    """Origins this report added, and origins it removed."""
    def group(items):
        bags = defaultdict(list)
        for origin in items:
            bags[_origin_key(origin)].append(copy.deepcopy(origin))
        return bags

    old = group(before)
    new = group(after)
    added = []
    removed = []
    for key in set(old) | set(new):
        prev = old.get(key, [])
        nxt = new.get(key, [])
        if len(nxt) > len(prev):
            added.extend(nxt[len(prev):])
        elif len(prev) > len(nxt):
            removed.extend(prev[len(nxt):])
    return added, removed


def _build_inverse(before, world):
    fire = _changed_cells(before["fire"], world.sim.fire)
    smoke = _changed_cells(before["smoke"], world.sim.smoke)
    blocked = _changed_cells(before["blocked"], world.blocked)
    added, removed = _origins_delta(before["origins"], world.sim.origins)
    bad = fire is None or smoke is None or blocked is None
    return {
        "bad": bad,
        "fire": fire or [],
        "smoke": smoke or [],
        "blocked": blocked or [],
        "searched": _searched_delta(before["searched"], world.searched),
        "origins_added": added,
        "origins_removed": removed,
    }


def _remember(world, report_id, inv):
    stack = list(getattr(world, "_report_undo", None) or [])
    live = {report["id"] for report in world.reports}
    stack = [item for item in stack if item[0] in live]
    stack.append((report_id, inv))
    if len(stack) > _MAX_UNDO:
        stack = stack[-_MAX_UNDO:]
    world._report_undo = stack


def _recall(world, report_id):
    stack = list(getattr(world, "_report_undo", None) or [])
    found = None
    for i in range(len(stack) - 1, -1, -1):
        if stack[i][0] == report_id:
            found = stack.pop(i)[1]
            break
    world._report_undo = stack
    return found


def _cells_fit(arr, cells):
    nfi, nr, nc = arr.shape
    for fi, r, c, _val in cells:
        if fi < 0 or r < 0 or c < 0 or fi >= nfi or r >= nr or c >= nc:
            return False
    return True


def _inverse_fits(world, inv):
    if not isinstance(inv, dict) or inv.get("bad"):
        return False
    pairs = (
        (world.sim.fire, inv.get("fire") or []),
        (world.sim.smoke, inv.get("smoke") or []),
        (world.blocked, inv.get("blocked") or []),
    )
    return all(_cells_fit(arr, cells) for arr, cells in pairs)


def _write_cells(arr, cells):
    for fi, r, c, val in cells:
        arr[fi, r, c] = val


def _pull_origin(origins, target):
    key = _origin_key(target)
    for i, origin in enumerate(origins):
        try:
            if _origin_key(origin) == key:
                origins.pop(i)
                return
        except (TypeError, ValueError):
            continue


def _apply_inverse(world, inv):
    _write_cells(world.sim.fire, inv.get("fire") or [])
    _write_cells(world.sim.smoke, inv.get("smoke") or [])
    _write_cells(world.blocked, inv.get("blocked") or [])
    for key, prev in (inv.get("searched") or {}).items():
        if prev is None:
            world.searched.pop(key, None)
        else:
            world.searched[key] = copy.deepcopy(prev)
    origins = world.sim.origins
    for added in inv.get("origins_added") or []:
        _pull_origin(origins, added)
    for removed in inv.get("origins_removed") or []:
        origins.append(copy.deepcopy(removed))


def _room_mask(grid, info):
    fi = grid.level_index[info["level"]]
    mask = (grid.room[fi] == info["index"]) & grid.walkable[fi]
    return fi, mask


def _disk(grid, fi, r, c, radius):
    cx, cy = grid.center_of(r, c)
    mask = (np.hypot(grid.X - cx, grid.Y - cy) <= radius) & grid.walkable[fi]
    if 0 <= r < grid.rows and 0 <= c < grid.cols:
        mask = mask.copy()
        mask[r, c] = bool(grid.walkable[fi, r, c])
    return mask


def _vertical(world, vid):
    for vert in world.model.get("vertical_connections") or []:
        if vert.get("id") == vid:
            return vert
    return None


def _resolve(world, update):
    target_id = update.get("target_id")
    if not target_id:
        return None, None
    info = world.grid.room_by_id.get(target_id)
    if info is not None:
        return "room", info
    vert = _vertical(world, target_id)
    if vert is not None:
        return "vertical", vert
    return None, None


def _stair_rooms(world, vertical):
    name = _norm(vertical.get("name") or "")
    named = []
    for info in world.grid.rooms:
        if info.get("type") != "stairwell":
            continue
        if name and _norm(info.get("label") or "") == name:
            named.append(info)
    if named:
        return named
    positions = vertical.get("positions") or {}
    best = {}
    for info in world.grid.rooms:
        if info.get("type") != "stairwell":
            continue
        level = info.get("level")
        pos = positions.get(level)
        if pos is None:
            pos = positions.get(str(level)) if level is not None else None
        if pos is None:
            pos = (vertical.get("x"), vertical.get("y"))
        centroid = info.get("centroid")
        if not pos or not centroid or pos[0] is None or pos[1] is None:
            continue
        dist = float(np.hypot(centroid[0] - pos[0], centroid[1] - pos[1]))
        if dist > _STAIR_ROOM_M:
            continue
        prev = best.get(level)
        if prev is None or dist < prev[0]:
            best[level] = (dist, info)
    return [pair[1] for pair in best.values()]


def _masks(world, where, obj, kind):
    """Cells an update paints. Vertical block/smoke covers every floor."""
    acc = {}

    def add(fi, mask):
        if mask is None or not np.any(mask):
            return
        if fi in acc:
            acc[fi] |= mask
        else:
            acc[fi] = np.array(mask, copy=True)

    if where == "room":
        fi, mask = _room_mask(world.grid, obj)
        if kind == "clear":
            cell = world.grid.nearest_walkable(
                fi, *obj["centroid"], max_dist=50.0, mask=(world.grid.room[fi] == obj["index"]),
            )
            if cell is not None:
                mask = mask | _disk(world.grid, fi, cell[0], cell[1], _IGNITE_CLEAR_M)
        add(fi, mask)
        return acc

    cells = world.grid.vertical_cells.get(obj["id"]) or {}
    if kind in ("smoke", "clear", "fire"):
        for info in _stair_rooms(world, obj):
            add(*_room_mask(world.grid, info))
    if kind in ("blocked", "open", "smoke", "clear"):
        for fi, r, c in cells.values():
            add(fi, _disk(world.grid, fi, r, c, _BLOCK_RADIUS))
    return acc


def _label(update):
    return update.get("target_label") or update.get("target_id") or "unknown"


def _drop_origins(world, fi, mask):
    grid = world.grid
    level = grid.levels[fi]
    kept = []
    for origin in world.sim.origins:
        if origin.get("level") != level:
            kept.append(origin)
            continue
        r, c = grid.cell_of(origin.get("x", 0), origin.get("y", 0))
        if 0 <= r < grid.rows and 0 <= c < grid.cols and mask[r, c]:
            continue
        kept.append(origin)
    world.sim.origins[:] = kept


def _paint(world, where, obj, kind, update, by):
    label = _label(update)
    if kind == "fire" and where == "room":
        try:
            ok = world.sim.ignite_room(obj["id"])
        except KeyError:
            ok = False
        return [f"Fire in {label}"] if ok else [f"Could not place fire in {label}"]
    if kind == "fire" and where == "vertical":
        cells = world.grid.vertical_cells.get(obj["id"]) or {}
        placed = 0
        for fi, r, c in cells.values():
            x, y = world.grid.center_of(r, c)
            if world.sim.ignite_point(fi, x, y):
                placed += 1
        if not placed:
            return [f"Could not place fire in {label}"]
        return [f"Fire in {label}"]
    if kind == "searched":
        world.searched[obj["id"]] = {
            "by": by,
            "t": time.time(),
            "note": update.get("raw") or "",
        }
        return [f"{label} searched"]

    masks = _masks(world, where, obj, kind)
    if kind == "smoke":
        level = _SMOKE_HEAVY if update.get("severity") == "heavy" else _SMOKE_LIGHT
        value = _smoke_value(level)
        for fi, mask in masks.items():
            world.sim.smoke[fi][mask] = np.maximum(world.sim.smoke[fi][mask], value)
        if update.get("severity") == "heavy":
            return [f"Heavy smoke in {label}"]
        if update.get("severity") == "light":
            return [f"Light smoke in {label}"]
        return [f"Smoke in {label}"]
    if kind == "clear":
        for fi, mask in masks.items():
            world.sim.fire[fi][mask] = False
            world.sim.smoke[fi][mask] = np.float32(0)
            world.blocked[fi][mask] = False
            _drop_origins(world, fi, mask)
        return [f"{label} clear"]
    if kind == "blocked":
        for fi, mask in masks.items():
            world.blocked[fi][mask] = True
        if not masks:
            return [f"Could not block {label}"]
        return [f"{label} blocked"]
    if kind == "open":
        for fi, mask in masks.items():
            world.blocked[fi][mask] = False
        return [f"{label} open"]
    return []


def _apply_one(world, update, by):
    kind = update.get("kind")
    label = _label(update)
    if kind == "note":
        text = update.get("raw") or label
        return [f"Note: {text}"]
    if kind == "victim":
        count = update.get("count") or 1
        noun = "victim" if count == 1 else "victims"
        if update.get("target_id"):
            return [f"{count} {noun} in {label}"]
        return [f"{count} {noun} reported"]
    if kind not in _GRID_KINDS and kind != "searched":
        return []
    where, obj = _resolve(world, update)
    if where is None:
        return [f"No location for {label}"]
    return _paint(world, where, obj, kind, update, by)


def apply(world, updates, by, text=None):
    """Apply updates, append one report, refresh routes once.

    Returns human-readable change lines. Undo stores the previous value of
    each cell and key this report changed.
    """
    by = "" if by is None else str(by).strip()
    updates = [dict(item) for item in (updates or [])]
    if text is None:
        text = ". ".join(item.get("raw") or "" for item in updates if item.get("raw"))
    else:
        text = str(text)
    if not updates:
        world.refresh_routes()
        return []

    before = {
        "fire": world.sim.fire.copy(),
        "smoke": world.sim.smoke.copy(),
        "blocked": world.blocked.copy(),
        "searched": copy.deepcopy(world.searched),
        "origins": copy.deepcopy(world.sim.origins),
    }
    changes = []
    for update in updates:
        changes.extend(_apply_one(world, update, by))
    inv = _build_inverse(before, world)
    world.refresh_routes()
    changes.append("Routes updated")
    report = {
        "id": uuid.uuid4().hex[:8],
        "t": time.time(),
        "by": by,
        "text": text,
        "updates": copy.deepcopy(updates),
        "changes": list(changes),
    }
    world.reports.append(report)
    overflow = len(world.reports) - _MAX_REPORTS
    if overflow > 0:
        del world.reports[:overflow]
    _remember(world, report["id"], inv)
    try:
        store.log("field_report", {
            "id": report["id"],
            "by": by,
            "text": text,
            "changes": list(changes),
            "building": (world.model or {}).get("building"),
        })
    except Exception:
        pass
    return changes


def undo(world):
    """Reverse the newest report and refresh routes. {ok, changes}.

    Writes back only the cells and keys that report changed, so later fire
    steps stay on the grid.
    """
    if not world.reports:
        return {"ok": False, "changes": ["No report to undo"]}
    last = world.reports[-1]
    inv = _recall(world, last["id"])
    if inv is None:
        world.reports.pop()
        world.refresh_routes()
        return {"ok": True, "changes": [f"Removed report {last['id']}", "Routes updated"]}
    if not _inverse_fits(world, inv):
        _remember(world, last["id"], inv)
        return {"ok": False, "changes": ["Could not undo the last report"]}
    _apply_inverse(world, inv)
    world.reports.pop()
    world.refresh_routes()
    return {"ok": True, "changes": [f"Undid report {last['id']}", "Routes updated"]}


def reset(world):
    """Clear reports, searched rooms, blocked cells, and the undo log.

    Does not reset the fire simulation. POST /api/fire/reset should call
    this and then refresh routes.
    """
    world.reports.clear()
    world.searched.clear()
    world.blocked[:] = False
    world._report_undo = []


def _bit(update):
    kind = update.get("kind")
    if kind == "smoke":
        if update.get("severity") == "heavy":
            return "heavy smoke"
        if update.get("severity") == "light":
            return "light smoke"
        return "smoke"
    if kind == "fire":
        return "on fire"
    if kind == "victim":
        count = update.get("count") or 1
        return "1 victim" if count == 1 else f"{count} victims"
    return kind or "noted"


def confirmation(updates, applied=True):
    """Short radio read-back. Routes updated only after a real apply."""
    groups = []
    notes = []
    for update in updates or []:
        if update.get("kind") == "note":
            raw = " ".join(str(update.get("raw") or "").split())
            if len(raw) > 80:
                raw = raw[:77] + "..."
            if raw:
                notes.append(raw)
            continue
        label = update.get("target_label") or "area"
        key = (update.get("target_id"), label)
        bit = _bit(update)
        if groups and groups[-1][0] == key:
            if bit not in groups[-1][2]:
                groups[-1][2].append(bit)
        else:
            groups.append([key, label, [bit]])
    parts = []
    for (_key, label, bits) in groups:
        if bits == ["1 victim"] or (len(bits) == 1 and bits[0].endswith("victims")):
            parts.append(bits[0] if label == "area" else f"{bits[0]} in {label}")
        else:
            parts.append(f"{label} " + ", ".join(bits))
    parts.extend(notes)
    body = ". ".join(parts) if parts else "Nothing to add"
    text = f"Copy. {body}."
    if applied and updates:
        text += " Routes updated."
    return text


def transcribe(content, filename="audio.webm", content_type="application/octet-stream"):
    """Send audio to Fish ASR. 503 when FISH_API_KEY is missing or the call fails."""
    key = _api_key()
    if not key:
        raise HTTPException(503, "FISH_API_KEY is not set")
    if not content:
        raise HTTPException(400, "audio is empty")
    headers = {
        "Authorization": f"Bearer {key}",
        "model": _ASR_MODEL,
    }
    files = {
        "audio": (filename or "audio.webm", content, content_type or "application/octet-stream"),
    }
    data = {
        "language": "en",
        "ignore_timestamps": "true",
        "tag_audio_events": "false",
    }
    try:
        resp = httpx.post(_ASR_URL, headers=headers, files=files, data=data, timeout=_ASR_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(503, "Fish Audio transcription timed out")
    except httpx.HTTPError:
        raise HTTPException(503, "Fish Audio transcription failed")
    if resp.status_code != 200:
        raise HTTPException(503, "Fish Audio transcription failed")
    try:
        body = resp.json()
    except Exception:
        raise HTTPException(503, "Fish Audio transcription failed")
    if not isinstance(body, dict):
        raise HTTPException(503, "Fish Audio transcription failed")
    heard = _MARK.sub(" ", str(body.get("text") or ""))
    heard = " ".join(heard.split())
    return {"text": heard, "duration": body.get("duration")}


def _empty_report(by, text):
    return {
        "id": None,
        "t": None,
        "by": by,
        "text": text,
        "updates": [],
        "changes": [],
    }


def _commit(world, updates, by, text):
    if not updates:
        return [], _empty_report(by, text)
    changes = apply(world, updates, by, text)
    report = copy.deepcopy(world.reports[-1]) if world.reports else _empty_report(by, text)
    return changes, report


def _submit(text, by, dry_run, get_world, lock):
    text = " ".join(str(text or "").split())
    if not text:
        raise HTTPException(400, "text is required")
    by = "" if by is None else str(by).strip()
    with lock:
        model = get_world().model
    # Cortex can take tens of seconds. Parse outside the lock, then only copy and apply under it.
    updates = parse(text, model)
    if dry_run:
        report = {
            "id": None,
            "t": None,
            "by": by,
            "text": text,
            "updates": updates,
            "changes": [],
        }
        return {
            "report": report,
            "changes": [],
            "confirmation": confirmation(updates, applied=False),
        }
    for _attempt in range(8):
        with lock:
            world = get_world()
            if world.model is model:
                changes, report = _commit(world, updates, by, text)
                break
            model = world.model
        updates = parse(text, model)
    else:
        with lock:
            changes, report = _commit(get_world(), updates, by, text)
    return {
        "report": report,
        "changes": changes,
        "confirmation": confirmation(updates, applied=bool(updates)),
    }


def _searched_payload(world):
    body = {rid: dict(rec) for rid, rec in world.searched.items()}
    pending = []
    for info in world.grid.rooms:
        if info.get("type") in _SKIP_SEARCH:
            continue
        if info["id"] in world.searched:
            continue
        pending.append({
            "id": info["id"],
            "label": info.get("label") or info["id"],
            "level": info.get("level"),
        })
    body["unsearched"] = pending
    return body


def make_router(get_world, lock) -> APIRouter:
    router = APIRouter()

    @router.post("/api/voice/stt")
    def voice_stt(audio: UploadFile = File(...)):
        data = audio.file.read()
        return transcribe(data, audio.filename or "audio.webm", audio.content_type or "application/octet-stream")

    @router.post("/api/reports")
    def post_report(body: ReportIn):
        return _submit(body.text, body.by, bool(body.dry_run), get_world, lock)

    @router.post("/api/reports/audio")
    def post_report_audio(audio: UploadFile = File(...), by: str | None = Form(None)):
        data = audio.file.read()
        heard = transcribe(data, audio.filename or "audio.webm", audio.content_type or "application/octet-stream")
        return _submit(heard["text"], by, False, get_world, lock)

    @router.get("/api/reports")
    def list_reports():
        with lock:
            rows = copy.deepcopy(get_world().reports)
        rows.reverse()
        return rows

    @router.get("/api/reports/searched")
    def searched():
        with lock:
            return _searched_payload(get_world())

    @router.post("/api/reports/undo")
    def post_undo():
        with lock:
            return undo(get_world())

    return router
