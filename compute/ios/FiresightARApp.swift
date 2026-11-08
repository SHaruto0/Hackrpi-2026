//
//  FiresightARApp.swift
//  Firesight — HackRPI 2026 AR Navigation Client (iOS / SwiftUI + ARKit + RealityKit)
//
//   Drop this file into a new Xcode "Augmented Reality App" (SwiftUI + RealityKit) project,
//  add your pre-rendered MP3 files to the Xcode bundle (hazard_ahead_rerouting.mp3,
//  proceed_to_next_waypoint.mp3, arrived_at_exit.mp3, mayday_transmitting.mp3, system_online.mp3),
//  and set SERVER_WS_URL to your laptop's Wi-Fi IP address.
//

import SwiftUI
import ARKit
import RealityKit
import AVFoundation
import Combine

// MARK: - 1. JSON Models from FastAPI Backend (/ws/ar)
struct RoutePayload: Decodable {
    let start_node: String
    let start_label: String
    let target_exit: String?
    let path_ids: [String]
    let path_coords: [[Float]]
    let total_distance_m: Float?
    let reachable: Bool
}

struct ServerMessage: Decodable {
    let type: String
    let localized: Bool?
    let position: [Float]?
    let quaternion: [Float]?
    let simd_float4x4: [Float]?
    let current_node: String?
    let current_label: String?
    let route: RoutePayload?
    let audio_cue: String?
    let num_inliers: Int?
    let latency_ms: Float?
}

// MARK: - 2. WebSocket + Audio + VPS Coordinator
@MainActor
final class FiresightVPSManager: NSObject, ObservableObject {
    // Replace with your laptop's local Wi-Fi IP running `uvicorn main:app --host 0.0.0.0 --port 8000`
    @Published var serverURLString: String = "ws://192.168.1.100:8000/ws/ar"
    @Published var isConnected: Bool = false
    @Published var isLocalized: Bool = false
    @Published var currentRoomLabel: String = "Scanning Hallway..."
    @Published var targetExitLabel: String = "—"
    @Published var distanceRemaining: Float = 0.0
    @Published var inlierCount: Int = 0
    @Published var latencyMs: Float = 0.0
    @Published var isMaydayActive: Bool = false
    @Published var pathCoords: [[Float]] = []
    @Published var worldAlignmentTransform: simd_float4x4 = matrix_identity_float4x4

    private var webSocketTask: URLSessionWebSocketTask?
    private var audioPlayer: AVAudioPlayer?
    private let ciContext = CIContext(options: [.useSoftwareRenderer: false])
    private var lastFrameSentTime: TimeInterval = 0
    private let frameIntervalSeconds: TimeInterval = 1.25 // Grab ARFrame every 1.25s

    func connect() {
        disconnect()
        guard let url = URL(string: serverURLString) else { return }
        let session = URLSession(configuration: .default)
        webSocketTask = session.webSocketTask(with: url)
        webSocketTask?.resume()
        isConnected = true
        listenForMessages()
    }

    func disconnect() {
        webSocketTask?.cancel(with: .goingAway, reason: nil)
        webSocketTask = nil
        isConnected = false
    }

    private func listenForMessages() {
        webSocketTask?.receive { [weak self] result in
            guard let self = self else { return }
            Task { @MainActor in
                switch result {
                case .success(let message):
                    if case .string(let text) = message,
                       let data = text.data(using: .utf8),
                       let decoded = try? JSONDecoder().decode(ServerMessage.self, from: data) {
                        self.handleServerMessage(decoded)
                    }
                    self.listenForMessages()
                case .failure:
                    self.isConnected = false
                }
            }
        }
    }

    private func handleServerMessage(_ msg: ServerMessage) {
        if let route = msg.route {
            self.currentRoomLabel = route.start_label
            self.targetExitLabel = route.target_exit ?? "BLOCKED"
            self.distanceRemaining = route.total_distance_m ?? 0.0
            self.pathCoords = route.path_coords
        }

        if let loc = msg.localized, loc {
            self.isLocalized = true
            self.inlierCount = msg.num_inliers ?? 0
            self.latencyMs = msg.latency_ms ?? 0.0
        }

        if let cue = msg.audio_cue, !cue.isEmpty {
            playPreRenderedMP3(named: cue)
        }
    }

