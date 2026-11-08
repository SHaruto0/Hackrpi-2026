"""Door-number OCR and matching onto a loaded building model.

read_text runs RapidOCR on a JPEG (CPU, models bundled with the package).
match_rooms links those strings to spaces. Coordinates stay in the plan frame
(x right, y down), the same frame as world.model.
"""
import logging
import re
import threading

import cv2
import numpy as np

log = logging.getLogger(__name__)

LONG_SIDE = 960
FUZZY_PENALTY = 0.3
EXIT_SCORE = 0.5

_LEAD_FLOOR = re.compile(r"^F\d+-")
_WORD_ROOM = re.compile(r"\b(?:ROOM|RM)\b")
_GLUED_ROOM = re.compile(r"(?:ROOM|RM)(?=\d)")
_SUFFIX_ROOM = re.compile(r"(?<=\d)(?:ROOM|RM)")
_ID_SUFFIX = re.compile(r"^F\d+-(.+)$", re.IGNORECASE)

_engine = None
_engine_missing = False
_engine_lock = threading.Lock()


def normalize_token(text) -> str:
    """Uppercase, drop RM/ROOM, strip spaces, turn O into 0 inside digit runs.

    A leading floor id prefix (F3-318) is removed so the room number remains.
    Dots stay, so labels like 134C.02 still match.
    """
    s = str(text if text is not None else "").upper().strip()
    if not s:
        return ""
    s = _LEAD_FLOOR.sub("", s)
    s = _WORD_ROOM.sub(" ", s)
    s = _GLUED_ROOM.sub("", s)
    s = _SUFFIX_ROOM.sub("", s)
    s = re.sub(r"[^A-Z0-9.]", "", s)
    return _o_to_zero(s)


def match_label(token, label):
    """Compare a token to the room-number part of a space label.

    The room number is the first word: "318" in "318 CLASS LEC", "1ST1" in
    "1ST1 STAIR". Returns "exact", "fuzzy" (one character different), or None.
    """
    room = _room_number(label)
    if not room:
        return None
    keys = _token_keys(token)
    if room in keys:
        return "exact"
    if any(_one_char_off(key, room) for key in keys):
        return "fuzzy"
    return None


def read_text(jpeg_bytes) -> list:
    """OCR a JPEG. Each hit is {text, conf, box} with box [x0, y0, x1, y1].

    The long side is scaled to at most 960 px before recognition. Boxes are
    mapped back to the original image. A missing OCR package returns [] and
    logs one warning.
    """
    engine = _get_engine()
    if engine is None:
        return []
    img = _decode(jpeg_bytes)
    if img is None:
        return []
    small, sx, sy = _resize_long(img, LONG_SIDE)
    try:
        with _engine_lock:
            raw = engine(small)
    except Exception:
        log.exception("OCR failed")
        return []
    rows = raw[0] if isinstance(raw, tuple) else raw
    if not rows:
        return []
    hits = []
    for row in rows:
        parsed = _parse_row(row, sx, sy)
        if parsed is not None:
            hits.append(parsed)
    return hits


def match_rooms(tokens, model) -> list:
    """Match OCR tokens to spaces in a loaded building model.

    Each hit is {room_id, level, label, points, score}. points are door
    centers whose connects include the room id, or the room polygon centroid
    when the room has no door. score is the token confidence, minus 0.3 when
    the match is one character off. The token EXIT matches every exterior
    door (room_id "EXIT", score 0.5), grouped by floor.
    """
    if not tokens or not model:
        return []
    rooms, exact, extra, buckets, exits = _catalog(model)
    found = []
    for tok in tokens:
        text, conf = _token_parts(tok)
        keys = _token_keys(text)
        if not keys:
            continue
        if "EXIT" in keys:
            for level in sorted(exits):
                pts = exits[level]
                if not pts:
                    continue
                found.append({
                    "room_id": "EXIT",
                    "level": level,
                    "label": "EXIT",
                    "points": [p[:] for p in pts],
                    "score": EXIT_SCORE,
                })
        idxs = _lookup_exact(keys, exact, extra)
        fuzzy = False
        if not idxs:
            idxs = _lookup_fuzzy(keys, buckets)
            fuzzy = bool(idxs)
        if not idxs:
            continue
        score = conf if not fuzzy else max(0.0, conf - FUZZY_PENALTY)
        for idx in idxs:
            room = rooms[idx]
            found.append({
                "room_id": room["id"],
                "level": room["level"],
                "label": room["label"],
                "points": [p[:] for p in room["points"]],
                "score": score,
            })
    return found


