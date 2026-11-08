import XCTest
@testable import Firesight

final class GridTests: XCTestCase {
    func testSampleGridMatchesFixture() throws {
        let cases = try Fixtures.object(named: "nav_cases")
        let gridJSON = cases["grid"] as? [String: Any] ?? [:]
        let model = try BuildingLoader.load(data: Fixtures.data(named: "sample_building"))
        let grid = BuildingGrid(model: model)

        XCTAssertEqual(grid.originX, Fixtures.dbl(gridJSON["origin_x"]), accuracy: 1e-6)
        XCTAssertEqual(grid.originY, Fixtures.dbl(gridJSON["origin_y"]), accuracy: 1e-6)
        XCTAssertEqual(grid.rows, Fixtures.int(gridJSON["rows"]))
        XCTAssertEqual(grid.cols, Fixtures.int(gridJSON["cols"]))
        XCTAssertEqual(grid.levels, (gridJSON["levels"] as? [Any] ?? []).map { Fixtures.int($0) })
        XCTAssertEqual(grid.walkableCRC(), Fixtures.u32(gridJSON["walkable_crc"]))
        XCTAssertEqual(grid.roomCRC(), Fixtures.u32(gridJSON["room_crc"]))
        XCTAssertEqual(grid.rooms.count, Fixtures.int(gridJSON["room_count"]))

        let expected = gridJSON["links"] as? [[String: Any]] ?? []
        XCTAssertEqual(grid.links.count, expected.count)
        for (link, row) in zip(grid.links, expected) {
            XCTAssertEqual(link.name, row["name"] as? String)
            XCTAssertEqual(link.type, row["type"] as? String)
            let a = (row["a"] as? [Any] ?? []).map { Fixtures.int($0) }
            let b = (row["b"] as? [Any] ?? []).map { Fixtures.int($0) }
            XCTAssertEqual([link.aFi, link.aR, link.aC], a)
            XCTAssertEqual([link.bFi, link.bR, link.bC], b)
            XCTAssertEqual(link.cost, Fixtures.dbl(row["cost"]), accuracy: 1e-3)
        }
    }

    func testRoomPoseMatchesFixture() throws {
        let cases = try Fixtures.object(named: "nav_cases")
        let pose = cases["pose"] as? [String: Any] ?? [:]
        let model = try BuildingLoader.load(data: Fixtures.data(named: "sample_building"))
        let grid = BuildingGrid(model: model)
        let id = pose["room_id"] as? String ?? ""
        let spot = try XCTUnwrap(grid.poseForRoom(id))
        XCTAssertEqual(spot.level, Fixtures.int(pose["level"]))
        XCTAssertEqual(spot.x, Fixtures.dbl(pose["x"]), accuracy: 1e-3)
        XCTAssertEqual(spot.y, Fixtures.dbl(pose["y"]), accuracy: 1e-3)
        XCTAssertEqual(spot.label, pose["label"] as? String)
    }

    func testFacesRasterMatchesFixture() throws {
        let cases = try Fixtures.object(named: "nav_cases")
        let raw = try JSONSerialization.data(withJSONObject: cases["faces_building"] ?? [:])
        let meta = cases["faces"] as? [String: Any] ?? [:]
        let model = try BuildingLoader.load(data: raw)
        let grid = BuildingGrid(model: model)
        XCTAssertEqual(grid.rows, Fixtures.int(meta["rows"]))
        XCTAssertEqual(grid.cols, Fixtures.int(meta["cols"]))
        XCTAssertEqual(grid.walkableCRC(), Fixtures.u32(meta["walkable_crc"]))
        XCTAssertEqual(grid.roomCRC(), Fixtures.u32(meta["room_crc"]))
    }
}
