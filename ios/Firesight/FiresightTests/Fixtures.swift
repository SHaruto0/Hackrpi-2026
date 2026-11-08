import XCTest
@testable import Firesight

enum Fixtures {
    static func data(named name: String) throws -> Data {
        let bundle = Bundle(for: GridTests.self)
        let url = bundle.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
            ?? bundle.url(forResource: name, withExtension: "json")
        guard let url else {
            XCTFail("missing fixture \(name).json")
            return Data()
        }
        return try Data(contentsOf: url)
    }

    static func object(named name: String) throws -> [String: Any] {
        let raw = try JSONSerialization.jsonObject(with: data(named: name))
        return raw as? [String: Any] ?? [:]
    }

    static func u32(_ value: Any?) -> UInt32 {
        (value as? NSNumber)?.uint32Value ?? 0
    }

    static func dbl(_ value: Any?) -> Double {
        (value as? NSNumber)?.doubleValue ?? .nan
    }

    static func int(_ value: Any?) -> Int {
        (value as? NSNumber)?.intValue ?? 0
    }
}

/// Plays back the draws recorded by scripts/export_nav_fixtures.py.
final class ReplayRandom: RandomSource {
    let normals: [Double]
    let integers: [Int]
    let units: [Double]
    private var ni = 0
    private var ii = 0
    private var ui = 0

    init(normals: [Double], integers: [Int], units: [Double]) {
        self.normals = normals
        self.integers = integers
        self.units = units
    }

    func nextNormal(mean: Double, std: Double) -> Double {
        let value = normals[ni]
        ni += 1
        return value
    }

    func nextInt(low: Int, high: Int) -> Int {
        let value = integers[ii]
        ii += 1
        return value
    }

    func nextUnit() -> Double {
        let value = units[ui]
        ui += 1
        return value
    }

    var finished: Bool {
        ni == normals.count && ii == integers.count && ui == units.count
    }
}
