import AVFoundation
import Combine
import SwiftUI

/// Sensors stay active while either the floor map or camera is visible.
struct NavigateScreen: View {
    @ObservedObject var world: WorldModel
    var onExpandMap: () -> Void
    var onHome: () -> Void

    @StateObject private var camera = CameraSession()
    @State private var heading = HeadingMonitor()
    @State private var steps = StepCounter()
    @State private var speech = GuideSpeech()
    @State private var cameraView = false
    @State private var cameraOn = false
    @State private var muted = false
    @State private var active = false
    @State private var motionStatus = "Starting motion tracking..."
    @State private var lastQR = ""
    @State private var lastQRAt = Date.distantPast
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button(action: onHome) { Label("Home", systemImage: "house.fill") }
                    .accessibilityHint("Change your current location and destination")
                Spacer()
                Text("Floor \(world.pose?.level ?? 0)").fontWeight(.bold)
                Spacer()
                Button(action: onExpandMap) { Label("Expand", systemImage: "arrow.up.left.and.arrow.down.right") }
            }
            .font(.system(size: 16, weight: .bold))
            .padding(12)

            VStack(alignment: .leading, spacing: 6) {
                Text(world.actionText()).font(.system(size: 23, weight: .heavy))
                Text(world.whereLine()).font(.system(size: 15, weight: .semibold))
                HStack {
                    Text("To \(world.destinationLabel)")
                    Spacer()
                    if let meters = world.route?.distanceM {
                        Text("\(Int(meters.rounded())) m remaining")
                    }
                }
                .font(.system(size: 14))
                .foregroundStyle(PhoneColor.muted)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(world.arrived ? PhoneColor.safe : PhoneColor.card)
            .accessibilityElement(children: .combine)

            Picker("Navigation view", selection: $cameraView) {
                Text("3D floor map").tag(false)
                Text("Camera").tag(true)
            }
            .pickerStyle(.segmented)
            .padding(10)

            ZStack(alignment: .topLeading) {
                if cameraView {
                    CameraPreview(session: camera.session)
                    GuideOverlay(angle: arrowRadians, show: showArrow, unsure: unsure)
                    if !cameraOn { mapChip("Camera unavailable. Use the 3D floor map.") }
                    else if world.posMode == .sensors && world.headingDeg == nil {
                        mapChip("Waiting for compass. Use the floor map to orient yourself.")
                    }
                    VStack {
                        Spacer()
                        HStack {
                            Spacer()
                            BuildingSceneView(world: world, mode: .mini, onExpand: onExpandMap)
                                .frame(width: 132, height: 132)
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                                .overlay(RoundedRectangle(cornerRadius: 12).stroke(.white, lineWidth: 2))
                                .padding(10)
                        }
                    }
                } else {
                    BuildingSceneView(world: world, mode: .map, followsUser: true)
                    mapChip(world.posMode == .demo ? "Demo position | Blue line is your path" : "You are here | Blue line is your path")
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .clipped()
            .accessibilityLabel("Current floor map with your position and route")

            controlBar
        }
        .foregroundStyle(.white)
        .background(PhoneColor.bg)
        .onAppear(perform: startSensors)
        .onDisappear(perform: stopSensors)
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { startSensors() } else { stopSensors() }
        }
        .onReceive(Timer.publish(every: 0.1, on: .main, in: .common).autoconnect()) { _ in
            guard active else { return }
            if heading.currentHeading == nil && world.headingDeg != nil { world.updateHeading(nil) }
            if world.arrived { world.walking = false }
            else if world.walking { world.demoWalk(dt: 0.1) }
        }
        .onChange(of: world.speechLine) { _, line in
            if active && !muted { speech.say(line) }
        }
        .onChange(of: world.toast) { _, text in
            guard !text.isEmpty else { return }
            Task {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                if world.toast == text { world.toast = "" }
            }
        }
    }

    private var controlBar: some View {
        VStack(spacing: 8) {
            if !world.toast.isEmpty {
                Text(world.toast).font(.system(size: 14, weight: .bold))
            }
            Picker("Position source", selection: Binding(get: { world.posMode }, set: { world.setPosMode($0) })) {
                Text("Live walking").tag(PosMode.sensors)
                Text("Demo walk").tag(PosMode.demo)
            }
            .pickerStyle(.segmented)
            if world.posMode == .demo {
                Button(world.walking ? "Pause demo" : "Play demo") { world.toggleWalk() }
                    .buttonStyle(BarFillStyle(fill: PhoneColor.go, border: .white))
                    .disabled(world.route?.reachable != true || world.arrived)
            } else {
                Text("\(motionStatus) | \(world.pose?.nSteps ?? 0) steps")
                    .font(.system(size: 13, weight: .semibold))
                    .multilineTextAlignment(.center)
            }
            HStack(spacing: 8) {
                Button(muted ? "Voice off" : "Voice on") {
                    muted.toggle()
                    if muted { speech.stop() }
                    else { speech.say(world.actionText(), force: true) }
                }
                .buttonStyle(BarFillStyle(fill: PhoneColor.card, border: .white))
                Button("Replay") {
                    muted = false
                    speech.say(world.actionText(), force: true)
                    if AVAudioSession.sharedInstance().outputVolume == 0 {
                        world.showToast("Media volume is zero. Turn it up to hear directions.")
                    }
                }
                .buttonStyle(BarFillStyle(fill: PhoneColor.card, border: .white))
                .accessibilityLabel("Replay spoken directions")
            }
            Button("I took the stairs") { world.jumpStairs() }
                .font(.system(size: 15, weight: .bold))
                .frame(maxWidth: .infinity, minHeight: 44)
        }
        .padding(10)
        .background(PhoneColor.bg)
    }

    private var unsure: Bool { world.posMode == .sensors && world.pose?.unsure == true }
    private var showArrow: Bool {
        world.route?.reachable == true && !world.arrived && (world.posMode == .demo || world.headingDeg != nil)
    }
    private var arrowRadians: Double {
        (world.relativeHeading ?? 0) * .pi / 180
    }
    private func mapChip(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 13, weight: .bold))
            .padding(10)
            .background(PhoneColor.bg.opacity(0.9), in: RoundedRectangle(cornerRadius: 10))
            .padding(10)
            .allowsHitTesting(false)
    }

    private func startSensors() {
        guard !active else { return }
        active = true
        speech.onError = { world.showToast($0) }
        heading.onUpdate = { deg in
            guard active else { return }
            world.updateHeading(deg)
        }
        heading.start()
        steps.onStatus = { motionStatus = $0 }
        steps.start(heading: { [heading] in heading.currentHeading }) { deg, length in
            if active && world.posMode == .sensors { world.applyStep(headingDeg: deg, lengthM: length) }
        }
        camera.onQR = { raw in
            DispatchQueue.main.async {
                guard active, world.posMode == .sensors else { return }
                let code = QRCode.roomCode(from: raw)
                let now = Date()
                if code == lastQR && now.timeIntervalSince(lastQRAt) < 10 { return }
                lastQR = code
                lastQRAt = now
                world.snap(toQR: raw)
            }
        }
        camera.onFrame = { image in
            Task {
                let hits = await TextScanner.recognize(image)
                await MainActor.run {
                    if active && world.posMode == .sensors { world.observe(readings: hits) }
                }
            }
        }
        AVCaptureDevice.requestAccess(for: .video) { ok in
            DispatchQueue.main.async {
                guard active else { return }
                cameraOn = ok
                if ok { camera.start() }
            }
        }
        if !muted { speech.say(world.actionText(), force: true) }
    }

    private func stopSensors() {
        active = false
        world.walking = false
        camera.onQR = nil
        camera.onFrame = nil
        camera.stop()
        heading.stop()
        steps.stop()
        world.updateHeading(nil)
        speech.stop()
    }
}

