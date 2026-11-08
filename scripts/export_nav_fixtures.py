"""Export grid, room-route, hint, OCR, and particle fixtures for the iOS app.

The Dijkstra here is the spec the Swift router copies: 8-connected, no corner
cutting, stair links, binary heap ordered by (distance, node index). Exit
distances are checked against backend.core.routing.RouteField.

Run from the repo root:
    py -3 scripts/export_nav_fixtures.py
"""
import json
import math
import shutil
import sys
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.grid import BuildingGrid
from backend.core.model import load_building, load_building_file
from backend.core.routing import RouteField
from backend.services.localize import ParticleFilter
from backend.services.occupants import _room_pose, nav_hints
from backend.services.vision import match_rooms

OUT = ROOT / "ios" / "Firesight" / "FiresightTests" / "Fixtures"
SAMPLE = ROOT / "data" / "buildings" / "sample_building.json"

NO_PRED = -9999
SMOKE_PENALTY = 12.0
DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]


class SplitMix:
    """Box-Muller splitmix. The fixture records outputs; Swift replays them."""

    def __init__(self, seed):
        self.s = int(seed) & ((1 << 64) - 1)
        self.spare = None

    def next_u64(self):
        mask = (1 << 64) - 1
        self.s = (self.s + 0x9E3779B97F4A7C15) & mask
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        return (z ^ (z >> 31)) & mask

    def unit(self):
        return (self.next_u64() >> 11) * (1.0 / 9007199254740992.0)

    def normal_one(self, mean, std):
        if self.spare is not None:
            z = self.spare
            self.spare = None
            return mean + std * z
        u1 = max(self.unit(), 1e-300)
        u2 = self.unit()
        r = math.sqrt(-2.0 * math.log(u1))
        theta = 2.0 * math.pi * u2
        z0 = r * math.cos(theta)
        self.spare = r * math.sin(theta)
        return mean + std * z0


class LogRNG:
    def __init__(self, seed):
        self.inner = SplitMix(seed)
        self.normals = []
        self.ints = []
        self.units = []

    def normal(self, loc=0.0, scale=1.0, size=None):
        count = 1 if size is None else int(size)
        out = [self.inner.normal_one(float(loc), float(scale)) for _ in range(count)]
        self.normals.extend(out)
        if size is None:
            return out[0]
        return np.asarray(out, dtype=float)

    def integers(self, low, high=None, size=None):
        if high is None:
            low, high = 0, low
        span = int(high) - int(low)

        def one():
            if span <= 0:
                return int(low)
            return int(low) + (self.inner.next_u64() % span)

        count = 1 if size is None else int(size)
        out = [one() for _ in range(count)]
        self.ints.extend(out)
        if size is None:
            return out[0]
        return np.asarray(out, dtype=np.int64)

    def random(self, size=None):
        count = 1 if size is None else int(size)
        out = [self.inner.unit() for _ in range(count)]
        self.units.extend(out)
        if size is None:
            return out[0]
        return np.asarray(out, dtype=float)

    def choice(self, a, size=None, replace=True):
        pool = np.asarray(a)
        count = int(size)
        picks = [pool[self.inner.next_u64() % pool.shape[0]] for _ in range(count)]
        self.ints.extend(int(p) for p in picks)
        return np.asarray(picks)