    /// Compress ARFrame CVPixelBuffer to 640x480 Grayscale JPEG and stream over WebSocket
    func processARFrame(_ frame: ARFrame) {
        guard isConnected else { return }
        let now = frame.timestamp
        guard now - lastFrameSentTime >= frameIntervalSeconds else { return }
        lastFrameSentTime = now

        let pixelBuffer = frame.capturedImage
        let arkitCameraTransform = frame.camera.transform // T_wc_local

        Task.detached(priority: .userInitiated) { [weak self] in
            guard let self = self else { return }
            guard let jpegData = self.compressTo640x480GrayscaleJPEG(pixelBuffer: pixelBuffer) else { return }

            await MainActor.run {
                // Send binary compressed grayscale JPEG (<25 KB per frame for campus Wi-Fi)
                self.webSocketTask?.send(.data(jpegData)) { _ in }
                // Cache local camera transform so we can align map root when server pose arrives
                _ = arkitCameraTransform
            }
        }
    }

    nonisolated private func compressTo640x480GrayscaleJPEG(pixelBuffer: CVPixelBuffer) -> Data? {
        // Rotate portrait upright and downscale to 640x480 monochrome (luma) JPEG
        let ciImage = CIImage(cvPixelBuffer: pixelBuffer).oriented(.right)
        let srcW = ciImage.extent.width
        let srcH = ciImage.extent.height
        guard srcW > 0, srcH > 0 else { return nil }

        let targetW: CGFloat = 480.0
        let targetH: CGFloat = 640.0
        let scaleX = targetW / srcW
        let scaleY = targetH / srcH

        let scaled = ciImage
            .transformed(by: CGAffineTransform(scaleX: scaleX, y: scaleY))
            .applyingFilter("CIPhotoEffectMono")

        let colorSpace = CGColorSpaceCreateDeviceGray()
        guard let cgImage = ciContext.createCGImage(scaled, from: CGRect(x: 0, y: 0, width: targetW, height: targetH), format: .L8, colorSpace: colorSpace) else {
            return nil
        }
        let uiImage = UIImage(cgImage: cgImage)
        return uiImage.jpegData(compressionQuality: 0.55)
    }

    /// Push-to-Talk Mayday transmission
    func sendMayday(active: Bool) {
        isMaydayActive = active
        let payload = "{\"type\":\"mayday\",\"active\":\(active ? "true" : "false")}"
        webSocketTask?.send(.string(payload)) { _ in }
        if active {
            playPreRenderedMP3(named: "mayday_transmitting.mp3")
        }
    }

    /// Play pre-rendered ElevenLabs / Fish Audio MP3 bundled in Xcode project
    func playPreRenderedMP3(named filename: String) {
        let cleanName = (filename as NSString).deletingPathExtension
        guard let url = Bundle.main.url(forResource: cleanName, withExtension: "mp3") else {
            AudioServicesPlaySystemSound(1005) // Fallback tactical alert chime if MP3 not bundled yet
            return
        }
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .voicePrompt, options: [.duckOthers])
            try AVAudioSession.sharedInstance().setActive(true)
            audioPlayer = try AVAudioPlayer(contentsOf: url)
            audioPlayer?.play()
        } catch {
            print("[Firesight] Audio error: \(error)")
        }
    }
}

// MARK: - 3. RealityKit ARView Container (Floor Arrows & Coordinate Alignment)
struct FiresightARViewContainer: UIViewRepresentable {
    @ObservedObject var vps: FiresightVPSManager

    func makeCoordinator() -> Coordinator {
        Coordinator(vps: vps)
    }

    func makeUIView(context: Context) -> ARView {
        let arView = ARView(frame: .zero)
        let config = ARWorldTrackingConfiguration()
        config.planeDetection = [.horizontal]
        config.worldAlignment = .gravity
        arView.session.delegate = context.coordinator
        arView.session.run(config)

        arView.scene.addAnchor(context.coordinator.mapRootAnchor)
        return arView
    }

    func updateUIView(_ uiView: ARView, context: Context) {
        context.coordinator.updateFloorArrows(pathCoords: vps.pathCoords)
    }

    final class Coordinator: NSObject, ARSessionDelegate {
        let vps: FiresightVPSManager
        let mapRootAnchor = AnchorEntity(world: matrix_identity_float4x4)
        private var lastRenderedPath: [[Float]] = []

        init(vps: FiresightVPSManager) {
            self.vps = vps
        }

        func session(_ session: ARSession, didUpdate frame: ARFrame) {
            Task { @MainActor in
                vps.processARFrame(frame)
            }
        }

