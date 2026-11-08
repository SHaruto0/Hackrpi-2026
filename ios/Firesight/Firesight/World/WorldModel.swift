import Combine
import Foundation

enum PickMode: String {
    case destination
    case start
}

enum PosMode: Hashable {
    case sensors
    case demo
}

/// One building, one pose, one route. The 3D map and the camera both read this.
final class WorldModel: ObservableObject {
    let model: BuildingModel
    let grid: BuildingGrid
    let filter: ParticleFilter
    private let routeQueue = DispatchQueue(label: "firesight.route")
    private var gen = 0
    private var field: RouteField?
    private var speechKey = ""
    private var qrOverrides: [String: QRFix] = [:]

    @Published var pose: FilterPose?
    @Published var destinationId: String?
    @Published var destinationLabel: String = ""
    @Published var startRoomId: String?
    @Published var startLabel: String = ""
    @Published var route: NavRoute?
    @Published var hints: NavHints?
    @Published var speechLine: String = ""
    @Published var status: String = "Pick a room to walk to."
    @Published var seenText: String = ""
    @Published var headingDeg: Double?
    @Published var pickMode: PickMode = .destination
    @Published var posMode: PosMode = .sensors
    @Published var walking = false
    @Published var toast: String = ""
    private var demoSeg = 0
    private var demoPath: [PathPoint] = []

