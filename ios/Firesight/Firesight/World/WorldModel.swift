import Combine
import Foundation

enum PickMode: String {
    case destination
    case start
}

enum PosMode {
    case sensors
    case demo
}

/// One building, one pose, one route. The 3D map and the camera both read this.
final class WorldModel: ObservableObject {
    let model: BuildingModel
    let grid: BuildingGrid
    let filter: ParticleFilter
    private let fire: FireSim
    private let simQueue = DispatchQueue(label: "firesight.sim")
    private var gen = 0
    private var field: RouteField?
    private var speechKey = ""
    private var playTimer: Timer?
    private var qrOverrides: [String: QRFix] = [:]

    @Published var pose: FilterPose?
    @Published var destinationId: String?
    @Published var destinationLabel: String = ""
    @Published var startRoomId: String?
    @Published var startLabel: String = ""
    @Published var route: NavRoute?
    @Published var hints: NavHints?
    @Published var hazard: String?
    @Published var speechLine: String = ""
    @Published var status: String = "Pick a room to walk to."
    @Published var seenText: String = ""
    @Published var playing = false
    @Published var hazardRev = 0
    @Published var fireTime = 0
    @Published var fireCells: [Bool]
    @Published var smokeCells: [Float]
    @Published var floorFilter: Int?
    @Published var pickMode: PickMode = .destination
    @Published var posMode: PosMode = .sensors
    @Published var walking = false
    @Published var toast: String = ""
    private var demoSeg = 0