        /// Render glowing neon cyan/orange navigation arrows along the floor between Dijkstra waypoints
        func updateFloorArrows(pathCoords: [[Float]]) {
            guard pathCoords != lastRenderedPath else { return }
            lastRenderedPath = pathCoords

            mapRootAnchor.children.removeAll()
            guard pathCoords.count >= 2 else { return }

            let arrowMaterial = UnlitMaterial(color: UIColor(red: 0.0, green: 0.92, blue: 1.0, alpha: 0.92))
            let nodeMaterial = UnlitMaterial(color: UIColor(red: 1.0, green: 0.42, blue: 0.15, alpha: 0.95))

            for i in 0..<(pathCoords.count - 1) {
                let p0 = SIMD3<Float>(pathCoords[i][0], pathCoords[i][1] - 1.1, pathCoords[i][2])
                let p1 = SIMD3<Float>(pathCoords[i + 1][0], pathCoords[i + 1][1] - 1.1, pathCoords[i + 1][2])
                let delta = p1 - p0
                let segLen = simd_length(delta)
                guard segLen > 0.1 else { continue }
                let dir = simd_normalize(delta)

                // Waypoint beacon disc on the floor
                let discMesh = MeshResource.generateCylinder(height: 0.02, radius: 0.22)
                let discEntity = ModelEntity(mesh: discMesh, materials: [nodeMaterial])
                discEntity.position = p0
                mapRootAnchor.addChild(discEntity)

                // Place glowing directional chevron/arrow markers every 1.2 meters along segment
                let steps = max(1, Int(segLen / 1.2))
                for s in 0..<steps {
                    let frac = (Float(s) + 0.5) / Float(steps)
                    let pos = p0 + delta * frac

                    let shaftMesh = MeshResource.generateBox(size: SIMD3<Float>(0.14, 0.025, 0.48), cornerRadius: 0.01)
                    let arrowEntity = ModelEntity(mesh: shaftMesh, materials: [arrowMaterial])
                    arrowEntity.position = pos
                    arrowEntity.look(at: p1, from: pos, relativeTo: mapRootAnchor)
                    mapRootAnchor.addChild(arrowEntity)
                }
            }
        }
    }
}

// MARK: - 4. SwiftUI Firefighter HUD & Push-to-Talk Mayday View
struct FiresightMainView: View {
    @StateObject private var vps = FiresightVPSManager()

    var body: some View {
        ZStack {
            FiresightARViewContainer(vps: vps)
                .ignoresSafeArea()

            VStack {
                // Top Tactical Visor HUD
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Circle()
                            .fill(vps.isConnected ? Color.green : Color.red)
                            .frame(width: 10, height: 10)
                        Text(vps.isConnected ? "VPS BRAIN ONLINE" : "VPS DISCONNECTED")
                            .font(.caption.bold())
                            .foregroundColor(.white)
                        Spacer()
                        Text("\(vps.inlierCount) inliers • \(Int(vps.latencyMs))ms")
                            .font(.caption2.monospaced())
                            .foregroundColor(.cyan)
                    }

                    Text(vps.currentRoomLabel.uppercased())
                        .font(.title2.weight(.heavy))
                        .foregroundColor(.orange)

                    HStack {
                        Text("EGRESS: \(vps.targetExitLabel.uppercased())")
                            .font(.subheadline.bold())
                            .foregroundColor(.cyan)
                        Spacer()
                        Text(String(format: "%.1f m", vps.distanceRemaining))
                            .font(.headline.monospaced())
                            .foregroundColor(.white)
                    }

                    HStack {
                        TextField("ws://laptop-ip:8000/ws/ar", text: $vps.serverURLString)
                            .textFieldStyle(.roundedBorder)
                            .font(.caption.monospaced())
                            .autocapitalization(.none)
                        Button(vps.isConnected ? "Reconnect" : "Connect") {
                            vps.connect()
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.orange)
                    }
                }
                .padding(14)
                .background(.ultraThinMaterial)
                .cornerRadius(16)
                .padding(.horizontal, 16)
                .padding(.top, 8)

                Spacer()

                // Bottom Push-to-Talk MAYDAY Button
                Button(action: {}) {
                    HStack(spacing: 12) {
                        Image(systemName: vps.isMaydayActive ? "waveform.circle.fill" : "mic.fill")
                            .font(.title2)
                        Text(vps.isMaydayActive ? "TRANSMITTING MAYDAY..." : "HOLD FOR MAYDAY PTT")
                            .font(.headline.weight(.black))
                    }
                    .foregroundColor(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 18)
                    .background(vps.isMaydayActive ? Color.red : Color(red: 0.75, green: 0.1, blue: 0.1))
                    .cornerRadius(18)
                    .shadow(color: .red.opacity(0.6), radius: 12)
                }
                .simultaneousGesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { _ in
                            if !vps.isMaydayActive { vps.sendMayday(active: true) }
                        }
                        .onEnded { _ in
                            vps.sendMayday(active: false)
                        }
                )
                .padding(.horizontal, 24)
                .padding(.bottom, 24)
            }
        }
        .onAppear {
            vps.connect()
        }
    }
}
