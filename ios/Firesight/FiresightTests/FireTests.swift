import XCTest
@testable import Firesight

final class FireTests: XCTestCase {
    func testFireStaysOnWalkableCellsAndSmokeRises() throws {
        let model = try BuildingLoader.load(data: Fixtures.data(named: "sample_building"))
        let grid = BuildingGrid(model: model)
        let sim = FireSim(grid: grid, seed: 7)
        let link = grid.links.first { $0.type == "stairs" && $0.name.localizedCaseInsensitiveContains("West") }
            ?? grid.links.first { $0.type == "stairs" }
        let stair = try XCTUnwrap(link)
        let lower = stair.aFi <= stair.bFi ? (stair.aFi, stair.aR, stair.aC, stair.bFi, stair.bR, stair.bC)
            : (stair.bFi, stair.bR, stair.bC, stair.aFi, stair.aR, stair.aC)
        let spot = grid.centerOf(lower.1, lower.2)
        XCTAssertTrue(sim.ignite(fi: lower.0, x: spot.x, y: spot.y, radius: 0.4))
        sim.step(1)
        for i in 0..<grid.count where sim.fire[i] {
            XCTAssertNotEqual(grid.walkable[i], 0)
        }
        let up = grid.flat(lower.3, lower.4, lower.5)
        XCTAssertGreaterThan(sim.smoke[up], 0.5)
    }
}
