import CryptoKit
import Foundation

struct RoomInfo {
    var id: String
    var label: String
    var type: String
    var polygon: [PlanPoint]
    var level: Int
    var index: Int
    var centroid: PlanPoint
}

struct GridLink {
    var aFi: Int
    var aR: Int
    var aC: Int
    var bFi: Int
    var bR: Int
    var bC: Int
    var cost: Double
    var id: String
    var name: String
    var type: String

    var a: (Int, Int, Int) { (aFi, aR, aC) }
    var b: (Int, Int, Int) { (bFi, bR, bC) }
}

/// 0.5 m occupancy grid. Cell centers and link costs match backend/core/grid.py.
final class BuildingGrid {
    static let cellDefault = 0.5
    static let wallHalf = 0.3
    static let doorPad = 0.25
    static let stairCostM = 10.0
    static let rampCostM = 7.0

    let model: BuildingModel
    let cell: Double
    let levels: [Int]
    let levelIndex: [Int: Int]
    let originX: Double
    let originY: Double
    let rows: Int
    let cols: Int
    let count: Int

    private(set) var walkable: [UInt8]
    private(set) var room: [Int32]
    private(set) var rooms: [RoomInfo] = []
    private(set) var roomById: [String: RoomInfo] = [:]
    private(set) var exits: [(fi: Int, r: Int, c: Int, doorId: String)] = []
    private(set) var links: [GridLink] = []
    private(set) var warnings: [String] = []

    init(model: BuildingModel, cell: Double = BuildingGrid.cellDefault, pad: Double = 1) {
        self.model = model
        self.cell = cell
        self.levels = model.levels
        var index: [Int: Int] = [:]
        for (i, lv) in model.levels.enumerated() { index[lv] = i }
        self.levelIndex = index

        var minX = Double.infinity, minY = Double.infinity
        var maxX = -Double.infinity, maxY = -Double.infinity
        func take(_ x: Double, _ y: Double) {
            minX = min(minX, x); maxX = max(maxX, x)
            minY = min(minY, y); maxY = max(maxY, y)
        }
        for fl in model.floors {
            for p in fl.outline { take(p.x, p.y) }
            for w in fl.walls { take(w.x1, w.y1); take(w.x2, w.y2) }
            for s in fl.spaces { for p in s.polygon { take(p.x, p.y) } }
            for d in fl.doors { take(d.x, d.y) }
        }
        for v in model.verticals { take(v.x, v.y) }
        if minX == .infinity { fatalError("Building has no geometry") }

        originX = minX - pad
        originY = minY - pad
        cols = Int(ceil((maxX - minX + 2 * pad) / cell))
        rows = Int(ceil((maxY - minY + 2 * pad) / cell))
        count = model.levels.count * rows * cols
        walkable = [UInt8](repeating: 0, count: count)
        room = [Int32](repeating: -1, count: count)
        rasterize()
        indexRooms()
        buildExits()
        buildLinks()
    }

    private init(model: BuildingModel, originX: Double, originY: Double, rows: Int, cols: Int,
                 walkable: [UInt8], room: [Int32]) {
        self.model = model
        self.cell = BuildingGrid.cellDefault
        self.levels = model.levels
        var index: [Int: Int] = [:]
        for (i, lv) in model.levels.enumerated() { index[lv] = i }
        self.levelIndex = index
        self.originX = originX
        self.originY = originY
        self.rows = rows
        self.cols = cols
        self.count = model.levels.count * rows * cols
        self.walkable = walkable
        self.room = room
        indexRooms()
        buildExits()
        buildLinks()
    }

    /// Rasterize, or reuse a cache written when a previous launch took over 2 seconds.
    static func load(model: BuildingModel, json: Data, cacheDir: URL?) -> BuildingGrid {
        let digest = SHA256.hash(data: json).map { String(format: "%02x", $0) }.joined()
        let cacheURL = cacheDir?.appendingPathComponent("grid-\(digest).bin")
        if let cacheURL, let cached = BuildingGrid.readCacheTrusted(url: cacheURL, model: model) {
            return cached
        }
        let t0 = Date()
        let grid = BuildingGrid(model: model)
        if Date().timeIntervalSince(t0) > 2, let cacheURL {
            grid.writeCache(url: cacheURL)
        }
        return grid
    }

