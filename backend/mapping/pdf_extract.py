"""Build the building JSON straight from vector CAD floor plan PDFs.

The DCC plans are InSite Visual-FM vector exports: every wall is a real line, every door
swing is a real arc, and every room label is real text. Reading them directly gives exact
geometry, which a vision model can only estimate.

    walls   black stroked lines / filled shapes (door leaf lines removed)
    doors   quarter-circle arcs with radius 0.6 to 1.3 m (hinge, closed edge -> opening)
    spaces  regions enclosed by walls + closed doors (+ dashed room lines), named by the
            room number text inside them; type from the room use text
    exits   doors with the outside region on one side
    stairs  same stair id on several floors (1ST1, 2ST1, 3ST1 -> ST1); elevator "-E"
    align   floors shifted so their shared stairs/elevator line up

Usage:
    python -m backend.mapping.pdf_extract data/plans/darrin-1.pdf data/plans/darrin-2.pdf \
        data/plans/darrin-3.pdf -o data/buildings/dcc.json [--debug data/debug]
"""
import argparse
import json
import math
import os
import re
import sys

import cv2
import numpy as np
import pymupdf

RES = 0.1                    # m per pixel for room segmentation
DOOR_R = (0.6, 1.3)          # door swing radius range (m)
KAPPA = 0.5523               # bezier handle ratio of a quarter circle
LEAF_TOL = 0.15              # m, how close a door leaf line must touch hinge/arc end
MIN_WALL = 0.05              # m
MIN_ROOM_M2 = 1.0
ROOM_RE = re.compile(r"^(\d{3}[A-Z]?(?:\.\d+)?|\dST\d+|\d-E)$")
# Not walkable: no floor here (open to below), dirt under raked seating, shafts. "OPEN TO ABOVE" is NOT a void:
# it marks a double-height space whose floor is walkable (e.g. the front of a lecture hall on the lower level).
VOID_TEXT = ("OPEN TO BELOW", "OTB", "UNEXCAVATED", "SHAFT", "AIR SHAFT")

TYPE_RULES = [            # first match on the room use text wins
    ("STAIR", "stairwell"), ("ELEVATOR", "elevator"), ("CORRIDOR", "hallway"),
    ("LOBBY", "lobby"), ("VESTIBULE", "lobby"), ("LOUNGE", "lobby"),
    ("LAV", "restroom"), ("TOILET", "restroom"), ("CLASS LEC", "lecture_hall"),
    ("CLASS", "classroom"), ("LAB", "classroom"), ("STUDIO", "classroom"),
    ("MECH", "mechanical"), ("ELEC", "mechanical"), ("JANITOR", "storage"),
    ("STOR", "storage"), ("CLOSET", "storage"), ("OFF", "office"), ("MEETING", "office"),
    ("MEDIA", "office"), ("CONF", "office"), ("MERCH", "lobby"),
]


def _is_black(c):
    return c is not None and max(c) < 0.15


def _is_room_line(c):        # teal/cyan dashed room separators in open areas
    return c is not None and c[0] < 0.2 and c[1] > 0.4 and c[2] > 0.4


def _scale(page):
    m = re.search(r'SCALE:\s*([\d.]+)"\s*=\s*([\d.]+)\'', page.get_text("text"))
    if not m:
        raise ValueError("no 'SCALE: 1.0\" = N\\'' text on the page")
    return float(m[2]) * 0.3048 / (72.0 * float(m[1]))


def _line_isect(p, d, q, e):
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-9:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return (p[0] + t * d[0], p[1] + t * d[1])


def _quarter_arc(p1, c1, c2, p2):
    """Return (center, radius) if the bezier is ~a quarter circle, else None."""
    d1 = (c1[0] - p1[0], c1[1] - p1[1]); d2 = (c2[0] - p2[0], c2[1] - p2[1])
    corner = _line_isect(p1, d1, p2, d2)
    if corner is None:
        return None
    c = (p1[0] + p2[0] - corner[0], p1[1] + p2[1] - corner[1])
    r1, r2 = math.dist(c, p1), math.dist(c, p2)
    if r1 < 1e-6 or abs(r1 - r2) > 0.15 * max(r1, r2):
        return None
    u = ((p1[0] - c[0]) / r1, (p1[1] - c[1]) / r1); v = ((p2[0] - c[0]) / r2, (p2[1] - c[1]) / r2)
    if abs(u[0] * v[0] + u[1] * v[1]) > 0.3:
        return None
    return c, (r1 + r2) / 2


def _arc_piece(p1, c1, c2, p2):
    """Circular arc piece of any sweep: (center, radius, p_start, p_end, sweep_deg) or None."""
    d1 = (c1[0] - p1[0], c1[1] - p1[1]); d2 = (c2[0] - p2[0], c2[1] - p2[1])
    if math.hypot(*d1) < 1e-9 or math.hypot(*d2) < 1e-9:
        return None
    c = _line_isect(p1, (-d1[1], d1[0]), p2, (-d2[1], d2[0]))      # normals at both ends meet at the center
    if c is None:
        return None
    r1, r2 = math.dist(c, p1), math.dist(c, p2)
    if r1 < 1e-6 or abs(r1 - r2) > 0.12 * max(r1, r2):
        return None
    cosang = ((p1[0] - c[0]) * (p2[0] - c[0]) + (p1[1] - c[1]) * (p2[1] - c[1])) / (r1 * r2)
    sweep = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))
    if not (8 <= sweep <= 100):
        return None
    return c, (r1 + r2) / 2, p1, p2, sweep


def _merge_arc_pieces(pieces, touch=0.04):
    """Chain arc pieces that touch end to end (similar radius and center), so a swing drawn as 30+60 or 45+45 deg
    becomes one ~90 deg door. ~360 deg chains are tables/columns and are dropped."""
    uniq = []                                # the same piece drawn twice (black + colored overlay) counts once
    for pc in pieces:
        if not any(abs(pc[1] - u[1]) < 0.05 and
                   min(math.dist(pc[2], u[2]) + math.dist(pc[3], u[3]),
                       math.dist(pc[2], u[3]) + math.dist(pc[3], u[2])) < 0.08 for u in uniq):
            uniq.append(pc)
    pieces = uniq
    n = len(pieces)
    parent = list(range(n))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(n):
        ci, ri, ai, bi, _ = pieces[i]
        for j in range(i + 1, n):
            cj, rj, aj, bj, _ = pieces[j]
            if abs(ri - rj) > 0.12 or math.dist(ci, cj) > 0.3:
                continue
            if min(math.dist(p, q) for p in (ai, bi) for q in (aj, bj)) <= touch:
                parent[find(i)] = find(j)
    chains = {}
    for i in range(n):
        chains.setdefault(find(i), []).append(pieces[i])
    key = lambda e: (round(e[0] / 0.03), round(e[1] / 0.03))
    arcs = []
    for chain in chains.values():
        total = sum(pc[4] for pc in chain)
        if not (70 <= total <= 115):
            continue
        cnt = {}
        for pc in chain:
            for e in (pc[2], pc[3]):
                cnt.setdefault(key(e), [e, 0])[1] += 1
        free = [v[0] for v in cnt.values() if v[1] == 1]
        if len(free) != 2:
            pts = [e for pc in chain for e in (pc[2], pc[3])]
            free = max(((a, b) for a in pts for b in pts), key=lambda ab: math.dist(*ab))
        c = (sum(pc[0][0] for pc in chain) / len(chain), sum(pc[0][1] for pc in chain) / len(chain))
        arcs.append((c, sum(pc[1] for pc in chain) / len(chain), free[0], free[1]))
    return arcs


def _bezier_points(p1, c1, c2, p2, n=8):
    out = []
    for i in range(n + 1):
        t = i / n; a = (1 - t) ** 3; b = 3 * t * (1 - t) ** 2; cc = 3 * t * t * (1 - t); d = t ** 3
        out.append((a * p1[0] + b * c1[0] + cc * c2[0] + d * p2[0],
                    a * p1[1] + b * c1[1] + cc * c2[1] + d * p2[1]))
    return out


