import Foundation

protocol RandomSource: AnyObject {
    func nextNormal(mean: Double, std: Double) -> Double
    func nextInt(low: Int, high: Int) -> Int
    func nextUnit() -> Double
}

/// SplitMix64 with Box-Muller normals. `nextUnit` is 53 bits in [0, 1).
final class SplitMix: RandomSource {
    private var s: UInt64
    private var spare: Double?

    init(seed: UInt64) {
        s = seed
    }

    func nextU64() -> UInt64 {
        s &+= 0x9E3779B97F4A7C15
        var z = s
        z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
        z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
        return z ^ (z >> 31)
    }

    func nextUnit() -> Double {
        Double(nextU64() >> 11) * (1.0 / 9007199254740992.0)
    }

    func nextInt(low: Int, high: Int) -> Int {
        let span = high - low
        if span <= 0 { return low }
        return low + Int(nextU64() % UInt64(span))
    }

    func nextNormal(mean: Double, std: Double) -> Double {
        if let z = spare {
            spare = nil
            return mean + std * z
        }
        let u1 = max(nextUnit(), 1e-300)
        let u2 = nextUnit()
        let r = sqrt(-2 * log(u1))
        let theta = 2 * Double.pi * u2
        spare = r * sin(theta)
        return mean + std * r * cos(theta)
    }
}