    func idx(_ fi: Int, _ r: Int, _ c: Int) -> Int {
        (fi * rows + r) * cols + c
    }

    func walkable(_ fi: Int, _ r: Int, _ c: Int) -> Bool {
        walkable[idx(fi, r, c)] != 0
    }

    func roomAt(_ fi: Int, _ r: Int, _ c: Int) -> Int32 {
        room[idx(fi, r, c)]
    }

    func inRange(_ r: Int, _ c: Int) -> Bool {
        r >= 0 && r < rows && c >= 0 && c < cols
    }

    func centerX(_ c: Int) -> Double { originX + (Double(c) + 0.5) * cell }
    func centerY(_ r: Int) -> Double { originY + (Double(r) + 0.5) * cell }

    func centerOf(_ r: Int, _ c: Int) -> PlanPoint {
        PlanPoint(x: centerX(c), y: centerY(r))
    }

    func cellOf(_ x: Double, _ y: Double) -> (Int, Int) {
        let c = Int(floor((x - originX) / cell))
        let r = Int(floor((y - originY) / cell))
        return (r, c)
    }

    func flat(_ fi: Int, _ r: Int, _ c: Int) -> Int { idx(fi, r, c) }

    func unflat(_ index: Int) -> (Int, Int, Int) {
        let plane = rows * cols
        let fi = index / plane
        let rem = index % plane
        return (fi, rem / cols, rem % cols)
    }

    func nearestWalkable(fi: Int, x: Double, y: Double, maxDist: Double = 3,
                         mask: ((Int, Int) -> Bool)? = nil) -> (Int, Int)? {
        let (r, c) = cellOf(x, y)
        if inRange(r, c) && walkable(fi, r, c) && (mask?(r, c) ?? true) {
            return (r, c)
        }
        var best = Double.infinity
        var found: (Int, Int)?
        for rr in 0..<rows {
            for cc in 0..<cols {
                if !walkable(fi, rr, cc) { continue }
                if let mask, !mask(rr, cc) { continue }
                let dx = centerX(cc) - x
                let dy = centerY(rr) - y
                let d = hypot(dx, dy)
                if d < best {
                    best = d
                    found = (rr, cc)
                }
            }
        }
        if let found, best <= maxDist { return found }
        return nil
    }

    func poseForRoom(_ id: String) -> (level: Int, x: Double, y: Double, label: String)? {
        guard let info = roomById[id], let fi = levelIndex[info.level] else { return nil }
        let reach = max(50, Double(rows + cols) * cell)
        guard let cell = nearestWalkable(fi: fi, x: info.centroid.x, y: info.centroid.y,
                                          maxDist: reach, mask: { self.roomAt(fi, $0, $1) == Int32(info.index) })
        else { return nil }
        let p = centerOf(cell.0, cell.1)
        return (info.level, p.x, p.y, info.label)
    }

    func walkableCRC() -> UInt32 { CRC32.hash(walkable) }

    func roomCRC() -> UInt32 {
        var bytes: [UInt8] = []
        bytes.reserveCapacity(room.count * 4)
        for v in room {
            let u = UInt32(bitPattern: v)
            bytes.append(UInt8(u & 0xff))
            bytes.append(UInt8((u >> 8) & 0xff))
            bytes.append(UInt8((u >> 16) & 0xff))
            bytes.append(UInt8((u >> 24) & 0xff))
        }
        return CRC32.hash(bytes)
    }

    // MARK: raster

