"""Quality checks on a loaded building model.

Finds the problems that break routing and fire spread (missing interior walls,
rooms with no door, areas cut off from exits, stairs outside their stairwell).
Each issue is a plain sentence so it can be shown to a human or sent back to
Gemini as feedback.

Usage:
    python -m backend.core.checks data/buildings/dcc.json
"""
import math
import sys

import numpy as np
from scipy.ndimage import label

from .grid import BuildingGrid, point_in_poly, seg_dist
from .model import load_building_file
from .routing import RouteField, best_room_cell

WALL_NEAR_M = 0.4         # a boundary point counts as walled if a wall is this close
DOOR_ON_WALL_M = 0.6      # a door should sit on a wall (or the outline) within this
MIN_WALL_COVER = 0.5      # rooms need at least this share of their boundary walled
OPEN_TYPES = {"hallway", "lobby"}   # open plan spaces may lack walls between them
POCKET_MIN_M2 = 2.0


def _boundary_points(poly, step=0.5):
    pts = []
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        n = max(1, int(math.dist((x1, y1), (x2, y2)) / step))
        for k in range(n):
            t = (k + 0.5) / n
            pts.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
    return np.array(pts)


def _dist_to_walls(xs, ys, walls):
    d = np.full(xs.shape, np.inf)
    for w in walls:
        d = np.minimum(d, seg_dist(xs, ys, w["x1"], w["y1"], w["x2"], w["y2"]))
    return d