private struct BarFillStyle: ButtonStyle {
    var fill: Color
    var border: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 16, weight: .heavy))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 52)
            .background(fill)
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(border, lineWidth: 2))
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

/// Arrow and chevron ribbon. Geometry matches `drawArrow` and `drawRibbon` in phone.html.
struct GuideOverlay: View {
    var angle: Double
    var show: Bool
    var unsure: Bool

    var body: some View {
        Canvas { ctx, size in
            let w = size.width
            let h = size.height
            ctx.fill(
                Path(CGRect(x: 0, y: h * 0.58, width: w, height: h * 0.42)),
                with: .linearGradient(
                    Gradient(stops: [
                        .init(color: .clear, location: 0),
                        .init(color: Color.black.opacity(0.28), location: 1)
                    ]),
                    startPoint: CGPoint(x: 0, y: h * 0.58),
                    endPoint: CGPoint(x: 0, y: h)
                )
            )
            guard show else { return }
            drawRibbon(ctx, w: w, h: h)
            drawArrow(ctx, w: w, h: h)
        }
        .allowsHitTesting(false)
    }

    private func drawRibbon(_ ctx: GraphicsContext, w: CGFloat, h: CGFloat) {
        let depth = min(h * 0.30, 240)
        let n = 6
        var base = ctx
        base.translateBy(x: w / 2, y: h * 0.88)
        base.rotate(by: .radians(angle))
        for i in stride(from: n - 1, through: 0, by: -1) {
            let dist = (CGFloat(i) + 0.15) / CGFloat(n)
            let scale = 1.2 - dist * 0.85
            var mark = base
            mark.translateBy(x: 0, y: -dist * depth)
            mark.scaleBy(x: scale, y: scale * (0.48 + (1 - dist) * 0.4))
            mark.opacity = 0.4 + (1 - dist) * 0.55
            mark.fill(chevron, with: .color(Color(rgb: 0xF4FBFF)))
            mark.stroke(chevron, with: .color(Color.black.opacity(0.75)), lineWidth: 4)
        }
    }

