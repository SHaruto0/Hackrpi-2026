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
    var followsUser = false
    var onExpand: () -> Void = {}

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView()
        view.scene = context.coordinator.scene
        view.backgroundColor = UIColor(rgb: 0x2A313C)
        view.antialiasingMode = .multisampling2X
        view.autoenablesDefaultLighting = true
        view.allowsCameraControl = mode == .map && !followsUser
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped(_:)))
        view.addGestureRecognizer(tap)
        context.coordinator.view = view
        context.coordinator.sync()
        return view
    }

    func updateUIView(_ uiView: SCNView, context: Context) {
        context.coordinator.world = world
        context.coordinator.mode = mode
        context.coordinator.followsUser = followsUser
        context.coordinator.onExpand = onExpand
        uiView.allowsCameraControl = mode == .map && !followsUser
        context.coordinator.sync()
    }

    func makeCoordinator() -> Coordinator {
        let coordinator = Coordinator(world: world, mode: mode, onExpand: onExpand)
        coordinator.followsUser = followsUser
        return coordinator
    }

    final class Coordinator: NSObject {
        var world: WorldModel
        var mode: SceneMode
        var followsUser = false
        var onExpand: () -> Void
        let scene = SCNScene()
        weak var view: SCNView?
        private var built = false
        private var floorNodes: [Int: SCNNode] = [:]
        private var roomNodes: [String: SCNNode] = [:]
        private let routeNode = SCNNode()
        private let dot = SCNNode(geometry: SCNSphere(radius: 0.6))
        private var routeSig = ""
        private let headingArrow = SCNNode()
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
            mat.readsFromDepthBuffer = false
            mat.lightingModel = .constant
            dot.renderingOrder = 100
            dot.geometry?.materials = [mat]
            scene.rootNode.addChildNode(dot)
            let ring = SCNNode(geometry: SCNTorus(ringRadius: 0.9, pipeRadius: 0.12))
            let white = SCNMaterial()
            white.diffuse.contents = UIColor.white
            white.emission.contents = UIColor.white
            white.readsFromDepthBuffer = false
            ring.geometry?.materials = [white]
            ring.renderingOrder = 101
            dot.addChildNode(ring)
            let label = textNode("YOU ARE HERE", color: .white)
            label.position = SCNVector3(0, 1.7, 0)
            dot.addChildNode(label)
            let arrow = SCNNode(geometry: SCNCone(topRadius: 0, bottomRadius: 0.4, height: 1.2))
            arrow.geometry?.materials = [white]
            arrow.eulerAngles.x = -.pi / 2
            arrow.position = SCNVector3(0, 0, -1.5)
            arrow.renderingOrder = 101
            headingArrow.addChildNode(arrow)
            dot.addChildNode(headingArrow)
        }

        func buildIfNeeded() {
            if built { return }
            built = true
            let height = world.model.floorHeightM
            for fl in world.model.floors {
                let fi = world.grid.levelIndex[fl.level] ?? 0
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
                scene.rootNode.addChildNode(floor)
                floorNodes[fl.level] = floor
            }
            frameCamera()
        }

        func sync() {
            buildIfNeeded()
            updateHighlights()
            updateRoute()
            updateDot()
            applyFloors()
            if mode == .mini || followsUser { aimAtUser() }
        }

        @objc func tapped(_ gesture: UITapGestureRecognizer) {
            if mode == .mini {
                onExpand()
                return
            }
        }

        private func updateHighlights() {
            let currentRoomId = world.currentRoom?.id
            for (id, node) in roomNodes {
                let mat = node.geometry?.firstMaterial
                if id == world.destinationId {
                    mat?.emission.contents = UIColor(rgb: 0xD29922)
                } else if id == currentRoomId {
                    mat?.emission.contents = UIColor(rgb: 0x3FB950)
                } else {
                    mat?.emission.contents = UIColor.black
                }
            }
        }

        private func updateRoute() {
            let sig = "\(world.pose?.level ?? 0)|" + (world.route?.path.map { "\($0.level),\($0.x),\($0.y)" }.joined(separator: ";") ?? "")
            if sig == routeSig { return }
            routeSig = sig
            routeNode.childNodes.forEach { $0.removeFromParentNode() }
            guard let path = world.route?.path, !path.isEmpty else { return }
            let mat = SCNMaterial()
            mat.diffuse.contents = UIColor(rgb: 0x58A6FF)
            mat.emission.contents = UIColor(rgb: 0x58A6FF)
            mat.lightingModel = .constant
            mat.readsFromDepthBuffer = false
            for i in path.indices.dropFirst() {
                let a = path[i - 1], b = path[i]
                guard a.level == world.pose?.level, b.level == a.level else { continue }
                let pa = worldPoint(a)
                let pb = worldPoint(b)
                let dx = pb.x - pa.x, dy = pb.y - pa.y, dz = pb.z - pa.z
                let len = sqrt(dx * dx + dy * dy + dz * dz)
                if len < 0.05 { continue }
                let cyl = SCNCylinder(radius: 0.24, height: CGFloat(len))
                cyl.materials = [mat]
                let node = SCNNode(geometry: cyl)
                node.position = SCNVector3((pa.x + pb.x) / 2, (pa.y + pb.y) / 2, (pa.z + pb.z) / 2)
                node.look(at: pb, up: SCNVector3(0, 1, 0), localFront: SCNVector3(0, 1, 0))
                node.renderingOrder = 50
                routeNode.addChildNode(node)
            }
            if let last = path.last, last.level == world.pose?.level {
                let end = SCNNode(geometry: SCNSphere(radius: 0.4))
                let gold = mat.copy() as! SCNMaterial
                gold.diffuse.contents = UIColor(rgb: 0xFFD166)
                gold.emission.contents = UIColor(rgb: 0xFFD166)
                end.geometry?.materials = [gold]
                end.renderingOrder = 60
                end.position = worldPoint(last)
                routeNode.addChildNode(end)
                let label = textNode(world.destinationLabel, color: UIColor(rgb: 0xFFD166))
                label.position = end.position
                label.position.y += 1.5
                routeNode.addChildNode(label)
            }
            if let index = path.indices.dropFirst().first(where: { path[$0 - 1].level == world.pose?.level && path[$0].level != world.pose?.level }) {
                let landing = path[index - 1]
                let label = textNode("STAIRS TO FLOOR \(path[index].level)", color: UIColor(rgb: 0xFFD166))
                label.position = worldPoint(landing)
                label.position.y += 1.5
                routeNode.addChildNode(label)
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
            dot.geometry?.firstMaterial?.diffuse.contents = UIColor(rgb: pose.unsure ? 0xFFD166 : 0x58A6FF)
            dot.geometry?.firstMaterial?.emission.contents = UIColor(rgb: pose.unsure ? 0xFFD166 : 0x58A6FF)
            let bearing = world.posMode == .demo ? world.hints?.headingPlanDeg : world.headingDeg.map { $0 - world.model.planNorthDeg }
            headingArrow.isHidden = bearing == nil
            headingArrow.eulerAngles.y = -Float(bearing ?? 0) * .pi / 180

        }

        private func applyFloors() {
            let show = world.pose?.level ?? world.model.levels.first
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

        private func aimAtUser() {
            guard let pose = world.pose else { return }
            let fi = world.grid.levelIndex[pose.level] ?? 0
            let y = Float(fi) * Float(world.model.floorHeightM)
            let x = Float(pose.x), z = Float(pose.y)
            camera.camera?.usesOrthographicProjection = true
            camera.camera?.orthographicScale = mode == .mini ? 11 : 15
            camera.position = SCNVector3(x, y + 30, z + (mode == .mini ? 0.01 : 16))
            camera.look(at: SCNVector3(x, y, z))
            view?.pointOfView = camera
            view?.allowsCameraControl = false
        }

        private func textNode(_ text: String, color: UIColor) -> SCNNode {
            let geometry = SCNText(string: text, extrusionDepth: 0)
            geometry.font = UIFont.systemFont(ofSize: 1, weight: .heavy)
            geometry.flatness = 0.2
            let material = SCNMaterial()
            material.diffuse.contents = color
            material.emission.contents = color
            material.lightingModel = .constant
            material.readsFromDepthBuffer = false
            geometry.materials = [material]
            let node = SCNNode(geometry: geometry)
            let (min, max) = geometry.boundingBox
            node.pivot = SCNMatrix4MakeTranslation((min.x + max.x) / 2, min.y, 0)
            node.scale = SCNVector3(0.6, 0.6, 0.6)
            node.constraints = [SCNBillboardConstraint()]
            node.renderingOrder = 102
            return node
        }

        private func worldPoint(_ p: PathPoint) -> SCNVector3 {
            let fi = world.grid.levelIndex[p.level] ?? 0
            let y = Float(fi) * Float(world.model.floorHeightM) + 0.4
            return SCNVector3(Float(p.x), y, Float(p.y))
        }

        private func wallNode(_ walls: [WallSeg]) -> SCNNode {
            var verts: [SCNVector3] = []
            var indices: [Int32] = []
            let h: Float = 0.7
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