def _use_type(text):
    t = text.upper()
    for key, typ in TYPE_RULES:
        if key in t:
            return typ
    return "unknown"


# ----------------------------------------------------------------------------------------
def read_floor(path, level):
    """Extract raw geometry of one floor in page meters (x right, y down)."""
    page = pymupdf.open(path)[0]
    mpp = _scale(page)
    rot = page.rotation_matrix

    def P(pt):
        q = pymupdf.Point(pt) * rot
        return (q.x * mpp, q.y * mpp)

    walls, room_lines, fills, pieces = [], [], [], []
    for d in page.get_drawings():
        dashed = (d.get("dashes") or "[] 0").strip() not in ("[] 0", "[]")
        stroke_black = _is_black(d.get("color")) and not dashed   # dashed black = overhead/hidden line
        fill_black = _is_black(d.get("fill"))
        teal = _is_room_line(d.get("color"))
        colored = d.get("color") is not None and not (stroke_black or teal)
        if not (stroke_black or fill_black or teal or colored):
            continue
        for it in d["items"]:
            if colored and it[0] != "c":      # colored drawings: only their door arcs matter
                continue
            kind = it[0]
            if kind == "l":
                segs = [(P(it[1]), P(it[2]))]
            elif kind == "re":
                r = it[1]; pts = [P(r.tl), P(r.tr), P(r.br), P(r.bl)]
                segs = list(zip(pts, pts[1:] + pts[:1]))
                if fill_black:
                    fills.append(pts)
            elif kind == "qu":
                q = it[1]; pts = [P(q.ul), P(q.ur), P(q.lr), P(q.ll)]
                segs = list(zip(pts, pts[1:] + pts[:1]))
                if fill_black:
                    fills.append(pts)
            elif kind == "c":
                pts = [P(p) for p in it[1:5]]
                pc = _arc_piece(*pts) if (stroke_black or colored) else None
                if pc and DOOR_R[0] <= pc[1] <= DOOR_R[1]:
                    pieces.append(pc)          # door swings are often drawn as 2 pieces (30+60 or 45+45 deg)
                    continue
                if colored:
                    continue
                bp = _bezier_points(*pts)
                segs = list(zip(bp, bp[1:]))
            else:
                continue
            for a, b in segs:
                if math.dist(a, b) < MIN_WALL:
                    continue
                (room_lines if teal and not stroke_black else walls).append((a, b))

    arcs = _merge_arc_pieces(pieces)

    # doors: hinge C, arc ends A/B; the leaf line runs C -> open end, the other end is closed
    doors, leaf_idx = [], set()
    for c, r, a, b in arcs:
        closed = None
        for i, (p, q) in enumerate(walls):
            for s, t in ((p, q), (q, p)):
                if math.dist(s, c) < LEAF_TOL:
                    if math.dist(t, a) < LEAF_TOL:
                        closed = b; leaf_idx.add(i)
                    elif math.dist(t, b) < LEAF_TOL:
                        closed = a; leaf_idx.add(i)
            if closed:
                break
        if closed is None:       # no leaf line drawn: assume the end nearer a wall line is closed
            closed = min((a, b), key=lambda e: _near_wall_dist(e, c, walls))
        doors.append({"hinge": c, "closed": closed, "r": r})
    walls = [w for i, w in enumerate(walls) if i not in leaf_idx]
    doors = _merge_double_doors(doors)

    labels, voids, sfs = [], [], []
    for b in page.get_text("dict")["blocks"]:
        lines = [" ".join(s["text"] for s in ln["spans"]).strip() for ln in b.get("lines", [])]
        lines = [ln for ln in lines if ln]
        if not lines:
            continue
        x0, y0, x1, y1 = b["lines"][0]["bbox"]
        pos = P(((x0 + x1) / 2, (y0 + y1) / 2))
        for ln in lines:
            msf = re.match(r"^(\d+)sf$", ln)
            if msf:
                sfs.append((pos, float(msf[1]) * 0.0929))
        if ROOM_RE.match(lines[0]):
            labels.append({"num": lines[0], "use": " ".join(lines[1:2]), "extra": lines[2:], "pos": pos,
                           "m2": None})
        elif any(lines[0].upper().startswith(v) for v in VOID_TEXT):
            voids.append(pos)
    for lab in labels:          # area text is its own block just below the label
        near = [(math.dist(lab["pos"], q), m2) for q, m2 in sfs]
        if near:
            d, m2 = min(near)
            if d < 3.0:
                lab["m2"] = m2
    walls = _strip_leaders(walls, labels)
    stair_marks = ocr_stair_marks(path, page, mpp, detect_flights(walls))
    marks = []
    for text, pos in ocr_annotations(path, page, mpp):
        if any(k in text for k in ("UNEXCAV", "SHAFT", "BELOW")) or text == "OTB":
            voids.append(pos)
        elif text in ("UP", "DN", "DOWN"):
            marks.append({"text": text, "pos": pos})
    return {"level": level, "walls": walls, "room_lines": room_lines, "fills": fills, "marks": marks + stair_marks,
            "doors": doors, "labels": labels, "voids": voids}


ANNOT_RE = re.compile(r"^(UP|DN|DOWN|OTB|STAIRS?|RAMP|(AIR )?SHAFT|UNEXCAVATED|OPEN TO (ABOVE|BELOW))$")


def ocr_annotations(path, page, mpp, zoom=4.0, tile=1400):
    """Notes like UNEXCAVATED, OPEN TO ABOVE, OTB, UP/DN are often drawn as vector strokes, not text, so read them
    with OCR from a high-res render. Cached next to the PDF (<name>.ocr.json) since it takes ~30 s per floor.
    Returns [(TEXT, (x_m, y_m))] in the same page-meter frame as everything else."""
    cache = os.path.splitext(path)[0] + ".ocr.json"
    if os.path.isfile(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
        with open(cache, encoding="utf-8") as fh:
            return [(t, tuple(p)) for t, p in json.load(fh)]
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        print("  (OCR not installed: skipping drawn annotations)")
        return []
    eng = RapidOCR()
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))     # rendered in display (rotated) orientation
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
    out, seen = [], set()
    for y in range(0, pix.h, tile - 200):
        for x in range(0, pix.w, tile - 200):
            part = img[y:y + tile, x:x + tile]
            if part.min() > 200:
                continue
            res, _ = eng(part)
            for box, text, conf in (res or []):
                t = " ".join(text.upper().split())
                if float(conf) < 0.6 or not (ANNOT_RE.match(t) or "UNEXCAV" in t or t.startswith("OPEN TO")):
                    continue
                cx = (sum(b[0] for b in box) / 4 + x) / zoom * mpp
                cy = (sum(b[1] for b in box) / 4 + y) / zoom * mpp
                key = (t, round(cx), round(cy))
                if key not in seen:
                    seen.add(key)
                    out.append((t, (round(cx, 2), round(cy, 2))))
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out


_OCR = None