    private func drawArrow(_ ctx: GraphicsContext, w: CGFloat, h: CGFloat) {
        let cy = h * 0.44
        var g = ctx
        g.translateBy(x: w / 2, y: cy)
        g.rotate(by: .radians(angle))
        if unsure { g.opacity = 0.38 }
        g.fill(arrow, with: .color(.white))
        g.stroke(arrow, with: .color(Color.black.opacity(0.8)), lineWidth: 8)
    }

    private var chevron: Path {
        var p = Path()
        p.move(to: CGPoint(x: 0, y: -16))
        p.addLine(to: CGPoint(x: 22, y: 12))
        p.addLine(to: CGPoint(x: 9, y: 12))
        p.addLine(to: CGPoint(x: 0, y: 1))
        p.addLine(to: CGPoint(x: -9, y: 12))
        p.addLine(to: CGPoint(x: -22, y: 12))
        p.closeSubpath()
        return p
    }

    private var arrow: Path {
        var p = Path()
        p.move(to: CGPoint(x: 0, y: -78))
        p.addLine(to: CGPoint(x: 36, y: 8))
        p.addLine(to: CGPoint(x: 15, y: 8))
        p.addLine(to: CGPoint(x: 15, y: 64))
        p.addLine(to: CGPoint(x: -15, y: 64))
        p.addLine(to: CGPoint(x: -15, y: 8))
        p.addLine(to: CGPoint(x: -36, y: 8))
        p.closeSubpath()
        return p
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.preview.session = session
        view.preview.videoGravity = .resizeAspectFill
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {
        if uiView.preview.session !== session {
            uiView.preview.session = session
        }
    }
}

final class PreviewView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
    var preview: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
}
