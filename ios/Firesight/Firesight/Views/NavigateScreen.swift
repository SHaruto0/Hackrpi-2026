import AVFoundation
import Combine
import SwiftUI

/// Live camera screen. Layout and copy follow viewer/phone.html `#live`.
struct NavigateScreen: View {
    @ObservedObject var world: WorldModel
    var onExpandMap: () -> Void

    @StateObject private var camera = CameraSession()
    @State private var heading = HeadingMonitor()
    @State private var steps = StepCounter()
    @State private var speech = GuideSpeech()
    @State private var phoneHeading: Double?
    @State private var compassOn = false
    @State private var cameraOn = false
    @State private var muted = false
    @State private var flash = ""
    @State private var flashOn = false
    @State private var lastQR = ""
    @State private var lastQRAt = Date.distantPast
    @State private var lastSig = ""

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .bottom) {
                CameraPreview(session: camera.session)
                    .ignoresSafeArea()
                GuideOverlay(
                    angle: arrowRadians,
                    show: showArrow,
                    unsure: unsure
                )
                .ignoresSafeArea()
                VStack(alignment: .leading, spacing: 0) {
                    banner
                    if !world.whereLine().isEmpty {
                        Text(world.whereLine())
                            .font(.system(size: 15, weight: .heavy))
                            .foregroundStyle(.white)
                            .shadow(color: .black, radius: 2, y: 1)
                            .padding(.top, 8)
                            .padding(.horizontal, 12)
                    }
                    if !compassOn {
                        chip("Compass off")
                    }
                    if !cameraOn {
                        chip("Camera off")
                    }
                    if unsure {
                        Text("Not sure where you are. Point the camera at a door number.")
                            .font(.system(size: 15, weight: .bold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(Color(rgb: 0x0E1116, alpha: 0.9))
                            .overlay(RoundedRectangle(cornerRadius: 999).stroke(Color.white.opacity(0.8), lineWidth: 2))
                            .clipShape(RoundedRectangle(cornerRadius: 999))
                            .padding(.top, 8)
                            .padding(.horizontal, 12)
                    }
                    if !world.seenText.isEmpty {
                        chip("Door \(world.seenText)")
                    }
                    Spacer()
                }
                if flashOn {
                    Text(flash)
                        .font(.system(size: 28, weight: .heavy))
                        .foregroundStyle(Color(rgb: 0x111111))
                        .padding(.horizontal, 18)
                        .padding(.vertical, 14)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 14))
                        .position(x: geo.size.width / 2, y: geo.size.height * 0.46)
                        .allowsHitTesting(false)
                }
                if world.arrived {
                    VStack(spacing: 6) {
                        Text("You are at \(world.destinationLabel).")
                            .font(.system(size: 34, weight: .bold))
                        Text("You made it.")
                            .font(.system(size: 18, weight: .bold))
                    }
                    .foregroundStyle(.white)
                    .padding(18)
                    .frame(maxWidth: .infinity)
                    .background(PhoneColor.safe)
                    .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.white, lineWidth: 2))
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .padding(.horizontal, 16)
                    .position(x: geo.size.width / 2, y: geo.size.height * 0.42)
                    .allowsHitTesting(false)
                }
                if !world.toast.isEmpty {
                    Text(world.toast)
                        .font(.system(size: 18, weight: .heavy))
                        .foregroundStyle(Color(rgb: 0x111111))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 12)
                        .frame(maxWidth: .infinity)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .padding(.horizontal, 12)
                        .position(x: geo.size.width / 2, y: geo.size.height - 300)
                        .allowsHitTesting(false)
                }
                BuildingSceneView(world: world, mode: .mini, onExpand: onExpandMap)
                    .frame(width: geo.size.width * 0.40, height: min(geo.size.width * 0.40, geo.size.height * 0.30))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white, lineWidth: 2))
                    .position(x: geo.size.width - 10 - geo.size.width * 0.20, y: geo.size.height - 264 - min(geo.size.width * 0.40, geo.size.height * 0.30) / 2)
                    .accessibilityLabel("Floor map. Tap to expand.")
                controlBar
            }
        }
        .background(Color(rgb: 0x05070A))
        .onAppear(perform: startSensors)
        .onDisappear(perform: stopSensors)
        .onReceive(Timer.publish(every: 0.1, on: .main, in: .common).autoconnect()) { _ in
            if world.arrived {
                world.walking = false
            } else if world.walking {
                world.demoWalk(dt: 0.1)
            }
        }
        .onChange(of: routeSig) { _, sig in
            if !lastSig.isEmpty && sig != lastSig {
                showFlash("Route changed")
                if !muted { speech.say("Route changed") }
            }
            lastSig = sig
        }
        .onChange(of: world.speechLine) { _, line in
            if !muted { speech.say(line) }
        }
        .onChange(of: world.toast) { _, text in
            guard !text.isEmpty else { return }
            Task {
                try? await Task.sleep(nanoseconds: 2_200_000_000)
                if world.toast == text { world.toast = "" }
            }
        }
    }

    private var banner: some View {
        let hazard = world.hazardText()
        return VStack(alignment: .leading, spacing: 4) {
            if !hazard.isEmpty {
                Text(hazard)
                    .font(.system(size: 24, weight: .heavy))
            }
            Text(world.actionText())
                .font(.system(size: 24, weight: .heavy))
            if !world.destinationLabel.isEmpty {
                Text(world.destinationLabel)
                    .font(.system(size: 16, weight: .bold))
                    .foregroundStyle(Color(rgb: 0xE8EEF5))
                    .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(hazard.isEmpty ? Color(rgb: 0x080A0E, alpha: 0.88) : PhoneColor.bad)
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(hazard.isEmpty ? Color.white.opacity(0.85) : Color(rgb: 0xFFD2D2), lineWidth: 2)
        )
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .padding(.top, 10)
        .padding(.horizontal, 12)
        .accessibilityElement(children: .combine)
    }

    private var controlBar: some View {
        VStack(spacing: 8) {
            HStack(spacing: 0) {
                segButton("Sensors", on: world.posMode == .sensors) {
                    world.setPosMode(.sensors)
                }
                segButton("Demo walk", on: world.posMode == .demo) {
                    world.setPosMode(.demo)
                }
            }
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white, lineWidth: 2))
            .clipShape(RoundedRectangle(cornerRadius: 12))
            HStack(spacing: 8) {
                barButton(world.walking ? "Walking" : "Walk", filled: world.walking) {
                    world.toggleWalk()
                }
                barButton(muted ? "Muted" : "Mute", filled: muted, fill: Color(rgb: 0x3A4250)) {
                    muted.toggle()
                    if muted { speech.stop() }
                }
            }
            barButton("I took the stairs", filled: false) {
                world.jumpStairs()
            }
            HStack(spacing: 8) {
                Button("I'm safe") { world.mark("safe") }
                    .buttonStyle(BarFillStyle(fill: PhoneColor.safe, border: Color(rgb: 0x8EEDB0)))
                Button("I'm trapped") { world.mark("trapped") }
                    .buttonStyle(BarFillStyle(fill: PhoneColor.trap, border: Color(rgb: 0xFFB4B4)))
            }
        }
        .padding(.horizontal, 10)
        .padding(.top, 28)
        .padding(.bottom, 10)
        .background(
            LinearGradient(
                colors: [.clear, Color.black.opacity(0.82)],
                startPoint: .top,
                endPoint: UnitPoint(x: 0.5, y: 0.28)
            )
        )
        .frame(maxWidth: .infinity, alignment: .bottom)
    }

    private var showArrow: Bool {
        guard let route = world.route, route.reachable, !world.arrived else { return false }
        return true
    }

    private var unsure: Bool {
        world.posMode == .sensors && (world.pose?.unsure == true)
    }

    private var arrowRadians: Double {
        guard let hints = world.hints else { return 0 }
        let deg: Double
        if compassOn, let phoneHeading {
            deg = hints.headingCompassDeg - phoneHeading
        } else {
            deg = hints.headingPlanDeg
        }
        return deg * .pi / 180
    }

    private var routeSig: String {
        guard let first = world.route?.path.first else { return world.destinationId ?? "" }
        return "\(world.destinationId ?? "")|\(first.level),\(first.x),\(first.y)"
    }

    private func chip(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 14, weight: .heavy))
            .foregroundStyle(.white)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Color.black.opacity(0.78))
            .overlay(Capsule().stroke(Color.white, lineWidth: 2))
            .clipShape(Capsule())
            .padding(.top, 8)
            .padding(.leading, 12)
    }

    private func segButton(_ title: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .font(.system(size: 15, weight: .heavy))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(on ? PhoneColor.go : Color.clear)
    }

    private func barButton(_ title: String, filled: Bool, fill: Color = PhoneColor.go, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(BarFillStyle(fill: filled ? fill : Color(rgb: 0x0A0C10, alpha: 0.9), border: .white))
    }

    private func showFlash(_ text: String) {
        flash = text
        flashOn = true
        Task {
            try? await Task.sleep(nanoseconds: 2_200_000_000)
            if flash == text { flashOn = false }
        }
    }

    private func startSensors() {
        heading.onUpdate = { deg in
            DispatchQueue.main.async {
                phoneHeading = deg
                compassOn = true
            }
        }
        heading.start()
        steps.start(heading: { [heading] in heading.heading ?? 0 }) { deg, length in
            DispatchQueue.main.async {
                if world.posMode == .sensors {
                    world.applyStep(headingDeg: deg, lengthM: length)
                }
            }
        }
        camera.onQR = { raw in
            DispatchQueue.main.async {
                let code = QRCode.roomCode(from: raw)
                let now = Date()
                if code == lastQR && now.timeIntervalSince(lastQRAt) < 5 { return }
                lastQR = code
                lastQRAt = now
                world.snap(toQR: raw)
            }
        }
        camera.onFrame = { image in
            Task {
                let hits = await TextScanner.recognize(image)
                await MainActor.run {
                    if world.posMode == .sensors {
                        world.observe(readings: hits)
                    }
                }
            }
        }
        AVCaptureDevice.requestAccess(for: .video) { ok in
            DispatchQueue.main.async {
                cameraOn = ok
                if ok { camera.start() }
            }
        }
        if !muted, !world.speechLine.isEmpty {
            speech.say(world.speechLine)
        }
        lastSig = routeSig
    }

    private func stopSensors() {
        camera.stop()
        heading.stop()
        steps.stop()
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
        base.translateBy(x: w / 2, y: h - 270)
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
        let cy = max(176, min(h * 0.40, h - 332))
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
