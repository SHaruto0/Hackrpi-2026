import SwiftUI

@main
struct FiresightApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}

private final class AppModel: ObservableObject {
    @Published var world: WorldModel?
    @Published var failed = false

    func load() async {
        if world != nil || failed { return }
        guard let data = BuildingFile.dcc() else {
            failed = true
            return
        }
        let cache = BuildingFile.cacheDir()
        let box: WorldBox? = await Task.detached(priority: .userInitiated) {
            guard let made = try? WorldLoader.make(json: data, cacheDir: cache) else { return nil }
            return WorldBox(made)
        }.value
        if let box {
            world = box.world
        } else {
            failed = true
        }
    }
}

enum BuildingFile {
    static func dcc() -> Data? {
        let bundle = Bundle.main
        let url = bundle.url(forResource: "dcc", withExtension: "json")
            ?? bundle.url(forResource: "dcc", withExtension: "json", subdirectory: "Resources")
        guard let url else { return nil }
        return try? Data(contentsOf: url)
    }

    static func cacheDir() -> URL? {
        guard let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let dir = base.appendingPathComponent("firesight", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }
}

struct RootView: View {
    @StateObject private var app = AppModel()
    @State private var screen: PhoneStep = .whereYou
    @State private var mapOpen = false
    @State private var locationError: String?

    private var rooms: [RoomChoice] {
        guard let world = app.world else { return [] }
        return RoomList.all(world.model)
    }

    var body: some View {
        ZStack {
            PhoneColor.bg.ignoresSafeArea()
            if let world = app.world {
                flow(world)
                if mapOpen {
                    MapScreen(world: world, onClose: { mapOpen = false }, onHome: { goHome(world) })
                }
            } else if app.failed {
                Text("Could not read the building map.")
                    .font(.system(size: 20, weight: .bold))
                    .foregroundStyle(.white)
                    .padding()
            } else {
                ProgressView("Loading the building...")
                    .tint(.white)
                    .foregroundStyle(.white)
            }
        }
        .preferredColorScheme(.dark)
        .task { await app.load() }
    }

    @ViewBuilder
    private func flow(_ world: WorldModel) -> some View {
        switch screen {
        case .whereYou:
            RoomPickScreen(title: "Where are you?", rooms: rooms, message: locationError) { room in
                if world.setStart(roomId: room.id) {
                    locationError = nil
                    screen = .whereTo
                } else {
                    locationError = world.status
                }
            }
        case .whereTo:
            RoomPickScreen(title: "Where to?", rooms: rooms, message: nil, onBack: { screen = .whereYou }) { room in
                world.setDestination(room.id)
                screen = .ready
            }
        case .ready:
            ReadyScreen(
                world: world,
                onStart: { screen = .live },
                onBack: { screen = .whereYou }
            )
        case .live:
            NavigateScreen(world: world, onExpandMap: { mapOpen = true }, onHome: { goHome(world) })
        }
    }

    private func goHome(_ world: WorldModel) {
        mapOpen = false
        locationError = nil
        world.endNavigation()
        screen = .whereYou
    }
}

private enum PhoneStep {
    case whereYou
    case whereTo
    case ready
    case live
}
