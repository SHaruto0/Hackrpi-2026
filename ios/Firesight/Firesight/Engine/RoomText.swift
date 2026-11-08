import Foundation

struct RoomMatch {
    var roomId: String
    var level: Int
    var label: String
    var points: [PlanPoint]
    var score: Double
}

/// Door-number matching. Token rules match backend/services/vision.py. OCR itself is Vision.
enum RoomText {
    static let fuzzyPenalty = 0.3
    static let exitScore = 0.5

    static func match(tokens: [(text: String, conf: Double)], model: BuildingModel) -> [RoomMatch] {
        if tokens.isEmpty { return [] }
        let cat = catalog(model)
        var found: [RoomMatch] = []
        for tok in tokens {
            let conf = min(1, max(0, tok.conf))
            let keys = tokenKeys(tok.text)
            if keys.isEmpty { continue }
            if keys.contains("EXIT") {
                for level in cat.exits.keys.sorted() {
                    let pts = cat.exits[level] ?? []
                    if pts.isEmpty { continue }
                    found.append(RoomMatch(roomId: "EXIT", level: level, label: "EXIT", points: pts, score: exitScore))
                }
            }
            var idxs = lookupExact(keys, cat.exact, cat.extra)
            var fuzzy = false
            if idxs.isEmpty {
                idxs = lookupFuzzy(keys, cat.buckets)
                fuzzy = !idxs.isEmpty
            }
            if idxs.isEmpty { continue }
            let score = fuzzy ? max(0, conf - fuzzyPenalty) : conf
            for idx in idxs {
                let room = cat.rooms[idx]
                found.append(RoomMatch(
                    roomId: room.id, level: room.level, label: room.label,
                    points: room.points, score: score
                ))
            }
        }
        return found
    }