def _ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def ocr_stair_marks(path, page, mpp, flights, pad_m=2.5, zoom=10.0):
    """Small "UP"/"DN" arrow labels on stairs need a close look: OCR a tight high-res crop around each flight.
    Cached in <name>.stairs.json. Returns [{"text": "UP"|"DN", "pos": (x_m, y_m)}]."""
    cache = os.path.splitext(path)[0] + ".stairs.json"
    if os.path.isfile(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
        with open(cache, encoding="utf-8") as fh:
            return [{"text": m["text"], "pos": tuple(m["pos"])} for m in json.load(fh)]
    try:
        eng = _ocr_engine()
    except ImportError:
        return []
    out, seen = [], set()
    for f in flights:
        xs = [p[0] for p in f["poly"]]; ys = [p[1] for p in f["poly"]]
        clip = pymupdf.Rect((min(xs) - pad_m) / mpp, (min(ys) - pad_m) / mpp,
                            (max(xs) + pad_m) / mpp, (max(ys) + pad_m) / mpp)
        pix = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(zoom, zoom))
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
        for im, rot in ((img, 0), (cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE), 90)):   # labels may run sideways
            res, _ = eng(im)
            for box, text, conf in (res or []):
                t = text.strip().upper()
                # OCR misreads of thin CAD lettering
                t = {"DOWN": "DN", "DWN": "DN", "DZ": "DN", "DM": "DN", "0N": "DN", "ON": "DN", "DH": "DN",
                     "D N": "DN", "UF": "UP", "JP": "UP", "LP": "UP", "UR": "UP", "U P": "UP"}.get(t, t)
                if t not in ("UP", "DN") or float(conf) < 0.5:
                    continue
                u = sum(b[0] for b in box) / 4; v = sum(b[1] for b in box) / 4
                if rot == 90:                                   # map back from the rotated crop
                    u, v = v, pix.h - u
                x = round((clip.x0 + u / zoom) * mpp, 2); y = round((clip.y0 + v / zoom) * mpp, 2)
                k = (t, round(x), round(y))
                if k not in seen:
                    seen.add(k); out.append({"text": t, "pos": (x, y)})
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out


def detect_flights(walls, length=(0.7, 12.0), gap=(0.18, 0.5), min_steps=5):
    """Stair treads = runs of >= min_steps parallel, similar-length lines, evenly spaced 0.18-0.5 m apart and
    overlapping along their length. Seat rows (~0.9 m apart) and double wall faces (pairs) do not qualify.
    Returns flights: {center, along (unit vector of a tread), up (unit normal), steps, length_m, width_m, poly}."""
    by_angle = {}
    for idx, (a, b) in enumerate(walls):
        L = math.dist(a, b)
        if not (length[0] <= L <= length[1]):
            continue
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
        by_angle.setdefault(int(round(ang / 3.0)) % 60, []).append((a, b, L, idx))
    flights = []
    for key, segs in by_angle.items():
        th = math.radians(key * 3.0)
        ux, uy = math.cos(th), math.sin(th)
        nx, ny = -uy, ux
        items = []
        for a, b, L, idx in segs:
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            pa, pb = sorted((a[0] * ux + a[1] * uy, b[0] * ux + b[1] * uy))
            items.append((mx * nx + my * ny, pa, pb, L, idx))
        items.sort()
        chains, extras = [], {}
        for d, pa, pb, L, idx in items:
            best, dup = None, False
            for ch in chains:
                ld, la, lb, lL, _ = ch[-1]
                ov = min(pb, lb) - max(pa, la)
                if d - ld < gap[0] and ov >= 0.6 * min(L, lL):
                    dup = True            # other edge of the same (filled) tread
                    break
                if not (gap[0] <= d - ld <= gap[1]):
                    continue
                ov = min(pb, lb) - max(pa, la)
                if ov >= 0.6 * min(L, lL) and abs(L - lL) <= 0.3 * max(L, lL):
                    best = ch
                    break
            if dup:
                extras.setdefault(id(ch), []).append(idx)     # remember it so it is removed from walls too
                continue
            if best is None:
                chains.append([(d, pa, pb, L, idx)])
            else:
                best.append((d, pa, pb, L, idx))
        for ch in chains:
            if len(ch) < min_steps:
                continue
            gaps = np.diff([c[0] for c in ch])
            if gaps.std() > 0.1:
                continue
            d0, d1 = ch[0][0], ch[-1][0]
            a0 = float(np.median([c[1] for c in ch])); a1 = float(np.median([c[2] for c in ch]))
            corners = [(a0, d0), (a1, d0), (a1, d1), (a0, d1)]
            poly = [[round(p * ux + q * nx, 2), round(p * uy + q * ny, 2)] for p, q in corners]
            cx = sum(p[0] for p in poly) / 4; cy = sum(p[1] for p in poly) / 4
            flights.append({"center": [round(cx, 2), round(cy, 2)], "along": [round(ux, 3), round(uy, 3)],
                            "up": [round(nx, 3), round(ny, 3)], "steps": len(ch),
                            "length_m": round(d1 - d0, 2), "width_m": round(a1 - a0, 2), "poly": poly,
                            "_segs": [c[4] for c in ch] + extras.get(id(ch), [])})
    return flights


def _opening_exits(lv, R, comp, block, outside, by_comp, doors, start, min_m=0.9, max_m=3.0,
                   kinds=("lobby", "hallway", "stairwell")):
    """Entrances drawn without a door swing (glass storefronts, dashed walls, open archways): every place where
    indoor floor meets the outdoors through a gap >= min_m becomes an exit "door" at the gap's center."""
    ext = np.isin(comp, list(outside))
    indoor = (block == 0) & ~ext & (comp > 0)
    touch = cv2.dilate(ext.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    opening = (touch & indoor).astype(np.uint8)
    n, lab, stats, cents = cv2.connectedComponentsWithStats(opening, connectivity=8)
    have = [(d["x"], d["y"]) for d in doors if d["exterior_exit"]]
    out = []
    for k in range(1, n):
        w, h = stats[k, cv2.CC_STAT_WIDTH], stats[k, cv2.CC_STAT_HEIGHT]
        span = max(w, h) * RES
        if span < min_m:
            continue
        ys, xs = np.nonzero(lab == k)
        horizontal = w >= h
        order = np.argsort(xs if horizontal else ys)
        pieces = max(1, int(math.ceil(span / max_m)))          # long glass fronts -> several exits
        for chunk in np.array_split(order, pieces):
            cx, cy = R.m(int(xs[chunk].mean()), int(ys[chunk].mean()))
            if any(math.dist((cx, cy), q) < 1.5 for q in have):
                continue
            ins = [by_comp[c]["id"] for c in set(comp[ys[chunk], xs[chunk]].tolist()) if c in by_comp]
            types = [by_comp[c]["type"] for c in set(comp[ys[chunk], xs[chunk]].tolist()) if c in by_comp]
            if not any(t in kinds for t in types):      # entrances open from lobbies/corridors/stairs only
                continue
            width = min(max_m, span / pieces)
            out.append({"id": f"F{lv}-X{start + len(out) + 1}", "x": round(cx, 2), "y": round(cy, 2),
                        "width": round(max(width, 0.9), 2), "connects": ins[:1] + ["OUTSIDE"],
                        "exterior_exit": True, "axis": [1.0, 0.0] if horizontal else [0.0, 1.0],
                        "confidence": 0.6})
            have.append((cx, cy))
    return out


def _orient_flights(flights, marks, near_m=3.0):
    """Use the plan's UP/DN arrow labels: "DN" means the steps go down from this floor and the top of the flight
    is the end by the label; "UP" means they climb away from it. f["up"] ends up pointing uphill."""
    for f in flights:
        c = f["center"]
        best = None
        for m in marks:
            if m["text"] not in ("UP", "DN"):
                continue
            d = min(math.dist(m["pos"], p) for p in f["poly"] + [c])
            if d <= near_m and (best is None or d < best[0]):
                best = (d, m)
        if not best:
            continue
        m = best[1]
        s = (m["pos"][0] - c[0]) * f["up"][0] + (m["pos"][1] - c[1]) * f["up"][1]
        if (m["text"] == "DN" and s < 0) or (m["text"] == "UP" and s > 0):
            f["up"] = [-f["up"][0], -f["up"][1]]
        f["goes"] = "down" if m["text"] == "DN" else "up"
        f["dir_source"] = "label"


def _exterior_stair_comps(flights, labels, R, comp, outside, label_m=8.0):
    """Outdoor steps (e.g. the wide stair outside the floor 3 lobby doors) are drawn as long tread lines and enclose
    regions the segmentation thinks are indoors. A flight far from any stair label whose regions touch the outside
    is outdoors: its regions count as OUTSIDE, so the doors onto it become exits."""
    stair_pos = [l["pos"] for l in labels if re.search(r"\dST\d+$", l["num"])]
    out_mask = np.isin(comp, list(outside))
    near_out = cv2.dilate(out_mask.astype(np.uint8), np.ones((21, 21), np.uint8)) > 0   # within ~1 m
    extra = set()
    for f in flights:
        if any(math.dist(f["center"], p) <= label_m for p in stair_pos):
            continue
        m = np.zeros(comp.shape, np.uint8)
        cv2.fillPoly(m, [np.array([R.px(p) for p in f["poly"]], np.int32)], 1)
        m = cv2.dilate(m, np.ones((5, 5), np.uint8)) > 0
        ks = set(np.unique(comp[m])) - {0} - set(outside)
        if ks and (np.isin(comp, list(ks)) & near_out).any():
            f["exterior"] = True
            extra |= ks
    return extra


def _attach_flights(spaces, flights, search_m=8.0, cluster_m=6.0, pad_m=0.5):
    """Give each labeled stairwell the tread flights next to its label: polygon = padded hull of those flights,
    "link_points" = flight ends (where people get on and off), so labels and stair links sit on the real steps."""
    for sp in spaces:
        if sp["type"] != "stairwell" or not re.search(r"\dST\d+$", sp["id"]):
            continue
        pos = sp.get("pos")
        if not pos:
            continue
        near = sorted((math.dist(f["center"], pos), i) for i, f in enumerate(flights)
                      if math.dist(f["center"], pos) <= search_m and "stair" not in f)
        if not near:
            continue
        first = flights[near[0][1]]
        group = [flights[i] for _, i in near if math.dist(flights[i]["center"], first["center"]) <= cluster_m]
        pts = np.array([p for f in group for p in f["poly"]], np.float32)
        hull = cv2.convexHull(pts).reshape(-1, 2)
        c = hull.mean(axis=0)
        grown = [[round(float(c[0] + (x - c[0]) * (1 + pad_m / max(np.hypot(x - c[0], y - c[1]), 0.1))), 2),
                  round(float(c[1] + (y - c[1]) * (1 + pad_m / max(np.hypot(x - c[0], y - c[1]), 0.1))), 2)]
                 for x, y in hull]
        sp["flight_poly"] = grown
        sp["link_points"] = []
        for f in group:
            f["stair"] = sp["id"]
            half = f["length_m"] / 2 + 0.4
            for sgn in (1, -1):
                sp["link_points"].append((f["center"][0] + sgn * half * f["up"][0], f["center"][1] + sgn * half * f["up"][1]))


def _strip_leaders(walls, labels, near=1.2):
    """Remove diagonal callout lines that start at a label; move the label to their far end."""
    keep = []
    for a, b in walls:
        L = math.dist(a, b)
        ang = abs(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))) % 90
        diagonal = 8 < ang < 82
        hit = None
        if diagonal and L > 1.0:
            for lab in labels:
                for s, t in ((a, b), (b, a)):
                    if math.dist(s, lab["pos"]) < near:
                        hit = (lab, s, t)
                        break
                if hit:
                    break
        if hit:
            lab, s, t = hit
            ux, uy = (t[0] - s[0]) / L, (t[1] - s[1]) / L
            lab["pos"] = (t[0] + 0.3 * ux, t[1] + 0.3 * uy)
            continue
        keep.append((a, b))
    return keep


