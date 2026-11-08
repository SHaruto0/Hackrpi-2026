import SwiftUI

/// A cutaway of the user's current floor. Position changes require an explicit fix.
struct MapScreen: View {
    @ObservedObject var world: WorldModel
    var onClose: () -> Void
    var onHome: () -> Void
    @State private var followsUser = true

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button(action: onHome) { Label("Home", systemImage: "house.fill") }
                Spacer()
                Text("Floor \(world.pose?.level ?? 0)").fontWeight(.heavy)
                Spacer()
                Button("Close", action: onClose)
            }
            .font(.system(size: 17, weight: .bold))
            .padding(16)
            BuildingSceneView(world: world, mode: .map, followsUser: followsUser)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            VStack(spacing: 10) {
                Text(world.whereLine()).font(.headline)
                Text(world.actionText()).font(.subheadline)
                Text("You are here | Blue path | Gold destination")
                    .font(.caption)
                    .foregroundStyle(PhoneColor.muted)
                Button(followsUser ? "Explore floor" : "Recenter on me") { followsUser.toggle() }
                    .buttonStyle(GhostButtonStyle())
                Text(followsUser ? "Map follows you as you walk." : "Drag to rotate. Pinch to zoom.")
                    .font(.caption)
            }
            .padding(12)
        }
        .foregroundStyle(.white)
        .background(PhoneColor.bg)
        .onChange(of: world.pose?.level) { _, _ in followsUser = true }
    }
}
