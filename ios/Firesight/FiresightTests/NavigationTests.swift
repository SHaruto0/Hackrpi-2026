import XCTest
@testable import Firesight

final class NavigationTests: XCTestCase {
    func testStepDetectorCountsWalkingAccelerationInG() {
        var detector = StepDetector()
        var lengths: [Double] = []
        // Ten seconds of two steps/second at 50 Hz, including gravity.
        for i in 0..<500 {
            let t = Double(i) / 50
            let g = 1 + 0.22 * sin(4 * .pi * t)
            if let length = detector.sample(x: 0, y: 0, z: g, timestamp: t) {
                lengths.append(length)
            }
        }
        XCTAssertTrue((18...21).contains(lengths.count), "Detected \(lengths.count) steps")
        XCTAssertTrue(lengths.allSatisfy { (0.45...0.9).contains($0) })
    }

    func testStepDetectorIgnoresStationaryPhoneAndInvalidSamples() {
        var detector = StepDetector()
        for i in 0..<500 {
            XCTAssertNil(detector.sample(x: 1, y: 0, z: 0, timestamp: Double(i) / 50))
        }
        XCTAssertNil(detector.sample(x: .nan, y: 0, z: 1, timestamp: 11))
        XCTAssertNil(detector.sample(x: 0, y: 0, z: 1, timestamp: .infinity))
    }

    func testStairApproachIsNotArrival() {
        let hints = NavMath.hints(path: [
            PathPoint(level: 2, x: 0, y: 0),
            PathPoint(level: 2, x: 0, y: -5),
            PathPoint(level: 1, x: 0, y: -5),
            PathPoint(level: 1, x: 8, y: -5),
        ], level: 2, x: 0, y: 0, planNorth: 0)
        XCTAssertEqual(hints.turn, "stairs_down")
        XCTAssertEqual(hints.distanceToNextM, 5)
    }

    @MainActor
    func testDemoCompletesRouteAcrossFloorsWithoutSkippingCorners() async throws {
        let world = try await readyWorld()
        world.setPosMode(.demo)
        world.toggleWalk()
        var floors = Set<Int>()
        for _ in 0..<1600 {
            let before = try XCTUnwrap(world.pose)
            floors.insert(before.level)
            world.demoWalk(dt: 0.1)
            let after = try XCTUnwrap(world.pose)
            if before.level == after.level {
                XCTAssertLessThanOrEqual(hypot(after.x - before.x, after.y - before.y), 0.121)
            }
            if world.arrived { break }
        }
        XCTAssertTrue(world.arrived)
        XCTAssertEqual(world.pose?.level, 1)
        XCTAssertTrue(floors.contains(3))
        XCTAssertFalse(world.walking)
    }

    @MainActor
    func testLiveModeDoesNotStartDemoAndRestoresLivePosition() async throws {
        let world = try await readyWorld()
        let live = try XCTUnwrap(world.pose)
        world.toggleWalk()
        XCTAssertEqual(world.posMode, .sensors)
        XCTAssertFalse(world.walking)
        world.setPosMode(.demo)
        world.toggleWalk()
        for _ in 0..<30 { world.demoWalk(dt: 0.1) }
        world.setPosMode(.sensors)
        let restored = try XCTUnwrap(world.pose)
        XCTAssertEqual(restored.level, live.level)
        XCTAssertEqual(restored.x, live.x, accuracy: 0.001)
        XCTAssertEqual(restored.y, live.y, accuracy: 0.001)
        XCTAssertFalse(world.walking)
    }

    @MainActor
    func testGuidanceUsesFacingAndCurrentRoom() async throws {
        let world = try await readyWorld()
        let hints = try XCTUnwrap(world.hints)
        world.pose?.unsure = false
        world.updateHeading(wrap360(hints.headingCompassDeg + 180))
        XCTAssertEqual(world.actionText(), "Turn around to follow the route.")
        world.updateHeading(hints.headingCompassDeg)
        XCTAssertFalse(world.actionText().contains("Turn around"))

        let target = try XCTUnwrap(world.grid.poseForRoom("F1-105"))
        world.pose = FilterPose(level: target.level, x: target.x, y: target.y,
                                spreadM: 0.2, confidence: 1, source: "test", unsure: false, nSteps: 20)
        XCTAssertEqual(world.currentRoom?.id, "F1-105")
        XCTAssertTrue(world.whereLine().contains("105"))
        XCTAssertFalse(world.whereLine().contains("302"))
    }

    @MainActor
    func testCannotJumpToStairsFromAnotherRoom() async throws {
        let world = try await readyWorld()
        world.setPosMode(.demo)
        let before = try XCTUnwrap(world.pose)
        world.jumpStairs()
        XCTAssertEqual(world.pose?.level, before.level)
        XCTAssertEqual(world.pose?.x, before.x)
        XCTAssertEqual(world.pose?.y, before.y)
        XCTAssertEqual(world.toast, "Walk to the stairs first.")
    }

    @MainActor
    func testHomeClearsNavigationAndDiscardsPendingRoute() async throws {
        let world = try await readyWorld()
        world.setDestination("F3-302")
        world.endNavigation()
        try await Task.sleep(nanoseconds: 150_000_000)
        XCTAssertNil(world.pose)
        XCTAssertNil(world.route)
        XCTAssertNil(world.hints)
        XCTAssertNil(world.destinationId)
        XCTAssertEqual(world.speechLine, "")
        XCTAssertFalse(world.walking)
        XCTAssertFalse(world.canNavigate)
        XCTAssertFalse(world.setStart(roomId: "missing-room"))
    }

    @MainActor
    private func readyWorld() async throws -> WorldModel {
        let model = try BuildingLoader.load(data: Fixtures.data(named: "sample_building"))
        let grid = BuildingGrid(model: model)
        let world = WorldModel(model: model, grid: grid)
        XCTAssertTrue(world.setStart(roomId: "F3-302"))
        world.setDestination("F1-105")
        for _ in 0..<500 {
            if world.route != nil { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertTrue(try XCTUnwrap(world.route).reachable)
        return world
    }
}