    init(model: BuildingModel, grid: BuildingGrid) {
        self.model = model
        self.grid = grid
        let seed = UInt64(Date().timeIntervalSince1970 * 1000) ^ 0xF15E_51A7
        filter = ParticleFilter(grid: grid, planNorth: model.planNorthDeg, rng: SplitMix(seed: seed))
        fire = FireSim(grid: grid, seed: 7)
        fireCells = [Bool](repeating: false, count: grid.count)
        smokeCells = [Float](repeating: 0, count: grid.count)
        if let url = Bundle.main.url(forResource: "qr", withExtension: "json")
            ?? Bundle.main.url(forResource: "qr", withExtension: "json", subdirectory: "Resources"),
           let data = try? Data(contentsOf: url),
           let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for (code, value) in raw {
                if let fix = QRFix(code: code, value: value) { qrOverrides[code] = fix }
            }
        }
    }

    var canNavigate: Bool { pose != nil && destinationId != nil }

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
        status = "Walking to \(destinationLabel)."
        speechKey = ""
        rebuildField()
    }

    func setStart(roomId: String) {
        guard let spot = grid.poseForRoom(roomId) else {
            status = "That room has no walkable spot."
            return
        }
        startRoomId = roomId
        startLabel = spot.label
        demoSeg = 0
        filter.start(level: spot.level, x: spot.x, y: spot.y, sigma: 0.5, roomId: roomId)
        pose = filter.pose()
        seenText = ""
        status = "You are in \(spot.label)."
        refreshRoute()
    }

    @discardableResult
    func snap(toQR raw: String) -> Bool {
        let code = QRCode.roomCode(from: raw)
        if let fix = qrOverrides[code] {
            startRoomId = fix.roomId
            startLabel = fix.label
            filter.start(level: fix.level, x: fix.x, y: fix.y, sigma: 0.4, roomId: fix.roomId)
            pose = filter.pose()
            status = "QR fix at \(fix.label)."
            refreshRoute()
            return true
        }
        if grid.roomById[code] != nil {
            setStart(roomId: code)
            status = "QR fix at \(startLabel)."
            return true
        }
        status = "Unknown code \(code)."
        return false
    }

    func applyStep(headingDeg: Double, lengthM: Double) {
        guard pose != nil else { return }
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
            let cands = RoomText.match(tokens: [reading], model: model)
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

    func ignite(roomId: String) {
        let gen = nextGen()
        let dest = destinationId
        simQueue.async { [weak self] in
            guard let self else { return }
            let ok = self.fire.ignite(roomId: roomId)
            self.publishSim(gen: gen, dest: dest, note: ok ? "Fire started." : "Could not place fire there.")
        }
    }

    func stepFire(_ n: Int) {
        let gen = nextGen()
        let dest = destinationId
        simQueue.async { [weak self] in
            guard let self else { return }
            self.fire.step(n)
            self.publishSim(gen: gen, dest: dest, note: nil)
        }
    }

    func resetFire() {
        let gen = nextGen()
        let dest = destinationId
        simQueue.async { [weak self] in
            guard let self else { return }
            self.fire.reset()
            self.publishSim(gen: gen, dest: dest, note: "Fire reset.")
        }
    }

    func setPlaying(_ on: Bool) {
        playing = on
        playTimer?.invalidate()
        playTimer = nil
        guard on else { return }
        playTimer = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: true) { [weak self] _ in
            self?.stepFire(1)
        }
    }

    /// Banner copy follows viewer/phone.html (`actionText`, `hazardText`).
    func actionText() -> String {
        guard destinationId != nil else { return "Pick where you are going." }
        guard pose != nil else { return "Finding a route..." }
        guard let route else { return "Finding a route..." }
        if !route.reachable {
            if route.reason == "not on a walkable area" { return "Step into a hallway." }
            if route.reason == "in fire" { return "You are in the fire." }
            return "No safe route."
        }
        guard let hints else { return "Finding a route..." }
        let near = hints.distanceToNextM >= 1
        let meters = near ? Int(hints.distanceToNextM.rounded()) : 0
        switch hints.turn {
        case "left": return near ? "In \(meters) m, turn left" : "Turn left"
        case "right": return near ? "In \(meters) m, turn right" : "Turn right"
        case "straight": return near ? "In \(meters) m, continue straight" : "Continue straight"
        case "stairs_down": return "Take the stairs down"
        case "stairs_up": return "Take the stairs up"
        case "arrive":
            if hints.nextLevel == nil || hints.distanceToNextM < 2 {
                return "You are at \(destinationLabel)."
            }
            return "Ahead: \(destinationLabel)."
        default: return "Head for \(destinationLabel)."
        }
    }

    func hazardText() -> String {
        if hazard == "fire_near" { return "Fire ahead. Turn around." }
        if hazard == "smoke_on_route" { return "Smoke ahead. Stay low." }
        return ""
    }

    func whereLine() -> String {
        guard let pose else { return "" }
        if startLabel.isEmpty { return "Floor \(pose.level)" }
        return "Floor \(pose.level) · \(startLabel)"
    }

    var arrived: Bool {
        guard let route, route.reachable, hints?.turn == "arrive" else { return false }
        if hints?.nextLevel == nil { return true }
        return (route.distanceM ?? 99) < 2
    }

    func setPosMode(_ mode: PosMode) {
        posMode = mode
        if mode == .demo { walking = false }
        if mode == .sensors, let pose {
            filter.start(level: pose.level, x: pose.x, y: pose.y, sigma: 0.5, roomId: startRoomId)
            self.pose = filter.pose()
            refreshRoute()
        }
    }

    func toggleWalk() {
        if posMode != .demo {
            posMode = .demo
            walking = true
            return
        }
        walking.toggle()
    }

    /// Demo walk along the route, same step size as phone.html (`1.2 m/s`).
    func demoWalk(dt: Double) {
        guard posMode == .demo, walking, var p = pose, let path = route?.path, !path.isEmpty else { return }
        var budget = 1.2 * dt
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
            if hypot(p.x - b.x, p.y - b.y) < 0.15 {
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
    }

    func jumpStairs() {
        guard let pose, let path = route?.path else {
            showToast("No stairs ahead.")
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
            }
            return
        }
        for pt in path where pt.level != pose.level {
            self.pose = FilterPose(level: pt.level, x: pt.x, y: pt.y, spreadM: 0.2, confidence: 1,
                                   source: "demo", unsure: false, nSteps: pose.nSteps)
            demoSeg = 0
            showToast("Now on floor \(pt.level).")
            refreshRoute()
            return
        }
        showToast("No stairs ahead.")
    }

    func mark(_ status: String) {
        if status == "safe" {
            showToast("You are marked safe.")
        } else {
            showToast("You are marked trapped. Help is coming.")
        }
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
        simQueue.async { [weak self] in
            self?.publishSim(gen: gen, dest: dest, note: nil)
        }
    }

    private func publishSim(gen: Int, dest: String?, note: String?) {
        let fireCopy = fire.fire
        let smokeCopy = fire.smoke
        let t = fire.t
        let field = dest.map {
            RouteField(grid: grid, fire: fireCopy, smoke: smokeCopy, destinationRoomId: $0)
        }
        DispatchQueue.main.async { [weak self] in
            guard let self, gen == self.gen else { return }
            self.fireCells = fireCopy
            self.smokeCells = smokeCopy
            self.fireTime = t
            self.field = field
            self.hazardRev += 1
            if let note { self.status = note }
            self.refreshRoute()
        }
    }

    private func refreshRoute() {
        guard let pose, let field else {
            route = nil
            hints = nil
            hazard = nil
            return
        }
        let r = field.route(level: pose.level, x: pose.x, y: pose.y)
        route = r
        let h = NavMath.hints(path: r.path, level: pose.level, x: pose.x, y: pose.y, planNorth: model.planNorthDeg)
        hints = h
        hazard = r.reachable
            ? NavMath.hazard(grid: grid, fire: fireCells, level: pose.level, x: pose.x, y: pose.y, maxSmoke: r.maxSmoke)
            : nil
        updateSpeech(route: r, hints: h)
    }

    private func updateSpeech(route: NavRoute, hints: NavHints) {
        let key = "\(destinationId ?? "")|\(route.reachable)|\(route.reason)|\(hints.turn)|\(hazard ?? "")"
        if key == speechKey { return }
        speechKey = key
        guard let line = spokenLine(route: route, hints: hints) else { return }
        speechLine = line
    }

    private func spokenLine(route: NavRoute, hints: NavHints) -> String? {
        if !route.reachable {
            return actionText()
        }
        if let first = route.instructions.first, !first.isEmpty { return first }
        if hints.turn == "arrive" { return "You are at \(destinationLabel)." }
        let dist = Int(hints.distanceToNextM.rounded())
        let move: String
        switch hints.turn {
        case "left": move = "Turn left."
        case "right": move = "Turn right."
        case "stairs_up": move = "Take the stairs up."
        case "stairs_down": move = "Take the stairs down."
        default: move = "Continue straight."
        }
        var line = "\(move) \(dist) meters."
        if hazard == "fire_near" { line += " Fire is close. Turn around." }
        else if hazard == "smoke_on_route" { line += " Smoke ahead. Stay low." }
        return line
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
