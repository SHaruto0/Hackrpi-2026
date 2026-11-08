#!/usr/bin/env python3
"""
generate_preview.py
Generates an interactive 3D Three.js viewer (map_preview.html) from final_model/map_descriptors.npz
and the MiSensorKit camera/ poses so you can inspect the 3D point cloud, camera path, and look rays.
Works with pure Python stdlib (no heavy dependencies required locally).
"""

import ast
import glob
import json
import struct
import webbrowser
import zipfile
from pathlib import Path


def read_npy_from_zip(zf: zipfile.ZipFile, name: str):
    raw = zf.read(name)
    ver_maj = raw[6]
    hlen = struct.unpack("<H", raw[8:10])[0] if ver_maj == 1 else struct.unpack("<I", raw[8:12])[0]
    hdr_start = 10 if ver_maj == 1 else 12
    offset = hdr_start + hlen
    hdr = ast.literal_eval(raw[hdr_start:offset].decode("latin1").strip())
    return hdr, raw[offset:]


def main():
    npz_path = Path("final_model/map_descriptors.npz")
    if not npz_path.exists():
        raise FileNotFoundError(f"Could not find {npz_path}")

    with zipfile.ZipFile(npz_path, "r") as zf:
        hdr_xyz, xyz_bytes = read_npy_from_zip(zf, "xyz.npy")
        hdr_pid, pid_bytes = read_npy_from_zip(zf, "point_ids.npy")

    num_pts = hdr_xyz["shape"][0]
    pts = []
    for i in range(num_pts):
        x, y, z = struct.unpack_from("<3f", xyz_bytes, i * 12)
        pid = struct.unpack_from("<q", pid_bytes, i * 8)[0]
        pts.append([round(x, 4), round(y, 4), round(z, 4), int(pid)])

    # Find camera json files (prefer "2nd floor part 2" recording)
    cam_files = sorted(glob.glob("2nd floor part 2/camera/frame_*.json"))
    if not cam_files:
        cam_files = sorted(glob.glob("2nd floor/camera/frame_*.json"))
    if not cam_files:
        cam_files = sorted(glob.glob("camera/frame_*.json"))
    if not cam_files:
        cam_files = sorted(glob.glob("misensorkit_*/camera/frame_*.json"))

    cams = []
    for idx, jf in enumerate(cam_files, start=1):
        with open(jf, "r", encoding="utf-8") as f:
            d = json.load(f)
        R = d["extrinsics"]["R"]
        t = d["extrinsics"]["t"]
        look = [-R[0][2], -R[1][2], -R[2][2]]
        if idx <= 365:
            room_lbl = "232 CLASSROOM"
        elif idx <= 440:
            room_lbl = "219 CORRIDOR (OUTSIDE 232)"
        elif idx <= 680:
            room_lbl = "219 CORRIDOR (OUTSIDE 234)"
        elif idx <= 930:
            room_lbl = "219 CORRIDOR (OUTSIDE 235)"
        elif idx <= 1138:
            room_lbl = "235 CLASSROOM"
        elif idx <= 1480:
            room_lbl = "219 CORRIDOR (OUTSIDE 236)"
        elif idx <= 1720:
            room_lbl = "219 CORRIDOR (OUTSIDE 238)"
        elif idx <= 1838:
            room_lbl = "219 CORRIDOR (OUTSIDE 239)"
        elif idx <= 2115:
            room_lbl = "239 CLASSROOM"
        elif idx <= 2280:
            room_lbl = "219 CORRIDOR (OUTSIDE 240)"
        elif idx <= 2385:
            room_lbl = "219 CORRIDOR (EAST / STAIR 2ST6)"
        else:
            room_lbl = "242 EAST LOBBY / STAIR 2ST6 EXIT"
        cams.append(
            {
                "id": idx,
                "name": Path(jf).stem + ".jpg",
                "room": room_lbl,
                "pos": [round(float(v), 4) for v in t],
                "look": [round(float(v), 4) for v in look],
            }
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Firesight — 3D VPS Map Preview (final_model)</title>
  <style>
    body {{ margin: 0; background: #0b0f19; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif; overflow: hidden; }}
    #hud {{
      position: absolute; top: 16px; left: 16px; z-index: 10;
      background: rgba(15, 23, 42, 0.88); backdrop-filter: blur(8px);
      border: 1px solid rgba(255, 107, 53, 0.4); border-radius: 12px;
      padding: 16px 20px; max-width: 360px; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    }}
    h1 {{ margin: 0 0 6px 0; font-size: 18px; color: #ff6b35; letter-spacing: 0.5px; }}
    .stat {{ font-size: 13px; color: #cbd5e1; margin: 4px 0; }}
    .stat b {{ color: #fff; }}
    .legend {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid #334155; font-size: 12px; }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
    .controls {{ margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }}
    button {{
      background: #1e293b; color: #f8fafc; border: 1px solid #475569;
      padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 12px;
    }}
    button:hover {{ background: #334155; border-color: #ff6b35; }}
  </style>
</head>
<body>
  <div id="hud">
    <h1>FIRESIGHT // 3D VPS MAP</h1>
    <div class="stat">Triangulated 3D Landmarks: <b>{len(pts)} points</b></div>
    <div class="stat">Registered ARKit Poses: <b>{len(cams)} frames</b></div>
    <div class="stat">Descriptors: <b>256-D SuperPoint (LightGlue ready)</b></div>
    <div class="legend">
      <div><span class="dot" style="background:#38bdf8;"></span>3D Feature Cloud (colored by height Y)</div>
      <div style="margin-top:4px;"><span class="dot" style="background:#ff6b35;"></span>ARKit Camera Trajectory (114 poses)</div>
      <div style="margin-top:4px;"><span class="dot" style="background:#22c55e;"></span>Camera Viewing Rays (-Z optical axis)</div>
    </div>
    <div class="controls">
      <button id="btnReset">Reset View</button>
      <button id="btnRays">Toggle Look Rays</button>
      <button id="btnAnim">Simulate Localization</button>
    </div>
    <div id="simStatus" style="margin-top:10px; font-size:12px; color:#22c55e;"></div>
  </div>

  <script type="importmap">
    {{
      "imports": {{
        "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
        "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
      }}
    }}
  </script>
  <script type="module">
    import * as THREE from 'three';
    import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';

    const POINTS = {json.dumps(pts)};
    const CAMS = {json.dumps(cams)};

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b0f19);

    const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.05, 200);
    camera.position.set(0, 2.8, 4.5);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(window.devicePixelRatio);
    document.body.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(0, 0.8, -1.0);
    controls.update();

    // Grid & Axes
    const grid = new THREE.GridHelper(12, 24, 0x334155, 0x1e293b);
    scene.add(grid);
    scene.add(new THREE.AxesHelper(0.75));

    // 1. 3D Point Cloud
    const ptGeo = new THREE.BufferGeometry();
    const positions = new Float32Array(POINTS.length * 3);
    const colors = new Float32Array(POINTS.length * 3);

    let minY = Infinity, maxY = -Infinity;
    for (const p of POINTS) {{
      if (p[1] < minY) minY = p[1];
      if (p[1] > maxY) maxY = p[1];
    }}

    const colorTmp = new THREE.Color();
    POINTS.forEach((p, i) => {{
      positions[i * 3] = p[0];
      positions[i * 3 + 1] = p[1];
      positions[i * 3 + 2] = p[2];
      const t = Math.max(0, Math.min(1, (p[1] - minY) / (maxY - minY + 1e-5)));
      colorTmp.setHSL(0.58 - t * 0.45, 0.9, 0.6);
      colors[i * 3] = colorTmp.r;
      colors[i * 3 + 1] = colorTmp.g;
      colors[i * 3 + 2] = colorTmp.b;
    }});

    ptGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    ptGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));

    const ptMat = new THREE.PointsMaterial({{ size: 0.07, vertexColors: true, transparent: true, opacity: 0.9 }});
    const pointCloud = new THREE.Points(ptGeo, ptMat);
    scene.add(pointCloud);

    // 2. Camera Trajectory & Frustum Rays
    const camPts = CAMS.map(c => new THREE.Vector3(...c.pos));
    if (camPts.length > 1) {{
      const pathGeo = new THREE.BufferGeometry().setFromPoints(camPts);
      const pathMat = new THREE.LineBasicMaterial({{ color: 0xff6b35, linewidth: 2 }});
      scene.add(new THREE.Line(pathGeo, pathMat));
    }}

    const raysGroup = new THREE.Group();
    CAMS.forEach((c, idx) => {{
      if (idx % 4 !== 0) return;
      const start = new THREE.Vector3(...c.pos);
      const dir = new THREE.Vector3(...c.look).normalize();
      const end = start.clone().add(dir.multiplyScalar(0.6));
      const g = new THREE.BufferGeometry().setFromPoints([start, end]);
      const m = new THREE.LineBasicMaterial({{ color: 0x22c55e, transparent: true, opacity: 0.45 }});
      raysGroup.add(new THREE.Line(g, m));
    }});
    scene.add(raysGroup);

    // Live localization marker (simulated query frame)
    const activeSphere = new THREE.Mesh(
      new THREE.SphereGeometry(0.06, 16, 16),
      new THREE.MeshBasicMaterial({{ color: 0xfacc15 }})
    );
    activeSphere.visible = false;
    scene.add(activeSphere);

    const activeRayGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
    const activeRay = new THREE.Line(activeRayGeo, new THREE.LineBasicMaterial({{ color: 0xfacc15 }}));
    activeRay.visible = false;
    scene.add(activeRay);

    document.getElementById('btnReset').onclick = () => {{
      camera.position.set(0, 2.8, 4.5);
      controls.target.set(0, 0.8, -1.0);
      controls.update();
    }};
    document.getElementById('btnRays').onclick = () => {{
      raysGroup.visible = !raysGroup.visible;
    }};

    let simIdx = -1;
    let simTimer = null;
    document.getElementById('btnAnim').onclick = () => {{
      if (simTimer) {{
        clearInterval(simTimer);
        simTimer = null;
        activeSphere.visible = false;
        activeRay.visible = false;
        document.getElementById('simStatus').textContent = '';
        return;
      }}
      simIdx = 0;
      activeSphere.visible = true;
      activeRay.visible = true;
      simTimer = setInterval(() => {{
        const c = CAMS[simIdx % CAMS.length];
        const p = new THREE.Vector3(...c.pos);
        const l = new THREE.Vector3(...c.look).normalize();
        activeSphere.position.copy(p);
        activeRayGeo.setFromPoints([p, p.clone().add(l.multiplyScalar(2.5))]);
        document.getElementById('simStatus').textContent =
          `[${{c.room}}] ${{c.name}} -> XYZ: (${{c.pos[0].toFixed(2)}}, ${{c.pos[1].toFixed(2)}}, ${{c.pos[2].toFixed(2)}})m`;
        simIdx++;
      }}, 80);
    }};

    window.addEventListener('resize', () => {{
      camera.aspect = window.innerWidth / window.innerHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight);
    }});

    function animate() {{
      requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    }}
    animate();
  </script>
</body>
</html>
"""
    out_path = Path("map_preview.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"[generate_preview] Created {out_path.resolve()} with {len(pts)} 3D points and {len(cams)} cameras.")


if __name__ == "__main__":
    main()
