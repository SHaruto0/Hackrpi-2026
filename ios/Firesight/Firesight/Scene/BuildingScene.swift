import SceneKit
import SwiftUI
import UIKit

enum SceneMode {
    case map
    case mini
}

struct BuildingSceneView: UIViewRepresentable {
    @ObservedObject var world: WorldModel
    var mode: SceneMode
    var placesYou = false
    var onExpand: () -> Void = {}

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView()
        view.scene = context.coordinator.scene
        view.backgroundColor = UIColor(rgb: 0x2A313C)
        view.antialiasingMode = .multisampling2X
        view.autoenablesDefaultLighting = true
        view.allowsCameraControl = mode == .map
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped(_:)))
        let hold = UILongPressGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.held(_:)))
        hold.minimumPressDuration = 0.45
        view.addGestureRecognizer(tap)
        view.addGestureRecognizer(hold)
        context.coordinator.view = view
        context.coordinator.buildIfNeeded()
        return view
    }

    func updateUIView(_ uiView: SCNView, context: Context) {
        context.coordinator.world = world
        context.coordinator.mode = mode
        context.coordinator.placesYou = placesYou
        context.coordinator.onExpand = onExpand
        uiView.allowsCameraControl = mode == .map
        context.coordinator.sync()
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(world: world, mode: mode, onExpand: onExpand)
    }

    final class Coordinator: NSObject {
        var world: WorldModel
        var mode: SceneMode
        var placesYou = false
        var onExpand: () -> Void
        let scene = SCNScene()
        weak var view: SCNView?
        private var built = false
        private var floorNodes: [Int: SCNNode] = [:]
        private var roomNodes: [String: SCNNode] = [:]
        private var hazardNodes: [Int: SCNNode] = [:]
        private let routeNode = SCNNode()
        private let dot = SCNNode(geometry: SCNSphere(radius: 0.45))
        private var routeSig = ""
        private var hazardSeen = -1
        private let camera = SCNNode()

        init(world: WorldModel, mode: SceneMode, onExpand: @escaping () -> Void) {
            self.world = world
            self.mode = mode
            self.onExpand = onExpand
            super.init()
            camera.camera = SCNCamera()
            scene.rootNode.addChildNode(camera)
            scene.rootNode.addChildNode(routeNode)
            let mat = SCNMaterial()
            mat.diffuse.contents = UIColor(rgb: 0x58A6FF)
            mat.emission.contents = UIColor(rgb: 0x58A6FF)
            dot.geometry?.materials = [mat]
            scene.rootNode.addChildNode(dot)
        }

        func buildIfNeeded() {
            if built { return }
            built = true
            let height = world.model.floorHeightM
            for (fi, fl) in world.model.floors.enumerated() {
                let floor = SCNNode()
                floor.position.y = Float(fi) * Float(height)
                floor.name = "floor:\(fl.level)"
                if fl.outline.count >= 3 {
                    let plate = shapeNode(fl.outline, depth: 0.04, color: UIColor(rgb: 0x3D4654))
                    plate.position.y = -0.02
                    floor.addChildNode(plate)
                }
                for space in fl.spaces where space.polygon.count >= 3 {
                    let node = shapeNode(space.polygon, depth: 0.06, color: Self.roomColor(space.type))
                    node.name = "room:\(space.id)"
                    node.position.y = 0.03
                    floor.addChildNode(node)
                    roomNodes[space.id] = node
                }
                if mode == .map {
                    let walls = wallNode(fl.walls)
                    floor.addChildNode(walls)
                }
                for door in fl.doors where door.exteriorExit {
                    let box = SCNBox(width: 0.45, height: 2.2, length: 0.45, chamferRadius: 0)
                    let mat = SCNMaterial()
                    mat.diffuse.contents = UIColor(rgb: 0x3FB950)
                    mat.emission.contents = UIColor(rgb: 0x3FB950)
                    box.materials = [mat]
                    let post = SCNNode(geometry: box)
                    post.position = SCNVector3(Float(door.x), 1.1, Float(door.y))
                    floor.addChildNode(post)
                }
                let hazards = SCNNode()
                hazards.name = "hazards"
                floor.addChildNode(hazards)
                hazardNodes[fl.level] = hazards
                scene.rootNode.addChildNode(floor)
                floorNodes[fl.level] = floor
            }
            frameCamera()
        }

        func sync() {
            buildIfNeeded()
            updateHighlights()
            updateRoute()
            updateHazards()
            updateDot()
            applyFloors()
            if mode == .mini { aimMini() }
        }

        @objc func tapped(_ gesture: UITapGestureRecognizer) {
            guard let view else { return }
            if mode == .mini {
                onExpand()
                return
            }
            let point = gesture.location(in: view)
            guard let id = roomId(at: point, in: view) else { return }
            if placesYou {
                world.placeAt(roomId: id)
            } else {
                world.choose(roomId: id)
            }
        }

        @objc func held(_ gesture: UILongPressGestureRecognizer) {
            guard mode == .map, gesture.state == .began, let view else { return }
            let point = gesture.location(in: view)
            guard let id = roomId(at: point, in: view) else { return }
            world.ignite(roomId: id)
        }

        private func roomId(at point: CGPoint, in view: SCNView) -> String? {
            let hits = view.hitTest(point, options: [.searchMode: SCNHitTestSearchMode.all.rawValue])
            for hit in hits {
                var node: SCNNode? = hit.node
                while let current = node {
                    if let name = current.name, name.hasPrefix("room:") {
                        return String(name.dropFirst(5))
                    }
                    node = current.parent
                }
            }
            return nil
        }

        private func updateHighlights() {
            for (id, node) in roomNodes {
                let mat = node.geometry?.firstMaterial
                if id == world.destinationId {
                    mat?.emission.contents = UIColor(rgb: 0xD29922)
                } else if id == world.startRoomId {
                    mat?.emission.contents = UIColor(rgb: 0x3FB950)
                } else {
                    mat?.emission.contents = UIColor.black
                }
            }
        }

        private func updateRoute() {
            let sig = world.route?.path.map { "\($0.level),\($0.x),\($0.y)" }.joined(separator: ";") ?? ""
            if sig == routeSig { return }
            routeSig = sig
            routeNode.childNodes.forEach { $0.removeFromParentNode() }
            guard let path = world.route?.path, path.count >= 2 else { return }
            let mat = SCNMaterial()
            mat.diffuse.contents = UIColor(rgb: 0x58A6FF)
            mat.emission.contents = UIColor(rgb: 0x58A6FF)
            for i in 1..<path.count {
                let a = path[i - 1], b = path[i]
                let pa = worldPoint(a)
                let pb = worldPoint(b)
                let dx = pb.x - pa.x, dy = pb.y - pa.y, dz = pb.z - pa.z
                let len = sqrt(dx * dx + dy * dy + dz * dz)
                if len < 0.05 { continue }
                let cyl = SCNCylinder(radius: 0.18, height: CGFloat(len))
                cyl.materials = [mat]
                let node = SCNNode(geometry: cyl)
                node.position = SCNVector3((pa.x + pb.x) / 2, (pa.y + pb.y) / 2, (pa.z + pb.z) / 2)
                node.look(at: pb, up: SCNVector3(0, 1, 0), localFront: SCNVector3(0, 1, 0))
                routeNode.addChildNode(node)
            }
            if let last = path.last {
                let end = SCNNode(geometry: SCNSphere(radius: 0.4))
                end.geometry?.materials = [mat]
                end.position = worldPoint(last)
                routeNode.addChildNode(end)
            }
        }

        private func updateHazards() {
            if world.hazardRev == hazardSeen { return }
            hazardSeen = world.hazardRev
            for node in hazardNodes.values { node.childNodes.forEach { $0.removeFromParentNode() } }
            var firePts: [Int: [(Int, Int)]] = [:]
            var smokePts: [Int: [(Int, Int, Float)]] = [:]
            let grid = world.grid
            for fi in 0..<grid.levels.count {
                let level = grid.levels[fi]
                let base = fi * grid.rows * grid.cols
                for r in 0..<grid.rows {
                    for c in 0..<grid.cols {
                        let i = base + r * grid.cols + c
                        if i >= world.fireCells.count { continue }
                        if world.fireCells[i] {
                            firePts[level, default: []].append((r, c))
                        } else if i < world.smokeCells.count, world.smokeCells[i] >= 0.15 {
                            smokePts[level, default: []].append((r, c, world.smokeCells[i]))
                        }
                    }
                }
            }
            for (level, cells) in firePts {
                hazardNodes[level]?.addChildNode(cellMesh(cells.map { ($0.0, $0.1, Float(1)) }, color: UIColor(rgb: 0xFF4D3D), y: 0.12))
            }
            for (level, cells) in smokePts {
                hazardNodes[level]?.addChildNode(cellMesh(cells, color: UIColor(rgb: 0xD9A441), y: 0.08))
            }
        }

        private func updateDot() {
            guard let pose = world.pose, let fi = world.grid.levelIndex[pose.level] else {
                dot.isHidden = true
                return
            }
            dot.isHidden = false
            let y = Float(fi) * Float(world.model.floorHeightM) + 1.2
            dot.position = SCNVector3(Float(pose.x), y, Float(pose.y))
        }

        private func applyFloors() {
            let show: Int?
            if mode == .mini {
                show = world.pose?.level ?? world.model.levels.first
            } else {
                show = world.floorFilter
            }
            for (level, node) in floorNodes {
                node.isHidden = show != nil && level != show
            }
        }

        private func frameCamera() {
            let grid = world.grid
            let cx = Float(grid.originX + Double(grid.cols) * grid.cell / 2)
            let cz = Float(grid.originY + Double(grid.rows) * grid.cell / 2)
            let span = Float(max(grid.cols, grid.rows)) * Float(grid.cell)
            camera.position = SCNVector3(cx, span * 0.8, cz + span * 0.55)
            camera.look(at: SCNVector3(cx, 0, cz))
            view?.pointOfView = camera
        }

        private func aimMini() {
            let grid = world.grid
            let pose = world.pose
            let x = Float(pose?.x ?? (grid.originX + Double(grid.cols) * grid.cell / 2))
            let z = Float(pose?.y ?? (grid.originY + Double(grid.rows) * grid.cell / 2))
            let span = Float(max(grid.cols, grid.rows)) * Float(grid.cell)
            camera.camera?.usesOrthographicProjection = true
            camera.camera?.orthographicScale = Double(span) * (mode == .mini ? 0.22 : 0.5)
            camera.position = SCNVector3(x, 80, z + 0.01)
            camera.look(at: SCNVector3(x, 0, z))
            view?.pointOfView = camera
            view?.allowsCameraControl = false
        }

        private func worldPoint(_ p: PathPoint) -> SCNVector3 {
            let fi = world.grid.levelIndex[p.level] ?? 0
            let y = Float(fi) * Float(world.model.floorHeightM) + 0.4
            return SCNVector3(Float(p.x), y, Float(p.y))
        }

        private func cellMesh(_ cells: [(Int, Int, Float)], color: UIColor, y: Float) -> SCNNode {
            let parent = SCNNode()
            let grid = world.grid
            let mat = SCNMaterial()
            mat.diffuse.contents = color
            mat.emission.contents = color
            mat.transparency = 0.85
            let box = SCNBox(width: CGFloat(grid.cell * 0.9), height: 0.08, length: CGFloat(grid.cell * 0.9), chamferRadius: 0)
            box.materials = [mat]
            for (r, c, _) in cells.prefix(4000) {
                let node = SCNNode(geometry: box)
                node.position = SCNVector3(Float(grid.centerX(c)), y, Float(grid.centerY(r)))
                parent.addChildNode(node)
            }
            return parent
        }

        private func wallNode(_ walls: [WallSeg]) -> SCNNode {
            var verts: [SCNVector3] = []
            var indices: [Int32] = []
            let h: Float = 2.4
            let thick: Float = 0.12
            for w in walls {
                let x1 = Float(w.x1), z1 = Float(w.y1)
                let x2 = Float(w.x2), z2 = Float(w.y2)
                let dx = x2 - x1, dz = z2 - z1
                let len = sqrt(dx * dx + dz * dz)
                if len < 0.02 { continue }
                let nx = -dz / len * thick * 0.5
                let nz = dx / len * thick * 0.5
                let base = Int32(verts.count)
                verts.append(contentsOf: [
                    SCNVector3(x1 + nx, 0, z1 + nz), SCNVector3(x2 + nx, 0, z2 + nz),
                    SCNVector3(x2 - nx, 0, z2 - nz), SCNVector3(x1 - nx, 0, z1 - nz),
                    SCNVector3(x1 + nx, h, z1 + nz), SCNVector3(x2 + nx, h, z2 + nz),
                    SCNVector3(x2 - nx, h, z2 - nz), SCNVector3(x1 - nx, h, z1 - nz),
                ])
                let faces: [Int32] = [
                    0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6,
                    0, 4, 5, 0, 5, 1, 1, 5, 6, 1, 6, 2,
                    2, 6, 7, 2, 7, 3, 3, 7, 4, 3, 4, 0,
                ]
                indices.append(contentsOf: faces.map { base + $0 })
            }
            guard !verts.isEmpty else { return SCNNode() }
            let src = SCNGeometrySource(vertices: verts)
            let elem = SCNGeometryElement(indices: indices, primitiveType: .triangles)
            let geo = SCNGeometry(sources: [src], elements: [elem])
            let mat = SCNMaterial()
            mat.diffuse.contents = UIColor(rgb: 0xD0D7DE)
            mat.isDoubleSided = true
            geo.materials = [mat]
            return SCNNode(geometry: geo)
        }

        private func shapeNode(_ poly: [PlanPoint], depth: CGFloat, color: UIColor) -> SCNNode {
            let path = UIBezierPath()
            guard let first = poly.first else { return SCNNode() }
            path.move(to: CGPoint(x: first.x, y: -first.y))
            for p in poly.dropFirst() { path.addLine(to: CGPoint(x: p.x, y: -p.y)) }
            path.close()
            let shape = SCNShape(path: path, extrusionDepth: depth)
            let mat = SCNMaterial()
            mat.diffuse.contents = color
            mat.isDoubleSided = true
            shape.materials = [mat]
            let node = SCNNode(geometry: shape)
            let a: Float = -.pi / 2
            let c = cos(a), s = sin(a)
            var m = matrix_identity_float4x4
            m.columns.1 = simd_float4(0, c, s, 0)
            m.columns.2 = simd_float4(0, -s, c, 0)
            node.simdTransform = m
            return node
        }

        private static func roomColor(_ type: String) -> UIColor {
            switch type {
            case "hallway": return UIColor(rgb: 0x66738A)
            case "stairwell": return UIColor(rgb: 0x3F7A52)
            case "elevator": return UIColor(rgb: 0x6A6A7A)
            default: return UIColor(rgb: 0x56627A)
            }
        }
    }
}

extension UIColor {
    convenience init(rgb: Int) {
        self.init(
            red: CGFloat((rgb >> 16) & 0xFF) / 255,
            green: CGFloat((rgb >> 8) & 0xFF) / 255,
            blue: CGFloat(rgb & 0xFF) / 255,
            alpha: 1
        )
    }
}
