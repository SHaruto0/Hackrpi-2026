"""Rasterize the building into a 3D occupancy grid (floor, row, col).

All floors share one frame, so a stairwell at (x, y) lands on the same cell
on every floor. Cell (r, c) center = (origin_x + (c + .5) * cell,
origin_y + (r + .5) * cell).
"""
import math
import numpy as np

CELL = 0.5            # meters per cell
WALL_HALF = 0.3       # wall half-thickness for rasterizing (m)
DOOR_PAD = 0.25       # extra clearance around a door opening (m)
STAIR_COST_M = 10.0   # walking-meters equivalent per floor of stairs (~20 steps, slower than walking)
RAMP_COST_M = 7.0     # per floor for ramps / tiered hall aisles


def point_in_poly(X, Y, poly):
    inside = np.zeros(X.shape, dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        denom = (yj - yi) if yj != yi else 1e-12
        cond = ((yi > Y) != (yj > Y)) & (X < (xj - xi) * (Y - yi) / denom + xi)
        inside ^= cond
        j = i
    return inside


def seg_dist(X, Y, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return np.hypot(X - x1, Y - y1)
    t = np.clip(((X - x1) * dx + (Y - y1) * dy) / L2, 0.0, 1.0)
    return np.hypot(X - (x1 + t * dx), Y - (y1 + t * dy))


class BuildingGrid:
    def __init__(self, model, cell=CELL, pad=1.0):
        self.model = model
        self.cell = cell
        self.levels = list(model["levels"])
        self.level_index = {lv: i for i, lv in enumerate(self.levels)}
        self.warnings = []

        xs, ys = [], []
        for fl in model["floors"]:
            for p in fl["outline"]:
                xs.append(p[0]); ys.append(p[1])
            for w in fl["walls"]:
                xs += [w["x1"], w["x2"]]; ys += [w["y1"], w["y2"]]
            for s in fl["spaces"]:
                for p in s["polygon"]:
                    xs.append(p[0]); ys.append(p[1])
            for d in fl["doors"]:
                xs.append(d["x"]); ys.append(d["y"])
        for v in model["vertical_connections"]:
            xs.append(v["x"]); ys.append(v["y"])
        if not xs:
            raise ValueError("Building has no geometry")

        self.origin_x = min(xs) - pad
        self.origin_y = min(ys) - pad
        self.cols = int(math.ceil((max(xs) - min(xs) + 2 * pad) / cell))
        self.rows = int(math.ceil((max(ys) - min(ys) + 2 * pad) / cell))
        F = len(self.levels)
        X = self.origin_x + (np.arange(self.cols) + 0.5) * cell
        Y = self.origin_y + (np.arange(self.rows) + 0.5) * cell
        self.X, self.Y = np.meshgrid(X, Y)

        self.walkable = np.zeros((F, self.rows, self.cols), dtype=bool)
        self.inside = np.zeros((F, self.rows, self.cols), dtype=bool)   # inside the floor outline
        self.room = np.full((F, self.rows, self.cols), -1, dtype=np.int32)
        self.rooms = []        # list of space dicts (+ 'level', 'index')
        self.room_by_id = {}

        faces = model.get("wall_style") == "faces"
        for fl in model["floors"]:
            fi = self.level_index[fl["level"]]
            if fl["outline"]:
                inside = point_in_poly(self.X, self.Y, fl["outline"])
            else:
                inside = np.zeros_like(self.X, dtype=bool)
                for s in fl["spaces"]:
                    inside |= point_in_poly(self.X, self.Y, s["polygon"])
                if not inside.any():
                    inside[:] = True
            self.inside[fi] = inside
            wall = np.zeros_like(inside)
            if faces:   # CAD wall faces: block exactly the cells each thin line passes through
                for w in fl["walls"]:
                    n = max(2, int(math.hypot(w["x2"] - w["x1"], w["y2"] - w["y1"]) / (cell / 4)) + 2)
                    xs = np.linspace(w["x1"], w["x2"], n); ys = np.linspace(w["y1"], w["y2"], n)
                    cc = ((xs - self.origin_x) // cell).astype(int); rr = ((ys - self.origin_y) // cell).astype(int)
                    ok = (rr >= 0) & (rr < self.rows) & (cc >= 0) & (cc < self.cols)
                    wall[rr[ok], cc[ok]] = True
            else:
                for w in fl["walls"]:   # only touch the cells near each wall
                    c0 = max(0, int((min(w["x1"], w["x2"]) - WALL_HALF - self.origin_x) // cell))
                    c1 = min(self.cols, int((max(w["x1"], w["x2"]) + WALL_HALF - self.origin_x) // cell) + 2)
                    r0 = max(0, int((min(w["y1"], w["y2"]) - WALL_HALF - self.origin_y) // cell))
                    r1 = min(self.rows, int((max(w["y1"], w["y2"]) + WALL_HALF - self.origin_y) // cell) + 2)
                    if c0 >= c1 or r0 >= r1:
                        continue
                    wall[r0:r1, c0:c1] |= seg_dist(self.X[r0:r1, c0:c1], self.Y[r0:r1, c0:c1],
                                                   w["x1"], w["y1"], w["x2"], w["y2"]) <= WALL_HALF
            walk = inside & ~wall
            for d in fl["doors"]:
                if d.get("axis"):   # oriented: along the opening +-width/2, through the wall +-0.6 m
                    ux, uy = d["axis"]
                    dx, dy = self.X - d["x"], self.Y - d["y"]
                    along = np.abs(dx * ux + dy * uy); across = np.abs(-dx * uy + dy * ux)
                    walk |= (along <= d["width"] / 2 + 0.05) & (across <= 0.6) & inside
                else:
                    r = d["width"] / 2 + (0.1 if faces else DOOR_PAD)
                    walk |= (np.hypot(self.X - d["x"], self.Y - d["y"]) <= r) & inside
            for s in fl["spaces"]:
                if s["type"] == "void":
                    walk &= ~point_in_poly(self.X, self.Y, s["polygon"])
            self.walkable[fi] = walk

            for s in fl["spaces"]:
                mask = point_in_poly(self.X, self.Y, s["polygon"]) & inside
                idx = len(self.rooms)
                self.room[fi][mask] = idx
                info = dict(s, level=fl["level"], index=idx)
                info["centroid"] = self._centroid(s["polygon"])
                self.rooms.append(info)
                self.room_by_id[s["id"]] = info

        # exits
        self.exits = []   # list of (fi, r, c, door_id)
        for fl in model["floors"]:
            fi = self.level_index[fl["level"]]
            for d in fl["doors"]:
                if d["exterior_exit"]:
                    cell_ = self.nearest_walkable(fi, d["x"], d["y"], max_dist=2.0)
                    if cell_ is None:
                        self.warnings.append(f"Exit {d['id']} not reachable from any walkable cell")
                    else:
                        self.exits.append((fi, cell_[0], cell_[1], d["id"]))

        # vertical links: list of (cellA, cellB, cost_m, vc)
        self.links = []
        self.vertical_cells = {}   # vc id -> {level: (fi, r, c)}
        for v in model["vertical_connections"]:
            cells = {}
            for lv in v["floors"]:
                fi = self.level_index[lv]
                px, py = v.get("positions", {}).get(lv, (v["x"], v["y"]))
                c = self.nearest_walkable(fi, px, py, max_dist=3.0)
                if c is None:
                    self.warnings.append(f"{v['id']} has no walkable cell near it on floor {lv}")
                else:
                    cells[lv] = (fi, c[0], c[1])
            self.vertical_cells[v["id"]] = cells
            lvs = sorted(cells)
            for a, b in zip(lvs, lvs[1:]):
                span = self.level_index[b] - self.level_index[a]
                (fa, ra, ca), (fb, rb, cb) = cells[a], cells[b]
                run = self.cell * math.hypot(ra - rb, ca - cb)         # horizontal travel along the flights
                per = RAMP_COST_M if v["type"] == "ramp" else STAIR_COST_M
                self.links.append((cells[a], cells[b], per * span + run, v))

    @staticmethod
    def _centroid(poly):
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        return [sum(xs) / len(xs), sum(ys) / len(ys)]

    # --- coordinate helpers -------------------------------------------------
    def cell_of(self, x, y):
        c = int((x - self.origin_x) // self.cell)
        r = int((y - self.origin_y) // self.cell)
        return r, c

    def center_of(self, r, c):
        return (self.origin_x + (c + 0.5) * self.cell,
                self.origin_y + (r + 0.5) * self.cell)

    def nearest_walkable(self, fi, x, y, max_dist=3.0, mask=None):
        walk = self.walkable[fi] if mask is None else (self.walkable[fi] & mask)
        r, c = self.cell_of(x, y)
        if 0 <= r < self.rows and 0 <= c < self.cols and walk[r, c]:
            return (r, c)
        d = np.hypot(self.X - x, self.Y - y)
        d = np.where(walk, d, np.inf)
        k = int(np.argmin(d))
        if not math.isfinite(d.flat[k]) or d.flat[k] > max_dist:
            return None
        return divmod(k, self.cols)

    def flat(self, fi, r, c):
        return (fi * self.rows + r) * self.cols + c

    def unflat(self, idx):
        fi, rem = divmod(int(idx), self.rows * self.cols)
        r, c = divmod(rem, self.cols)
        return fi, r, c

    def meta(self):
        return {
            "origin_x": self.origin_x, "origin_y": self.origin_y,
            "cell": self.cell, "rows": self.rows, "cols": self.cols,
            "levels": self.levels,
        }