    init(model: BuildingModel, grid: BuildingGrid) {
        self.model = model
        self.grid = grid
        let seed = UInt64(Date().timeIntervalSince1970 * 1000) ^ 0xF15E_51A7
        filter = ParticleFilter(grid: grid, planNorth: model.planNorthDeg, rng: SplitMix(seed: seed))
        if let url = Bundle.main.url(forResource: "qr", withExtension: "json")
            ?? Bundle.main.url(forResource: "qr", withExtension: "json", subdirectory: "Resources"),
           let data = try? Data(contentsOf: url),
           let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for (code, value) in raw {
                if let fix = QRFix(code: code, value: value) { qrOverrides[code] = fix }
            }
        }
    }

    var canNavigate: Bool { pose != nil && destinationId != nil && route?.reachable == true }

    func choose(roomId: String) {
        if pickMode == .start {
            setStart(roomId: roomId)
        } else {
            setDestination(roomId)
        }
    }

    func setDestination(_ id: String) {
        guard grid.roomById[id] != nil else { return }
        destinationId = id
        destinationLabel = grid.roomById[id]?.label ?? id
        walking = false
        demoPath = []
        status = "Walking to \(destinationLabel)."
        speechKey = ""
        rebuildField()
    }

    @discardableResult
    func setStart(roomId: String) -> Bool {
        guard let spot = grid.poseForRoom(roomId) else {
            status = "That room has no walkable spot."
            return false
        }
        startRoomId = roomId
        startLabel = spot.label
        demoSeg = 0
        demoPath = []
        walking = false
        filter.start(level: spot.level, x: spot.x, y: spot.y, sigma: 0.5, roomId: roomId)
        pose = filter.pose()
        seenText = ""
        status = "You are in \(spot.label)."
        refreshRoute()
        return true
    }

    @discardableResult
    func snap(toQR raw: String) -> Bool {
        let code = QRCode.roomCode(from: raw)
        if let fix = qrOverrides[code] {
            demoPath = []
            startRoomId = fix.roomId
            startLabel = fix.label
            filter.start(level: fix.level, x: fix.x, y: fix.y, sigma: 0.4, roomId: nil)
            pose = filter.pose()
            status = "QR fix at \(fix.label)."
            refreshRoute()
            return true
        }
        if grid.roomById[code] != nil {
            guard setStart(roomId: code) else { return false }
            status = "QR fix at \(startLabel)."
            return true
        }
        status = "Unknown code \(code)."
        return false
    }

    func applyStep(headingDeg: Double, lengthM: Double) {
        guard posMode == .sensors, pose != nil, headingDeg.isFinite,
              lengthM.isFinite, lengthM > 0 else { return }
        filter.step(headingDeg: headingDeg, lengthM: lengthM)
        pose = filter.pose()
        refreshRoute()
    }

    func tookStairs(_ direction: String) {
        guard pose != nil else { return }
        filter.stairs(direction: direction)
        pose = filter.pose()
        let way = direction == "up" ? "up" : "down"
        status = "Stairs \(way). Now floor \(pose?.level ?? 0)."
        refreshRoute()
    }

    func observe(readings: [(text: String, conf: Double)]) {
        guard pose != nil, !readings.isEmpty else { return }
        var sights: [Sighting] = []
        var labels: [String] = []
        for reading in readings {
            // A repeated room number on another floor must not move the user there.
            let cands = RoomText.match(tokens: [reading], model: model)
                .filter { $0.level == pose?.level && $0.score >= 0.6 }
            guard let best = cands.max(by: { $0.score < $1.score }) else { continue }
            sights.append(Sighting(text: reading.text, level: best.level, score: best.score, points: best.points))
            if best.roomId != "EXIT" { labels.append(best.label) }
        }
        guard !sights.isEmpty else { return }
        filter.observe(sights)
        pose = filter.pose()
        if let label = labels.last { seenText = label }
        refreshRoute()
    }

    func actionText() -> String {
        guard destinationId != nil else { return "Pick where you are going." }
        guard pose != nil else { return "Finding a route..." }
        guard let route else { return "Finding a route..." }
        if !route.reachable {
            if route.reason == "not on a walkable area" { return "Step into a hallway." }
            return "No route to this room. Choose another destination."
        }
        if arrived { return "You are at \(destinationLabel)." }
        if posMode == .sensors, pose?.unsure == true {
            return "Location uncertain. Scan a room QR or confirm your location from Home."
        }
        guard let hints else { return "Finding a route..." }
        if let delta = relativeHeading, abs(delta) > 55 {
            if abs(delta) > 135 { return "Turn around to follow the route." }
            return delta > 0 ? "Turn right to face the route." : "Turn left to face the route."
        }
        let near = hints.distanceToNextM >= 1
        let meters = near ? Int(hints.distanceToNextM.rounded()) : 0
        switch hints.turn {
        case "left": return near ? "In \(meters) m, turn left" : "Turn left"
        case "right": return near ? "In \(meters) m, turn right" : "Turn right"
        case "straight": return near ? "In \(meters) m, continue straight" : "Continue straight"
        case "stairs_down": return near ? "In \(meters) m, take the stairs down" : "Take the stairs down"
        case "stairs_up": return near ? "In \(meters) m, take the stairs up" : "Take the stairs up"
        case "arrive": return "Continue \(max(1, meters)) m to \(destinationLabel)."
        default: return "Head for \(destinationLabel)."
        }
    }

    var currentRoom: RoomInfo? {
        guard let pose, let fi = grid.levelIndex[pose.level] else { return nil }
        let (r, c) = grid.cellOf(pose.x, pose.y)
        guard grid.inRange(r, c) else { return nil }
        let index = Int(grid.roomAt(fi, r, c))
        return grid.rooms.first { $0.index == index && $0.level == pose.level }
    }

    func whereLine() -> String {
        guard let pose else { return "Location not set" }
        let label = currentRoom.map { " | \($0.label)" } ?? ""
        let estimate = pose.unsure ? " | Location uncertain" : ""
        return "Floor \(pose.level)\(label)\(estimate)"
    }

    var arrived: Bool {
        guard let route, route.reachable, let pose,
              let destination = grid.roomById[destinationId ?? ""],
              pose.level == destination.level, let distance = route.distanceM,
              let last = route.path.last, last.level == pose.level else { return false }
        return distance < 1 && hypot(last.x - pose.x, last.y - pose.y) < 1
            && (posMode == .demo || !pose.unsure)
    }

    var relativeHeading: Double? {
        guard posMode == .sensors, let headingDeg, let hints else { return nil }
        return pyMod(hints.headingCompassDeg - headingDeg + 180, 360) - 180
    }

    func updateHeading(_ heading: Double?) {
        headingDeg = heading.flatMap { $0.isFinite ? wrap360($0) : nil }
        if let route, let hints { updateSpeech(route: route, hints: hints) }
    }

    func endNavigation() {
        walking = false
        posMode = .sensors
        headingDeg = nil
        demoPath = []
        demoSeg = 0
        destinationId = nil
        destinationLabel = ""
        pose = nil
        startRoomId = nil
        startLabel = ""
        field = nil
        gen += 1 // Discard any destination computation still in flight.
        route = nil
        hints = nil
        speechLine = ""
        speechKey = ""
        seenText = ""
        toast = ""
        status = "Choose your current location."
    }

    func setPosMode(_ mode: PosMode) {
        posMode = mode
        walking = false
        demoPath = []
        demoSeg = 0
        if mode == .sensors, pose != nil {
            // Demo never advances the real sensor filter. Restore the live position.
            self.pose = filter.pose()
            refreshRoute()
        }
    }

    func toggleWalk() {
        guard posMode == .demo, route?.reachable == true, !arrived else { return }
        if demoPath.isEmpty {
            demoPath = route?.path ?? []
            demoSeg = 0
        }
        walking.toggle()
    }

    /// Demo walk along the route, same step size as phone.html (`1.2 m/s`).
    func demoWalk(dt: Double) {
        guard posMode == .demo, walking, var p = pose, !demoPath.isEmpty else { return }
        let path = demoPath
        guard dt.isFinite, dt > 0 else { return }
        var budget = 1.2 * min(dt, 0.5)
        var guardN = 0
        while budget > 0.0001 && guardN < 24 {
            guardN += 1
            if demoSeg >= path.count - 1 {
                let last = path[path.count - 1]
                if last.level != p.level {
                    p.level = last.level
                    p.x = last.x
                    p.y = last.y
                } else {
                    budget -= move(&p, last.x, last.y, budget)
                }
                break
            }
            let a = path[demoSeg]
            let b = path[demoSeg + 1]
            if a.level != p.level {
                p.level = a.level
                p.x = a.x
                p.y = a.y
                continue
            }
            if b.level != p.level {
                budget -= move(&p, a.x, a.y, budget)
                if hypot(p.x - a.x, p.y - a.y) < 0.25 {
                    p.level = b.level
                    p.x = b.x
                    p.y = b.y
                    demoSeg += 1
                }
                continue
            }
            budget -= move(&p, b.x, b.y, budget)
            if hypot(p.x - b.x, p.y - b.y) < 0.0001 {
                p.x = b.x
                p.y = b.y
                demoSeg += 1
                continue
            }
            break
        }
        pose = FilterPose(level: p.level, x: p.x, y: p.y, spreadM: 0.2, confidence: 1,
                          source: "demo", unsure: false, nSteps: p.nSteps)
        refreshRoute()
        if arrived { walking = false }
    }

    func jumpStairs() {
        guard let pose, let path = route?.path else {
            showToast("No stairs ahead.")
            return
        }
        guard let landing = path.first(where: { $0.level != pose.level }),
              let index = path.firstIndex(of: landing), index > 0,
              hypot(path[index - 1].x - pose.x, path[index - 1].y - pose.y) <= 2 else {
            showToast("Walk to the stairs first.")
            return
        }
        if posMode == .sensors {
            guard let dir = stairDirection() else {
                showToast("No stairs ahead.")
                return
            }
            let before = pose.level
            tookStairs(dir)
            if self.pose?.level != before {
                showToast("Now on floor \(self.pose?.level ?? before).")
            } else {
                showToast("Could not confirm the floor. Scan a QR at the landing or choose your location from Home.")
            }
            return
        }
        self.pose = FilterPose(level: landing.level, x: landing.x, y: landing.y, spreadM: 0.2, confidence: 1,
                               source: "demo", unsure: false, nSteps: pose.nSteps)
        demoSeg = 0
        demoPath = []
        walking = false
        showToast("Now on floor \(landing.level).")
        refreshRoute()
    }

    func showToast(_ text: String) {
        toast = text
    }

    func placeAt(roomId: String) {
        guard let spot = grid.poseForRoom(roomId) else {
            status = "That room is not on the map."
            return
        }
        startRoomId = roomId
        startLabel = spot.label
        demoSeg = 0
        demoPath = []
        walking = false
        if posMode == .sensors {
            filter.start(level: spot.level, x: spot.x, y: spot.y, sigma: 0.5, roomId: roomId)
            pose = filter.pose()
        } else {
            pose = FilterPose(level: spot.level, x: spot.x, y: spot.y, spreadM: 0.2, confidence: 1,
                              source: "demo", unsure: false, nSteps: 0)
        }
        status = "You are in \(spot.label)."
        refreshRoute()
    }

    private func stairDirection() -> String? {
        if hints?.turn == "stairs_up" { return "up" }
        if hints?.turn == "stairs_down" { return "down" }
        if let next = hints?.nextLevel, let pose, next != pose.level {
            return next > pose.level ? "up" : "down"
        }
        return nil
    }

    private func move(_ p: inout FilterPose, _ x: Double, _ y: Double, _ budget: Double) -> Double {
        let dx = x - p.x
        let dy = y - p.y
        let dist = hypot(dx, dy)
        if dist < 1e-4 { return 0 }
        let step = min(dist, budget)
        p.x += (dx / dist) * step
        p.y += (dy / dist) * step
        return step
    }

    // MARK: private

    private func nextGen() -> Int {
        gen += 1
        return gen
    }

    private func rebuildField() {
        let gen = nextGen()
        let dest = destinationId
        field = nil
        route = nil
        hints = nil
        speechLine = ""
        routeQueue.async { [weak self] in
            guard let self else { return }
            let field = dest.map {
                RouteField(grid: self.grid, fire: nil, smoke: nil, destinationRoomId: $0)
            }
            DispatchQueue.main.async { [weak self] in
                guard let self, gen == self.gen else { return }
                self.field = field
                self.refreshRoute()
            }
        }
    }

    private func refreshRoute() {
        guard let pose, let field else {
            route = nil
            hints = nil
            return
        }
        let r = field.route(level: pose.level, x: pose.x, y: pose.y)
        route = r
        let h = NavMath.hints(path: r.path, level: pose.level, x: pose.x, y: pose.y, planNorth: model.planNorthDeg)
        hints = h
        updateSpeech(route: r, hints: h)
    }

    private func updateSpeech(route: NavRoute, hints: NavHints) {
        // Announce changes in maneuver, facing, floor, and distance band, not every step.
        let facing: String
        if let delta = relativeHeading, abs(delta) > 55 {
            facing = abs(delta) > 135 ? "back" : (delta > 0 ? "right" : "left")
        } else { facing = "ahead" }
        let band = Int(max(0, hints.distanceToNextM) / 5)
        let key = "\(destinationId ?? "")|\(route.reachable)|\(hints.turn)|\(pose?.level ?? 0)|\(band)|\(facing)|\(arrived)|\(pose?.unsure ?? false)"
        guard key != speechKey else { return }
        speechKey = key
        speechLine = actionText()
    }

}

private struct QRFix {
    var roomId: String?
    var label: String
    var level: Int
    var x: Double
    var y: Double

    init?(code: String, value: Any) {
        guard let d = value as? [String: Any],
              let level = jsonInt(d["level"]),
              let x = jsonDouble(d["x"]),
              let y = jsonDouble(d["y"]),
              x.isFinite, y.isFinite else { return nil }
        self.level = level
        self.x = x
        self.y = y
        if let id = d["room_id"] as? String, !id.isEmpty {
            roomId = id
        } else {
            roomId = code
        }
        label = (d["label"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? code
    }
}

enum WorldLoader {
    static func make(json: Data, cacheDir: URL?) throws -> WorldModel {
        let model = try BuildingLoader.load(data: json)
        let grid = BuildingGrid.load(model: model, json: json, cacheDir: cacheDir)
        return WorldModel(model: model, grid: grid)
    }
}

final class WorldBox: @unchecked Sendable {
    let world: WorldModel
    init(_ world: WorldModel) { self.world = world }
}
