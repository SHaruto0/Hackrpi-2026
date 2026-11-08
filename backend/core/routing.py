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

HALL_COST_X = 3.0     # cost multiplier for walking through lecture hall seating
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
        # walking across a lecture hall (seating) is slow: routes go around halls unless they start/end in one
        hall_ids = [r["index"] for r in g.rooms if r["type"] == "lecture_hall"]
        slow = np.where(np.isin(g.room, hall_ids), HALL_COST_X, 1.0) if hall_ids else np.ones(g.room.shape)
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
            ha = slow[:, r0:r1, c0:c1][ok]
            hb = slow[:, r0 + dr:r1 + dr, c0 + dc:c1 + dc][ok]
            wts.append(step * (1.0 + SMOKE_PENALTY * (sa + sb) / 2.0) * (ha + hb) / 2.0)

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
        self.graph = graph            # kept for point-to-point routes (route_between)

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
    def route_between(self, fi, x, y, gfi, gx, gy):
        """Safest path from (fi, x, y) to any walkable goal (gfi, gx, gy): same costs as evacuation routes
        (fire and blocked cells impassable, smoke expensive, stairs cost, no elevators)."""
        g = self.grid
        a = g.nearest_walkable(fi, x, y, max_dist=3.0, mask=self.passable[fi])
        b = g.nearest_walkable(gfi, gx, gy, max_dist=3.0, mask=self.passable[gfi])
        if a is None or b is None:
            return {"reachable": False, "reason": "start or goal is not on a safe walkable area",
                    "path": [], "distance_m": None, "instructions": []}
        start, goal = g.flat(fi, *a), g.flat(gfi, *b)
        dist, pred = dijkstra(self.graph, directed=True, indices=goal, return_predecessors=True)
        if not math.isfinite(dist[start]):
            return {"reachable": False, "reason": "no safe path between those points",
                    "path": [], "distance_m": None, "instructions": []}
        nodes = [start]
        while nodes[-1] != goal and pred[nodes[-1]] != NO_PRED and len(nodes) < 1_000_000:
            nodes.append(int(pred[nodes[-1]]))
        cells = [g.unflat(k) for k in nodes]
        steps = self._instructions(cells, nodes, arrive="Walk {d} m{where} to arrive")
        return {"reachable": True, "reason": "", "path": self._simplify(cells),
                "distance_m": round(self._length(cells), 1),
                "max_smoke": round(float(max(self.smoke[c] for c in cells)), 2), "instructions": steps}

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
        link_cost = {(l[0], l[1]): l[2] for l in g.links}
        link_cost.update({(l[1], l[0]): l[2] for l in g.links})
        for a, b in zip(cells, cells[1:]):
            if a[0] == b[0]:
                total += g.cell * math.hypot(a[1] - b[1], a[2] - b[2])
            else:                       # stairs count as their walking-equivalent cost
                total += link_cost.get((tuple(a), tuple(b)), 5.0)
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

    def _instructions(self, cells, nodes, arrive="Walk {d} m{where} to exit {exit}"):
        """Turn-by-turn steps from the path shape: "Walk 3 m, then turn left", "Take Stair 3 down to floor 1",
        "Walk 7 m to the exit". Grid zig-zags are smoothed first (Douglas-Peucker, 0.75 m)."""
        g = self.grid
        runs, cur = [], [0]
        for i in range(1, len(cells)):
            if cells[i][0] != cells[i - 1][0]:
                runs.append(cur); cur = [i]
            else:
                cur.append(i)
        runs.append(cur)
        xy = lambda k: g.center_of(cells[k][1], cells[k][2])

        def rdp(idx, eps=0.75):
            if len(idx) < 3:
                return idx
            a, b = xy(idx[0]), xy(idx[-1])
            L = math.dist(a, b) or 1e-9
            far, fk = -1.0, 0
            for k in range(1, len(idx) - 1):
                p = xy(idx[k])
                d = abs((b[0] - a[0]) * (a[1] - p[1]) - (a[0] - p[0]) * (b[1] - a[1])) / L
                if d > far:
                    far, fk = d, k
            if far <= eps:
                return [idx[0], idx[-1]]
            return rdp(idx[:fk + 1], eps)[:-1] + rdp(idx[fk:], eps)

        def where(k0, k1):              # room the walk mostly passes through ("through 218 LOBBY")
            rid = int(g.room[cells[(k0 + k1) // 2]])
            if rid < 0:
                return ""
            info = g.rooms[rid]
            return f" through {info['label'] if info['type'] != 'hallway' else 'the hallway ' + info['label'].split(' ')[0]}"

        steps = []
        for n, run in enumerate(runs):
            ids = rdp(run)
            pts = [xy(k) for k in ids]
            dist, k0 = 0.0, ids[0]
            for i in range(1, len(pts)):
                dist += math.dist(pts[i - 1], pts[i])
                if i == len(pts) - 1:
                    break
                d1 = (pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                d2 = (pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                ang = math.degrees(math.atan2(d1[0] * d2[1] - d1[1] * d2[0], d1[0] * d2[0] + d1[1] * d2[1]))
                if abs(ang) < 35 or math.dist(pts[i], pts[i + 1]) < 1.0:
                    continue            # y points down, so a positive cross product is a right turn
                side = "right" if ang > 0 else "left"
                turn = f"turn {'slightly ' if abs(ang) < 60 else ''}{side}" if abs(ang) < 150 else "turn around"
                steps.append(f"Walk {max(1, round(dist))} m{where(k0, ids[i])}, then {turn}")
                dist, k0 = 0.0, ids[i]
            if n < len(runs) - 1:
                a, b = run[-1], runs[n + 1][0]
                if dist >= 1.0:
                    steps.append(f"Walk {round(dist)} m{where(k0, run[-1])} to the stairs")
                name = self.link_names.get((nodes[a], nodes[b]), "the stairs")
                direction = "down" if cells[b][0] < cells[a][0] else "up"
                steps.append(f"Take {name} {direction} to floor {g.levels[cells[b][0]]}")
            else:
                exit_id = self.exit_ids.get(nodes[-1], "")
                steps.append(arrive.format(d=max(1, round(dist)), where=where(k0, run[-1]), exit=exit_id))
        return steps


def best_room_cell(field, info):
    """Start cell for "can people in this room get out?": the walkable cell in the room nearest its center that
    still has a route. A room counts as escapable if any part of it reaches an exit, not only its center
    (a merged room's center can sit in a closet). Returns (fi, (r, c)) or (fi, None) if the room has no floor."""
    g = field.grid
    fi = g.level_index[info["level"]]
    in_room = (g.room[fi] == info["index"]) & g.walkable[fi]
    if not in_room.any():
        return fi, None
    dist = field.dist.reshape(g.walkable.shape)[fi]
    ok = in_room & np.isfinite(dist)
    pool = ok if ok.any() else in_room
    cx, cy = info["centroid"]
    d2 = np.where(pool, (g.X - cx) ** 2 + (g.Y - cy) ** 2, np.inf)
    k = int(np.argmin(d2))
    return fi, divmod(k, g.cols)

