"""Particle-filter localization for the phone (steps, stairs, door numbers).

Sessions live in memory and expire after 30 minutes idle.
`make_router(get_world, lock, seed=None)` mounts the /api/loc routes.
"""
import math
import threading
import time

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

N_PARTICLES = 500
IDLE_S = 30 * 60
MAX_SESSIONS = 200
WALL_WEIGHT = 0.02
STAIR_MISS_WEIGHT = 0.05
STAIR_REACH_M = 5.0
VISION_SIGMA_M = 3.0
UNDERFLOW = 1e-300


class ParticleFilter:
    """N walkable-floor particles. One shared numpy Generator (pass a seeded one in tests)."""

    N = N_PARTICLES

    def __init__(self, grid, model, rng):
        self.grid = grid
        self.model = model
        self.rng = rng
        self.plan_north = float(model.get("plan_north_deg") or 0.0) % 360.0
        self.session = None
        self.fi = np.zeros(self.N, dtype=np.int32)
        self.x = np.zeros(self.N, dtype=float)
        self.y = np.zeros(self.N, dtype=float)
        self.bias = np.zeros(self.N, dtype=float)
        self.w = np.full(self.N, 1.0 / self.N)
        self.source = "start"
        self.n_steps = 0
        self._last_fi = 0
        self._last_x = 0.0
        self._last_y = 0.0
        self._stairs = _stair_table(grid)

    def start(self, level, x, y, sigma_m, room_id=None):
        """Place the cloud on one floor, or uniformly on a room's walkable cells."""
        if sigma_m is None:
            sigma_m = 0.5
        sigma_m = float(sigma_m)
        if room_id:
            info = self.grid.room_by_id.get(str(room_id))
            if info is None:
                raise KeyError(room_id)
            level = info["level"]
        if level not in self.grid.level_index:
            raise KeyError(level)
        fi = self.grid.level_index[level]
        self.source = "start"
        self.n_steps = 0
        self.bias = self.rng.normal(0.0, 8.0, size=self.N)
        self.w = np.full(self.N, 1.0 / self.N)
        if room_id:
            self._fill_room(fi, self.grid.room_by_id[str(room_id)], float(x), float(y), sigma_m)
        else:
            self.fi = np.full(self.N, fi, dtype=np.int32)
            self.x = self.rng.normal(float(x), sigma_m, size=self.N)
            self.y = self.rng.normal(float(y), sigma_m, size=self.N)
        self._capture()

    def step(self, heading_deg, length_m):
        """Move one step. Compass heading, plan frame: 0 is up (-y), clockwise."""
        self._capture()
        plan = float(heading_deg) - self.plan_north
        heading = plan + self.bias + self.rng.normal(0.0, 6.0, size=self.N)
        length = float(length_m) * self.rng.normal(1.0, 0.12, size=self.N)
        rad = np.deg2rad(heading)
        dx = length * np.sin(rad)
        dy = -length * np.cos(rad)
        clear = self._segment_clear(self.x, self.y, dx, dy)
        self.bias = self.bias + self.rng.normal(0.0, 1.0, size=self.N)
        self.x = np.where(clear, self.x + dx, self.x)
        self.y = np.where(clear, self.y + dy, self.y)
        self.w = np.where(clear, self.w, self.w * WALL_WEIGHT)
        self.n_steps += 1
        self.source = "steps"
        self._after_weights()

    def stairs(self, direction):
        """Teleport particles near a stair (not an elevator) one floor up or down."""
        direction = str(direction).lower()
        if direction not in ("up", "down"):
            raise ValueError("direction must be up or down")
        self._capture()
        self.source = "stairs"
        take, dest_fi, dest_x, dest_y = self._stair_moves(direction)
        self.fi = np.where(take, dest_fi, self.fi).astype(np.int32)
        self.x = np.where(take, dest_x, self.x)
        self.y = np.where(take, dest_y, self.y)
        self.w = np.where(take, self.w, self.w * STAIR_MISS_WEIGHT)
        self._after_weights()

    def observe(self, matches):
        """Reweight from room sightings. Best score wins when several share a token."""
        self._capture()
        applied = False
        for m in _best_matches(matches):
            if self._apply_match(m):
                applied = True
        if applied:
            self.source = "vision"
        self._after_weights()

    def pose(self):
        level, x, y, spread, conf, _fi = self._estimate()
        body = {
            "level": int(level),
            "x": float(x),
            "y": float(y),
            "spread_m": float(spread),
            "confidence": float(conf),
            "source": self.source,
            "unsure": bool(spread > 4.0),
            "n_steps": int(self.n_steps),
        }
        if self.session is not None:
            from . import store   # position trace in MongoDB Atlas (throttled, no-op when off)
            store.log("loc_pose", {"session": self.session, **body})
            return {"session": self.session, **body}
        return body

    # --- internals ----------------------------------------------------------
    def _fill_room(self, fi, info, x, y, sigma_m):
        g = self.grid
        mask = g.walkable[fi] & (g.room[fi] == info["index"])
        rr, cc = np.nonzero(mask)
        self.fi = np.full(self.N, fi, dtype=np.int32)
        if len(rr) == 0:
            cx, cy = info.get("centroid") or (x, y)
            self.x = self.rng.normal(float(cx), sigma_m, size=self.N)
            self.y = self.rng.normal(float(cy), sigma_m, size=self.N)
            return
        pick = self.rng.integers(0, len(rr), size=self.N)
        self.x = g.origin_x + (cc[pick] + 0.5) * g.cell
        self.y = g.origin_y + (rr[pick] + 0.5) * g.cell

    def _segment_clear(self, x, y, dx, dy):
        """True when 4 (or denser) samples along the move stay on this floor's walkable cells."""
        dist = np.hypot(dx, dy)
        max_dist = float(dist.max()) if dist.size else 0.0
        cell = float(self.grid.cell)
        n = 4
        if max_dist > cell:
            n = min(256, max(4, int(math.ceil(max_dist / (cell * 0.5)))))
        t = (np.arange(1, n + 1, dtype=float) / n)[:, None]
        px = x[None, :] + t * dx[None, :]
        py = y[None, :] + t * dy[None, :]
        return self._points_walkable(self.fi, px, py)

    def _points_walkable(self, fi, px, py):
        """px, py shaped (samples, N). Every sample must be in-grid and walkable."""
        g = self.grid
        c = np.floor((px - g.origin_x) / g.cell).astype(np.int32)
        r = np.floor((py - g.origin_y) / g.cell).astype(np.int32)
        inside = (r >= 0) & (r < g.rows) & (c >= 0) & (c < g.cols)
        rr = np.clip(r, 0, g.rows - 1)
        cc = np.clip(c, 0, g.cols - 1)
        walk = g.walkable[fi[None, :], rr, cc]
        return np.all(inside & walk, axis=0)

    def _mask_walkable(self, fi, x, y):
        g = self.grid
        c = np.floor((np.asarray(x) - g.origin_x) / g.cell).astype(np.int32)
        r = np.floor((np.asarray(y) - g.origin_y) / g.cell).astype(np.int32)
        inside = (r >= 0) & (r < g.rows) & (c >= 0) & (c < g.cols)
        rr = np.clip(r, 0, g.rows - 1)
        cc = np.clip(c, 0, g.cols - 1)
        return inside & g.walkable[int(fi), rr, cc]

    def _stair_moves(self, direction):
        st = self._stairs
        stay_fi = self.fi.copy()
        stay_x = self.x.copy()
        stay_y = self.y.copy()
        if st is None:
            return np.zeros(self.N, dtype=bool), stay_fi, stay_x, stay_y
        sign = 1 if direction == "up" else -1
        d0 = np.hypot(self.x[:, None] - st["x"][None, :, 0], self.y[:, None] - st["y"][None, :, 0])
        d1 = np.hypot(self.x[:, None] - st["x"][None, :, 1], self.y[:, None] - st["y"][None, :, 1])
        on0 = self.fi[:, None] == st["fi"][None, :, 0]
        on1 = self.fi[:, None] == st["fi"][None, :, 1]
        dir0 = (st["lv"][None, :, 1] - st["lv"][None, :, 0]) * sign > 0
        dir1 = (st["lv"][None, :, 0] - st["lv"][None, :, 1]) * sign > 0
        d0 = np.where(on0 & dir0, d0, np.inf)
        d1 = np.where(on1 & dir1, d1, np.inf)
        both = np.concatenate([d0, d1], axis=1)
        k = np.argmin(both, axis=1)
        best = both[np.arange(self.N), k]
        take = best <= STAIR_REACH_M
        s_count = st["fi"].shape[0]
        stair = np.where(k < s_count, k, k - s_count)
        dest_end = np.where(k < s_count, 1, 0)
        dest_fi = st["fi"][stair, dest_end]
        dest_x = st["x"][stair, dest_end]
        dest_y = st["y"][stair, dest_end]
        return take, dest_fi, dest_x, dest_y

    def _apply_match(self, m):
        try:
            level = int(m.get("level"))
            mfi = self.grid.level_index[level]
        except (TypeError, ValueError, KeyError):
            return False
        same = self.fi == mfi
        pts = m.get("points") or []
        if np.any(same):
            if pts:
                px = np.array([float(p[0]) for p in pts], dtype=float)
                py = np.array([float(p[1]) for p in pts], dtype=float)
                d2 = (self.x[same, None] - px[None, :]) ** 2 + (self.y[same, None] - py[None, :]) ** 2
                d = np.sqrt(d2.min(axis=1))
                like = 0.2 + np.exp(-(d ** 2) / (2.0 * VISION_SIGMA_M ** 2))
                self.w[same] *= like
            else:
                self.w[same] *= 0.2
        other = ~same
        if np.any(other):
            self.w[other] *= 0.3
        return True

    def _estimate(self):
        w = self.w
        if (not np.isfinite(w).all()) or float(w.sum()) <= 0.0:
            w = np.ones(self.N)
        wn = w / w.sum()
        best_fi = int(self.fi[0])
        best_mass = -1.0
        for fi in np.unique(self.fi):
            mass = float(wn[self.fi == fi].sum())
            if mass > best_mass:
                best_mass = mass
                best_fi = int(fi)
        on = self.fi == best_fi
        ww = wn[on]
        ww = ww / ww.sum()
        xs = self.x[on]
        ys = self.y[on]
        x = float(np.sum(ww * xs))
        y = float(np.sum(ww * ys))
        vx = float(np.sum(ww * (xs - x) ** 2))
        vy = float(np.sum(ww * (ys - y) ** 2))
        spread = math.sqrt(max(0.0, vx + vy))
        conf = float(np.clip(1.0 - spread / 6.0, 0.0, 1.0))
        level = int(self.grid.levels[best_fi])
        return level, x, y, spread, conf, best_fi

    def _capture(self):
        _level, x, y, _spread, _conf, fi = self._estimate()
        self._last_fi = int(fi)
        self._last_x = float(x)
        self._last_y = float(y)

    def _dead(self):
        if not np.isfinite(self.w).all():
            return True
        return float(self.w.sum()) <= UNDERFLOW

    def _after_weights(self):
        if self._dead():
            self._reinit()
            return
        self._resample_if_needed()

    def _resample_if_needed(self):
        sw = float(self.w.sum())
        sq = float(np.sum(self.w * self.w))
        # Squares underflow to 0 while the sum is still above the dead-cloud cutoff.
        if sq == 0.0 or not math.isfinite(sq) or not math.isfinite(sw):
            self._reinit()
            return
        neff = (sw * sw) / sq
        if neff >= self.N / 2.0:
            return
        u0 = float(self.rng.random())
        positions = (np.arange(self.N) + u0) / self.N
        cdf = np.cumsum(self.w)
        cdf /= cdf[-1]
        idx = np.searchsorted(cdf, positions, side="left")
        idx = np.minimum(idx, self.N - 1)
        self.fi = self.fi[idx].astype(np.int32)
        self.x = self.x[idx].copy()
        self.y = self.y[idx].copy()
        self.bias = self.bias[idx].copy()
        self.w = np.full(self.N, 1.0 / self.N)

    def _reinit(self):
        """Cloud collapsed. Scatter again on walkable cells around the last pose. Source stays."""
        fi = int(self._last_fi)
        x = float(self._last_x)
        y = float(self._last_y)
        got_x = []
        got_y = []
        for _ in range(40):
            if len(got_x) >= self.N:
                break
            cx = self.rng.normal(x, 2.0, size=self.N)
            cy = self.rng.normal(y, 2.0, size=self.N)
            ok = self._mask_walkable(fi, cx, cy)
            got_x.extend(cx[ok].tolist())
            got_y.extend(cy[ok].tolist())
        if len(got_x) < self.N:
            g = self.grid
            rr, cc = np.nonzero(g.walkable[fi])
            need = self.N - len(got_x)
            if len(rr) == 0:
                got_x.extend([x] * need)
                got_y.extend([y] * need)
            else:
                gx = g.origin_x + (cc + 0.5) * g.cell
                gy = g.origin_y + (rr + 0.5) * g.cell
                d2 = (gx - x) ** 2 + (gy - y) ** 2
                near = np.flatnonzero(d2 <= 4.0)
                pool = near if len(near) else np.argsort(d2)[: max(1, min(len(d2), 40))]
                pick = self.rng.choice(pool, size=need, replace=True)
                got_x.extend(gx[pick].tolist())
                got_y.extend(gy[pick].tolist())
        self.x = np.asarray(got_x[: self.N], dtype=float)
        self.y = np.asarray(got_y[: self.N], dtype=float)
        self.fi = np.full(self.N, fi, dtype=np.int32)
        self.bias = self.rng.normal(0.0, 8.0, size=self.N)
        self.w = np.full(self.N, 1.0 / self.N)