def _near_wall_dist(e, c, walls):
    best = math.inf
    for p, q in walls:
        if math.dist(p, c) < LEAF_TOL or math.dist(q, c) < LEAF_TOL:
            continue
        best = min(best, _pt_seg(e, p, q))
    return best


def _pt_seg(e, p, q):
    vx, vy = q[0] - p[0], q[1] - p[1]; L = vx * vx + vy * vy
    t = 0 if L == 0 else max(0, min(1, ((e[0] - p[0]) * vx + (e[1] - p[1]) * vy) / L))
    return math.dist(e, (p[0] + t * vx, p[1] + t * vy))


def _split_merged_rooms(spaces, comp, R, lv):
    """One region holding several room labels (vestibules joined by open doorways) is split between them: each
    pixel goes to the nearest label. Adds a space per extra label. Returns {comp: f(x, y) -> space id}."""
    owners = {}
    for sp in list(spaces):
        others = sp.pop("others", None)
        if not others or sp["comp"] is None or not sp.get("pos"):
            continue
        members = [sp["comp"]] + sp.get("extra_comps", [])
        mask = np.isin(comp, members)
        ys, xs = np.nonzero(mask)
        seeds = [(sp["id"], sp["pos"])] + [(f"F{lv}-{o['num']}", o["pos"]) for o in others]
        P = np.array([R.px(p) for _, p in seeds], np.float32)
        d = (xs[:, None] - P[None, :, 0]) ** 2 + (ys[:, None] - P[None, :, 1]) ** 2
        who = np.argmin(d, axis=1)
        for i, (sid, pos) in enumerate(seeds):
            cell = np.zeros(comp.shape, np.uint8)
            cell[ys[who == i], xs[who == i]] = 1
            poly = _polygon(cell, R)
            if not poly:
                continue
            if i == 0:
                sp["polygon"] = poly
                sp["label"] = sp["label"].split(" + ")[0]
                sp["also"] = []
                continue
            o = others[i - 1]
            typ = _use_type(o["use"])
            spaces.append({"id": sid, "label": f"{o['num']} {o['use']}".strip(), "type": typ, "comp": None,
                           "also": [], "m2": o["m2"], "pos": o["pos"], "polygon": poly,
                           "area": float(cell.sum()) * RES * RES})
        names = [sid for sid, _ in seeds]
        def owner(x, y, P=P, names=names):
            px, py = R.px((x, y))
            return names[int(np.argmin((P[:, 0] - px) ** 2 + (P[:, 1] - py) ** 2))]
        for k in members:
            owners[k] = owner
    return owners


def _merge_double_doors(doors):
    """Pair two leaves into one double door only when it really is one: hinges 1.2-3.2 m apart and both closed
    ends meeting near the middle. The same swing drawn twice is kept once; everything else stays a single door."""
    uniq = []
    for d in doors:                                   # drop exact duplicates (same hinge and closed end)
        if not any(math.dist(d["hinge"], u["hinge"]) < 0.1 and math.dist(d["closed"], u["closed"]) < 0.1 for u in uniq):
            uniq.append(d)
    out, used = [], set()
    for i, d in enumerate(uniq):
        if i in used:
            continue
        mate = None
        for j in range(i + 1, len(uniq)):
            if j in used:
                continue
            e = uniq[j]
            span = math.dist(d["hinge"], e["hinge"])
            mid = ((d["hinge"][0] + e["hinge"][0]) / 2, (d["hinge"][1] + e["hinge"][1]) / 2)
            if (math.dist(d["closed"], e["closed"]) < 0.25 and 1.2 <= span <= 3.2
                    and math.dist(d["closed"], mid) < 0.45 and math.dist(e["closed"], mid) < 0.45):
                mate = j
                break
        if mate is None:
            out.append({"a": d["hinge"], "b": d["closed"]})
        else:
            used.add(mate)
            out.append({"a": d["hinge"], "b": uniq[mate]["hinge"]})
    return out


# ----------------------------------------------------------------------------------------
class Raster:
    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0 = x0 - 2, y0 - 2
        self.w = int((x1 - x0 + 4) / RES) + 1
        self.h = int((y1 - y0 + 4) / RES) + 1

    def px(self, p):
        return (int(round((p[0] - self.x0) / RES)), int(round((p[1] - self.y0) / RES)))

    def m(self, px, py):
        return (round(self.x0 + px * RES, 2), round(self.y0 + py * RES, 2))