    private func rasterize() {
        let faces = model.faces
        let plane = rows * cols
        var nextRoom = 0
        for fl in model.floors {
            guard let fi = levelIndex[fl.level] else { continue }
            var inside = [UInt8](repeating: 0, count: plane)
            if fl.outline.count >= 3 {
                fillPoly(fl.outline, into: &inside)
            } else {
                for s in fl.spaces { fillPoly(s.polygon, into: &inside) }
                if !inside.contains(1) {
                    for i in 0..<plane { inside[i] = 1 }
                }
            }
            var wall = [UInt8](repeating: 0, count: plane)
            if faces {
                for w in fl.walls { stampFace(w, into: &wall) }
            } else {
                for w in fl.walls { stampThick(w, into: &wall) }
            }
            var walk = inside
            for i in 0..<plane where wall[i] != 0 { walk[i] = 0 }
            for d in fl.doors { carve(d, faces: faces, inside: inside, into: &walk) }
            for s in fl.spaces where s.type == "void" {
                var hole = [UInt8](repeating: 0, count: plane)
                fillPoly(s.polygon, into: &hole)
                for i in 0..<plane where hole[i] != 0 { walk[i] = 0 }
            }
            let base = fi * plane
            for i in 0..<plane { walkable[base + i] = walk[i] }
            for s in fl.spaces {
                let index = nextRoom
                nextRoom += 1
                let box = bbox(s.polygon)
                for r in box.r0..<box.r1 {
                    let y = centerY(r)
                    for c in box.c0..<box.c1 {
                        let i = r * cols + c
                        if inside[i] != 0 && pointInPoly(centerX(c), y, s.polygon) {
                            room[base + i] = Int32(index)
                        }
                    }
                }
            }
        }
    }

    private func indexRooms() {
        rooms = []
        roomById = [:]
        for fl in model.floors {
            for s in fl.spaces {
                let index = rooms.count
                let info = RoomInfo(
                    id: s.id, label: s.label, type: s.type, polygon: s.polygon,
                    level: fl.level, index: index, centroid: Self.centroid(s.polygon)
                )
                rooms.append(info)
                roomById[s.id] = info
            }
        }
    }

    private func buildExits() {
        for fl in model.floors {
            guard let fi = levelIndex[fl.level] else { continue }
            for d in fl.doors where d.exteriorExit {
                if let cell = nearestWalkable(fi: fi, x: d.x, y: d.y, maxDist: 2) {
                    exits.append((fi, cell.0, cell.1, d.id))
                } else {
                    warnings.append("Exit \(d.id) not reachable from any walkable cell")
                }
            }
        }
    }

    private func buildLinks() {
        for v in model.verticals {
            var cells: [Int: (Int, Int, Int)] = [:]
            for lv in v.floors {
                guard let fi = levelIndex[lv] else { continue }
                let p = v.positions[lv] ?? PlanPoint(x: v.x, y: v.y)
                if let c = nearestWalkable(fi: fi, x: p.x, y: p.y, maxDist: 3) {
                    cells[lv] = (fi, c.0, c.1)
                } else {
                    warnings.append("\(v.id) has no walkable cell near it on floor \(lv)")
                }
            }
            let lvs = cells.keys.sorted()
            for i in 0..<max(0, lvs.count - 1) {
                let aLv = lvs[i], bLv = lvs[i + 1]
                guard let a = cells[aLv], let b = cells[bLv],
                      let ia = levelIndex[aLv], let ib = levelIndex[bLv] else { continue }
                let span = ib - ia
                let run = cell * hypot(Double(a.1 - b.1), Double(a.2 - b.2))
                let per = v.type == "ramp" ? Self.rampCostM : Self.stairCostM
                links.append(GridLink(
                    aFi: a.0, aR: a.1, aC: a.2,
                    bFi: b.0, bR: b.1, bC: b.2,
                    cost: per * Double(span) + run,
                    id: v.id, name: v.name, type: v.type
                ))
            }
        }
    }

    private struct Box { var r0, r1, c0, c1: Int }

    private func bbox(_ poly: [PlanPoint]) -> Box {
        var minX = Double.infinity, minY = Double.infinity
        var maxX = -Double.infinity, maxY = -Double.infinity
        for p in poly {
            minX = min(minX, p.x); maxX = max(maxX, p.x)
            minY = min(minY, p.y); maxY = max(maxY, p.y)
        }
        let c0 = max(0, Int(floor((minX - originX) / cell)) - 1)
        let c1 = min(cols, Int(floor((maxX - originX) / cell)) + 2)
        let r0 = max(0, Int(floor((minY - originY) / cell)) - 1)
        let r1 = min(rows, Int(floor((maxY - originY) / cell)) + 2)
        return Box(r0: r0, r1: max(r0, r1), c0: c0, c1: max(c0, c1))
    }

