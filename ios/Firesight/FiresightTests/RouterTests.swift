import XCTest
@testable import Firesight

final class RouterTests: XCTestCase {
    func testRoomToRoomRouteMatchesFixture() throws {
        let cases = try Fixtures.object(named: "nav_cases")
        let spec = cases["route"] as? [String: Any] ?? [:]
        let pose = cases["pose"] as? [String: Any] ?? [:]
        let model = try BuildingLoader.load(data: Fixtures.data(named: "sample_building"))
        let grid = BuildingGrid(model: model)
        let dest = spec["dest_room"] as? String ?? ""
        let field = RouteField(grid: grid, fire: nil, smoke: nil, destinationRoomId: dest)
        let route = field.route(
            level: Fixtures.int(pose["level"]),
            x: Fixtures.dbl(pose["x"]),
            y: Fixtures.dbl(pose["y"])
        )
        XCTAssertTrue(route.reachable)
        XCTAssertEqual(route.distanceM ?? -1, Fixtures.dbl(spec["distance_m"]), accuracy: 1e-6)
        XCTAssertEqual(route.maxSmoke, Fixtures.dbl(spec["max_smoke"]), accuracy: 1e-6)
        XCTAssertEqual(route.instructions, spec["instructions"] as? [String])
        let blob = route.instructions.joined(separator: "\n")
        XCTAssertFalse(blob.contains("Elevator"))
        XCTAssertTrue(blob.contains("Stairs") || blob.contains("stairs"))
        XCTAssertTrue(route.instructions.last?.hasPrefix("Arrive at") == true)
        assertPath(route.path, spec["path"])

        let hints = NavMath.hints(
            path: route.path,
            level: Fixtures.int(pose["level"]),
            x: Fixtures.dbl(pose["x"]),
            y: Fixtures.dbl(pose["y"]),
            planNorth: Fixtures.dbl(spec["plan_north_deg"])
        )
        XCTAssertEqual(hints.turn, spec["turn"] as? String)
        XCTAssertEqual(hints.distanceToNextM, Fixtures.dbl(spec["distance_to_next_m"]), accuracy: 1e-3)
        XCTAssertEqual(hints.headingPlanDeg, Fixtures.dbl(spec["heading_plan_deg"]), accuracy: 1e-3)
        XCTAssertEqual(hints.headingCompassDeg, Fixtures.dbl(spec["heading_compass_deg"]), accuracy: 1e-3)
        if let next = spec["next"] as? [String: Any] {
            XCTAssertEqual(hints.nextLevel, Fixtures.int(next["level"]))
            XCTAssertEqual(hints.nextX ?? -1, Fixtures.dbl(next["x"]), accuracy: 1e-3)
            XCTAssertEqual(hints.nextY ?? -1, Fixtures.dbl(next["y"]), accuracy: 1e-3)
        } else {
            XCTAssertNil(hints.nextLevel)
        }
        let hazard = NavMath.hazard(
            grid: grid, fire: [Bool](repeating: false, count: grid.count),
            level: Fixtures.int(pose["level"]), x: Fixtures.dbl(pose["x"]), y: Fixtures.dbl(pose["y"]),
            maxSmoke: route.maxSmoke
        )
        XCTAssertEqual(hazard, spec["hazard"] as? String)
    }

    func testBurnedRoomIsUnreachable() throws {
        let cases = try Fixtures.object(named: "nav_cases")
        let spec = cases["route"] as? [String: Any] ?? [:]
        let blocked = cases["blocked"] as? [String: Any] ?? [:]
        let pose = cases["pose"] as? [String: Any] ?? [:]
        let model = try BuildingLoader.load(data: Fixtures.data(named: "sample_building"))
        let grid = BuildingGrid(model: model)
        let dest = spec["dest_room"] as? String ?? ""
        let info = try XCTUnwrap(grid.roomById[dest])
        var fire = [Bool](repeating: false, count: grid.count)
        guard let fi = grid.levelIndex[info.level] else {
            XCTFail("missing floor")
            return
        }
        for r in 0..<grid.rows {
            for c in 0..<grid.cols where grid.roomAt(fi, r, c) == Int32(info.index) && grid.walkable(fi, r, c) {
                fire[grid.flat(fi, r, c)] = true
            }
        }
        let field = RouteField(grid: grid, fire: fire, smoke: nil, destinationRoomId: dest)
        let route = field.route(
            level: Fixtures.int(pose["level"]),
            x: Fixtures.dbl(pose["x"]),
            y: Fixtures.dbl(pose["y"])
        )
        XCTAssertFalse(route.reachable)
        XCTAssertEqual(route.reason, blocked["reason"] as? String)
    }

    private func assertPath(_ path: [PathPoint], _ raw: Any?) {
        let rows = raw as? [[Any]] ?? []
        XCTAssertEqual(path.count, rows.count)
        for (pt, row) in zip(path, rows) {
            XCTAssertEqual(pt.level, Fixtures.int(row.count > 0 ? row[0] : nil))
            XCTAssertEqual(pt.x, Fixtures.dbl(row.count > 1 ? row[1] : nil), accuracy: 0.011)
            XCTAssertEqual(pt.y, Fixtures.dbl(row.count > 2 ? row[2] : nil), accuracy: 0.011)
        }
    }
}