def segment_floor(fl):
    pts = [p for w in fl["walls"] for p in w]
    R = Raster(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    block = np.zeros((R.h, R.w), np.uint8)
    for a, b in fl["walls"] + fl["room_lines"]:
        cv2.line(block, R.px(a), R.px(b), 255, 2)
    for poly in fl["fills"]:
        cv2.fillPoly(block, [np.array([R.px(p) for p in poly], np.int32)], 255)
    for d in fl["doors"]:
        cv2.line(block, R.px(d["a"]), R.px(d["b"]), 255, 2)
    # Building footprint: close gaps in the outer lines up to ~3 m (dashed glass walls, storefronts, open
    # entrances) and fill the inside. Free space inside it is indoors; free space outside it is one EXT region.
    from scipy.ndimage import binary_fill_holes
    closed = cv2.morphologyEx((block > 0).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
    footprint = binary_fill_holes(closed > 0)
    free = block == 0
    n, comp = cv2.connectedComponents((free & footprint).astype(np.uint8), connectivity=4)
    ext = n                                   # label for everything outdoors
    comp[free & ~footprint] = ext
    return R, block, comp, {ext}, footprint


def _comp_at(R, comp, block, p, search=6):
    x, y = R.px(p)
    for rad in range(search + 1):
        ys, xs = np.mgrid[max(0, y - rad):min(R.h, y + rad + 1), max(0, x - rad):min(R.w, x + rad + 1)]
        vals = comp[ys, xs][block[ys, xs] == 0]
        vals = vals[vals > 0]
        if vals.size:
            return int(np.bincount(vals).argmax())
    return 0


def _grow_rooms(spaces, comp, block, area, taken, R):
    """Seat rows and stair treads cut a room into strips. If a room's region is much smaller
    than the area printed on the plan, merge neighbouring unlabeled strips into it (smallest
    rooms first) until it reaches that area. Seat rows are thick, so look 1.5 m across."""
    kernel = np.ones((15, 15), np.uint8)
    order = sorted((sp for sp in spaces if sp["comp"] is not None),
                   key=lambda sp: sp.get("m2") or 0)
    for sp in order:
        target = sp.get("m2") or (15.0 if sp["type"] == "stairwell" else 0)
        members = {sp["comp"]}
        if area[sp["comp"]] >= 0.6 * target:
            continue
        cap = max(3.0, 0.6 * target)          # a single strip can't be most of the room
        # a room stays compact: every merged pixel within ~1.5x the room's expected size of its label
        lx, ly = R.px(sp["pos"]) if sp.get("pos") else (None, None)
        reach_px = 1.5 * math.sqrt(max(target, 4.0)) / RES
        def compact(k):
            if lx is None:
                return True
            ys, xs = np.nonzero(comp == k)
            return float(np.max(np.hypot(xs - lx, ys - ly))) <= reach_px
        while area[list(members)].sum() < 0.9 * target:
            mask = np.isin(comp, list(members)).astype(np.uint8)
            ring = cv2.dilate(mask, kernel) > 0
            cand = set(np.unique(comp[ring])) - members - taken - {0}
            cand = {k for k in cand if area[k] <= cap and compact(k)}
            if not cand:
                break
            members |= cand
        sp["extra_comps"] = sorted(members - {sp["comp"]})
        taken |= members


def _interior_lines(walls, spaces, comp, R):
    """Indices of wall lines that lie inside a merged room (seat rows, treads)."""
    inner = np.zeros(comp.shape, np.uint8)
    for sp in spaces:
        if sp.get("extra_comps"):
            m = np.isin(comp, [sp["comp"]] + sp["extra_comps"]).astype(np.uint8)
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
            inner |= cv2.erode(m, np.ones((3, 3), np.uint8))
    drop = set()
    for i, (a, b) in enumerate(walls):
        pts = [R.px((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))) for t in np.linspace(0, 1, 9)]
        hits = sum(1 for x, y in pts if 0 <= x < R.w and 0 <= y < R.h and inner[y, x])
        if hits >= 6:
            drop.add(i)
    return drop


def _polygon(mask, R, eps_px=1.5):
    cs, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    c = cv2.approxPolyDP(c, eps_px, True)
    if len(c) < 3:
        return None
    return [list(R.m(int(p[0][0]), int(p[0][1]))) for p in c]


def build_floor(fl):
    lv = fl["level"]
    R, block, comp, outside, footprint = segment_floor(fl)
    area = np.bincount(comp.ravel()) * RES * RES

    void_comps = {_comp_at(R, comp, block, p) for p in fl["voids"]} - {0}
    flights = detect_flights(fl["walls"])
    _orient_flights(flights, fl.get("marks", []))
    true_outside = set(outside)
    ext_stair = _exterior_stair_comps(flights, fl["labels"], R, comp, outside)
    outside = true_outside | ext_stair       # outdoor steps count as outside (single exceptions: overrides file)
    spaces, comp_space = [], {}
    # rooms first, stairs last: an open stair must not claim the corridor it sits in
    for lab in sorted(fl["labels"], key=lambda l: _use_type(l["use"]) in ("stairwell", "elevator")):
        k = _comp_at(R, comp, block, lab["pos"])
        sid = f"F{lv}-{lab['num']}"
        use = lab["use"]
        typ = _use_type(use)
        too_big = area[k] > 2.5 * (lab["m2"] or 16.0)
        if typ in ("stairwell", "elevator") and (k in comp_space or k in outside or k == 0 or too_big):
            side = math.sqrt(lab["m2"] or 9.0) / 2
            x, y = lab["pos"]
            spaces.append({"id": sid, "label": f"{lab['num']} {use}", "type": typ, "comp": None,
                           "also": [], "m2": lab["m2"], "approx": True, "pos": lab["pos"],
                           "box": [[x - side, y - side], [x + side, y - side], [x + side, y + side], [x - side, y + side]]})
            continue
        if k == 0 or k in outside or k in void_comps:
            continue
        if k in comp_space:                      # second label in the same region
            comp_space[k]["also"].append(lab["num"])
            comp_space[k].setdefault("others", []).append(lab)
            continue
        label = f"{lab['num']} {use}".strip()
        if lab["extra"] and not re.match(r"^\d+sf$|^NONA$", lab["extra"][-1]):
            label += f" ({lab['extra'][-1]})"
        sp = {"id": sid, "label": label, "type": typ, "comp": k, "also": [], "m2": lab["m2"], "pos": lab["pos"]}
        comp_space[k] = sp
        spaces.append(sp)
    _grow_rooms(spaces, comp, block, area, set(comp_space) | void_comps | outside, R)
    for k in void_comps:
        spaces.append({"id": f"F{lv}-VOID{k}", "label": "OPEN TO ABOVE/BELOW", "type": "void",
                       "comp": k, "also": []})
    # unlabeled enclosed regions big enough to stand in
    for k in range(1, len(area)):
        if k in outside or k in comp_space or k in void_comps or area[k] < 4.0:
            continue
        sp = {"id": f"F{lv}-U{k}", "label": "unlabeled", "type": "unknown", "comp": k, "also": []}
        comp_space[k] = sp
        spaces.append(sp)

    for sp in spaces:
        if sp.get("box"):
            sp["polygon"] = [[round(x, 2), round(y, 2)] for x, y in sp.pop("box")]
            sp["area"] = (sp["polygon"][1][0] - sp["polygon"][0][0]) ** 2
            continue
        mask = np.isin(comp, [sp["comp"]] + sp.get("extra_comps", []))
        if sp.get("extra_comps"):
            mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8)) > 0
        sp["polygon"] = _polygon(mask, R)
        sp["area"] = float(area[[sp["comp"]] + sp.get("extra_comps", [])].sum())
    spaces = [s for s in spaces if s["polygon"] and s["area"] >= MIN_ROOM_M2]
    split_owner = _split_merged_rooms(spaces, comp, R, lv)
    by_comp = {s["comp"]: s for s in spaces if s["comp"] is not None}
    for s in spaces:
        for k in s.get("extra_comps", []):
            by_comp[k] = s
    drop = _interior_lines(fl["walls"], spaces, comp, R)
    for f in flights:                       # stair treads are steps, not walls
        drop |= set(f.pop("_segs", []))
    kept_walls = [w for i, w in enumerate(fl["walls"]) if i not in drop]

    inside = (footprint & ~np.isin(comp, list(outside))).astype(np.uint8)
    inside = cv2.morphologyEx(inside, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    outline = _polygon(inside, R, eps_px=2.0)
    # Every door in the outermost wall is an exit (the user's rule): door center within ~1 m of the envelope.
    edge = cv2.morphologyEx(inside, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
    near_edge = cv2.dilate(edge.astype(np.uint8), np.ones((21, 21), np.uint8)) > 0     # 1 m at 0.1 m/px
    doors = []
    for i, d in enumerate(fl["doors"]):
        a, b = d["a"], d["b"]
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        L = math.dist(a, b) or 1.0
        nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L
        sides = []
        for s in (1, -1):
            k = _comp_at(R, comp, block, (mid[0] + s * 0.5 * nx, mid[1] + s * 0.5 * ny), search=3)
            if k in outside:
                sides.append("OUTSIDE")
            elif k in by_comp:
                sp_id = by_comp[k]["id"]
                if k in split_owner:                       # merged room: the sub-room under this point
                    sp_id = split_owner[k](mid[0] + s * 0.5 * nx, mid[1] + s * 0.5 * ny)
                sides.append(sp_id)
        if len(sides) < 2 and "OUTSIDE" not in sides:
            # door into a pocket we dropped; still keep it so walls get an opening
            pass
        # Doors onto an outdoor stair only count as exits from circulation spaces; a door from an elevator or an
        # office that happens to touch such a region is not an exit.
        if "OUTSIDE" in sides:
            ks = [_comp_at(R, comp, block, (mid[0] + sg * 0.5 * nx, mid[1] + sg * 0.5 * ny), search=3) for sg in (1, -1)]
            via_true = any(k in true_outside for k in ks)
            inner = [by_comp[k]["type"] for k in ks if k in by_comp]
            if "elevator" in inner or (not via_true and
                                       not any(t in ("lobby", "hallway", "stairwell", "unknown") for t in inner)):
                sides = [x for x in sides if x != "OUTSIDE"]      # elevators are never evacuation exits
        px, py = R.px(mid)
        inner_types = [by_comp[k]["type"] for k in
                       (_comp_at(R, comp, block, (mid[0] + sg * 0.5 * nx, mid[1] + sg * 0.5 * ny), search=3)
                        for sg in (1, -1)) if k in by_comp]
        on_envelope = (0 <= py < R.h and 0 <= px < R.w and bool(near_edge[py, px])
                       and "elevator" not in inner_types)
        if on_envelope and "OUTSIDE" not in sides:
            sides = sides + ["OUTSIDE"]
        doors.append({"id": f"F{lv}-D{i + 1}", "x": round(mid[0], 2), "y": round(mid[1], 2),
                      "width": round(min(max(L, 0.6), 3.0), 2), "connects": sides,
                      "exterior_exit": ("OUTSIDE" in sides and len(set(sides)) > 1) or on_envelope,
                      "axis": [round((b[0] - a[0]) / L, 3), round((b[1] - a[1]) / L, 3)],
                      "confidence": 0.95})


    _attach_flights(spaces, flights)

    doors += _opening_exits(lv, R, comp, block, outside, by_comp, doors, start=len(fl["doors"]))

    walls = [{"id": f"F{lv}-W{i + 1}", "x1": round(a[0], 2), "y1": round(a[1], 2),
              "x2": round(b[0], 2), "y2": round(b[1], 2), "exterior": False, "confidence": 0.95}
             for i, (a, b) in enumerate(_merge_collinear(kept_walls))]
    for s in spaces:
        s.pop("comp"); s.pop("area"); s.pop("extra_comps", None); s.pop("m2", None); s.pop("pos", None)
        if s.get("flight_poly"):
            s["polygon"] = s.pop("flight_poly")      # stair shape + label on the actual treads
            s.pop("approx", None)                     # a measured stair is a good alignment anchor
        if s["also"]:
            s["label"] += " + " + ", ".join(s["also"])
        s.pop("also")
        s["multi_floor"] = False
        s["confidence"] = 0.95 if s["type"] != "unknown" else 0.5
    return {"level": lv, "alignment": {"offset_x": 0, "offset_y": 0, "rotation_deg": 0},
            "outline": outline, "walls": walls, "spaces": spaces, "doors": doors,
            "stair_flights": flights,
            "notes": "extracted from vector PDF"}, (R, block, comp)


def _merge_collinear(segs, tol=0.02):
    """Join segments that continue each other in a straight line (cuts wall count a lot)."""
    segs = [tuple(map(tuple, s)) for s in segs]
    changed = True
    while changed:
        changed = False
        ends = {}
        for i, (a, b) in enumerate(segs):
            for p in (a, b):
                ends.setdefault((round(p[0], 2), round(p[1], 2)), []).append(i)
        dead, out = set(), []
        for i, (a, b) in enumerate(segs):
            if i in dead:
                continue
            for p in (b, a):
                for j in ends.get((round(p[0], 2), round(p[1], 2)), []):
                    if j == i or j in dead:
                        continue
                    c, d = segs[j]
                    q = d if math.dist(c, p) < 0.011 else c
                    o = a if p is b else b
                    if _pt_seg(p, o, q) < tol and math.dist(o, q) > max(math.dist(o, p), math.dist(p, q)):
                        a, b = o, q
                        dead.add(j); changed = True
                        break
            out.append((a, b))
        segs = out
    return segs


# ----------------------------------------------------------------------------------------
def _stair_key(space_id):
    m = re.match(r"^F\d+-\d(ST\d+|-E)$", space_id)
    return m[1] if m else None


def align_and_link(floors):
    """Shift floors so shared stairs/elevator coincide, then add vertical connections."""
    def centroid(poly):
        return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))

    anchors = {}
    for fl in floors:
        for s in fl["spaces"]:
            k = _stair_key(s["id"])
            if k and not s.get("approx"):
                anchors.setdefault(k, {})[fl["level"]] = centroid(s["polygon"])
    ref = floors[0]["level"]
    report = []
    shifts = {ref: (0.0, 0.0)}
    order = sorted(floors, key=lambda f: f["level"])
    for fl in order[1:]:
        lv = fl["level"]
        # chain to the nearest already-aligned floor that shares anchors
        best = None
        for other in sorted(shifts, key=lambda o: abs(o - lv)):
            pairs = [(k, v[other], v[lv]) for k, v in anchors.items() if other in v and lv in v]
            if pairs:
                best = (other, pairs); break
        if not best:
            shifts[lv] = (0.0, 0.0); report.append(f"floor {lv}: no shared stairs, not aligned")
            continue
        other, keyed = best
        # elevator shafts are identical on every floor: trust them, keep stairs that agree
        diffs = {k: (a[0] - b[0], a[1] - b[1]) for k, a, b in keyed}
        base = diffs.get("-E") or tuple(np.median(list(diffs.values()), axis=0))
        inl = [d for d in diffs.values() if math.dist(d, base) < 1.5] or [base]
        pairs = [(a, b) for k, a, b in keyed]
        dx = float(np.mean([d[0] for d in inl])) + shifts[other][0]
        dy = float(np.mean([d[1] for d in inl])) + shifts[other][1]
        shifts[lv] = (dx, dy)
        res = [math.dist((b[0] + dx - shifts[other][0], b[1] + dy - shifts[other][1]), a) for a, b in pairs]
        report.append(f"floor {lv}: shift ({dx:.1f}, {dy:.1f}) m using {len(pairs)} shared stairs/elevator ({len(inl)} agree), "
                      f"residuals {', '.join(f'{r:.1f}' for r in res)} m")

    def mv(p, s):
        return [round(p[0] + s[0], 2), round(p[1] + s[1], 2)]

    for fl in floors:
        s = shifts[fl["level"]]
        fl["outline"] = [mv(p, s) for p in fl["outline"]]
        for sp in fl["spaces"]:
            sp["polygon"] = [mv(p, s) for p in sp["polygon"]]
            if sp.get("link_points"):
                sp["link_points"] = [mv(p, s) for p in sp["link_points"]]
        for f in fl.get("stair_flights", []):
            f["center"] = mv(f["center"], s); f["poly"] = [mv(p, s) for p in f["poly"]]
        for w in fl["walls"]:
            w["x1"], w["y1"] = mv((w["x1"], w["y1"]), s); w["x2"], w["y2"] = mv((w["x2"], w["y2"]), s)
        for d in fl["doors"]:
            d["x"], d["y"] = mv((d["x"], d["y"]), s)

    # origin = top-left of everything
    allp = [p for fl in floors for p in fl["outline"]]
    ox, oy = min(p[0] for p in allp), min(p[1] for p in allp)
    for fl in floors:
        fl["outline"] = [mv(p, (-ox, -oy)) for p in fl["outline"]]
        for sp in fl["spaces"]:
            sp["polygon"] = [mv(p, (-ox, -oy)) for p in sp["polygon"]]
            if sp.get("link_points"):
                sp["link_points"] = [mv(p, (-ox, -oy)) for p in sp["link_points"]]
        for f in fl.get("stair_flights", []):
            f["center"] = mv(f["center"], (-ox, -oy)); f["poly"] = [mv(p, (-ox, -oy)) for p in f["poly"]]
        for w in fl["walls"]:
            w["x1"], w["y1"] = mv((w["x1"], w["y1"]), (-ox, -oy)); w["x2"], w["y2"] = mv((w["x2"], w["y2"]), (-ox, -oy))
        for d in fl["doors"]:
            d["x"], d["y"] = mv((d["x"], d["y"]), (-ox, -oy))

    # vertical connections: per-floor link point = the stair/elevator door on that floor
    verticals = []
    groups = {}
    for fl in floors:
        for sp in fl["spaces"]:
            k = _stair_key(sp["id"])
            if k:
                groups.setdefault(k, {})[fl["level"]] = (fl, sp)
    for k, per in sorted(groups.items()):
        if len(per) < 2:
            continue
        # Per floor, candidates = the stair's doors + its centroid. Pick the one closest to where the
        # OTHER floors put this stair (median of their centroids), so a bad shape on one floor can't
        # teleport the route across the building.
        cents = {lv: tuple(centroid(sp["polygon"])) for lv, (fl, sp) in per.items()}
        positions = {}
        for lv, (fl, sp) in per.items():
            others = [c for o, c in cents.items() if o != lv] or [cents[lv]]
            ref = (float(np.median([c[0] for c in others])), float(np.median([c[1] for c in others])))
            cand = [tuple(p) for p in sp.get("link_points", [])] or                    [(d["x"], d["y"]) for d in fl["doors"] if sp["id"] in d["connects"]] + [cents[lv]]
            p = min(cand, key=lambda q: math.dist(q, ref))
            positions[str(lv)] = [round(p[0], 2), round(p[1], 2)]
            sp["multi_floor"] = True
        cx = sum(p[0] for p in positions.values()) / len(positions)
        cy = sum(p[1] for p in positions.values()) / len(positions)
        is_elev = k == "-E"
        verticals.append({"id": "ELEVATOR" if is_elev else f"STAIR-{k[2:]}",
                          "name": "Elevator" if is_elev else f"Stair {k[2:]}",
                          "type": "elevator" if is_elev else "stairs",
                          "x": round(cx, 2), "y": round(cy, 2), "positions": positions,
                          "floors": sorted(per), "confidence": 0.9})
    verticals += _link_tiered_halls(floors)

    _stack_flights(floors, verticals)

    for fl in floors:                       # leftover unnamed regions add clutter; walkability comes from walls
        gone = {sp["id"] for sp in fl["spaces"] if sp["label"] == "unlabeled"}
        fl["spaces"] = [sp for sp in fl["spaces"] if sp["id"] not in gone]
        for d in fl["doors"]:
            d["connects"] = [c for c in d["connects"] if c not in gone]

    for fl in floors:
        for sp in fl["spaces"]:
            sp.pop("link_points", None)
            if sp.pop("approx", None):
                sp["confidence"] = 0.6
    return verticals, report


