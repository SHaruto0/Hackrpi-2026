import Foundation

struct PlanPoint: Equatable {
    var x: Double
    var y: Double
}

struct Alignment {
    var offsetX: Double
    var offsetY: Double
    var rotationDeg: Double
}

struct WallSeg {
    var id: String
    var x1: Double
    var y1: Double
    var x2: Double
    var y2: Double
    var exterior: Bool
}

struct SpacePoly {
    var id: String
    var label: String
    var type: String
    var polygon: [PlanPoint]
    var multiFloor: Bool
}

struct DoorOpening {
    var id: String
    var x: Double
    var y: Double
    var width: Double
    var axis: PlanPoint?
    var connects: [String]
    var exteriorExit: Bool
}

struct StairFlight {
    var poly: [PlanPoint]
    var center: PlanPoint
    var up: PlanPoint
    var steps: Int
}

struct FloorPlan {
    var level: Int
    var outline: [PlanPoint]
    var walls: [WallSeg]
    var spaces: [SpacePoly]
    var doors: [DoorOpening]
}

struct VerticalLink {
    var id: String
    var name: String
    var type: String
    var x: Double
    var y: Double
    var floors: [Int]
    var positions: [Int: PlanPoint]
}

struct BuildingModel {
    var name: String
    var floorHeightM: Double
    var wallStyle: String
    var planNorthDeg: Double
    var levels: [Int]
    var floors: [FloorPlan]
    var verticals: [VerticalLink]
    var warnings: [String]

    var faces: Bool { wallStyle == "faces" }
}

enum BuildingError: Error, CustomStringConvertible {
    case message(String)
    var description: String {
        switch self {
        case .message(let text): return text
        }
    }
}

enum BuildingLoader {
    static let spaceTypes: Set<String> = [
        "classroom", "office", "hallway", "lobby", "restroom", "stairwell", "elevator",
        "mechanical", "lecture_hall", "storage", "unknown", "void",
    ]
    static let verticalTypes: Set<String> = ["stairs", "elevator", "ramp"]
    static let minWallLen = 0.05

    static func load(data: Data) throws -> BuildingModel {
        let obj = try JSONSerialization.jsonObject(with: data)
        guard let raw = obj as? [String: Any] else {
            throw BuildingError.message("Top-level JSON must be an object")
        }
        return try load(raw: raw)
    }