def _stair_table(grid):
    rows = []
    for a, b, _cost, v in grid.links:
        if str(v.get("type", "")).lower() == "elevator":
            continue
        fa, ra, ca = a
        fb, rb, cb = b
        ax, ay = grid.center_of(ra, ca)
        bx, by = grid.center_of(rb, cb)
        rows.append((fa, ax, ay, grid.levels[fa], fb, bx, by, grid.levels[fb]))
    if not rows:
        return None
    arr = np.array(rows, dtype=float)
    return {
        "fi": arr[:, [0, 4]].astype(np.int32),
        "x": arr[:, [1, 5]],
        "y": arr[:, [2, 6]],
        "lv": arr[:, [3, 7]].astype(np.int32),
    }


def _best_matches(matches):
    """Keep the highest score for each token text. Matches without text all stay."""
    if not matches:
        return []
    best = {}
    order = []
    solo = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        text = m.get("text", None)
        if text is None or text == "":
            token = m.get("token", None)
            if token is None or token == "":
                solo.append(m)
                continue
            text = token
        key = str(text)
        score = float(m.get("score") or 0.0)
        prev = best.get(key)
        if prev is None:
            order.append(key)
            best[key] = (score, m)
        elif score > prev[0]:
            best[key] = (score, m)
    return [best[k][1] for k in order] + solo