def annotate(jpeg_bytes, tokens) -> bytes:
    """Draw each token box and its text onto the JPEG. Returns JPEG bytes."""
    img = _decode(jpeg_bytes)
    if img is None:
        return _as_bytes(jpeg_bytes)
    if not tokens:
        return _as_bytes(jpeg_bytes)
    height, width = img.shape[:2]
    for tok in tokens:
        if not isinstance(tok, dict):
            continue
        box = tok.get("box")
        if not box or len(box) < 4:
            continue
        x0, y0, x1, y1 = (int(round(float(v))) for v in box[:4])
        x0 = max(0, min(width - 1, x0))
        x1 = max(0, min(width - 1, x1))
        y0 = max(0, min(height - 1, y0))
        y1 = max(0, min(height - 1, y1))
        cv2.rectangle(img, (x0, y0), (x1, y1), (40, 180, 40), 2)
        text = str(tok.get("text") or "")
        baseline = max(y0 - 6, 16)
        cv2.putText(
            img, text, (x0, baseline), cv2.FONT_HERSHEY_SIMPLEX,
            0.6, (0, 0, 220), 2, cv2.LINE_AA,
        )
    ok, buf = cv2.imencode(".jpg", img)
    if not ok:
        return _as_bytes(jpeg_bytes)
    return buf.tobytes()


def _o_to_zero(text: str) -> str:
    """Replace O with 0 in runs that already contain a digit (3O8 -> 308)."""
    chars = list(text)
    n = len(chars)
    i = 0
    while i < n:
        if chars[i].isdigit() or chars[i] == "O":
            j = i
            has_digit = False
            while j < n and (chars[j].isdigit() or chars[j] == "O"):
                if chars[j].isdigit():
                    has_digit = True
                j += 1
            if has_digit:
                for k in range(i, j):
                    if chars[k] == "O":
                        chars[k] = "0"
            i = j
        else:
            i += 1
    return "".join(chars)


def _room_number(label) -> str:
    parts = str(label if label is not None else "").upper().split()
    if not parts:
        return ""
    return normalize_token(parts[0])


def _id_suffix_key(space_id) -> str:
    text = str(space_id if space_id is not None else "").strip()
    m = _ID_SUFFIX.match(text)
    return normalize_token(m.group(1) if m else text)


def _token_keys(text) -> list:
    raw = str(text if text is not None else "").upper().strip()
    if not raw:
        return []
    keys = []
    full = normalize_token(raw)
    if full:
        keys.append(full)
    first = raw.split()[0]
    if first != raw:
        key = normalize_token(first)
        if key and key not in keys:
            keys.append(key)
    return keys


def _one_char_off(a: str, b: str) -> bool:
    """True when a and b differ by a single insertion, deletion, or swap."""
    if not a or not b or a == b:
        return False
    la, lb = len(a), len(b)
    if la < 2 or lb < 2 or abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(c1 != c2 for c1, c2 in zip(a, b)) == 1
    if la > lb:
        a, b = b, a
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
        elif skipped:
            return False
        else:
            skipped = True
        j += 1
    return True


def _token_parts(tok):
    if isinstance(tok, str):
        return tok, 1.0
    if not isinstance(tok, dict):
        return "", 0.0
    text = tok.get("text", "")
    conf = tok.get("conf", tok.get("score", 1.0))
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 1.0
    if conf != conf:  # NaN
        conf = 0.0
    return str(text if text is not None else ""), max(0.0, min(1.0, conf))


def _catalog(model):
    rooms = []
    exact = {}
    extra = {}
    buckets = {}
    exits = {}

    def add_exact(key, idx):
        if not key:
            return
        lst = exact.get(key)
        if lst is None:
            exact[key] = [idx]
        elif idx not in lst:
            lst.append(idx)
        else:
            return
        buckets.setdefault(len(key), []).append((key, idx))

    def add_extra(key, idx):
        if not key or key in exact:
            return
        lst = extra.get(key)
        if lst is None:
            extra[key] = [idx]
        elif idx not in lst:
            lst.append(idx)

    doors_for = {}
    floors = []
    for fl in model.get("floors") or []:
        try:
            level = int(fl.get("level"))
        except (TypeError, ValueError):
            continue
        floors.append((level, fl))
        for door in fl.get("doors") or []:
            x, y = door.get("x"), door.get("y")
            if x is None or y is None:
                continue
            pt = [float(x), float(y)]
            if door.get("exterior_exit"):
                exits.setdefault(level, []).append(pt)
            for cid in door.get("connects") or []:
                doors_for.setdefault(str(cid), []).append(pt)
    for level, fl in floors:
        for space in fl.get("spaces") or []:
            sid = str(space.get("id") or "")
            if not sid:
                continue
            pts = _dedupe(doors_for.get(sid) or [])
            if not pts:
                pts = [_centroid(space.get("polygon") or [])]
            idx = len(rooms)
            label = str(space.get("label") or sid)
            rooms.append({
                "id": sid,
                "level": level,
                "label": label,
                "points": pts,
            })
            for key in (_room_number(label), _id_suffix_key(sid)):
                add_exact(key, idx)
            add_extra(normalize_token(label), idx)
    for level in exits:
        exits[level] = _dedupe(exits[level])
    return rooms, exact, extra, buckets, exits