    private func fillPoly(_ poly: [PlanPoint], into mask: inout [UInt8]) {
        let box = bbox(poly)
        for r in box.r0..<box.r1 {
            let y = centerY(r)
            for c in box.c0..<box.c1 where pointInPoly(centerX(c), y, poly) {
                mask[r * cols + c] = 1
            }
        }
    }

    private func stampFace(_ w: WallSeg, into wall: inout [UInt8]) {
        let length = hypot(w.x2 - w.x1, w.y2 - w.y1)
        let n = max(2, Int(length / (cell / 4)) + 2)
        if n <= 1 { return }
        for i in 0..<n {
            let t = Double(i) / Double(n - 1)
            let x = w.x1 + (w.x2 - w.x1) * t
            let y = w.y1 + (w.y2 - w.y1) * t
            let c = Int(floor((x - originX) / cell))
            let r = Int(floor((y - originY) / cell))
            if inRange(r, c) { wall[r * cols + c] = 1 }
        }
    }

    private func stampThick(_ w: WallSeg, into wall: inout [UInt8]) {
        let half = Self.wallHalf
        let c0 = max(0, Int(floor((min(w.x1, w.x2) - half - originX) / cell)))
        let c1 = min(cols, Int(floor((max(w.x1, w.x2) + half - originX) / cell)) + 2)
        let r0 = max(0, Int(floor((min(w.y1, w.y2) - half - originY) / cell)))
        let r1 = min(rows, Int(floor((max(w.y1, w.y2) + half - originY) / cell)) + 2)
        if c0 >= c1 || r0 >= r1 { return }
        for r in r0..<r1 {
            for c in c0..<c1 {
                if segDist(centerX(c), centerY(r), w.x1, w.y1, w.x2, w.y2) <= half {
                    wall[r * cols + c] = 1
                }
            }
        }
    }

    private func carve(_ d: DoorOpening, faces: Bool, inside: [UInt8], into walk: inout [UInt8]) {
        let radius: Double
        if d.axis != nil {
            radius = d.width / 2 + 0.6
        } else {
            radius = d.width / 2 + (faces ? 0.1 : Self.doorPad)
        }
        let c0 = max(0, Int(floor((d.x - radius - originX) / cell)) - 1)
        let c1 = min(cols, Int(floor((d.x + radius - originX) / cell)) + 2)
        let r0 = max(0, Int(floor((d.y - radius - originY) / cell)) - 1)
        let r1 = min(rows, Int(floor((d.y + radius - originY) / cell)) + 2)
        for r in r0..<r1 {
            for c in c0..<c1 {
                let i = r * cols + c
                if inside[i] == 0 { continue }
                let dx = centerX(c) - d.x
                let dy = centerY(r) - d.y
                if let axis = d.axis {
                    let along = abs(dx * axis.x + dy * axis.y)
                    let across = abs(-dx * axis.y + dy * axis.x)
                    if along <= d.width / 2 + 0.05 && across <= 0.6 { walk[i] = 1 }
                } else if hypot(dx, dy) <= radius {
                    walk[i] = 1
                }
            }
        }
    }

    static func centroid(_ poly: [PlanPoint]) -> PlanPoint {
        guard !poly.isEmpty else { return PlanPoint(x: 0, y: 0) }
        var x = 0.0, y = 0.0
        for p in poly { x += p.x; y += p.y }
        let n = Double(poly.count)
        return PlanPoint(x: x / n, y: y / n)
    }

    // MARK: cache

    private static let magic = Data("FSG1".utf8)

