import Foundation

struct NavHints {
    var nextLevel: Int?
    var nextX: Double?
    var nextY: Double?
    var distanceToNextM: Double
    var turn: String
    var headingPlanDeg: Double
    var headingCompassDeg: Double

    static func empty(north: Double) -> NavHints {
        NavHints(nextLevel: nil, nextX: nil, nextY: nil, distanceToNextM: 0,
                 turn: "arrive", headingPlanDeg: 0, headingCompassDeg: wrap360(north))
    }
}

enum NavMath {
    static let nextM = 1.0
    static let turnDeg = 30.0
    static let fireM = 5.0
    static let smokeCut = 0.35

    static func hints(path: [PathPoint], level: Int, x: Double, y: Double, planNorth: Double) -> NavHints {
        let north = planNorth
        var chosen: (Int, Int, Double, Double, Double)?
        for (i, pt) in path.enumerated() {
            let dist = hypot(pt.x - x, pt.y - y)
            if pt.level != level || dist >= nextM {
                chosen = (i, pt.level, pt.x, pt.y, dist)
                break
            }
        }
        guard let (i, pl, px, py, dist) = chosen else { return .empty(north: north) }
        let heading = bearing(x, y, px, py)
        let headingPlan = heading ?? 0
        let turn: String
        if pl != level {
            turn = pl > level ? "stairs_up" : "stairs_down"
        } else if i + 1 >= path.count {
            turn = "arrive"
        } else {
            let fol = path[i + 1]
            if let after = bearing(px, py, fol.x, fol.y) {
                turn = turnName(heading, after)
            } else {
                turn = "arrive"
            }
        }
        return NavHints(
            nextLevel: pl, nextX: px, nextY: py,
            distanceToNextM: dist,
            turn: turn,
            headingPlanDeg: headingPlan,
            headingCompassDeg: wrap360(north + headingPlan)
        )
    }

    static func bearing(_ x1: Double, _ y1: Double, _ x2: Double, _ y2: Double) -> Double? {
        let dx = x2 - x1, dy = y2 - y1
        if dx * dx + dy * dy < 1e-12 { return nil }
        return wrap360(atan2(dx, -dy) * 180 / .pi)
    }

    static func turnName(_ heading: Double?, _ headingAfter: Double?) -> String {
        guard let heading, let headingAfter else { return "straight" }
        let delta = pyMod(headingAfter - heading + 180, 360) - 180
        if abs(delta) < turnDeg { return "straight" }
        return delta > 0 ? "right" : "left"
    }

    /// `fire_near` when any fire cell center is within 5 m, else smoke on the route.
    static func hazard(grid: BuildingGrid, fire: [Bool], level: Int, x: Double, y: Double, maxSmoke: Double) -> String? {
        guard let fi = grid.levelIndex[level] else { return nil }
        let plane = grid.rows * grid.cols
        let base = fi * plane
        if base + plane <= fire.count {
            for i in 0..<plane where fire[base + i] {
                let r = i / grid.cols
                let c = i % grid.cols
                let dx = grid.centerX(c) - x
                let dy = grid.centerY(r) - y
                if hypot(dx, dy) <= fireM { return "fire_near" }
            }
        }
        if maxSmoke > smokeCut { return "smoke_on_route" }
        return nil
    }
}
