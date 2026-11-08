"""Mock fire + smoke simulation on the building grid (cellular automaton).

- Fire spreads to walkable 4-neighbors with probability P_SPREAD per step
  (walls block it, open doors let it through).
- Smoke is 0..1, sourced from fire, diffuses with decay, and RISES through
  stairs/ramps/elevators (strongly up, weakly down).
Swap `step()` for real sensor data later: just write into self.fire / self.smoke.
"""
import numpy as np

P_SPREAD = 0.30
SMOKE_DECAY = 0.92       # per cell of travel
SMOKE_ITERS = 2          # smoke travels ~2 cells per step
SMOKE_UP = 0.97          # transfer factor up a stairwell
SMOKE_DOWN = 0.35        # transfer factor down a stairwell
P_LINK_UP = 0.30         # fire at a stair/hall-seating link climbs to the floor above
P_LINK_DOWN = 0.10       # ... or drops to the floor below
BURN_STEPS = 3           # a cell burning this long can break through the slab
P_CEIL = 0.04            # through the ceiling to the cell directly above (fire rises)
P_FLOOR = 0.01           # through the floor to the cell directly below (collapse)


def _neighbor_max(a):
    """Max of 4-neighbors within each floor. a: (F, R, C)."""
    out = np.zeros_like(a)
    out[:, 1:, :] = np.maximum(out[:, 1:, :], a[:, :-1, :])
    out[:, :-1, :] = np.maximum(out[:, :-1, :], a[:, 1:, :])
    out[:, :, 1:] = np.maximum(out[:, :, 1:], a[:, :, :-1])
    out[:, :, :-1] = np.maximum(out[:, :, :-1], a[:, :, 1:])
    return out


class FireSim:
    def __init__(self, grid, seed=7):
        self.grid = grid
        self.seed = seed
        self.reset()

    def reset(self):
        g = self.grid
        shape = g.walkable.shape
        self.fire = np.zeros(shape, dtype=bool)
        self.smoke = np.zeros(shape, dtype=np.float32)
        self.t = 0
        self.rng = np.random.default_rng(self.seed)
        self.origins = []
        self.burn = np.zeros(shape, dtype=np.int16)    # steps each cell has been burning
        if not hasattr(self, "mode"):
            self.mode = "simulate"                      # "reports": no spread, only reported/ignited fire

    def ignite_point(self, fi, x, y, radius=1.0):
        g = self.grid
        cell = g.nearest_walkable(fi, x, y, max_dist=5.0)
        if cell is None:
            return False
        cx, cy = g.center_of(*cell)
        patch = (np.hypot(g.X - cx, g.Y - cy) <= radius) & g.walkable[fi]
        self.fire[fi] |= patch
        self.smoke[fi][patch] = 1.0
        self.origins.append({"level": g.levels[fi], "x": cx, "y": cy})
        return True

    def ignite_room(self, room_id):
        g = self.grid
        info = g.room_by_id.get(room_id)
        if info is None:
            raise KeyError(room_id)
        fi = g.level_index[info["level"]]
        in_room = g.room[fi] == info["index"]
        cell = g.nearest_walkable(fi, *info["centroid"], max_dist=50.0, mask=in_room)
        if cell is None:
            return False
        return self.ignite_point(fi, *g.center_of(*cell))

    def step(self, n=1):
        g = self.grid
        walk = g.walkable
        for _ in range(n):
            if self.mode == "reports":                  # firefighter reports only: nothing spreads on its own
                self.t += 1
                continue
            self.burn[self.fire] += 1
            nb = _neighbor_max(self.fire.astype(np.uint8)).astype(bool)
            cand = nb & walk & ~self.fire
            new = cand & (self.rng.random(cand.shape) < P_SPREAD)
            # between floors: through stairs / tiered hall seating links
            for (fa, ra, ca), (fb, rb, cb), _cost, v in g.links:
                if v["type"] == "elevator":
                    continue
                if self.fire[fa, ra, ca] and not self.fire[fb, rb, cb] and self.rng.random() < P_LINK_UP:
                    new[fb, rb, cb] = walk[fb, rb, cb]
                if self.fire[fb, rb, cb] and not self.fire[fa, ra, ca] and self.rng.random() < P_LINK_DOWN:
                    new[fa, ra, ca] = walk[fa, ra, ca]
            # through slabs: long-burning cells ignite the cell above (likely) or below (rare)
            hot = self.burn >= BURN_STEPS
            if hot[:-1].any():
                up = hot[:-1] & (self.rng.random(hot[:-1].shape) < P_CEIL)
                new[1:] |= up & walk[1:] & ~self.fire[1:]
                down = hot[1:] & (self.rng.random(hot[1:].shape) < P_FLOOR)
                new[:-1] |= down & walk[:-1] & ~self.fire[:-1]
            self.fire |= new

            s = np.maximum(self.smoke, self.fire.astype(np.float32))
            for _ in range(SMOKE_ITERS):
                s = np.maximum(s, SMOKE_DECAY * _neighbor_max(s))
                s *= walk
                for (fa, ra, ca), (fb, rb, cb), _cost, _v in g.links:
                    lo, hi = s[fa, ra, ca], s[fb, rb, cb]   # a is the lower floor
                    s[fb, rb, cb] = max(hi, lo * SMOKE_UP)
                    s[fa, ra, ca] = max(lo, hi * SMOKE_DOWN)
            self.smoke = s.astype(np.float32)
            self.t += 1

    def room_status(self):
        """Per-room summary: 'fire' | 'smoke' | 'clear'."""
        g = self.grid
        out = {}
        for info in g.rooms:
            fi = g.level_index[info["level"]]
            m = g.room[fi] == info["index"]
            if not m.any():
                out[info["id"]] = "clear"
            elif self.fire[fi][m].any():
                out[info["id"]] = "fire"
            elif self.smoke[fi][m].max() > 0.35:
                out[info["id"]] = "smoke"
            else:
                out[info["id"]] = "clear"
        return out

    def vertical_status(self):
        g = self.grid
        out = {}
        for vid, cells in g.vertical_cells.items():
            worst = "clear"
            for fi, r, c in cells.values():
                if self.fire[fi, r, c]:
                    worst = "fire"; break
                if self.smoke[fi, r, c] > 0.35:
                    worst = "smoke"
            out[vid] = worst
        return out

    def cells_payload(self, smoke_min=0.15):
        g = self.grid
        fire, smoke = {}, {}
        for fi, lv in enumerate(g.levels):
            fr, fc = np.nonzero(self.fire[fi])
            fire[lv] = np.stack([fr, fc], axis=1).tolist()
            sm = (self.smoke[fi] >= smoke_min) & ~self.fire[fi]
            sr, sc = np.nonzero(sm)
            vals = np.round(self.smoke[fi][sm], 2)
            smoke[lv] = [[int(r), int(c), float(v)] for r, c, v in zip(sr, sc, vals)]
        return fire, smoke
