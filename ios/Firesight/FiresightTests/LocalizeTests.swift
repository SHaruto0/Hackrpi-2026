import XCTest
@testable import Firesight

final class LocalizeTests: XCTestCase {
    func testParticleReplayMatchesFixture() throws {
        let cases = try Fixtures.object(named: "nav_cases")
        let spec = cases["particle"] as? [String: Any] ?? [:]
        let model = try BuildingLoader.load(data: Fixtures.data(named: "sample_building"))
        let grid = BuildingGrid(model: model)
        let normals = (spec["normals"] as? [Any] ?? []).map { Fixtures.dbl($0) }
        let integers = (spec["integers"] as? [Any] ?? []).map { Fixtures.int($0) }
        let units = (spec["units"] as? [Any] ?? []).map { Fixtures.dbl($0) }
        let rng = ReplayRandom(normals: normals, integers: integers, units: units)
        let filter = ParticleFilter(grid: grid, planNorth: model.planNorthDeg, rng: rng)
        filter.start(
            level: Fixtures.int(spec["level"]),
            x: Fixtures.dbl(spec["x"]),
            y: Fixtures.dbl(spec["y"]),
            sigma: Fixtures.dbl(spec["sigma_m"]),
            roomId: spec["room_id"] as? String
        )
        filter.step(headingDeg: Fixtures.dbl(spec["heading_deg"]), lengthM: Fixtures.dbl(spec["length_m"]))
        let sight = spec["sight"] as? [String: Any] ?? [:]
        let points = (sight["points"] as? [[Any]] ?? []).map {
            PlanPoint(x: Fixtures.dbl($0.count > 0 ? $0[0] : nil), y: Fixtures.dbl($0.count > 1 ? $0[1] : nil))
        }
        filter.observe([
            Sighting(
                text: sight["text"] as? String ?? "",
                level: Fixtures.int(sight["level"]),
                score: Fixtures.dbl(sight["score"]),
                points: points
            )
        ])
        let pose = filter.pose()
        let want = spec["pose"] as? [String: Any] ?? [:]
        XCTAssertEqual(pose.level, Fixtures.int(want["level"]))
        XCTAssertEqual(pose.x, Fixtures.dbl(want["x"]), accuracy: 1e-3)
        XCTAssertEqual(pose.y, Fixtures.dbl(want["y"]), accuracy: 1e-3)
        XCTAssertEqual(pose.spreadM, Fixtures.dbl(want["spread_m"]), accuracy: 1e-3)
        XCTAssertEqual(pose.confidence, Fixtures.dbl(want["confidence"]), accuracy: 1e-3)
        XCTAssertEqual(pose.source, want["source"] as? String)
        XCTAssertEqual(pose.unsure, want["unsure"] as? Bool)
        XCTAssertEqual(pose.nSteps, Fixtures.int(want["n_steps"]))
        XCTAssertTrue(rng.finished)
    }

    func testRoomTextMatchesFixture() throws {
        let cases = try Fixtures.object(named: "nav_cases")
        let text = cases["text"] as? [String: Any] ?? [:]
        let model = try BuildingLoader.load(data: Fixtures.data(named: "sample_building"))
        let tokens = (text["tokens"] as? [[String: Any]] ?? []).map { row in
            (text: row["text"] as? String ?? "", conf: Fixtures.dbl(row["conf"]))
        }
        let got = RoomText.match(tokens: tokens, model: model)
        let want = text["matches"] as? [[String: Any]] ?? []
        XCTAssertEqual(got.count, want.count)
        for (match, row) in zip(got, want) {
            XCTAssertEqual(match.roomId, row["room_id"] as? String)
            XCTAssertEqual(match.level, Fixtures.int(row["level"]))
            XCTAssertEqual(match.label, row["label"] as? String)
            XCTAssertEqual(match.score, Fixtures.dbl(row["score"]), accuracy: 1e-3)
            let pts = row["points"] as? [[Any]] ?? []
            XCTAssertEqual(match.points.count, pts.count)
            for (p, raw) in zip(match.points, pts) {
                XCTAssertEqual(p.x, Fixtures.dbl(raw.count > 0 ? raw[0] : nil), accuracy: 1e-3)
                XCTAssertEqual(p.y, Fixtures.dbl(raw.count > 1 ? raw[1] : nil), accuracy: 1e-3)
            }
        }
    }

    func testQRCodeParsesPhoneLink() {
        XCTAssertEqual(QRCode.roomCode(from: "https://firesight.local/phone.html?qr=F3-318"), "F3-318")
        XCTAssertEqual(QRCode.roomCode(from: "F3-318"), "F3-318")
        XCTAssertEqual(QRCode.roomCode(from: "  F2-204  "), "F2-204")
    }
}