    func writeCache(url: URL) {
        var data = Data()
        data.append(Self.magic)
        appendInt(1, &data)
        appendDouble(originX, &data)
        appendDouble(originY, &data)
        appendInt(Int32(rows), &data)
        appendInt(Int32(cols), &data)
        appendInt(Int32(levels.count), &data)
        for lv in levels { appendInt(Int32(lv), &data) }
        data.append(contentsOf: walkable)
        for v in room { appendInt(v, &data) }
        try? data.write(to: url, options: .atomic)
    }

    /// Cache read. The file name is a hash of the JSON, so the masks match this model.
    static func readCacheTrusted(url: URL, model: BuildingModel) -> BuildingGrid? {
        guard let data = try? Data(contentsOf: url), data.count > 32 else { return nil }
        var o = 0
        guard data.subdata(in: 0..<4) == magic else { return nil }
        o = 4
        guard readInt(data, &o) == 1 else { return nil }
        let ox = readDouble(data, &o)
        let oy = readDouble(data, &o)
        let rows = Int(readInt(data, &o))
        let cols = Int(readInt(data, &o))
        let nf = Int(readInt(data, &o))
        guard rows > 0, cols > 0, nf == model.levels.count else { return nil }
        var levels: [Int] = []
        for _ in 0..<nf { levels.append(Int(readInt(data, &o))) }
        guard levels == model.levels else { return nil }
        let plane = nf * rows * cols
        guard o + plane + plane * 4 <= data.count else { return nil }
        let walk = [UInt8](data[o..<(o + plane)])
        o += plane
        var room = [Int32](repeating: -1, count: plane)
        for i in 0..<plane { room[i] = readInt(data, &o) }
        return BuildingGrid(model: model, originX: ox, originY: oy, rows: rows, cols: cols,
                            walkable: walk, room: room)
    }
}

private func appendInt(_ v: Int32, _ data: inout Data) {
    var le = v.littleEndian
    withUnsafeBytes(of: &le) { data.append(contentsOf: $0) }
}

private func appendDouble(_ v: Double, _ data: inout Data) {
    var le = v.bitPattern.littleEndian
    withUnsafeBytes(of: &le) { data.append(contentsOf: $0) }
}

private func readInt(_ data: Data, _ o: inout Int) -> Int32 {
    let v = data.subdata(in: o..<(o + 4)).withUnsafeBytes { $0.load(as: Int32.self) }
    o += 4
    return Int32(littleEndian: v)
}

private func readDouble(_ data: Data, _ o: inout Int) -> Double {
    let bits = data.subdata(in: o..<(o + 8)).withUnsafeBytes { $0.load(as: UInt64.self) }
    o += 8
    return Double(bitPattern: UInt64(littleEndian: bits))
}

enum CRC32 {
    static func hash(_ bytes: [UInt8]) -> UInt32 {
        var crc: UInt32 = 0xFFFFFFFF
        for b in bytes {
            crc ^= UInt32(b)
            for _ in 0..<8 {
                if crc & 1 == 1 {
                    crc = (crc >> 1) ^ 0xEDB88320
                } else {
                    crc >>= 1
                }
            }
        }
        return crc ^ 0xFFFFFFFF
    }
}

func pointInPoly(_ x: Double, _ y: Double, _ poly: [PlanPoint]) -> Bool {
    let n = poly.count
    if n < 3 { return false }
    var inside = false
    var j = n - 1
    for i in 0..<n {
        let xi = poly[i].x, yi = poly[i].y
        let xj = poly[j].x, yj = poly[j].y
        let denom = yj != yi ? (yj - yi) : 1e-12
        let cond = ((yi > y) != (yj > y)) && (x < (xj - xi) * (y - yi) / denom + xi)
        if cond { inside.toggle() }
        j = i
    }
    return inside
}

func segDist(_ x: Double, _ y: Double, _ x1: Double, _ y1: Double, _ x2: Double, _ y2: Double) -> Double {
    let dx = x2 - x1, dy = y2 - y1
    let l2 = dx * dx + dy * dy
    if l2 == 0 { return hypot(x - x1, y - y1) }
    var t = ((x - x1) * dx + (y - y1) * dy) / l2
    if t < 0 { t = 0 } else if t > 1 { t = 1 }
    return hypot(x - (x1 + t * dx), y - (y1 + t * dy))
}