    static func normalize(_ text: String) -> String {
        var s = text.uppercased().trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return "" }
        s = replace(s, "^F\\d+-", "")
        s = replace(s, "\\b(?:ROOM|RM)\\b", " ")
        s = replace(s, "(?:ROOM|RM)(?=\\d)", "")
        s = replace(s, "(?<=\\d)(?:ROOM|RM)", "")
        s = s.uppercased()
        var kept = ""
        for ch in s where ch.isLetter || ch.isNumber || ch == "." {
            kept.append(ch)
        }
        return oToZero(kept)
    }

    private struct CatRoom {
        var id: String
        var level: Int
        var label: String
        var points: [PlanPoint]
    }

    private struct Catalog {
        var rooms: [CatRoom]
        var exact: [String: [Int]]
        var extra: [String: [Int]]
        var buckets: [Int: [(String, Int)]]
        var exits: [Int: [PlanPoint]]
    }

    private static func catalog(_ model: BuildingModel) -> Catalog {
        var rooms: [CatRoom] = []
        var exact: [String: [Int]] = [:]
        var extra: [String: [Int]] = [:]
        var buckets: [Int: [(String, Int)]] = [:]
        var exits: [Int: [PlanPoint]] = [:]
        var doorsFor: [String: [PlanPoint]] = [:]

        func addExact(_ key: String, _ idx: Int) {
            if key.isEmpty { return }
            var lst = exact[key] ?? []
            if lst.contains(idx) { return }
            lst.append(idx)
            exact[key] = lst
            buckets[key.count, default: []].append((key, idx))
        }
        func addExtra(_ key: String, _ idx: Int) {
            if key.isEmpty || exact[key] != nil { return }
            var lst = extra[key] ?? []
            if lst.contains(idx) { return }
            lst.append(idx)
            extra[key] = lst
        }

        for fl in model.floors {
            for door in fl.doors {
                let pt = PlanPoint(x: door.x, y: door.y)
                if door.exteriorExit {
                    exits[fl.level, default: []].append(pt)
                }
                for cid in door.connects {
                    doorsFor[cid, default: []].append(pt)
                }
            }
        }
        for fl in model.floors {
            for space in fl.spaces where !space.id.isEmpty {
                var pts = dedupe(doorsFor[space.id] ?? [])
                if pts.isEmpty { pts = [centroid(space.polygon)] }
                let idx = rooms.count
                rooms.append(CatRoom(id: space.id, level: fl.level, label: space.label, points: pts))
                for key in [roomNumber(space.label), idSuffix(space.id)] {
                    addExact(key, idx)
                }
                addExtra(normalize(space.label), idx)
            }
        }
        for level in exits.keys {
            exits[level] = dedupe(exits[level] ?? [])
        }
        return Catalog(rooms: rooms, exact: exact, extra: extra, buckets: buckets, exits: exits)
    }

    private static func lookupExact(_ keys: [String], _ exact: [String: [Int]], _ extra: [String: [Int]]) -> [Int] {
        var found: [Int] = []
        var seen = Set<Int>()
        for key in keys {
            for idx in exact[key] ?? [] where !seen.contains(idx) {
                seen.insert(idx)
                found.append(idx)
            }
            for idx in extra[key] ?? [] where !seen.contains(idx) {
                seen.insert(idx)
                found.append(idx)
            }
        }
        return found
    }

    private static func lookupFuzzy(_ keys: [String], _ buckets: [Int: [(String, Int)]]) -> [Int] {
        var found: [Int] = []
        var seen = Set<Int>()
        for key in keys {
            let n = key.count
            if n < 2 { continue }
            for length in [n - 1, n, n + 1] {
                for (roomKey, idx) in buckets[length] ?? [] {
                    if seen.contains(idx) { continue }
                    if oneCharOff(key, roomKey) {
                        seen.insert(idx)
                        found.append(idx)
                    }
                }
            }
        }
        return found
    }

    private static func tokenKeys(_ text: String) -> [String] {
        let raw = text.uppercased().trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.isEmpty { return [] }
        var keys: [String] = []
        let full = normalize(raw)
        if !full.isEmpty { keys.append(full) }
        let first = raw.split(whereSeparator: \.isWhitespace).first.map(String.init) ?? raw
        if first != raw {
            let key = normalize(first)
            if !key.isEmpty && !keys.contains(key) { keys.append(key) }
        }
        return keys
    }

    private static func roomNumber(_ label: String) -> String {
        let parts = label.uppercased().split(whereSeparator: \.isWhitespace)
        guard let first = parts.first else { return "" }
        return normalize(String(first))
    }

    private static func idSuffix(_ spaceId: String) -> String {
        let text = spaceId.trimmingCharacters(in: .whitespacesAndNewlines)
        if let re = try? NSRegularExpression(pattern: "^F\\d+-(.+)$", options: [.caseInsensitive]) {
            let range = NSRange(text.startIndex..., in: text)
            if let m = re.firstMatch(in: text, options: [], range: range), m.numberOfRanges > 1,
               let r = Range(m.range(at: 1), in: text) {
                return normalize(String(text[r]))
            }
        }
        return normalize(text)
    }

    private static func oneCharOff(_ a: String, _ b: String) -> Bool {
        if a.isEmpty || b.isEmpty || a == b { return false }
        let aa = Array(a), bb = Array(b)
        var la = aa.count, lb = bb.count
        if la < 2 || lb < 2 || abs(la - lb) > 1 { return false }
        if la == lb {
            return zip(aa, bb).filter { $0 != $1 }.count == 1
        }
        var a1 = aa, b1 = bb
        if la > lb {
            swap(&a1, &b1)
            swap(&la, &lb)
        }
        var i = 0, j = 0, skipped = false
        while i < a1.count && j < b1.count {
            if a1[i] == b1[j] {
                i += 1
            } else if skipped {
                return false
            } else {
                skipped = true
            }
            j += 1
        }
        return true
    }

    private static func oToZero(_ text: String) -> String {
        var chars = Array(text)
        let n = chars.count
        var i = 0
        while i < n {
            if chars[i].isNumber || chars[i] == "O" {
                var j = i
                var hasDigit = false
                while j < n && (chars[j].isNumber || chars[j] == "O") {
                    if chars[j].isNumber { hasDigit = true }
                    j += 1
                }
                if hasDigit {
                    for k in i..<j where chars[k] == "O" { chars[k] = "0" }
                }
                i = j
            } else {
                i += 1
            }
        }
        return String(chars)
    }

    private static func dedupe(_ points: [PlanPoint]) -> [PlanPoint] {
        var out: [PlanPoint] = []
        var seen = Set<String>()
        for pt in points {
            let key = "\(round3(pt.x)),\(round3(pt.y))"
            if seen.contains(key) { continue }
            seen.insert(key)
            out.append(pt)
        }
        return out
    }

    private static func centroid(_ poly: [PlanPoint]) -> PlanPoint {
        var pts = poly
        if pts.count >= 2 && pts[0] == pts[pts.count - 1] { pts.removeLast() }
        if pts.isEmpty { return PlanPoint(x: 0, y: 0) }
        let n = Double(pts.count)
        return PlanPoint(x: pts.reduce(0) { $0 + $1.x } / n, y: pts.reduce(0) { $0 + $1.y } / n)
    }

    private static func round3(_ x: Double) -> Double { (x * 1000).rounded() / 1000 }

    private static func replace(_ text: String, _ pattern: String, _ with: String) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return text }
        let range = NSRange(text.startIndex..., in: text)
        return re.stringByReplacingMatches(in: text, range: range, withTemplate: with)
    }
}
