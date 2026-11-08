import Foundation

struct FilterPose {
    var level: Int
    var x: Double
    var y: Double
    var spreadM: Double
    var confidence: Double
    var source: String
    var unsure: Bool
    var nSteps: Int
}

struct Sighting {
    var text: String
    var level: Int
    var score: Double
    var points: [PlanPoint]
}

/// 500 walkable-floor particles. Step, stair, and vision updates match backend/services/localize.py.
final class ParticleFilter {
    static let count = 500
    static let wallWeight = 0.02
    static let stairMiss = 0.05
    static let stairReach = 5.0
    static let visionSigma = 3.0
    static let underflow = 1e-300

    let grid: BuildingGrid
    let planNorth: Double
    let rng: RandomSource
    private let n = ParticleFilter.count

    private var fi: [Int]
    private var x: [Double]
    private var y: [Double]
    private var bias: [Double]
    private var w: [Double]
    private var source = "start"
    private var nSteps = 0
    private var lastFi = 0
    private var lastX = 0.0
    private var lastY = 0.0
    private let stairs: [StairPair]

    init(grid: BuildingGrid, planNorth: Double, rng: RandomSource) {
        self.grid = grid
        self.planNorth = planNorth.truncatingRemainder(dividingBy: 360)
        self.rng = rng
        fi = [Int](repeating: 0, count: ParticleFilter.count)
        x = [Double](repeating: 0, count: ParticleFilter.count)
        y = [Double](repeating: 0, count: ParticleFilter.count)
        bias = [Double](repeating: 0, count: ParticleFilter.count)
        w = [Double](repeating: 1.0 / Double(ParticleFilter.count), count: ParticleFilter.count)
        var pairs: [StairPair] = []
        for link in grid.links where link.type.lowercased() != "elevator" {
            let a = grid.centerOf(link.aR, link.aC)
            let b = grid.centerOf(link.bR, link.bC)
            pairs.append(StairPair(
                aFi: link.aFi, ax: a.x, ay: a.y, aLevel: grid.levels[link.aFi],
                bFi: link.bFi, bx: b.x, by: b.y, bLevel: grid.levels[link.bFi]
            ))
        }
        stairs = pairs
    }

    func start(level: Int, x x0: Double, y y0: Double, sigma: Double, roomId: String?) {
        var level = level
        if let roomId, let info = grid.roomById[roomId] {
            level = info.level
        }
        guard let fi0 = grid.levelIndex[level] else { return }
        source = "start"
        nSteps = 0
        for i in 0..<n { bias[i] = rng.nextNormal(mean: 0, std: 8) }
        w = [Double](repeating: 1.0 / Double(n), count: n)
        if let roomId, let info = grid.roomById[roomId] {
            fillRoom(fi0, info, x0, y0, sigma)
        } else {
            for i in 0..<n { fi[i] = fi0 }
            for i in 0..<n { x[i] = rng.nextNormal(mean: x0, std: sigma) }
            for i in 0..<n { y[i] = rng.nextNormal(mean: y0, std: sigma) }
        }
        capture()
    }

    func step(headingDeg: Double, lengthM: Double) {
        capture()
        let plan = headingDeg - planNorth
        var headN = [Double](repeating: 0, count: n)
        var lenN = [Double](repeating: 0, count: n)
        for i in 0..<n { headN[i] = rng.nextNormal(mean: 0, std: 6) }
        for i in 0..<n { lenN[i] = rng.nextNormal(mean: 1, std: 0.12) }
        var dx = [Double](repeating: 0, count: n)
        var dy = [Double](repeating: 0, count: n)
        for i in 0..<n {
            let heading = plan + bias[i] + headN[i]
            let length = lengthM * lenN[i]
            let rad = heading * .pi / 180
            dx[i] = length * sin(rad)
            dy[i] = -length * cos(rad)
        }
        let clear = segmentClear(dx, dy)
        for i in 0..<n { bias[i] += rng.nextNormal(mean: 0, std: 1) }
        for i in 0..<n where clear[i] {
            x[i] += dx[i]
            y[i] += dy[i]
        }
        for i in 0..<n where !clear[i] { w[i] *= Self.wallWeight }
        nSteps += 1
        source = "steps"
        afterWeights()
    }

    func stairs(direction: String) {
        let dir = direction.lowercased()
        guard dir == "up" || dir == "down" else { return }
        capture()
        source = "stairs"
        let sign = dir == "up" ? 1 : -1
        for i in 0..<n {
            var best = Double.infinity
            var destFi = fi[i]
            var destX = x[i]
            var destY = y[i]
            var take = false
            for s in stairs {
                let up = (s.bLevel - s.aLevel) * sign > 0
                if fi[i] == s.aFi && up {
                    let d = hypot(x[i] - s.ax, y[i] - s.ay)
                    if d < best {
                        best = d
                        destFi = s.bFi
                        destX = s.bx
                        destY = s.by
                        take = best <= Self.stairReach
                    }
                }
            }
            for s in stairs {
                let up = (s.aLevel - s.bLevel) * sign > 0
                if fi[i] == s.bFi && up {
                    let d = hypot(x[i] - s.bx, y[i] - s.by)
                    if d < best {
                        best = d
                        destFi = s.aFi
                        destX = s.ax
                        destY = s.ay
                        take = best <= Self.stairReach
                    }
                }
            }
            if best <= Self.stairReach && take {
                fi[i] = destFi
                x[i] = destX
                y[i] = destY
            } else {
                w[i] *= Self.stairMiss
            }
        }
        afterWeights()
    }

