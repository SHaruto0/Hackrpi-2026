import CoreMotion
import Foundation

/// Peak step detector from viewer/pdr.js. Length is Weinberg, clamped to 0.45...0.9 m.
final class StepCounter {
    private let motion = CMMotionManager()
    private var onStep: ((Double, Double) -> Void)?
    private var heading: () -> Double? = { nil }
    private var detector = StepDetector()
    var onStatus: ((String) -> Void)?

    func start(heading: @escaping () -> Double?, onStep: @escaping (Double, Double) -> Void) {
        stop()
        detector = StepDetector()
        self.heading = heading
        self.onStep = onStep
        guard motion.isAccelerometerAvailable else {
            onStatus?("Motion tracking unavailable on this device.")
            return
        }
        motion.accelerometerUpdateInterval = 1.0 / 50.0
        motion.startAccelerometerUpdates(to: .main) { [weak self] data, error in
            guard let self else { return }
            if error != nil {
                self.onStatus?("Motion unavailable. Check Motion & Fitness in Settings.")
                return
            }
            guard let data else { return }
            let a = data.acceleration
            let length = self.detector.sample(x: a.x, y: a.y, z: a.z, timestamp: data.timestamp)
            guard let heading = self.heading(), heading.isFinite else {
                self.onStatus?("Waiting for compass. Hold the phone facing your walking direction.")
                return
            }
            self.onStatus?("Tracking your steps")
            if let length { self.onStep?(wrap360(heading), length) }
        }
    }

    func stop() {
        motion.stopAccelerometerUpdates()
        onStep = nil
    }
}

/// Core Motion supplies g; the peak thresholds and stride estimate use m/s².
struct StepDetector {
    private var lp: Double?
    private var prev: Double?
    private var peakArmed = false
    private var lastStepAt = 0.0
    private var amax = 0.0
    private var amin = 0.0

    mutating func sample(x: Double, y: Double, z: Double, timestamp: Double) -> Double? {
        let mag = hypot(x, hypot(y, z)) * 9.80665
        guard mag.isFinite, timestamp.isFinite else { return nil }
        let now = timestamp * 1000
        if lp == nil {
            lp = mag
            prev = mag
            amax = mag
            amin = mag
            return nil
        }
        let v = 0.25 * mag + 0.75 * (lp ?? mag)
        lp = v
        if v > amax { amax = v }
        if v < amin { amin = v }
        if let prev, v < prev, prev > 10.6 { peakArmed = true }
        if peakArmed && v < 9.4 {
            peakArmed = false
            if now - lastStepAt >= 280 {
                var amp = amax - amin
                if amp < 0 { amp = 0 }
                var length = 0.47 * pow(amp, 0.25)
                if !length.isFinite || length < 0.45 { length = 0.45 }
                if length > 0.9 { length = 0.9 }
                length = (length * 1000).rounded() / 1000
                lastStepAt = now
                amax = v
                amin = v
                prev = v
                return length
            }
        }
        prev = v
        return nil
    }
}