class StartBody(BaseModel):
    session: str
    level: int
    x: float
    y: float
    sigma_m: float = 0.5
    room_id: str | None = None


class StepBody(BaseModel):
    t: float | None = None
    heading_deg: float
    length_m: float


class StepsBody(BaseModel):
    session: str
    steps: list[StepBody] = Field(default_factory=list)


class StairsBody(BaseModel):
    session: str
    direction: str


def make_router(get_world, lock, seed=None):
    """Localization routes. `seed` fixes the numpy Generator for tests."""
    router = APIRouter()
    rng = np.random.default_rng(seed)
    sessions = {}
    gate = lock if lock is not None else threading.Lock()

    def _forget(sid):
        try:
            from . import store
            store.forget_session(sid)
        except Exception:
            pass

    def purge(now):
        dead = [k for k, s in sessions.items() if now - s["t"] > IDLE_S]
        for k in dead:
            sessions.pop(k, None)
            _forget(k)
        extra = len(sessions) - MAX_SESSIONS
        if extra <= 0:
            return
        oldest = sorted(sessions, key=lambda k: sessions[k]["t"])[:extra]
        for k in oldest:
            sessions.pop(k, None)
            _forget(k)

    def require(sid, now):
        purge(now)
        s = sessions.get(sid)
        if s is None:
            raise HTTPException(404, "Unknown session")
        s["t"] = now
        return s

    @router.post("/api/loc/start")
    def loc_start(body: StartBody):
        now = time.monotonic()
        with gate:
            world = get_world()
            grid = world.grid
            if body.room_id:
                if body.room_id not in grid.room_by_id:
                    raise HTTPException(404, f"Unknown room {body.room_id}")
            elif body.level not in grid.level_index:
                raise HTTPException(404, f"Unknown floor level {body.level}")
            if not math.isfinite(body.x) or not math.isfinite(body.y) or not math.isfinite(body.sigma_m):
                raise HTTPException(422, "x, y, and sigma_m must be finite")
            pf = ParticleFilter(grid, world.model, rng)
            try:
                pf.start(body.level, body.x, body.y, body.sigma_m, body.room_id)
            except KeyError as exc:
                raise HTTPException(404, f"Unknown location {exc}") from exc
            sid = str(body.session)
            pf.session = sid
            sessions[sid] = {"pf": pf, "t": now}
            purge(now)
            return pf.pose()

    @router.post("/api/loc/steps")
    def loc_steps(body: StepsBody):
        now = time.monotonic()
        with gate:
            pf = require(str(body.session), now)["pf"]
            for s in body.steps:
                pf.step(s.heading_deg, s.length_m)
            return pf.pose()

    @router.post("/api/loc/stairs")
    def loc_stairs(body: StairsBody):
        now = time.monotonic()
        direction = body.direction.lower()
        if direction not in ("up", "down"):
            raise HTTPException(400, "direction must be up or down")
        with gate:
            pf = require(str(body.session), now)["pf"]
            pf.stairs(direction)
            return pf.pose()

    @router.post("/api/loc/frame")
    def loc_frame(
        session: str = Form(...),
        image: UploadFile = File(...),
        heading_deg: float | None = Form(None),
    ):
        jpeg = image.file.read()
        now = time.monotonic()
        try:
            from . import vision
        except ImportError:
            with gate:
                pf = require(str(session), now)["pf"]
                out = pf.pose()
                out["seen"] = []
                out["detail"] = "vision module is not available"
                return out
        with gate:
            pf = require(str(session), now)["pf"]
            model = pf.model
        try:
            chosen, seen = _read_frame(vision, jpeg, model)
        except Exception as exc:
            with gate:
                pf = require(str(session), now)["pf"]
                out = pf.pose()
                out["seen"] = []
                out["detail"] = f"vision failed: {type(exc).__name__}"
                return out
        with gate:
            pf = require(str(session), now)["pf"]
            # heading_deg is the phone compass at capture time. The step API owns motion.
            _ = heading_deg
            pf.observe(chosen)
            out = pf.pose()
            out["seen"] = seen
            return out

    @router.get("/api/loc/{session}")
    def loc_get(session: str):
        now = time.monotonic()
        with gate:
            pf = require(session, now)["pf"]
            return pf.pose()

    return router


def _read_frame(vision, jpeg, model):
    """OCR, then the best room match for each token."""
    tokens = vision.read_text(jpeg) or []
    if not isinstance(tokens, list):
        tokens = []
    chosen = []
    seen = []
    for tok in tokens:
        if not isinstance(tok, dict):
            continue
        text = str(tok.get("text") or "")
        cands = vision.match_rooms([tok], model) or []
        if not isinstance(cands, list):
            cands = []
        dicts = [c for c in cands if isinstance(c, dict)]
        if not dicts:
            seen.append({"text": text, "room_id": None, "score": float(tok.get("conf") or 0.0)})
            continue
        best = max(dicts, key=lambda m: float(m.get("score") or 0.0))
        item = dict(best)
        item["text"] = text
        chosen.append(item)
        seen.append({
            "text": text,
            "room_id": best.get("room_id"),
            "score": float(best.get("score") or 0.0),
        })
    return chosen, seen