def _poly_mask(poly, x0, y0, w, h, res=0.25):
    m = np.zeros((h, w), np.uint8)
    pts = np.array([[(p[0] - x0) / res, (p[1] - y0) / res] for p in poly], np.int32)
    cv2.fillPoly(m, [pts], 1)
    return m.astype(bool)


def _link_tiered_halls(floors, min_cover=0.4, res=0.25):
    """Tiered lecture halls are double height: entered from the upper floor, seating slopes down to the floor
    below, where the PDF shows the lower tier as an unlabeled region. Find those regions (mostly covered by a
    lecture hall one floor up), name them as the hall's lower tier and link the two levels with a "ramp"
    (the seating aisles) so routing, smoke and the 3D view treat the hall as one space."""
    by_level = {fl["level"]: fl for fl in floors}
    allp = [p for fl in floors for p in fl["outline"]]
    x0, y0 = min(p[0] for p in allp) - 1, min(p[1] for p in allp) - 1
    w = int((max(p[0] for p in allp) - x0) / res) + 4
    h = int((max(p[1] for p in allp) - y0) / res) + 4
    links = []
    for lv, upper in by_level.items():
        lower = by_level.get(lv - 1)
        if not lower:
            continue
        for hall in upper["spaces"]:
            if hall["type"] != "lecture_hall":
                continue
            hm = _poly_mask(hall["polygon"], x0, y0, w, h, res)
            num = hall["label"].split()[0]
            tiers = []
            for sp in lower["spaces"]:
                if not (sp["label"] == "unlabeled" or sp["type"] in ("unknown", "lecture_hall")):
                    continue
                sm = _poly_mask(sp["polygon"], x0, y0, w, h, res)
                if sm.sum() and (sm & hm).sum() / sm.sum() >= min_cover:
                    tiers.append(sp)
            if not tiers:
                continue
            for k, sp in enumerate(tiers):
                sp["id"] = f"F{lower['level']}-{num}L" + (f"{k + 1}" if len(tiers) > 1 else "")
                sp["label"] = f"{num} CLASS LEC (lower tier)"
                sp["type"] = "lecture_hall"
                sp["multi_floor"] = True
                sp["confidence"] = 0.8
            hall["multi_floor"] = True
            hc = (sum(p[0] for p in hall["polygon"]) / len(hall["polygon"]),
                  sum(p[1] for p in hall["polygon"]) / len(hall["polygon"]))
            for k, tier in enumerate(tiers):        # every tier piece gets its own aisle link up to the hall
                lc = (sum(p[0] for p in tier["polygon"]) / len(tier["polygon"]),
                      sum(p[1] for p in tier["polygon"]) / len(tier["polygon"]))
                edge = min(hall["polygon"], key=lambda p: math.dist(p, lc))
                up_pt = ((edge[0] + hc[0]) / 2, (edge[1] + hc[1]) / 2)
                suffix = f"-{k + 1}" if len(tiers) > 1 else ""
                links.append({"id": f"HALL-{num}{suffix}", "name": f"Hall {num} seating", "type": "ramp",
                              "x": round((lc[0] + up_pt[0]) / 2, 2), "y": round((lc[1] + up_pt[1]) / 2, 2),
                              "positions": {str(lower["level"]): [round(lc[0], 2), round(lc[1], 2)],
                                            str(lv): [round(up_pt[0], 2), round(up_pt[1], 2)]},
                              "floors": [lower["level"], lv], "confidence": 0.7})
    return links


