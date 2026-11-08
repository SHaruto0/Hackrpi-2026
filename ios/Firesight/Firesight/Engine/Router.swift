import Foundation

struct PathPoint: Equatable {
    var level: Int
    var x: Double
    var y: Double
}

struct NavRoute {
    var reachable: Bool
    var reason: String
    var path: [PathPoint]
    var distanceM: Double?
    var maxSmoke: Double
    var instructions: [String]
}

/// Goal-seeded Dijkstra. Neighbor order and heap ties match scripts/export_nav_fixtures.py.
final class RouteField {
    static let smokePenalty = 12.0
    static let noPred = -9999
    static let dirs: [(Int, Int)] = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]

    let grid: BuildingGrid
    let destinationId: String
    let destinationLabel: String
    let passable: [Bool]
    let smoke: [Float]
    let dist: [Double]
    let pred: [Int]
    let linkNames: [Int64: String]

    init(grid: BuildingGrid, fire: [Bool]?, smoke: [Float]?, destinationRoomId: String) {
        self.grid = grid
        self.destinationId = destinationRoomId
        let n = grid.count
        var pass = [Bool](repeating: false, count: n)
        var sm = [Float](repeating: 0, count: n)
        for i in 0..<n {
            let onFire = fire?[i] ?? false
            pass[i] = grid.walkable[i] != 0 && !onFire
            if let smoke, i < smoke.count { sm[i] = smoke[i] }
        }
        self.passable = pass
        self.smoke = sm

        var names: [Int64: String] = [:]
        var incident: [[(Int, Double)]] = Array(repeating: [], count: n)
        var seen = Set<Int64>()
        for link in grid.links {
            if link.type == "elevator" { continue }
            let a = grid.flat(link.aFi, link.aR, link.aC)
            let b = grid.flat(link.bFi, link.bR, link.bC)
            if a < 0 || b < 0 || a >= n || b >= n { continue }
            if !pass[a] || !pass[b] { continue }
            let key = Self.pairKey(a, b, n)
            if seen.contains(key) { continue }
            seen.insert(key)
            seen.insert(Self.pairKey(b, a, n))
            names[key] = link.name
            names[Self.pairKey(b, a, n)] = link.name
            incident[a].append((b, link.cost))
            incident[b].append((a, link.cost))
        }
        self.linkNames = names

        var dist = [Double](repeating: .infinity, count: n)
        var pred = [Int](repeating: Self.noPred, count: n)
        var heap = MinHeap()
        if let info = grid.roomById[destinationRoomId], let fi = grid.levelIndex[info.level] {
            self.destinationLabel = info.label
            let target = Int32(info.index)
            for r in 0..<grid.rows {
                for c in 0..<grid.cols where grid.roomAt(fi, r, c) == target {
                    let s = grid.flat(fi, r, c)
                    if !pass[s] || dist[s] == 0 { continue }
                    dist[s] = 0
                    pred[s] = Self.noPred
                    heap.push(0, s)
                }
            }
        } else {
            self.destinationLabel = destinationRoomId
        }

        let sqrt2 = 2.0.squareRoot()
        while let (d, u) = heap.pop() {
            if d > dist[u] { continue }
            let (fi, r, c) = grid.unflat(u)
            for (dr, dc) in Self.dirs {
                let rr = r + dr
                let cc = c + dc
                if rr < 0 || rr >= grid.rows || cc < 0 || cc >= grid.cols { continue }
                let v = grid.flat(fi, rr, cc)
                if !pass[u] || !pass[v] { continue }
                if dr != 0 && dc != 0 {
                    if !pass[grid.flat(fi, rr, c)] || !pass[grid.flat(fi, r, cc)] { continue }
                }
                let step = grid.cell * ((dr != 0 && dc != 0) ? sqrt2 : 1)
                let sa = Double(sm[u])
                let sb = Double(sm[v])
                let w = step * (1 + Self.smokePenalty * (sa + sb) / 2)
                let nd = d + w
                if nd < dist[v] {
                    dist[v] = nd
                    pred[v] = u
                    heap.push(nd, v)
                }
            }
            for (v, base) in incident[u] where pass[v] {
                let s = (Double(sm[u]) + Double(sm[v])) / 2
                let w = base * (1 + Self.smokePenalty * s)
                let nd = d + w
                if nd < dist[v] {
                    dist[v] = nd
                    pred[v] = u
                    heap.push(nd, v)
                }
            }
        }
        self.dist = dist
        self.pred = pred
    }

    func route(level: Int, x: Double, y: Double) -> NavRoute {
        guard let fi = grid.levelIndex[level] else {
            return NavRoute(reachable: false, reason: "not on a walkable area", path: [], distanceM: nil, maxSmoke: 0, instructions: [])
        }
        guard let start = grid.nearestWalkable(fi: fi, x: x, y: y, maxDist: 3, mask: { r, c in
            self.passable[self.grid.flat(fi, r, c)]
        }) else {
            let (r, c) = grid.cellOf(x, y)
            var onFire = false
            if grid.inRange(r, c) && grid.walkable(fi, r, c) && !passable[grid.flat(fi, r, c)] {
                onFire = true
            }
            return NavRoute(reachable: false, reason: onFire ? "in fire" : "not on a walkable area",
                            path: [], distanceM: nil, maxSmoke: 0, instructions: [])
        }
        var node = grid.flat(fi, start.0, start.1)
        if !dist[node].isFinite {
            return NavRoute(reachable: false, reason: "no safe route to the room",
                            path: [], distanceM: nil, maxSmoke: 0, instructions: [])
        }
        var nodes = [node]
        while pred[node] != Self.noPred && nodes.count < 1_000_000 {
            node = pred[node]
            nodes.append(node)
        }
        let cells = nodes.map { grid.unflat($0) }
        var smokeMax: Float = 0
        for (cfi, r, c) in cells {
            smokeMax = max(smokeMax, smoke[grid.flat(cfi, r, c)])
        }
        return NavRoute(
            reachable: true,
            reason: "",
            path: simplify(cells),
            distanceM: round1(length(cells)),
            maxSmoke: round2(Double(smokeMax)),
            instructions: instructionLines(cells, nodes)
        )
    }

    private func simplify(_ cells: [(Int, Int, Int)]) -> [PathPoint] {
        if cells.isEmpty { return [] }
        var keep = [cells[0]]
        if cells.count >= 3 {
            for i in 1..<(cells.count - 1) {
                let prev = cells[i - 1], cur = cells[i], nxt = cells[i + 1]
                if cur.0 != prev.0 || cur.0 != nxt.0 {
                    keep.append(cur)
                    continue
                }
                let d1 = (cur.1 - prev.1, cur.2 - prev.2)
                let d2 = (nxt.1 - cur.1, nxt.2 - cur.2)
                if d1 != d2 { keep.append(cur) }
            }
        }
        if cells.count > 1 { keep.append(cells[cells.count - 1]) }
        return keep.map { fi, r, c in
            let p = grid.centerOf(r, c)
            return PathPoint(level: grid.levels[fi], x: round2(p.x), y: round2(p.y))
        }
    }

    private func length(_ cells: [(Int, Int, Int)]) -> Double {
        var linkCost: [String: Double] = [:]
        for link in grid.links {
            linkCost[Self.cellKey(link.aFi, link.aR, link.aC, link.bFi, link.bR, link.bC)] = link.cost
            linkCost[Self.cellKey(link.bFi, link.bR, link.bC, link.aFi, link.aR, link.aC)] = link.cost
        }
        var total = 0.0
        for i in 1..<cells.count {
            let a = cells[i - 1], b = cells[i]
            if a.0 == b.0 {
                total += grid.cell * hypot(Double(a.1 - b.1), Double(a.2 - b.2))
            } else {
                let key = Self.cellKey(a.0, a.1, a.2, b.0, b.1, b.2)
                total += linkCost[key] ?? 10
            }
        }
        return total
    }

    private func instructionLines(_ cells: [(Int, Int, Int)], _ nodes: [Int]) -> [String] {
        var steps: [String] = []
        var segRoom: Int?
        var segLen = 0.0
        let n = grid.count
        func flush() {
            if let seg = segRoom, segLen >= 1, grid.rooms.indices.contains(seg) {
                let info = grid.rooms[seg]
                let name = info.type == "hallway" ? "the hallway (\(info.label))" : info.label
                steps.append("Walk \(Int(segLen.rounded())) m through \(name)")
            }
        }
        for i in 0..<cells.count {
            let (fi, r, c) = cells[i]
            if i > 0 {
                let (pfi, pr, pc) = cells[i - 1]
                if pfi != fi {
                    flush()
                    segRoom = nil
                    segLen = 0
                    let name = linkNames[Self.pairKey(nodes[i - 1], nodes[i], n)] ?? "stairs"
                    let direction = fi < pfi ? "down" : "up"
                    steps.append("Take \(name) \(direction) to floor \(grid.levels[fi])")
                    continue
                }
                segLen += grid.cell * hypot(Double(r - pr), Double(c - pc))
            }
            let rid = Int(grid.roomAt(fi, r, c))
            if rid != -1 && rid != segRoom {
                if segRoom != nil {
                    flush()
                    segLen = 0
                }
                segRoom = rid
            }
        }
        flush()
        steps.append("Arrive at \(destinationLabel)")
        return steps
    }

    private static func pairKey(_ a: Int, _ b: Int, _ n: Int) -> Int64 {
        Int64(a) * Int64(n) + Int64(b)
    }

    private static func cellKey(_ af: Int, _ ar: Int, _ ac: Int, _ bf: Int, _ br: Int, _ bc: Int) -> String {
        "\(af),\(ar),\(ac)->\(bf),\(br),\(bc)"
    }
}

func round1(_ x: Double) -> Double { (x * 10).rounded() / 10 }
func round2(_ x: Double) -> Double { (x * 100).rounded() / 100 }

struct MinHeap {
    private var a: [(Double, Int)] = []

    mutating func push(_ d: Double, _ n: Int) {
        a.append((d, n))
        var i = a.count - 1
        while i > 0 {
            let p = (i - 1) / 2
            if !before(a[i], a[p]) { break }
            a.swapAt(i, p)
            i = p
        }
    }

    mutating func pop() -> (Double, Int)? {
        guard !a.isEmpty else { return nil }
        let top = a[0]
        let last = a.removeLast()
        if !a.isEmpty {
            a[0] = last
            var i = 0
            while true {
                let l = i * 2 + 1
                let r = l + 1
                var m = i
                if l < a.count && before(a[l], a[m]) { m = l }
                if r < a.count && before(a[r], a[m]) { m = r }
                if m == i { break }
                a.swapAt(i, m)
                i = m
            }
        }
        return top
    }

    private func before(_ x: (Double, Int), _ y: (Double, Int)) -> Bool {
        if x.0 < y.0 { return true }
        if x.0 > y.0 { return false }
        return x.1 < y.1
    }
}