def crc_bytes(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def flat(grid, fi, r, c):
    return (fi * grid.rows + r) * grid.cols + c


def unflat(grid, idx):
    fi, rem = divmod(int(idx), grid.rows * grid.cols)
    r, c = divmod(rem, grid.cols)
    return fi, r, c


def build_adj(grid, fire, smoke, use_elevators=False):
    """Neighbor order: DIRS, then stair links. Matches Engine/Router.swift."""
    f, r_count, c_count = grid.walkable.shape
    n = f * r_count * c_count
    passable = grid.walkable & ~fire
    adj = [[] for _ in range(n)]
    link_names = {}
    for dr, dc in DIRS:
        diag = dr != 0 and dc != 0
        step = grid.cell * (math.sqrt(2.0) if diag else 1.0)
        for fi in range(f):
            for r in range(r_count):
                rr = r + dr
                if rr < 0 or rr >= r_count:
                    continue
                for c in range(c_count):
                    cc = c + dc
                    if cc < 0 or cc >= c_count:
                        continue
                    if not passable[fi, r, c] or not passable[fi, rr, cc]:
                        continue
                    if diag and not (passable[fi, rr, c] and passable[fi, r, cc]):
                        continue
                    sa = float(smoke[fi, r, c])
                    sb = float(smoke[fi, rr, cc])
                    w = step * (1.0 + SMOKE_PENALTY * (sa + sb) / 2.0)
                    adj[flat(grid, fi, r, c)].append((flat(grid, fi, rr, cc), w))
    for (fa, ra, ca), (fb, rb, cb), cost, info in grid.links:
        if info["type"] == "elevator" and not use_elevators:
            continue
        if not (passable[fa, ra, ca] and passable[fb, rb, cb]):
            continue
        a = flat(grid, fa, ra, ca)
        b = flat(grid, fb, rb, cb)
        if (a, b) in link_names:
            continue
        s = (float(smoke[fa, ra, ca]) + float(smoke[fb, rb, cb])) / 2.0
        w = cost * (1.0 + SMOKE_PENALTY * s)
        adj[a].append((b, w))
        adj[b].append((a, w))
        link_names[(a, b)] = link_names[(b, a)] = info["name"]
    return adj, link_names, passable


def dijkstra(adj, sources):
    n = len(adj)
    dist = [math.inf] * n
    pred = [NO_PRED] * n
    heap = []
    import heapq
    for s in sources:
        if dist[s] == 0.0:
            continue
        dist[s] = 0.0
        pred[s] = NO_PRED
        heapq.heappush(heap, (0.0, s))
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u]:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                pred[v] = u
                heapq.heappush(heap, (nd, v))
    return dist, pred


def source_cells(grid, passable, room_id):
    info = grid.room_by_id[room_id]
    fi = grid.level_index[info["level"]]
    out = []
    for r in range(grid.rows):
        for c in range(grid.cols):
            if grid.room[fi, r, c] == info["index"] and passable[fi, r, c]:
                out.append(flat(grid, fi, r, c))
    return out


def simplify(grid, cells):
    if not cells:
        return []
    keep = [cells[0]]
    for prev, cur, nxt in zip(cells, cells[1:], cells[2:]):
        if cur[0] != prev[0] or cur[0] != nxt[0]:
            keep.append(cur)
            continue
        d1 = (cur[1] - prev[1], cur[2] - prev[2])
        d2 = (nxt[1] - cur[1], nxt[2] - cur[2])
        if d1 != d2:
            keep.append(cur)
    if len(cells) > 1:
        keep.append(cells[-1])
    out = []
    for fi, r, c in keep:
        x, y = grid.center_of(r, c)
        out.append([grid.levels[fi], round(x, 2), round(y, 2)])
    return out


def path_length(grid, cells):
    link_cost = {}
    for a, b, cost, _info in grid.links:
        link_cost[(a, b)] = cost
        link_cost[(b, a)] = cost
    total = 0.0
    for a, b in zip(cells, cells[1:]):
        if a[0] == b[0]:
            total += grid.cell * math.hypot(a[1] - b[1], a[2] - b[2])
        else:
            total += link_cost.get((tuple(a), tuple(b)), 10.0)
    return total