def _stack_flights(floors, verticals):
    """Give every flight an explicit height range z = [z0, z1] in floor heights relative to its own floor, so the
    3D view draws real switchbacks: floor -> half landing (+-0.5) -> next floor.
    Top floor: everything goes down. Bottom floor: up. Per stair, the flight nearest that floor's link point starts at
    the floor (0 -> +-0.5); the next one continues from the landing (+-0.5 -> +-1). Middle floors: one up, one down
    (an UP/DN label decides which when present). f["up"] is then oriented to point uphill."""
    levels = sorted(fl["level"] for fl in floors)
    lo_all, hi_all = levels[0], levels[-1]
    link = {}
    for v in verticals:
        if v["type"] == "stairs" and v["id"].startswith("STAIR-"):
            link["ST" + v["id"][6:]] = v
    for fl in floors:
        lv = fl["level"]
        groups = {}
        for f in fl.get("stair_flights", []):
            key = _stair_key(f["stair"]) if f.get("stair") else None
            groups.setdefault(key if key in link else None, []).append(f)
        for key, fs in groups.items():
            v = link.get(key)
            if v is None:                                   # unattached steps: follow the floor
                for f in fs:
                    sign = -1 if lv == hi_all else (1 if lv == lo_all else (-1 if f.get("goes") == "down" else 1))
                    _set_z(f, None, 0.0, 0.5 * sign)
                continue
            lp = v.get("positions", {}).get(str(lv)) or [v["x"], v["y"]]
            fs.sort(key=lambda f: math.dist(f["center"], lp))
            top, bottom = lv == max(v["floors"]), lv == min(v["floors"])
            if top or bottom:
                sign = -1 if top else 1
                for i, f in enumerate(fs):
                    z0 = 0.0 if i == 0 else 0.5 * sign
                    _set_z(f, lp, z0, z0 + 0.5 * sign)
            else:                                           # middle floor: one up, one down
                first_down = fs[0].get("goes") == "down"
                for i, f in enumerate(fs):
                    sign = (-1 if first_down else 1) * (1 if i % 2 == 0 else -1)
                    _set_z(f, lp, 0.0, 0.5 * sign)