def _outline_walls(outline):
    return [{"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}
            for a, b in zip(outline, outline[1:] + outline[:1])]


def check_building(model, grid=None):
    """Return a list of issue strings (empty = looks good)."""
    g = grid or BuildingGrid(model)
    issues = list(model["warnings"]) + list(g.warnings)
    # CAD (wall_style "faces") maps: doorways are often plain gaps and rooms are traced from the
    # lines, so door/wall-coverage checks are noise there; reachability is what matters.
    cad = model.get("wall_style") == "faces"
    ids = {s["id"] for fl in model["floors"] for s in fl["spaces"]}

    for fl in model["floors"]:
        lv = fl["level"]
        walls = fl["walls"]
        boundary = walls + _outline_walls(fl["outline"]) if fl["outline"] else walls
        with_door = {c for d in fl["doors"] for c in d["connects"]}

        for d in fl["doors"]:
            bad = [c for c in d["connects"] if c not in ids and c.upper() != "OUTSIDE"]
            if bad:
                issues.append(f"Floor {lv}: door {d['id']} connects unknown space ids {bad}")
            dist = 0.0 if cad else _dist_to_walls(np.array([d["x"]]), np.array([d["y"]]), boundary)[0]
            if dist > DOOR_ON_WALL_M:
                issues.append(f"Floor {lv}: door {d['id']} at ({d['x']:.1f}, {d['y']:.1f}) is "
                              f"{dist:.1f} m from any wall; doors must sit on a wall line")

        for s in fl["spaces"]:
            sid, poly = s["id"], s["polygon"]
            if fl["outline"]:
                xs = np.array([p[0] for p in poly]); ys = np.array([p[1] for p in poly])
                outside = ~point_in_poly(xs, ys, fl["outline"])
                # tolerate corners lying exactly on the outline
                if outside.any():
                    od = _dist_to_walls(xs[outside], ys[outside], _outline_walls(fl["outline"]))
                    if (od > 0.5).any():
                        issues.append(f"Floor {lv}: space {sid} extends outside the floor outline; "
                                      f"grow the outline or fix the polygon")
            lab = s["label"].upper()
            for word, want in (("STAIR", "stairwell"), ("ELEV", "elevator")):
                if word in lab and s["type"] != want:
                    issues.append(f"Floor {lv}: space {sid} is labeled '{s['label']}' but typed "
                                  f"'{s['type']}'; it should be a {want} linked to a vertical_connection")
            if s["type"] in OPEN_TYPES or cad:
                continue
            if sid not in with_door:
                issues.append(f"Floor {lv}: space {sid} ({s['label']}) has no door; add one on its "
                              f"boundary connecting it to the adjacent hallway/space")
            pts = _boundary_points(poly)
            cover = float((_dist_to_walls(pts[:, 0], pts[:, 1], boundary) <= WALL_NEAR_M).mean())
            if cover < MIN_WALL_COVER:
                issues.append(f"Floor {lv}: space {sid} ({s['label']}) has walls on only "
                              f"{cover:.0%} of its boundary; add wall segments along its edges "
                              f"(leave gaps only at doors)")

        fi = g.level_index[lv]
        lab, n = label(g.walkable[fi])
        if n > 1:
            sizes = np.bincount(lab.ravel())
            main = int(np.argmax(sizes[1:])) + 1
            for k in range(1, n + 1):
                area = sizes[k] * g.cell ** 2
                if k == main or area < POCKET_MIN_M2:
                    continue
                rr, cc = np.where(lab == k)
                xs, ys = g.X[rr, cc], g.Y[rr, cc]
                rooms = sorted({g.rooms[i]["id"] for i in g.room[fi][rr, cc] if i >= 0})
                issues.append(f"Floor {lv}: walkable area x {xs.min():.1f}-{xs.max():.1f}, "
                              f"y {ys.min():.1f}-{ys.max():.1f} ({area:.0f} m2, spaces {rooms}) "
                              f"is sealed off from the rest of the floor; a wall or the outline "
                              f"blocks it or a door is missing")

    for v in model["vertical_connections"]:
        want = "elevator" if v["type"] == "elevator" else "stairwell"
        for fl in model["floors"]:
            if fl["level"] not in v["floors"]:
                continue
            inside = [s for s in fl["spaces"]
                      if point_in_poly(np.array([v["x"]]), np.array([v["y"]]), s["polygon"])[0]]
            if not any(s["type"] in (want, "unknown") for s in inside):
                issues.append(f"Floor {fl['level']}: {v['type']} {v['id']} at ({v['x']:.1f}, "
                              f"{v['y']:.1f}) is not inside a {want} space on this floor")

    # stairwell spaces that no stair/ramp connection passes through on that floor
    for fl in model["floors"]:
        for s in fl["spaces"]:
            if s["type"] != "stairwell":
                continue
            linked = [v for v in model["vertical_connections"]
                      if v["type"] != "elevator" and fl["level"] in v["floors"]
                      and point_in_poly(np.array([v["x"]]), np.array([v["y"]]), s["polygon"])[0]]
            if not linked:
                cx, cy = BuildingGrid._centroid(s["polygon"])
                near = sorted(model["vertical_connections"],
                              key=lambda v: math.hypot(v["x"] - cx, v["y"] - cy))
                hint = ""
                if near and near[0]["type"] != "elevator" and math.hypot(near[0]["x"] - cx, near[0]["y"] - cy) < 6:
                    hint = f"; it is probably {near[0]['id']}: add {fl['level']} to its floors"
                issues.append(f"Floor {fl['level']}: stairwell {s['id']} at ({cx:.1f}, {cy:.1f}) is not "
                              f"linked to any vertical_connection on this floor{hint}")

    # floors that only the elevator connects to the rest of the building
    for lv in model["levels"]:
        stairs = [v for v in model["vertical_connections"] if v["type"] != "elevator" and lv in v["floors"]
                  and len(v["floors"]) > 1]
        if len(model["levels"]) > 1 and not stairs:
            issues.append(f"Floor {lv}: no stairs or ramp connect it to another floor "
                          f"(elevators are not used for evacuation)")

    field = RouteField(g)
    for info in g.rooms:
        if info["type"] in ("elevator",):
            continue
        fi, cell = best_room_cell(field, info)
        if cell is None:
            issues.append(f"Floor {info['level']}: space {info['id']} has no walkable floor area "
                          f"(covered by walls or outside the outline)")
            continue
        r = field.route_from(fi, *g.center_of(*cell))
        if not r["reachable"]:
            issues.append(f"Floor {info['level']}: space {info['id']} cannot reach any exterior exit")

    return issues


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/buildings/dcc.json"
    issues = check_building(load_building_file(path))
    for i in issues:
        print("-", i)
    print(f"{len(issues)} issue(s)")


if __name__ == "__main__":
    main()