def _lookup_exact(keys, exact, extra):
    found = []
    seen = set()
    for key in keys:
        for idx in exact.get(key, ()):
            if idx not in seen:
                seen.add(idx)
                found.append(idx)
        for idx in extra.get(key, ()):
            if idx not in seen:
                seen.add(idx)
                found.append(idx)
    return found


def _lookup_fuzzy(keys, buckets):
    found = []
    seen = set()
    for key in keys:
        n = len(key)
        if n < 2:
            continue
        for length in (n - 1, n, n + 1):
            for room_key, idx in buckets.get(length, ()):
                if idx in seen:
                    continue
                if _one_char_off(key, room_key):
                    seen.add(idx)
                    found.append(idx)
    return found


def _dedupe(points):
    out = []
    seen = set()
    for pt in points:
        key = (round(pt[0], 3), round(pt[1], 3))
        if key in seen:
            continue
        seen.add(key)
        out.append([float(pt[0]), float(pt[1])])
    return out


def _centroid(poly):
    pts = []
    for p in poly or []:
        if not p or len(p) < 2:
            continue
        pts.append((float(p[0]), float(p[1])))
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return [0.0, 0.0]
    n = float(len(pts))
    return [sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n]


def _get_engine():
    global _engine, _engine_missing
    if _engine is not None or _engine_missing:
        return _engine
    with _engine_lock:
        if _engine is not None or _engine_missing:
            return _engine
        cls = None
        try:
            from rapidocr_onnxruntime import RapidOCR
            cls = RapidOCR
        except ImportError:
            try:
                from rapidocr import RapidOCR
                cls = RapidOCR
            except ImportError:
                cls = None
        if cls is None:
            _engine_missing = True
            log.warning("rapidocr_onnxruntime is not installed; OCR disabled")
            return None
        try:
            _engine = cls()
        except Exception:
            _engine_missing = True
            log.warning("rapidocr_onnxruntime failed to load; OCR disabled")
            return None
        return _engine


def _decode(jpeg_bytes):
    if jpeg_bytes is None:
        return None
    try:
        buf = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    except TypeError:
        try:
            buf = np.frombuffer(bytes(jpeg_bytes), dtype=np.uint8)
        except Exception:
            return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _resize_long(img, limit):
    height, width = img.shape[:2]
    long_side = max(height, width)
    if long_side <= limit or long_side <= 0:
        return img, 1.0, 1.0
    scale = limit / float(long_side)
    nw = max(1, int(round(width * scale)))
    nh = max(1, int(round(height * scale)))
    small = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return small, width / float(nw), height / float(nh)


def _parse_row(row, sx, sy):
    if row is None:
        return None
    # Newer rapidocr objects expose txts/boxes/scores on the whole result,
    # not on a row. Rows from rapidocr_onnxruntime are [box, text, score].
    try:
        box, text, conf = row[0], row[1], row[2] if len(row) > 2 else 1.0
    except (TypeError, IndexError, KeyError):
        return None
    if isinstance(box, str):
        text, conf, box = row[0], row[1] if len(row) > 1 else 1.0, None
    if text is None:
        return None
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 1.0
    if conf != conf:
        conf = 0.0
    xyxy = _as_xyxy(box, sx, sy)
    if xyxy is None:
        return None
    return {
        "text": str(text),
        "conf": max(0.0, min(1.0, conf)),
        "box": xyxy,
    }


def _as_xyxy(box, sx, sy):
    if box is None:
        return None
    try:
        seq = list(box)
    except TypeError:
        return None
    if not seq:
        return None
    if len(seq) == 4 and not isinstance(seq[0], (list, tuple, np.ndarray)):
        x0, y0, x1, y1 = (float(v) for v in seq)
    else:
        xs, ys = [], []
        for p in seq:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
        if not xs:
            return None
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return [
        round(x0 * sx, 2), round(y0 * sy, 2),
        round(x1 * sx, 2), round(y1 * sy, 2),
    ]


def _as_bytes(jpeg_bytes) -> bytes:
    if isinstance(jpeg_bytes, (bytes, bytearray)):
        return bytes(jpeg_bytes)
    return b""
