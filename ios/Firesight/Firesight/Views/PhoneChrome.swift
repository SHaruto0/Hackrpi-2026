import SwiftUI

enum PhoneColor {
    static let bg = Color(rgb: 0x0E1116)
    static let ink = Color.white
    static let muted = Color(rgb: 0xD5DDE8)
    static let go = Color(rgb: 0x1F6FEB)
    static let warn = Color(rgb: 0xF0C14A)
    static let warnInk = Color(rgb: 0x1A1200)
    static let bad = Color(rgb: 0xC41515)
    static let safe = Color(rgb: 0x137333)
    static let trap = Color(rgb: 0x9D1C1C)
    static let eyebrow = Color(rgb: 0x9FD6FF)
    static let card = Color(rgb: 0x161B22)
    static let line = Color(rgb: 0x3D4654)
}

extension Color {
    init(rgb: UInt32, alpha: Double = 1) {
        self.init(
            .sRGB,
            red: Double((rgb >> 16) & 0xFF) / 255,
            green: Double((rgb >> 8) & 0xFF) / 255,
            blue: Double(rgb & 0xFF) / 255,
            opacity: alpha
        )
    }
}

struct RoomChoice: Identifiable {
    var id: String
    var label: String
    var level: Int
}

enum RoomList {
    static func all(_ model: BuildingModel) -> [RoomChoice] {
        var rows: [RoomChoice] = []
        for floor in model.floors {
            for space in floor.spaces {
                let label = space.label.isEmpty ? space.id : space.label
                rows.append(RoomChoice(id: space.id, label: label, level: floor.level))
            }
        }
        rows.sort {
            if $0.level != $1.level { return $0.level < $1.level }
            return $0.label.localizedStandardCompare($1.label) == .orderedAscending
        }
        return rows
    }
}

/// "Where are you?" and "Where to?" lists. Same layout as viewer/phone.html `#pick`.
struct RoomPickScreen: View {
    let title: String
    let rooms: [RoomChoice]
    var message: String?
    var onBack: (() -> Void)? = nil
    let onPick: (RoomChoice) -> Void

    @State private var query = ""

    private var filtered: [RoomChoice] {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if q.isEmpty { return rooms }
        return rooms.filter { row in
            "\(row.label) \(row.id) \(row.level)".lowercased().contains(q)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                Text("FIRESIGHT")
                    .font(.system(size: 13, weight: .heavy))
                    .tracking(1.2)
                    .foregroundStyle(PhoneColor.eyebrow)
                    .padding(.bottom, 6)
                Text(title)
                    .font(.system(size: 34, weight: .bold))
                    .foregroundStyle(PhoneColor.ink)
                    .padding(.bottom, 8)
                TextField("Search rooms", text: $query)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .padding(.horizontal, 14)
                    .frame(minHeight: 52)
                    .background(Color(rgb: 0x0B0E13))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white, lineWidth: 2))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                if let message, !message.isEmpty {
                    Text(message)
                        .font(.system(size: 18, weight: .heavy))
                        .foregroundStyle(PhoneColor.warnInk)
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(PhoneColor.warn)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .padding(.top, 12)
                }
                if let onBack {
                    Button("Back", action: onBack)
                        .buttonStyle(GhostButtonStyle())
                        .padding(.top, 10)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 18)
            .padding(.bottom, 8)

            ScrollView {
                LazyVStack(spacing: 8) {
                    if filtered.isEmpty {
                        Text(rooms.isEmpty ? "Loading rooms..." : "No rooms match.")
                            .font(.system(size: 18, weight: .bold))
                            .foregroundStyle(PhoneColor.muted)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 8)
                    } else {
                        ForEach(filtered) { room in
                            Button {
                                onPick(room)
                            } label: {
                                HStack {
                                    Text(room.label)
                                        .font(.system(size: 20, weight: .bold))
                                        .foregroundStyle(PhoneColor.ink)
                                    Spacer()
                                    Text("Floor \(room.level)")
                                        .font(.system(size: 16, weight: .bold))
                                        .foregroundStyle(PhoneColor.muted)
                                }
                                .padding(.horizontal, 14)
                                .frame(minHeight: 64)
                                .background(PhoneColor.card)
                                .overlay(RoundedRectangle(cornerRadius: 12).stroke(PhoneColor.line, lineWidth: 2))
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 24)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(PhoneColor.bg)
    }
}

/// Confirm screen. Copy follows viewer/phone.html `#ready`.
struct ReadyScreen: View {
    let floor: Int
    let label: String
    let destination: String
    let canStart: Bool
    let onStart: () -> Void
    let onBack: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("FIRESIGHT")
                .font(.system(size: 13, weight: .heavy))
                .tracking(1.2)
                .foregroundStyle(PhoneColor.eyebrow)
                .padding(.bottom, 6)
            Text("Floor \(floor)")
                .font(.system(size: 18, weight: .heavy))
                .foregroundStyle(PhoneColor.eyebrow)
                .padding(.bottom, 4)
            Text(label)
                .font(.system(size: 34, weight: .bold))
                .foregroundStyle(PhoneColor.ink)
                .padding(.bottom, 8)
            Text("To \(destination). Point your camera down the hall. The arrow shows the way.")
                .font(.system(size: 18))
                .foregroundStyle(PhoneColor.muted)
                .padding(.bottom, 18)
            Button("Start", action: onStart)
                .buttonStyle(GoButtonStyle())
                .disabled(!canStart)
                .opacity(canStart ? 1 : 0.45)
            Button("Choose another room", action: onBack)
                .buttonStyle(GhostButtonStyle())
                .padding(.top, 10)
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.top, 18)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(PhoneColor.bg)
    }
}

struct GoButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 24, weight: .heavy))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 68)
            .background(PhoneColor.go)
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct GhostButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 18, weight: .bold))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, minHeight: 52)
            .background(Color.clear)
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white, lineWidth: 2))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}
