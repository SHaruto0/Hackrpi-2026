import CoreLocation
import Foundation
import Vision

enum TextScanner {
    /// Read door numbers. Confidence is 0...1, the same range RapidOCR conf uses.
    static func recognize(_ image: CGImage) async -> [(text: String, conf: Double)] {
        await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let request = VNRecognizeTextRequest()
                request.recognitionLevel = .accurate
                request.usesLanguageCorrection = false
                let handler = VNImageRequestHandler(cgImage: image, options: [:])
                do {
                    try handler.perform([request])
                    let obs = (request.results as? [VNRecognizedTextObservation]) ?? []
                    let hits: [(String, Double)] = obs.compactMap { ob in
                        guard let c = ob.topCandidates(1).first else { return nil }
                        let text = c.string.trimmingCharacters(in: .whitespacesAndNewlines)
                        if text.isEmpty { return nil }
                        return (text, Double(c.confidence))
                    }
                    cont.resume(returning: hits)
                } catch {
                    cont.resume(returning: [])
                }
            }
        }
    }
}

/// Compass heading, smoothed the same way as the phone page. iOS needs location permission for heading.
final class HeadingMonitor: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private(set) var heading: Double?
    private var updatedAt = Date.distantPast
    var currentHeading: Double? {
        Date().timeIntervalSince(updatedAt) < 5 ? heading : nil
    }
    var onUpdate: ((Double) -> Void)?

    func start() {
        heading = nil
        updatedAt = .distantPast
        manager.delegate = self
        manager.headingFilter = 1
        manager.requestWhenInUseAuthorization()
        if CLLocationManager.headingAvailable() {
            manager.startUpdatingHeading()
        }
    }

    func stop() {
        manager.stopUpdatingHeading()
        heading = nil
        updatedAt = .distantPast
    }

    func locationManager(_ manager: CLLocationManager, didUpdateHeading newHeading: CLHeading) {
        let raw = newHeading.trueHeading >= 0 ? newHeading.trueHeading : newHeading.magneticHeading
        guard newHeading.headingAccuracy >= 0, newHeading.headingAccuracy <= 45, raw >= 0 else {
            heading = nil
            return
        }
        updatedAt = Date()
        smooth(raw)
        if let heading { onUpdate?(heading) }
    }

    func locationManagerShouldDisplayHeadingCalibration(_ manager: CLLocationManager) -> Bool { true }

    private func smooth(_ deg: Double) {
        let value = wrap360(deg)
        guard let current = heading else {
            heading = value
            return
        }
        let delta = pyMod(value - current + 180, 360) - 180
        heading = wrap360(current + 0.3 * delta)
    }
}