def instructions(grid, cells, nodes, link_names, dest_label):
    steps = []
    seg_room, seg_len = None, 0.0

    def flush():
        nonlocal seg_room, seg_len
        if seg_room is not None and seg_len >= 1.0:
            info = grid.rooms[seg_room]
            if info["type"] != "hallway":
                name = info["label"]
            else:
                name = f"the hallway ({info['label']})"
            steps.append(f"Walk {round(seg_len)} m through {name}")

    for i, (fi, r, c) in enumerate(cells):
        if i > 0:
            pfi, pr, pc = cells[i - 1]
            if pfi != fi:
                flush()
                seg_room, seg_len = None, 0.0
                name = link_names.get((nodes[i - 1], nodes[i]), "stairs")
                direction = "down" if fi < pfi else "up"
                steps.append(f"Take {name} {direction} to floor {grid.levels[fi]}")
                continue
            seg_len += grid.cell * math.hypot(r - pr, c - pc)
        rid = int(grid.room[fi, r, c])
        if rid != -1 and rid != seg_room:
            if seg_room is not None:
                flush()
                seg_len = 0.0
            seg_room = rid
    flush()
    steps.append(f"Arrive at {dest_label}")
    return steps


def route_between(grid, fire, smoke, start_fi, x, y, dest_id):
    adj, link_names, passable = build_adj(grid, fire, smoke, use_elevators=False)
    sources = source_cells(grid, passable, dest_id)
    dist, pred = dijkstra(adj, sources)
    start = grid.nearest_walkable(start_fi, x, y, max_dist=3.0, mask=passable[start_fi])
    label = grid.room_by_id[dest_id]["label"]
    if start is None:
        on_fire = False
        r, c = grid.cell_of(x, y)
        if 0 <= r < grid.rows and 0 <= c < grid.cols:
            on_fire = bool(grid.walkable[start_fi, r, c] and not passable[start_fi, r, c])
        return {
            "reachable": False,
            "reason": "in fire" if on_fire else "not on a walkable area",
            "path": [],
            "distance_m": None,
            "max_smoke": 0,
            "instructions": [],
        }
    node = flat(grid, start_fi, *start)
    if not math.isfinite(dist[node]):
        return {
            "reachable": False,
            "reason": "no safe route to the room",
            "path": [],
            "distance_m": None,
            "max_smoke": 0,
            "instructions": [],
        }
    nodes = [node]
    while pred[node] != NO_PRED and len(nodes) < 1_000_000:
        node = pred[node]
        nodes.append(node)
    cells = [unflat(grid, k) for k in nodes]
    smoke_max = max(float(smoke[c]) for c in cells) if cells else 0.0
    return {
        "reachable": True,
        "reason": "",
        "path": simplify(grid, cells),
        "distance_m": round(path_length(grid, cells), 1),
        "max_smoke": round(smoke_max, 2),
        "instructions": instructions(grid, cells, nodes, link_names, label),
    }


def check_exit_distances(grid):
    """Same edge weights as RouteField, measured from the exits."""
    fire = np.zeros_like(grid.walkable)
    smoke = np.zeros(grid.walkable.shape, np.float64)
    adj, _names, passable = build_adj(grid, fire, smoke, use_elevators=False)
    sources = [flat(grid, fi, r, c) for fi, r, c, _did in grid.exits if passable[fi, r, c]]
    dist, _pred = dijkstra(adj, sources)
    field = RouteField(grid)
    worst = 0.0
    for i, mine in enumerate(dist):
        theirs = float(field.dist[i])
        if math.isfinite(mine) or math.isfinite(theirs):
            if math.isfinite(mine) != math.isfinite(theirs):
                raise SystemExit(f"reachability mismatch at node {i}")
            if math.isfinite(mine):
                worst = max(worst, abs(mine - theirs))
    if worst > 1e-3:
        raise SystemExit(f"exit distance mismatch, max abs diff {worst}")
    print(f"exit distances match RouteField (max abs diff {worst:.3e})")


def room_pose(grid, info):
    class World:
        pass
    world = World()
    world.grid = grid
    return _room_pose(world, info)


def hazard_at(grid, fire, fi, x, y, max_smoke):
    near = np.hypot(grid.X - float(x), grid.Y - float(y)) <= 5.0
    if bool(np.any(fire[fi] & near)):
        return "fire_near"
    if float(max_smoke) > 0.35:
        return "smoke_on_route"
    return None