    func observe(_ matches: [Sighting]) {
        capture()
        var applied = false
        for m in bestMatches(matches) where apply(m) { applied = true }
        if applied { source = "vision" }
        afterWeights()
    }

    func pose() -> FilterPose {
        let est = estimate()
        return FilterPose(
            level: est.level, x: est.x, y: est.y, spreadM: est.spread,
            confidence: est.conf, source: source, unsure: est.spread > 4, nSteps: nSteps
        )
    }

    // MARK: internals

    private func fillRoom(_ fi0: Int, _ info: RoomInfo, _ x0: Double, _ y0: Double, _ sigma: Double) {
        var rr: [Int] = []
        var cc: [Int] = []
        for r in 0..<grid.rows {
            for c in 0..<grid.cols where grid.walkable(fi0, r, c) && grid.roomAt(fi0, r, c) == Int32(info.index) {
                rr.append(r)
                cc.append(c)
            }
        }
        for i in 0..<n { fi[i] = fi0 }
        if rr.isEmpty {
            for i in 0..<n { x[i] = rng.nextNormal(mean: info.centroid.x, std: sigma) }
            for i in 0..<n { y[i] = rng.nextNormal(mean: info.centroid.y, std: sigma) }
            return
        }
        for i in 0..<n {
            let pick = rng.nextInt(low: 0, high: rr.count)
            x[i] = grid.centerX(cc[pick])
            y[i] = grid.centerY(rr[pick])
        }
        _ = x0
        _ = y0
    }

    private func segmentClear(_ dx: [Double], _ dy: [Double]) -> [Bool] {
        var maxDist = 0.0
        for i in 0..<n { maxDist = max(maxDist, hypot(dx[i], dy[i])) }
        var samples = 4
        if maxDist > grid.cell {
            samples = min(256, max(4, Int(ceil(maxDist / (grid.cell * 0.5)))))
        }
        var clear = [Bool](repeating: true, count: n)
        for i in 0..<n {
            for s in 1...samples {
                let t = Double(s) / Double(samples)
                let px = x[i] + t * dx[i]
                let py = y[i] + t * dy[i]
                if !walkablePoint(fi[i], px, py) {
                    clear[i] = false
                    break
                }
            }
        }
        return clear
    }

    private func walkablePoint(_ fi0: Int, _ px: Double, _ py: Double) -> Bool {
        let c = Int(floor((px - grid.originX) / grid.cell))
        let r = Int(floor((py - grid.originY) / grid.cell))
        if !grid.inRange(r, c) { return false }
        return grid.walkable(fi0, r, c)
    }

    private func apply(_ m: Sighting) -> Bool {
        guard let mfi = grid.levelIndex[m.level] else { return false }
        for i in 0..<n {
            if fi[i] == mfi {
                if m.points.isEmpty {
                    w[i] *= 0.2
                } else {
                    var d = Double.infinity
                    for p in m.points {
                        d = min(d, hypot(x[i] - p.x, y[i] - p.y))
                    }
                    let like = 0.2 + exp(-(d * d) / (2 * Self.visionSigma * Self.visionSigma))
                    w[i] *= like
                }
            } else {
                w[i] *= 0.3
            }
        }
        return true
    }

    private func estimate() -> (level: Int, x: Double, y: Double, spread: Double, conf: Double, fi: Int) {
        var weights = w
        let sum = weights.reduce(0, +)
        if weights.contains(where: { !$0.isFinite }) || sum <= 0 {
            weights = [Double](repeating: 1, count: n)
        }
        let total = weights.reduce(0, +)
        var wn = weights.map { $0 / total }
        var mass: [Int: Double] = [:]
        for i in 0..<n { mass[fi[i], default: 0] += wn[i] }
        var bestFi = fi[0]
        var bestMass = -1.0
        for key in mass.keys.sorted() {
            let m = mass[key] ?? 0
            if m > bestMass {
                bestMass = m
                bestFi = key
            }
        }
        var onW = 0.0
        for i in 0..<n where fi[i] == bestFi { onW += wn[i] }
        if onW <= 0 { onW = 1 }
        var ex = 0.0, ey = 0.0
        for i in 0..<n where fi[i] == bestFi {
            let ww = wn[i] / onW
            ex += ww * x[i]
            ey += ww * y[i]
        }
        var vx = 0.0, vy = 0.0
        for i in 0..<n where fi[i] == bestFi {
            let ww = wn[i] / onW
            vx += ww * (x[i] - ex) * (x[i] - ex)
            vy += ww * (y[i] - ey) * (y[i] - ey)
        }
        let spread = sqrt(max(0, vx + vy))
        let conf = min(1, max(0, 1 - spread / 6))
        let level = grid.levels.indices.contains(bestFi) ? grid.levels[bestFi] : grid.levels[0]
        return (level, ex, ey, spread, conf, bestFi)
    }

