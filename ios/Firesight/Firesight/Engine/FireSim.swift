import Foundation

/// Mock fire and smoke. Spread rules match backend/core/fire.py. The RNG is local to the phone.
final class FireSim {
    static let pSpread = 0.30
    static let smokeDecay: Float = 0.92
    static let smokeIters = 2
    static let smokeUp: Float = 0.97
    static let smokeDown: Float = 0.35
    static let pLinkUp = 0.30
    static let pLinkDown = 0.10
    static let burnSteps = 3
    static let pCeil = 0.04
    static let pFloor = 0.01

    let grid: BuildingGrid
    let seed: UInt64
    private let rng: SplitMix
    private(set) var fire: [Bool]
    private(set) var smoke: [Float]
    private(set) var burn: [Int]
    private(set) var t: Int = 0

    init(grid: BuildingGrid, seed: UInt64 = 7) {
        self.grid = grid
        self.seed = seed
        self.rng = SplitMix(seed: seed)
        fire = [Bool](repeating: false, count: grid.count)
        smoke = [Float](repeating: 0, count: grid.count)
        burn = [Int](repeating: 0, count: grid.count)
    }

    func reset() {
        for i in 0..<grid.count {
            fire[i] = false
            smoke[i] = 0
            burn[i] = 0
        }
        t = 0
    }

    @discardableResult
    func ignite(roomId: String) -> Bool {
        guard let info = grid.roomById[roomId], let fi = grid.levelIndex[info.level] else { return false }
        guard let cell = grid.nearestWalkable(
            fi: fi, x: info.centroid.x, y: info.centroid.y, maxDist: 50,
            mask: { grid.roomAt(fi, $0, $1) == Int32(info.index) }
        ) else { return false }
        let p = grid.centerOf(cell.0, cell.1)
        return ignite(fi: fi, x: p.x, y: p.y, radius: 1)
    }

    @discardableResult
    func ignite(fi: Int, x: Double, y: Double, radius: Double = 1) -> Bool {
        guard let cell = grid.nearestWalkable(fi: fi, x: x, y: y, maxDist: 5) else { return false }
        let c = grid.centerOf(cell.0, cell.1)
        let r2 = radius * radius
        for row in 0..<grid.rows {
            for col in 0..<grid.cols where grid.walkable(fi, row, col) {
                let dx = grid.centerX(col) - c.x
                let dy = grid.centerY(row) - c.y
                if dx * dx + dy * dy <= r2 {
                    let i = grid.flat(fi, row, col)
                    fire[i] = true
                    smoke[i] = 1
                }
            }
        }
        return true
    }

    func step(_ n: Int = 1) {
        let rows = grid.rows, cols = grid.cols, floors = grid.levels.count
        let plane = rows * cols
        for _ in 0..<max(1, n) {
            for i in 0..<grid.count where fire[i] { burn[i] += 1 }
            var nb = [Bool](repeating: false, count: grid.count)
            for fi in 0..<floors {
                let base = fi * plane
                for r in 0..<rows {
                    for c in 0..<cols {
                        let i = base + r * cols + c
                        if r > 0 && fire[i - cols] { nb[i] = true }
                        if r + 1 < rows && fire[i + cols] { nb[i] = true }
                        if c > 0 && fire[i - 1] { nb[i] = true }
                        if c + 1 < cols && fire[i + 1] { nb[i] = true }
                    }
                }
            }
            var newFire = [Bool](repeating: false, count: grid.count)
            for i in 0..<grid.count {
                let roll = rng.nextUnit()
                if nb[i] && grid.walkable[i] != 0 && !fire[i] && roll < Self.pSpread {
                    newFire[i] = true
                }
            }
            for link in grid.links where link.type != "elevator" {
                let a = grid.flat(link.aFi, link.aR, link.aC)
                let b = grid.flat(link.bFi, link.bR, link.bC)
                if fire[a] && !fire[b] && rng.nextUnit() < Self.pLinkUp {
                    newFire[b] = grid.walkable[b] != 0
                }
                if fire[b] && !fire[a] && rng.nextUnit() < Self.pLinkDown {
                    newFire[a] = grid.walkable[a] != 0
                }
            }
            if floors > 1 {
                var hotBelow = false
                for fi in 0..<(floors - 1) {
                    let base = fi * plane
                    for i in 0..<plane where burn[base + i] >= Self.burnSteps { hotBelow = true; break }
                    if hotBelow { break }
                }
                if hotBelow {
                    for fi in 0..<(floors - 1) {
                        let lo = fi * plane
                        let hi = (fi + 1) * plane
                        for i in 0..<plane {
                            let upRoll = rng.nextUnit()
                            if burn[lo + i] >= Self.burnSteps && upRoll < Self.pCeil
                                && grid.walkable[hi + i] != 0 && !fire[hi + i] {
                                newFire[hi + i] = true
                            }
                        }
                    }
                    for fi in 0..<(floors - 1) {
                        let lo = fi * plane
                        let hi = (fi + 1) * plane
                        for i in 0..<plane {
                            let downRoll = rng.nextUnit()
                            if burn[hi + i] >= Self.burnSteps && downRoll < Self.pFloor
                                && grid.walkable[lo + i] != 0 && !fire[lo + i] {
                                newFire[lo + i] = true
                            }
                        }
                    }
                }
            }
            for i in 0..<grid.count where newFire[i] { fire[i] = true }

            var s = smoke
            for i in 0..<grid.count where fire[i] { s[i] = max(s[i], 1) }
            for _ in 0..<Self.smokeIters {
                let nbSmoke = neighborMax(s, floors: floors, rows: rows, cols: cols)
                for i in 0..<grid.count {
                    s[i] = max(s[i], Self.smokeDecay * nbSmoke[i])
                    if grid.walkable[i] == 0 { s[i] = 0 }
                }
                for link in grid.links {
                    let a = grid.flat(link.aFi, link.aR, link.aC)
                    let b = grid.flat(link.bFi, link.bR, link.bC)
                    let lo = s[a], hi = s[b]
                    s[b] = max(hi, lo * Self.smokeUp)
                    s[a] = max(lo, hi * Self.smokeDown)
                }
            }
            smoke = s
            t += 1
        }
    }

    func roomStatus() -> [String: String] {
        var out: [String: String] = [:]
        for info in grid.rooms {
            guard let fi = grid.levelIndex[info.level] else { continue }
            var any = false
            var burning = false
            var smokeMax: Float = 0
            for r in 0..<grid.rows {
                for c in 0..<grid.cols where grid.roomAt(fi, r, c) == Int32(info.index) {
                    any = true
                    let i = grid.flat(fi, r, c)
                    if fire[i] { burning = true }
                    smokeMax = max(smokeMax, smoke[i])
                }
            }
            if !any || (!burning && smokeMax <= 0.35) {
                out[info.id] = "clear"
            } else if burning {
                out[info.id] = "fire"
            } else {
                out[info.id] = "smoke"
            }
        }
        return out
    }

    private func neighborMax(_ a: [Float], floors: Int, rows: Int, cols: Int) -> [Float] {
        var out = [Float](repeating: 0, count: a.count)
        let plane = rows * cols
        for fi in 0..<floors {
            let base = fi * plane
            for r in 0..<rows {
                for c in 0..<cols {
                    let i = base + r * cols + c
                    var m = out[i]
                    if r > 0 { m = max(m, a[i - cols]) }
                    if r + 1 < rows { m = max(m, a[i + cols]) }
                    if c > 0 { m = max(m, a[i - 1]) }
                    if c + 1 < cols { m = max(m, a[i + 1]) }
                    out[i] = m
                }
            }
        }
        return out
    }
}