def faces_case():
    raw = {
        "building": "Faces box",
        "wall_style": "faces",
        "floor_height_m": 4,
        "floors": [{
            "level": 1,
            "outline": [[0, 0], [10, 0], [10, 10], [0, 10]],
            "walls": [
                {"id": "W0", "x1": 0, "y1": 0, "x2": 10, "y2": 0},
                {"id": "W1", "x1": 5, "y1": 0, "x2": 5, "y2": 10},
            ],
            "spaces": [{
                "id": "A",
                "label": "Office A",
                "type": "office",
                "polygon": [[0, 0], [5, 0], [5, 10], [0, 10]],
            }],
            "doors": [{
                "id": "D",
                "x": 5, "y": 5, "width": 1.0,
                "exterior_exit": True,
                "connects": ["A"],
            }],
        }],
        "vertical_connections": [],
    }
    model = load_building(raw)
    grid = BuildingGrid(model)
    return raw, {
        "rows": grid.rows,
        "cols": grid.cols,
        "walkable_crc": crc_bytes(grid.walkable.astype(np.uint8).tobytes()),
        "room_crc": crc_bytes(np.ascontiguousarray(grid.room.astype(np.int32)).tobytes()),
    }


def slim_match(m):
    return {
        "room_id": m.get("room_id"),
        "level": m.get("level"),
        "label": m.get("label"),
        "score": m.get("score"),
        "points": [[round(p[0], 3), round(p[1], 3)] for p in m.get("points") or []],
    }