    private func capture() {
        let est = estimate()
        lastFi = est.fi
        lastX = est.x
        lastY = est.y
    }

    private func dead() -> Bool {
        if w.contains(where: { !$0.isFinite }) { return true }
        return w.reduce(0, +) <= Self.underflow
    }

    private func afterWeights() {
        if dead() {
            reinit()
            return
        }
        resampleIfNeeded()
    }

    private func resampleIfNeeded() {
        let sw = w.reduce(0, +)
        var sq = 0.0
        for v in w { sq += v * v }
        if sq == 0 || !sq.isFinite || !sw.isFinite {
            reinit()
            return
        }
        let neff = (sw * sw) / sq
        if neff >= Double(n) / 2 { return }
        let u0 = rng.nextUnit()
        var cdf = [Double](repeating: 0, count: n)
        var acc = 0.0
        for i in 0..<n {
            acc += w[i]
            cdf[i] = acc
        }
        let last = cdf[n - 1]
        if last != 0 {
            for i in 0..<n { cdf[i] /= last }
        }
        var nfi = fi, nx = x, ny = y, nb = bias
        for i in 0..<n {
            let pos = (Double(i) + u0) / Double(n)
            var pick = searchLeft(cdf, pos)
            if pick > n - 1 { pick = n - 1 }
            nfi[i] = fi[pick]
            nx[i] = x[pick]
            ny[i] = y[pick]
            nb[i] = bias[pick]
        }
        fi = nfi
        x = nx
        y = ny
        bias = nb
        w = [Double](repeating: 1.0 / Double(n), count: n)
    }

    private func reinit() {
        let fi0 = lastFi
        let x0 = lastX
        let y0 = lastY
        var gotX: [Double] = []
        var gotY: [Double] = []
        for _ in 0..<40 {
            if gotX.count >= n { break }
            var cx = [Double](repeating: 0, count: n)
            var cy = [Double](repeating: 0, count: n)
            for i in 0..<n { cx[i] = rng.nextNormal(mean: x0, std: 2) }
            for i in 0..<n { cy[i] = rng.nextNormal(mean: y0, std: 2) }
            for i in 0..<n where walkablePoint(fi0, cx[i], cy[i]) {
                gotX.append(cx[i])
                gotY.append(cy[i])
            }
        }
        if gotX.count < n {
            var cells: [(Double, Double)] = []
            var d2: [Double] = []
            if grid.levelIndex.values.contains(fi0) || (fi0 >= 0 && fi0 < grid.levels.count) {
                for r in 0..<grid.rows {
                    for c in 0..<grid.cols where grid.walkable(fi0, r, c) {
                        let gx = grid.centerX(c)
                        let gy = grid.centerY(r)
                        cells.append((gx, gy))
                        d2.append((gx - x0) * (gx - x0) + (gy - y0) * (gy - y0))
                    }
                }
            }
            let need = n - gotX.count
            if cells.isEmpty {
                for _ in 0..<need { gotX.append(x0); gotY.append(y0) }
            } else {
                var near: [Int] = []
                for i in 0..<d2.count where d2[i] <= 4 { near.append(i) }
                let pool: [Int]
                if !near.isEmpty {
                    pool = near
                } else {
                    let order = d2.indices.sorted { d2[$0] < d2[$1] }
                    let take = max(1, min(d2.count, 40))
                    pool = Array(order.prefix(take))
                }
                for _ in 0..<need {
                    let pick = pool[rng.nextInt(low: 0, high: pool.count)]
                    gotX.append(cells[pick].0)
                    gotY.append(cells[pick].1)
                }
            }
        }
        for i in 0..<n {
            x[i] = gotX[i]
            y[i] = gotY[i]
            fi[i] = fi0
            bias[i] = rng.nextNormal(mean: 0, std: 8)
        }
        w = [Double](repeating: 1.0 / Double(n), count: n)
    }

    private func bestMatches(_ matches: [Sighting]) -> [Sighting] {
        var best: [String: (Double, Sighting)] = [:]
        var order: [String] = []
        var solo: [Sighting] = []
        for m in matches {
            if m.text.isEmpty {
                solo.append(m)
                continue
            }
            if let prev = best[m.text] {
                if m.score > prev.0 { best[m.text] = (m.score, m) }
            } else {
                order.append(m.text)
                best[m.text] = (m.score, m)
            }
        }
        return order.compactMap { best[$0]?.1 } + solo
    }

    private func searchLeft(_ cdf: [Double], _ pos: Double) -> Int {
        var lo = 0
        var hi = cdf.count
        while lo < hi {
            let mid = (lo + hi) / 2
            if cdf[mid] < pos { lo = mid + 1 } else { hi = mid }
        }
        return lo
    }
}

private struct StairPair {
    var aFi: Int
    var ax: Double
    var ay: Double
    var aLevel: Int
    var bFi: Int
    var bx: Double
    var by: Double
    var bLevel: Int
}
