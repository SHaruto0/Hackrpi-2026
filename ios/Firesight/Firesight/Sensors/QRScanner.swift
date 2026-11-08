import AVFoundation
import Combine
import CoreImage
import CoreMedia
import CoreVideo
import Foundation

enum QRCode {
    /// Printed cards encode `.../phone.html?qr={room id}`. A bare room id is accepted too.
    static func roomCode(from raw: String) -> String {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let items = URLComponents(string: text)?.queryItems,
           let value = items.first(where: { $0.name == "qr" })?.value,
           !value.isEmpty {
            return value
        }
        return text
    }
}

final class CameraSession: NSObject, ObservableObject, AVCaptureVideoDataOutputSampleBufferDelegate, AVCaptureMetadataOutputObjectsDelegate {
    let session = AVCaptureSession()
    var onQR: ((String) -> Void)?
    var onFrame: ((CGImage) -> Void)?
    private let queue = DispatchQueue(label: "firesight.camera")
    private var configured = false
    private var lastFrame = Date.distantPast
    private let ciContext = CIContext()

    func start() {
        queue.async { [weak self] in
            guard let self else { return }
            self.configure()
            if !self.session.isRunning { self.session.startRunning() }
        }
    }

    func stop() {
        queue.async { [weak self] in
            guard let self, self.session.isRunning else { return }
            self.session.stopRunning()
        }
    }

    private func configure() {
        if configured { return }
        session.beginConfiguration()
        // This video-only session must not replace the spoken guidance audio session.
        session.automaticallyConfiguresApplicationAudioSession = false
        session.sessionPreset = .high
        defer { session.commitConfiguration() }
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else { return }
        session.addInput(input)
        let video = AVCaptureVideoDataOutput()
        video.alwaysDiscardsLateVideoFrames = true
        video.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        video.setSampleBufferDelegate(self, queue: queue)
        if session.canAddOutput(video) { session.addOutput(video) }
        if let connection = video.connection(with: .video), connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        }
        let meta = AVCaptureMetadataOutput()
        if session.canAddOutput(meta) {
            session.addOutput(meta)
            meta.setMetadataObjectsDelegate(self, queue: queue)
            if meta.availableMetadataObjectTypes.contains(.qr) {
                meta.metadataObjectTypes = [.qr]
            }
        }
        configured = true
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        let now = Date()
        if now.timeIntervalSince(lastFrame) < 2 { return }
        lastFrame = now
        guard let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let image = CIImage(cvPixelBuffer: buffer)
        guard let cg = ciContext.createCGImage(image, from: image.extent) else { return }
        onFrame?(cg)
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard let code = metadataObjects.compactMap({ $0 as? AVMetadataMachineReadableCodeObject }).first,
              let text = code.stringValue, !text.isEmpty else { return }
        onQR?(text)
    }
}