def _set_z(f, lp, z0, z1):
    f["z"] = [z0, z1]
    f["goes"] = "down" if z1 < z0 else "up"
    if lp is None:
        return
    # start end (at z0) is the end facing the link point; uphill = toward the higher end
    s = (lp[0] - f["center"][0]) * f["up"][0] + (lp[1] - f["center"][1]) * f["up"][1]
    want_toward_start = z0 > z1
    if (s > 0) != want_toward_start:
        f["up"] = [-f["up"][0], -f["up"][1]]


def apply_overrides(building, path):
    """Hand fixes that survive re-extraction, keyed by position (final map meters), e.g. data/buildings/dcc.overrides.json:
      {"no_exit":  [{"level": 3, "near": [10.5, 60.5], "radius": 4, "note": "..."}],
       "exit":     [{"level": 2, "near": [x, y], "radius": 1.5}],
       "flight_goes": [{"level": 3, "near": [x, y], "radius": 2, "goes": "down"}],
       "remove_flight": [{"level": 3, "near": [x, y], "radius": 2}]}"""
    with open(path, encoding="utf-8") as fh:
        ov = json.load(fh)
    floors = {fl["level"]: fl for fl in building["floors"]}
    log = []
    def hits(items, rule):
        fl = floors.get(rule.get("level"))
        if not fl:
            return []
        c, r = rule["near"], rule.get("radius", 2.0)
        return [it for it in items(fl) if math.dist(c, it[0]) <= r]
    for rule in ov.get("no_exit", []):
        for _, d in hits(lambda fl: [((d["x"], d["y"]), d) for d in fl["doors"]], rule):
            if d["exterior_exit"]:
                d["exterior_exit"] = False
                d["connects"] = [c for c in d["connects"] if c != "OUTSIDE"]
                log.append(f"no exit {d['id']}")
    for rule in ov.get("exit", []):
        for _, d in hits(lambda fl: [((d["x"], d["y"]), d) for d in fl["doors"]], rule):
            d["exterior_exit"] = True
            if "OUTSIDE" not in d["connects"]:
                d["connects"].append("OUTSIDE")
            log.append(f"exit {d['id']}")
    for rule in ov.get("flight_goes", []):
        for _, f in hits(lambda fl: [(tuple(f["center"]), f) for f in fl.get("stair_flights", [])], rule):
            if f.get("goes") != rule["goes"]:
                f["up"] = [-f["up"][0], -f["up"][1]] if rule.get("flip") else f["up"]
            f["goes"] = rule["goes"]; f["dir_source"] = "override"
            log.append(f"flight at {f['center']} goes {rule['goes']}")
    for rule in ov.get("add_exit", []):                # exits added by hand in the editor
        fl = floors.get(rule.get("level"))
        if not fl:
            continue
        x, y = rule["near"]
        if any(math.dist((x, y), (d["x"], d["y"])) < 0.8 and d["exterior_exit"] for d in fl["doors"]):
            continue
        did = f"F{fl['level']}-E{len(fl['doors']) + 1}"
        fl["doors"].append({"id": did, "x": x, "y": y, "width": rule.get("width", 1.0), "connects": ["OUTSIDE"],
                            "exterior_exit": True, "confidence": 1.0})
        log.append(f"added exit {did} at ({x}, {y})")
    for rule in ov.get("vertical_floors", []):          # e.g. a stair that does not reach every floor
        for v in building["vertical_connections"]:
            if v["id"] == rule["id"]:
                v["floors"] = sorted(rule["floors"])
                v["positions"] = {k: p for k, p in v.get("positions", {}).items() if int(k) in v["floors"]}
                log.append(f"{v['id']} floors -> {v['floors']}")
    for rule in ov.get("remove_flight", []):
        fl = floors.get(rule.get("level"))
        if fl:
            keep = [f for f in fl.get("stair_flights", []) if math.dist(rule["near"], f["center"]) > rule.get("radius", 2.0)]
            log.append(f"removed {len(fl.get('stair_flights', [])) - len(keep)} flight(s) on floor {fl['level']}")
            fl["stair_flights"] = keep
    return log


def debug_png(fl_raw, fl, raster, path):
    R, block, comp = raster
    img = np.full((R.h, R.w, 3), 255, np.uint8)
    img[block > 0] = (0, 0, 0)
    for d in fl_raw["doors"]:
        cv2.line(img, R.px(d["a"]), R.px(d["b"]), (0, 0, 255), 2)
    for lab in fl_raw["labels"]:
        cv2.circle(img, R.px(lab["pos"]), 3, (255, 0, 0), -1)
    cv2.imwrite(path, img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="one PDF per floor, lowest floor first")
    ap.add_argument("-o", "--out", default="data/buildings/dcc.json")
    ap.add_argument("--first-level", type=int, default=1)
    ap.add_argument("--building", default="Darrin Communications Center (RPI)")
    ap.add_argument("--debug", help="folder for per-floor debug PNGs")
    args = ap.parse_args()

    floors = []
    for i, path in enumerate(args.pdfs):
        lv = args.first_level + i
        raw = read_floor(path, lv)
        fl, raster = build_floor(raw)
        floors.append(fl)
        print(f"Floor {lv}: {len(fl['walls'])} walls, {len(fl['spaces'])} spaces, {len(fl['doors'])} doors, "
              f"{sum(d['exterior_exit'] for d in fl['doors'])} exits")
        if args.debug:
            os.makedirs(args.debug, exist_ok=True)
            debug_png(raw, fl, raster, os.path.join(args.debug, f"floor{lv}.png"))

    verticals, report = align_and_link(floors)
    for r in report:
        print("Align", r)
    out = {"building": args.building, "units": "meters",
           "scale_method": "vector PDF, plotted scale from the drawing title block",
           "floor_height_m": 4.0, "wall_style": "faces", "floors": floors, "vertical_connections": verticals,
           "multi_floor_spaces": [], "global_notes": "Generated by backend/pdf_extract.py"}
    ov_path = os.path.splitext(args.out)[0] + ".overrides.json"
    if os.path.isfile(ov_path):
        for line in apply_overrides(out, ov_path):
            print("Override:", line)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"Saved {args.out}. Stairs/elevators linked: {[v['id'] for v in verticals]}")


if __name__ == "__main__":
    sys.exit(main())