def main():
    model = load_building_file(str(SAMPLE))
    grid = BuildingGrid(model)
    check_exit_distances(grid)

    classrooms = [r for r in grid.rooms if r["type"] == "classroom"]
    start_info = next(r for r in classrooms if r["level"] == 3)
    floor1 = [r for r in classrooms if r["level"] == 1]
    dest_info = max(floor1, key=lambda r: r["centroid"][0])
    pose = room_pose(grid, start_info)
    if pose is None:
        raise SystemExit("start room has no pose")

    fire = np.zeros_like(grid.walkable)
    smoke = np.zeros(grid.walkable.shape, np.float64)
    route = route_between(grid, fire, smoke, grid.level_index[pose["level"]], pose["x"], pose["y"], dest_info["id"])
    if not route["reachable"]:
        raise SystemExit("expected a route: " + route["reason"])
    if route["path"][0][0] == route["path"][-1][0]:
        raise SystemExit("expected a multi-floor route")
    blob = "\n".join(route["instructions"])
    if "Elevator" in blob:
        raise SystemExit("elevator was used: " + blob)
    if "Stairs" not in blob and "stairs" not in blob:
        raise SystemExit("no stair instruction: " + blob)
    if not route["instructions"][-1].startswith("Arrive at"):
        raise SystemExit(route["instructions"][-1])

    hints = nav_hints(route["path"], pose["level"], pose["x"], pose["y"], model["plan_north_deg"])
    haz = hazard_at(grid, fire, grid.level_index[pose["level"]], pose["x"], pose["y"], route["max_smoke"])

    burned = fire.copy()
    fi = grid.level_index[dest_info["level"]]
    burned[fi][grid.room[fi] == dest_info["index"]] = True
    burned &= grid.walkable
    blocked = route_between(
        grid, burned, smoke, grid.level_index[pose["level"]], pose["x"], pose["y"], dest_info["id"]
    )
    if blocked["reachable"]:
        raise SystemExit("burned destination should be unreachable")

    links = []
    for a, b, cost, info in grid.links:
        links.append({
            "name": info["name"],
            "type": info["type"],
            "a": [int(a[0]), int(a[1]), int(a[2])],
            "b": [int(b[0]), int(b[1]), int(b[2])],
            "cost": round(float(cost), 4),
        })

    label = start_info["label"]
    token = str(label).split()[0]
    fuzzy = token[:1] + "A" + token[2:] if len(token) >= 3 else token + "A"
    tokens = [
        {"text": token, "conf": 0.9},
        {"text": fuzzy, "conf": 0.9},
        {"text": "EXIT", "conf": 1},
    ]
    matches = [slim_match(m) for m in match_rooms(tokens, model)]

    rng = LogRNG(42)
    pf = ParticleFilter(grid, model, rng)
    pf.start(pose["level"], pose["x"], pose["y"], 0.5, room_id=start_info["id"])
    pf.step(90.0, 0.05)
    sw = float(pf.w.sum())
    sq = float(np.sum(pf.w * pf.w))
    neff = (sw * sw) / sq if sq else 0.0
    print(f"neff after step {neff:.2f}")
    sight = {
        "text": token,
        "level": int(start_info["level"]),
        "score": 0.9,
        "points": [[float(start_info["centroid"][0]), float(start_info["centroid"][1])]],
        "room_id": start_info["id"],
    }
    # A same-floor sighting far from the threshold keeps resampling stable.
    if abs(neff - (ParticleFilter.N / 2.0)) < 40:
        sight = {
            "text": "OTHER",
            "level": 1 if start_info["level"] != 1 else 2,
            "score": 0.5,
            "points": [[0.0, 0.0]],
            "room_id": "other",
        }
    pf.observe([sight])
    body = pf.pose()
    faces_raw, faces_meta = faces_case()

    payload = {
        "grid": {
            "origin_x": grid.origin_x,
            "origin_y": grid.origin_y,
            "cell": grid.cell,
            "rows": grid.rows,
            "cols": grid.cols,
            "levels": list(grid.levels),
            "walkable_crc": crc_bytes(grid.walkable.astype(np.uint8).tobytes()),
            "room_crc": crc_bytes(np.ascontiguousarray(grid.room.astype(np.int32)).tobytes()),
            "room_count": len(grid.rooms),
            "links": links,
        },
        "faces_building": faces_raw,
        "faces": faces_meta,
        "pose": {
            "room_id": pose["room_id"],
            "level": pose["level"],
            "x": pose["x"],
            "y": pose["y"],
            "label": pose["label"],
        },
        "route": {
            "dest_room": dest_info["id"],
            "dest_label": dest_info["label"],
            "reachable": True,
            "distance_m": route["distance_m"],
            "max_smoke": route["max_smoke"],
            "path": route["path"],
            "instructions": route["instructions"],
            "turn": hints["turn"],
            "distance_to_next_m": hints["distance_to_next_m"],
            "heading_plan_deg": hints["heading_plan_deg"],
            "heading_compass_deg": hints["heading_compass_deg"],
            "next": hints["next"],
            "hazard": haz,
            "plan_north_deg": model["plan_north_deg"],
        },
        "blocked": {
            "reachable": False,
            "reason": blocked["reason"],
        },
        "particle": {
            "seed_note": "replay the recorded draws; do not reseed",
            "room_id": start_info["id"],
            "level": pose["level"],
            "x": pose["x"],
            "y": pose["y"],
            "sigma_m": 0.5,
            "heading_deg": 90.0,
            "length_m": 0.05,
            "normals": rng.normals,
            "integers": rng.ints,
            "units": rng.units,
            "sight": sight,
            "pose": {
                "level": body["level"],
                "x": body["x"],
                "y": body["y"],
                "spread_m": body["spread_m"],
                "confidence": body["confidence"],
                "source": body["source"],
                "unsure": body["unsure"],
                "n_steps": body["n_steps"],
            },
        },
        "text": {"tokens": tokens, "matches": matches},
    }

    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / "nav_cases.json"
    with dest.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    shutil.copyfile(SAMPLE, OUT / "sample_building.json")
    print(f"wrote {dest}")
    print(f"route {start_info['id']} -> {dest_info['id']} {route['distance_m']} m, {len(route['path'])} points")
    print("turn", hints["turn"], "blocked", blocked["reason"])
    print("particle", body["source"], "draws", len(rng.normals), len(rng.ints), len(rng.units))


if __name__ == "__main__":
    main()
