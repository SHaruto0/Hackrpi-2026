import CoreMotion
import Foundation

/// Peak step detector from viewer/pdr.js. Length is Weinberg, clamped to 0.45...0.9 m.
final class StepCounter {
    private let motion = CMMotionManager()
    private var onStep: ((Double, Double) -> Void)?
    private var heading: () -> Double = { 0 }
    private var lp: Double?
    private var prev: Double?
    private var peakArmed = false
    private var lastStepAt = 0.0
    private var amax = 0.0
    private var amin = 0.0

    func start(heading: @escaping () -> Double, onStep: @escaping (Double, Double) -> Void) {
        self.heading = heading
        self.onStep = onStep
        guard motion.isAccelerometerAvailable else { return }
        motion.accelerometerUpdateInterval = 1.0 / 50.0
        motion.startAccelerometerUpdates(to: OperationQueue()) { [weak self] data, _ in
            guard let self, let data else { return }
            let a = data.acceleration
            let mag = hypot(a.x, hypot(a.y, a.z))
            if mag.isFinite { self.detect(mag, now: data.timestamp * 1000) }
        }
    }

    func stop() {
        motion.stopAccelerometerUpdates()
        onStep = nil
    }

    private func detect(_ mag: Double, now: Double) {
        if lp == nil {
            lp = mag
            prev = mag
            amax = mag
            amin = mag
            return
        }
        let v = 0.25 * mag + 0.75 * (lp ?? mag)
        lp = v
        if v > amax { amax = v }
        if v < amin { amin = v }
        if let prev, v < prev, prev > 10.6 { peakArmed = true }
        if peakArmed && v < 9.4 {
            peakArmed = false
            if now - lastStepAt >= 280 {
                var h = heading()
                if !h.isFinite { h = 0 }
                h = (h * 10).rounded() / 10
                var amp = amax - amin
                if amp < 0 { amp = 0 }
                var length = 0.47 * pow(amp, 0.25)
                if !length.isFinite || length < 0.45 { length = 0.45 }
                if length > 0.9 { length = 0.9 }
                length = (length * 1000).rounded() / 1000
                lastStepAt = now
                amax = v
                amin = v
                onStep?(wrap360(h), length)
            }
        }
        prev = v
    }
}
