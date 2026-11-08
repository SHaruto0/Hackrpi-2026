import SwiftUI

/// Full building. Tap a room to set where you are, the same job as phone.html `#mapFull`.
/// Hold a room to start the fire. Step and Play spread it so the route can flip.
struct MapScreen: View {
    @ObservedObject var world: WorldModel
    var onClose: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            BuildingSceneView(world: world, mode: .map, placesYou: true)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            VStack(spacing: 10) {
                HStack(spacing: 8) {
                    mapButton(world.playing ? "Pause" : "Play") { world.setPlaying(!world.playing) }
                    mapButton("Step") { world.stepFire(1) }
                    mapButton("Reset") { world.resetFire() }
                }
                HStack(spacing: 12) {
                    Text(caption)
                        .font(.system(size: 18, weight: .heavy))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button("Close", action: onClose)
                        .font(.system(size: 18, weight: .heavy))
                        .foregroundStyle(.white)
                        .frame(minWidth: 120, minHeight: 52)
                        .background(PhoneColor.card)
                        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white, lineWidth: 2))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                }
            }
            .padding(12)
            .background(PhoneColor.bg)
        }
        .background(PhoneColor.bg)
    }

    private var caption: String {
        if world.fireTime > 0 {
            return "Fire t=\(world.fireTime). Tap where you are."
        }
        return "Tap where you are. Hold a room to start a fire."
    }

    private func mapButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .font(.system(size: 16, weight: .heavy))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(Color(rgb: 0x0A0C10, alpha: 0.9))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white, lineWidth: 2))
            .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}