    static func load(raw: [String: Any]) throws -> BuildingModel {
        guard let floorsIn = raw["floors"] as? [Any], !floorsIn.isEmpty else {
            throw BuildingError.message("'floors' must be a non-empty list")
        }
        var warnings: [String] = []
        var used: Set<String> = []

        func uniqueId(_ proposed: Any?, _ fallback: String) -> String {
            let base: String
            if let s = proposed as? String, !s.isEmpty {
                base = s
            } else if let n = jsonDouble(proposed) {
                base = String(Int(n))
            } else {
                base = fallback
            }
            var uid = base
            var n = 2
            while used.contains(uid) {
                uid = "\(base)-\(n)"
                n += 1
            }
            if uid != base {
                warnings.append("Duplicate id '\(base)' renamed to '\(uid)'")
            }
            used.insert(uid)
            return uid
        }

        var floors: [FloorPlan] = []
        var seenLevels: Set<Int> = []
        var alignByLevel: [Int: Alignment] = [:]

        for (fi, item) in floorsIn.enumerated() {
            guard let f = item as? [String: Any] else {
                throw BuildingError.message("Floor #\(fi) has invalid 'level': nil")
            }
            guard let level = jsonInt(f["level"]) else {
                throw BuildingError.message("Floor #\(fi) has invalid 'level': \(String(describing: f["level"]))")
            }
            if seenLevels.contains(level) {
                throw BuildingError.message("Duplicate floor level \(level)")
            }
            seenLevels.insert(level)
            let align = alignment(f["alignment"])
            alignByLevel[level] = align

            func xf(_ p: PlanPoint) -> PlanPoint { transform(p, align) }

            var outline: [PlanPoint] = []
            for q in (f["outline"] as? [Any]) ?? [] {
                if let p = jsonPoint(q) { outline.append(xf(p)) }
            }
            if !outline.isEmpty && outline.count < 3 {
                warnings.append("Floor \(level): outline has < 3 points, ignored")
                outline = []
            }

            var walls: [WallSeg] = []
            for (i, item) in ((f["walls"] as? [Any]) ?? []).enumerated() {
                guard let w = item as? [String: Any] else { continue }
                guard let x1 = jsonDouble(w["x1"]), let y1 = jsonDouble(w["y1"]),
                      let x2 = jsonDouble(w["x2"]), let y2 = jsonDouble(w["y2"]) else {
                    warnings.append("Floor \(level): wall #\(i) missing coordinates, dropped")
                    continue
                }
                let a = xf(PlanPoint(x: x1, y: y1))
                let b = xf(PlanPoint(x: x2, y: y2))
                if hypot(a.x - b.x, a.y - b.y) < minWallLen { continue }
                walls.append(WallSeg(
                    id: uniqueId(w["id"], "F\(level)-W\(i)"),
                    x1: a.x, y1: a.y, x2: b.x, y2: b.y,
                    exterior: jsonBool(w["exterior"])
                ))
            }

            var spaces: [SpacePoly] = []
            for (i, item) in ((f["spaces"] as? [Any]) ?? []).enumerated() {
                guard let s = item as? [String: Any] else { continue }
                var poly: [PlanPoint] = []
                for q in (s["polygon"] as? [Any]) ?? [] {
                    if let p = jsonPoint(q) { poly.append(xf(p)) }
                }
                if poly.count < 3 {
                    warnings.append("Floor \(level): space \(s["id"] ?? i) has < 3 points, dropped")
                    continue
                }
                var stype = (s["type"] as? String ?? "unknown").lowercased()
                if !spaceTypes.contains(stype) {
                    warnings.append("Floor \(level): unknown space type '\(stype)', using 'unknown'")
                    stype = "unknown"
                }
                let sid = uniqueId(s["id"], "F\(level)-S\(i)")
                spaces.append(SpacePoly(
                    id: sid,
                    label: (s["label"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? sid,
                    type: stype,
                    polygon: poly,
                    multiFloor: jsonBool(s["multi_floor"])
                ))
            }

            var doors: [DoorOpening] = []
            for (i, item) in ((f["doors"] as? [Any]) ?? []).enumerated() {
                guard let d = item as? [String: Any] else { continue }
                guard let x = jsonDouble(d["x"]), let y = jsonDouble(d["y"]) else {
                    warnings.append("Floor \(level): door #\(i) missing coordinates, dropped")
                    continue
                }
                let p = xf(PlanPoint(x: x, y: y))
                let width = min(max(jsonDouble(d["width"]) ?? 0.9, 0.6), 3.0)
                var axis: PlanPoint?
                if let ax = d["axis"] as? [Any], ax.count == 2,
                   let ax0 = jsonDouble(ax[0]), let ax1 = jsonDouble(ax[1]) {
                    let o = xf(PlanPoint(x: 0, y: 0))
                    let q = xf(PlanPoint(x: ax0, y: ax1))
                    let nlen = hypot(q.x - o.x, q.y - o.y)
                    if nlen > 1e-6 {
                        axis = PlanPoint(x: (q.x - o.x) / nlen, y: (q.y - o.y) / nlen)
                    }
                }
                var connects: [String] = []
                for c in (d["connects"] as? [Any]) ?? [] {
                    connects.append(String(describing: c))
                }
                doors.append(DoorOpening(
                    id: uniqueId(d["id"], "F\(level)-D\(i)"),
                    x: p.x, y: p.y, width: width, axis: axis,
                    connects: connects,
                    exteriorExit: jsonBool(d["exterior_exit"])
                ))
            }

            if walls.isEmpty && spaces.isEmpty {
                warnings.append("Floor \(level): no walls or spaces found")
            }
            floors.append(FloorPlan(level: level, outline: outline, walls: walls, spaces: spaces, doors: doors))
        }

        floors.sort { $0.level < $1.level }
        let levels = floors.map(\.level)

        var verticals: [VerticalLink] = []
        for (i, item) in ((raw["vertical_connections"] as? [Any]) ?? []).enumerated() {
            guard let v = item as? [String: Any] else { continue }
            let x = jsonDouble(v["x"])
            let y = jsonDouble(v["y"])
            var vtype = (v["type"] as? String ?? "stairs").lowercased()
            if !verticalTypes.contains(vtype) { vtype = "stairs" }
            var vfloors: [Int] = []
            for lv in (v["floors"] as? [Any]) ?? [] {
                guard let n = jsonInt(lv), seenLevels.contains(n), !vfloors.contains(n) else { continue }
                vfloors.append(n)
            }
            vfloors.sort()
            if x == nil || y == nil || vfloors.count < 2 {
                warnings.append("Vertical connection \(v["id"] ?? i) invalid (needs x, y, 2+ known floors), dropped")
                continue
            }
            var positions: [Int: PlanPoint] = [:]
            if let dict = v["positions"] as? [String: Any] {
                for (key, pt) in dict {
                    guard let lv = Int(key), vfloors.contains(lv), let p = jsonPoint(pt) else { continue }
                    let align = alignByLevel[lv] ?? Alignment(offsetX: 0, offsetY: 0, rotationDeg: 0)
                    positions[lv] = transform(p, align)
                }
            }
            let vid = uniqueId(v["id"], "V\(i)")
            verticals.append(VerticalLink(
                id: vid,
                name: (v["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? vid,
                type: vtype,
                x: x ?? 0, y: y ?? 0,
                floors: vfloors,
                positions: positions
            ))
        }

        if floors.count > 1 && !verticals.contains(where: { $0.type == "stairs" || $0.type == "ramp" }) {
            warnings.append("Multiple floors but no stairs/ramps: upper floors cannot reach exits")
        }
        if !floors.contains(where: { $0.doors.contains(where: \.exteriorExit) }) {
            warnings.append("No exterior exits found: routing will fail until you mark exterior_exit doors")
        }

        let north = wrap360(jsonDouble(raw["plan_north_deg"]) ?? 0)
        return BuildingModel(
            name: (raw["building"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "Building",
            floorHeightM: jsonDouble(raw["floor_height_m"]) ?? 4,
            wallStyle: raw["wall_style"] as? String == "faces" ? "faces" : "centerline",
            planNorthDeg: north,
            levels: levels,
            floors: floors,
            verticals: verticals,
            warnings: warnings
        )
    }

    static func alignment(_ value: Any?) -> Alignment {
        let d = value as? [String: Any] ?? [:]
        return Alignment(
            offsetX: jsonDouble(d["offset_x"]) ?? 0,
            offsetY: jsonDouble(d["offset_y"]) ?? 0,
            rotationDeg: jsonDouble(d["rotation_deg"]) ?? 0
        )
    }

    static func transform(_ p: PlanPoint, _ align: Alignment) -> PlanPoint {
        let a = align.rotationDeg * .pi / 180
        let c = cos(a), s = sin(a)
        let xr = p.x * c - p.y * s
        let yr = p.x * s + p.y * c
        return PlanPoint(x: xr + align.offsetX, y: yr + align.offsetY)
    }
}

func wrap360(_ d: Double) -> Double {
    var m = d.truncatingRemainder(dividingBy: 360)
    if m < 0 { m += 360 }
    return m
}

func pyMod(_ a: Double, _ m: Double) -> Double {
    var r = a.truncatingRemainder(dividingBy: m)
    if r < 0 { r += m }
    return r
}

func jsonDouble(_ value: Any?) -> Double? {
    guard let value, !(value is Bool), !(value is NSNull) else { return nil }
    if let d = value as? Double { return d.isFinite ? d : nil }
    if let i = value as? Int { return Double(i) }
    if let n = value as? NSNumber {
        let d = n.doubleValue
        return d.isFinite ? d : nil
    }
    return nil
}

func jsonInt(_ value: Any?) -> Int? {
    guard let d = jsonDouble(value) else { return nil }
    if d < Double(Int.min) || d > Double(Int.max) { return nil }
    return Int(d)
}

func jsonBool(_ value: Any?) -> Bool {
    if let b = value as? Bool { return b }
    if let n = value as? NSNumber { return n.boolValue }
    return false
}

func jsonPoint(_ value: Any?) -> PlanPoint? {
    guard let arr = value as? [Any], arr.count >= 2,
          let x = jsonDouble(arr[0]), let y = jsonDouble(arr[1]) else { return nil }
    return PlanPoint(x: x, y: y)
}
