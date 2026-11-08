#!/usr/bin/env python3
"""
Firesight — FastAPI Backend ("The Brain")
- Receives compressed 640x480 grayscale JPEG frames over WebSockets from the iOS AR client.
- Runs SuperPoint + LightGlue against pre-loaded final_model (.bin + map_descriptors.npz).
- Uses pycolmap.absolute_pose_estimation() to compute 6-DoF camera pose (X, Y, Z, Rotation & simd_float4x4).
- Maintains a hardcoded 12-node waypoint graph and runs Dijkstra routing to the nearest unblocked exit.
- Syncs live telemetry, blocked-node hazard updates, and pre-rendered MP3 audio flags with the Commander Dashboard.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import heapq
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

try:
    import pycolmap
    HAS_PYCOLMAP = True
except ImportError:
    HAS_PYCOLMAP = False

try:
    import torch
    from lightglue import LightGlue, SuperPoint
    HAS_ML = True
except ImportError:
    HAS_ML = False


ROOT_DIR = Path(__file__).resolve().parent.parent
FINAL_MODEL_DIR = ROOT_DIR / "final_model"

# Portrait conversion matrix between ARKit camera frame and COLMAP upright image frame
C_PORTRAIT = np.array(
    [
        [0.0, 1.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 0.0, -1.0],
    ],
    dtype=np.float64,
)


def rotmat_to_xyzw_quat(R: np.ndarray) -> List[float]:
    """Convert 3x3 rotation matrix to [qx, qy, qz, qw] quaternion for ARKit/Three.js."""
    U, _, Vt = np.linalg.svd(R)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt

    tr = np.trace(R)
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2.0
        qw = 0.25 * s
        qx = (R[2, 1] - R[1, 2]) / s
        qy = (R[0, 2] - R[2, 0]) / s
        qz = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2.0
        qw = (R[2, 1] - R[1, 2]) / s
        qx = 0.25 * s
        qy = (R[0, 1] + R[1, 0]) / s
        qz = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2.0
        qw = (R[0, 2] - R[2, 0]) / s
        qx = (R[0, 1] + R[1, 0]) / s
        qy = 0.25 * s
        qz = (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2.0
        qw = (R[1, 0] - R[0, 1]) / s
        qx = (R[0, 2] + R[2, 0]) / s
        qy = (R[1, 2] + R[2, 1]) / s
        qz = 0.25 * s

    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw) + 1e-12
    return [round(qx / norm, 6), round(qy / norm, 6), round(qz / norm, 6), round(qw / norm, 6)]


def make_simd_float4x4(R_wc: np.ndarray, t_wc: np.ndarray) -> List[float]:
    """
    Build column-major 16-float array for Swift's simd_float4x4.
    Columns 0, 1, 2 are rotation axes; Column 3 is [tx, ty, tz, 1.0].
    """
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = R_wc
    T[:3, 3] = t_wc
    # Column-major flatten for Apple simd_float4x4
    return [round(float(x), 6) for x in T.T.flatten()]


# =====================================================================
# 1. HARDCODED DIJKSTRA WAYPOINT GRAPH (Strictly No NavMeshes)
# =====================================================================
class WaypointRouter:
    """
    Maintains a hardcoded 12-node building graph with Primary & Alternate Hallway routes.
    Supports dynamic blocking from the Commander Dashboard and Dijkstra shortest-path calculation.
    """

    def __init__(self) -> None:
        # Real Darrin Communications Center (DCC) 200 Level Waypoints extracted directly from "2nd floor" ARKit walk
        # Span: 46.3m West-to-East (+X along Corridor 219 at Z ≈ -5.2m; Room 232 at (0.1, 0, 0.5); East Lobby 242 at (43.6, 0, 0.1))
        self.nodes: Dict[str, Dict[str, Any]] = {
            "exit_west":       {"id": "exit_west",       "label": "218 West Lobby / Stair 2ST1", "pos": [-5.5, 0.0, -4.8], "is_exit": True,  "blocked": False},
            "corr_232_w":      {"id": "corr_232_w",      "label": "218 West Lobby Doors",        "pos": [-2.0, 0.0, -4.3], "is_exit": False, "blocked": False},
            "room_232":        {"id": "room_232",        "label": "Room 232 (Inside)",           "pos": [0.1,  0.0,  0.5], "is_exit": False, "blocked": False},
            "door_232":        {"id": "door_232",        "label": "Room 232 Doorway",            "pos": [-1.1, 0.0, -0.8], "is_exit": False, "blocked": False},
            "corr_232":        {"id": "corr_232",        "label": "Corridor 219 (Outside 232)",  "pos": [0.9,  0.0, -5.8], "is_exit": False, "blocked": False},
            "corr_234":        {"id": "corr_234",        "label": "Corridor 219 (Outside 234)",  "pos": [6.3,  0.0, -5.5], "is_exit": False, "blocked": False},
            "room_235_door":   {"id": "room_235_door",   "label": "Room 235 Alcove / Door",      "pos": [9.9,  0.0, -3.7], "is_exit": False, "blocked": False},
            "corr_235":        {"id": "corr_235",        "label": "Corridor 219 (Outside 235)",  "pos": [10.6, 0.0, -5.3], "is_exit": False, "blocked": False},
            "corr_236":        {"id": "corr_236",        "label": "Corridor 219 (Outside 236)",  "pos": [15.0, 0.0, -5.5], "is_exit": False, "blocked": False},
            "corr_238":        {"id": "corr_238",        "label": "Corridor 219 (Outside 238)",  "pos": [21.3, 0.0, -4.7], "is_exit": False, "blocked": False},
            "corr_239":        {"id": "corr_239",        "label": "Corridor 219 (Outside 239)",  "pos": [27.0, 0.0, -5.0], "is_exit": False, "blocked": False},
            "corr_240":        {"id": "corr_240",        "label": "Corridor 219 (Outside 240)",  "pos": [31.6, 0.0, -4.6], "is_exit": False, "blocked": False},
            "exit_stair_2st6": {"id": "exit_stair_2st6", "label": "Stair 2ST6 Exit",             "pos": [37.9, 0.0, -4.8], "is_exit": True,  "blocked": False},
            "corr_east_end":   {"id": "corr_east_end",   "label": "Corridor 219 East Jct",       "pos": [42.1, 0.0, -6.3], "is_exit": False, "blocked": False},
            "exit_east_242":   {"id": "exit_east_242",   "label": "242 East Lobby Exit",         "pos": [43.6, 0.0,  0.1], "is_exit": True,  "blocked": False},
        }

        self.edges: List[Tuple[str, str]] = [
            # Room 232 out to Corridor 219
            ("room_232", "door_232"),
            ("door_232", "corr_232_w"),
            ("door_232", "corr_232"),
            # West Exit branch
            ("corr_232_w", "exit_west"),
            ("corr_232_w", "corr_232"),
            # Main Corridor 219 West-to-East spine
            ("corr_232", "corr_234"),
            ("corr_234", "room_235_door"),
            ("room_235_door", "corr_235"),
            ("corr_234", "corr_235"),
            ("corr_235", "corr_236"),
            ("corr_236", "corr_238"),
            ("corr_238", "corr_239"),
            ("corr_239", "corr_240"),
            ("corr_240", "exit_stair_2st6"),
            ("exit_stair_2st6", "corr_east_end"),
            ("corr_east_end", "exit_east_242"),
        ]

        self.adj: Dict[str, List[Tuple[str, float]]] = {nid: [] for nid in self.nodes}
        self._rebuild_adjacency()

    def _rebuild_adjacency(self) -> None:
        self.adj = {nid: [] for nid in self.nodes}
        for u, v in self.edges:
            if u in self.nodes and v in self.nodes:
                pu = np.array(self.nodes[u]["pos"], dtype=np.float64)
                pv = np.array(self.nodes[v]["pos"], dtype=np.float64)
                dist = float(np.linalg.norm(pu - pv))
                self.adj[u].append((v, dist))
                self.adj[v].append((u, dist))

    def nearest_node(self, xyz: List[float]) -> str:
        p = np.array(xyz, dtype=np.float64)
        best_id = "room_232"
        best_dist = float("inf")
        for nid, nd in self.nodes.items():
            if nd["blocked"]:
                continue
            d = float(np.linalg.norm(p - np.array(nd["pos"], dtype=np.float64)))
            if d < best_dist:
                best_dist = d
                best_id = nid
        return best_id

    def toggle_block(self, node_id: str, force_state: Optional[bool] = None) -> bool:
        if node_id not in self.nodes:
            raise KeyError(f"Unknown node_id: {node_id}")
        if force_state is None:
            self.nodes[node_id]["blocked"] = not self.nodes[node_id]["blocked"]
        else:
            self.nodes[node_id]["blocked"] = bool(force_state)
        return self.nodes[node_id]["blocked"]

    def update_node_pos(self, node_id: str, pos: List[float]) -> None:
        if node_id in self.nodes and len(pos) == 3:
            self.nodes[node_id]["pos"] = [round(float(c), 3) for c in pos]
            self._rebuild_adjacency()

    def compute_dijkstra(self, start_xyz: List[float]) -> Dict[str, Any]:
        """Run Dijkstra from nearest unblocked waypoint to the nearest reachable exit."""
        start_id = self.nearest_node(start_xyz)
        exits = [nid for nid, nd in self.nodes.items() if nd["is_exit"] and not nd["blocked"]]

        if not exits or self.nodes[start_id]["blocked"]:
            return {
                "start_node": start_id,
                "start_label": self.nodes[start_id]["label"],
                "target_exit": None,
                "path_ids": [],
                "path_coords": [],
                "total_distance_m": None,
                "reachable": False,
            }

        dist_map: Dict[str, float] = {nid: float("inf") for nid in self.nodes}
        prev_map: Dict[str, Optional[str]] = {nid: None for nid in self.nodes}
        dist_map[start_id] = 0.0

        pq: List[Tuple[float, str]] = [(0.0, start_id)]
        visited: Set[str] = set()

        chosen_exit: Optional[str] = None
        while pq:
            d_u, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)

            if self.nodes[u]["is_exit"] and not self.nodes[u]["blocked"]:
                chosen_exit = u
                break

            for v, weight in self.adj.get(u, []):
                if self.nodes[v]["blocked"] or v in visited:
                    continue
                alt = d_u + weight
                if alt < dist_map[v]:
                    dist_map[v] = alt
                    prev_map[v] = u
                    heapq.heappush(pq, (alt, v))

        if chosen_exit is None:
            return {
                "start_node": start_id,
                "start_label": self.nodes[start_id]["label"],
                "target_exit": None,
                "path_ids": [],
                "path_coords": [],
                "total_distance_m": None,
                "reachable": False,
            }

        path_ids: List[str] = []
        curr: Optional[str] = chosen_exit
        while curr is not None:
            path_ids.append(curr)
            curr = prev_map[curr]
        path_ids.reverse()

        path_coords = [self.nodes[nid]["pos"] for nid in path_ids]
        return {
            "start_node": start_id,
            "start_label": self.nodes[start_id]["label"],
            "target_exit": chosen_exit,
            "path_ids": path_ids,
            "path_coords": path_coords,
            "total_distance_m": round(dist_map[chosen_exit], 2),
            "reachable": True,
        }


# =====================================================================
# 2. VISUAL POSITIONING SYSTEM (SuperPoint + LightGlue + pycolmap)
# =====================================================================
class VisualPositioningEngine:
    """
    Loads final_model/ (.bin files + map_descriptors.npz) into memory on startup
    and localizes incoming 640x480 grayscale JPEGs.
    """

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir
        self.recon = None
        self.xyz: np.ndarray = np.zeros((0, 3), dtype=np.float64)
        self.rgb: np.ndarray = np.zeros((0, 3), dtype=np.uint8)
        self.descriptors: np.ndarray = np.zeros((0, 256), dtype=np.float32)
        self.keypoints: np.ndarray = np.zeros((0, 2), dtype=np.float32)
        self.map_image_size = (448, 448)
        self.default_intrinsics = {"width": 448, "height": 448, "fx": 470.0, "fy": 470.0, "cx": 224.0, "cy": 224.0}
        self.trajectory: List[Dict[str, Any]] = []

        self.device = None
        self.extractor = None
        self.matcher = None
        self.map_feats_torch = None

        # OpenCV ORB keyframe fallback index (if torch/lightglue is not installed locally)
        self.orb = cv2.ORB_create(nfeatures=600)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self.orb_keyframes: List[Dict[str, Any]] = []

        self._load_map()
        self._init_ml()

    def _load_map(self) -> None:
        npz_path = self.model_dir / "map_descriptors.npz"
        if npz_path.exists():
            data = np.load(npz_path)
            self.xyz = data["xyz"].astype(np.float64)
            self.rgb = data["rgb"].astype(np.uint8)
            self.descriptors = data["descriptors"].astype(np.float32).T  # (N, 256)
            self.keypoints = data["keypoints"].astype(np.float32)
            img_sz = data["image_size"]
            self.map_image_size = (int(img_sz[0]), int(img_sz[1]))
            print(f"[VPS] Loaded {len(self.xyz)} 3D landmarks from {npz_path}")

        if HAS_PYCOLMAP and (self.model_dir / "cameras.bin").exists():
            try:
                self.recon = pycolmap.Reconstruction(str(self.model_dir))
                first_cam = next(iter(self.recon.cameras.values()))
                params = list(first_cam.params)
                if len(params) >= 4:
                    self.default_intrinsics = {
                        "width": int(first_cam.width),
                        "height": int(first_cam.height),
                        "fx": float(params[0]),
                        "fy": float(params[1]),
                        "cx": float(params[2]),
                        "cy": float(params[3]),
                    }
                print(
                    f"[VPS] Loaded pycolmap Reconstruction: "
                    f"{len(self.recon.images)} images, {len(self.recon.points3D)} 3D points"
                )
            except Exception as exc:
                print(f"[VPS] Note: pycolmap.Reconstruction load warning: {exc}")

        # Load recorded camera trajectory for dashboard visualization (prefer precomputed orb_keyframes.npz or "2nd floor")
        orb_npz = self.model_dir / "orb_keyframes.npz"
        if orb_npz.exists():
            try:
                odata = np.load(orb_npz, allow_pickle=True)
                if "trajectory" in odata:
                    for idx, pos in enumerate(odata["trajectory"], start=1):
                        self.trajectory.append({"id": idx, "pos": [round(float(v), 4) for v in pos]})
            except Exception as exc:
                print(f"[VPS] Warning loading trajectory from {orb_npz}: {exc}")

        if not self.trajectory:
            cam_files = sorted(glob.glob(str(ROOT_DIR / "2nd floor" / "camera" / "frame_*.json")))
            if not cam_files:
                cam_files = sorted(glob.glob(str(ROOT_DIR / "misensorkit_*" / "camera" / "frame_*.json")))
            for idx, jf in enumerate(cam_files[::5], start=1):
                try:
                    d = json.loads(Path(jf).read_text(encoding="utf-8"))
                    self.trajectory.append({"id": idx, "pos": [round(float(v), 4) for v in d["extrinsics"]["t"]]})
                except Exception:
                    pass

    def _init_ml(self) -> None:
        if not HAS_ML or len(self.xyz) == 0:
            orb_npz = self.model_dir / "orb_keyframes.npz"
            if orb_npz.exists():
                try:
                    odata = np.load(orb_npz, allow_pickle=True)
                    names = odata["names"]
                    kps_arr = odata["kps"]
                    des_arr = odata["des"]
                    R_arr = odata["R_arkit"]
                    t_arr = odata["t_arkit"]
                    for i in range(len(names)):
                        self.orb_keyframes.append(
                            {
                                "name": str(names[i]),
                                "kps": np.asarray(kps_arr[i], dtype=np.float32),
                                "des": np.asarray(des_arr[i], dtype=np.uint8),
                                "R_arkit": np.asarray(R_arr[i], dtype=np.float64),
                                "t_arkit": np.asarray(t_arr[i], dtype=np.float64).reshape(3),
                            }
                        )
                    print(f"[VPS] Loaded {len(self.orb_keyframes)} precomputed ORB keyframes from {orb_npz.name}")
                    return
                except Exception as exc:
                    print(f"[VPS] Warning loading {orb_npz}: {exc}")

            print("[VPS] Initializing fast OpenCV ORB keyframe index from '2nd floor' dataset...")
            cam_files = sorted(glob.glob(str(ROOT_DIR / "2nd floor" / "camera" / "frame_*.json")))
            if not cam_files:
                cam_files = sorted(glob.glob(str(ROOT_DIR / "misensorkit_*" / "camera" / "frame_*.json")))
            for jf in cam_files[::12]:
                stem = Path(jf).stem
                img_path = Path(jf).parent.parent / "rgb" / f"{stem}.jpg"
                if not img_path.exists():
                    continue
                im = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
                if im is None:
                    continue
                kps, des = self.orb.detectAndCompute(im, None)
                if des is None or len(kps) < 16:
                    continue
                d = json.loads(Path(jf).read_text(encoding="utf-8"))
                R_arkit = np.array(d["extrinsics"]["R"], dtype=np.float64)
                t_arkit = np.array(d["extrinsics"]["t"], dtype=np.float64).reshape(3)
                self.orb_keyframes.append(
                    {
                        "name": stem,
                        "kps": np.array([kp.pt for kp in kps], dtype=np.float32),
                        "des": des,
                        "R_arkit": R_arkit,
                        "t_arkit": t_arkit,
                    }
                )
            print(f"[VPS] Indexed {len(self.orb_keyframes)} ORB reference keyframes for instant localization.")
            return

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.extractor = SuperPoint(max_num_keypoints=1024).eval().to(self.device)
        self.matcher = LightGlue(features="superpoint").eval().to(self.device)
        self.map_feats_torch = {
            "keypoints": torch.from_numpy(self.keypoints)[None].to(self.device),
            "descriptors": torch.from_numpy(self.descriptors)[None].to(self.device),
            "image_size": torch.tensor(
                [[self.map_image_size[0], self.map_image_size[1]]],
                dtype=torch.float32,
                device=self.device,
            ),
        }
        print(f"[VPS] SuperPoint + LightGlue initialized on {self.device}")

    def localize_jpeg(
        self,
        jpeg_bytes: bytes,
        custom_intrinsics: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """
        Decode grayscale/RGB JPEG frame, run SuperPoint + LightGlue + pycolmap.absolute_pose_estimation(),
        and return 6-DoF ARKit pose + simd_float4x4 alignment transform.
        """
        t0 = time.perf_counter()
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            return {"localized": False, "error": "Invalid JPEG bytes"}

        h, w = gray.shape[:2]
        intr = dict(self.default_intrinsics)
        if custom_intrinsics:
            intr.update(custom_intrinsics)
        else:
            # Scale default intrinsics if incoming frame is 640x480 or different from map size
            sx = w / float(intr["width"])
            sy = h / float(intr["height"])
            intr = {
                "width": w,
                "height": h,
                "fx": intr["fx"] * sx,
                "fy": intr["fy"] * sy,
                "cx": intr["cx"] * sx,
                "cy": intr["cy"] * sy,
            }

        if not HAS_ML or self.extractor is None or self.matcher is None:
            if not self.orb_keyframes:
                return {"localized": False, "error": "ML models not initialized"}
            q_im = cv2.resize(gray, (448, 448))
            q_kps, q_des = self.orb.detectAndCompute(q_im, None)
            if q_des is None or len(q_kps) < 12:
                return {"localized": False, "num_matches": 0, "num_inliers": 0, "latency_ms": 5.0}

            best_kf = None
            best_inliers = 0
            best_matches = 0
            q_pts = np.array([kp.pt for kp in q_kps], dtype=np.float32)

            for kf in self.orb_keyframes:
                raw_m = self.bf.match(q_des, kf["des"])
                good = [m for m in raw_m if m.distance < 52]
                if len(good) > best_matches and len(good) >= 10:
                    src_p = np.float32([q_pts[m.queryIdx] for m in good])
                    dst_p = np.float32([kf["kps"][m.trainIdx] for m in good])
                    _, mask = cv2.findFundamentalMat(src_p, dst_p, cv2.FM_RANSAC, 4.0, 0.99)
                    inl = int(mask.sum()) if mask is not None else len(good)
                    if inl > best_inliers:
                        best_inliers = inl
                        best_matches = len(good)
                        best_kf = kf

            if best_kf is None or best_inliers < 8:
                return {
                    "localized": False,
                    "num_matches": best_matches,
                    "num_inliers": best_inliers,
                    "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                }

            R_wc_arkit = best_kf["R_arkit"]
            t_wc = best_kf["t_arkit"]
            return {
                "localized": True,
                "position": [round(float(c), 4) for c in t_wc],
                "quaternion": rotmat_to_xyzw_quat(R_wc_arkit),
                "simd_float4x4": make_simd_float4x4(R_wc_arkit, t_wc),
                "num_matches": best_matches,
                "num_inliers": best_inliers,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }

        img_tensor = torch.from_numpy(gray / 255.0).float()[None, None].to(self.device)
        with torch.no_grad():
            q_feats = self.extractor.extract(img_tensor)
            matches_out = self.matcher({"image0": q_feats, "image1": self.map_feats_torch})

        matches = matches_out["matches"][0].cpu().numpy()
        num_matches = int(len(matches))
        if num_matches < 10:
            return {
                "localized": False,
                "num_matches": num_matches,
                "num_inliers": 0,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }

        p2d = q_feats["keypoints"][0].cpu().numpy()[matches[:, 0]].astype(np.float64)
        p3d = self.xyz[matches[:, 1]].astype(np.float64)

        R_cw: Optional[np.ndarray] = None
        t_cw: Optional[np.ndarray] = None
        num_inliers = 0

        if HAS_PYCOLMAP:
            camera = pycolmap.Camera(
                model="PINHOLE",
                width=int(intr["width"]),
                height=int(intr["height"]),
                params=[float(intr["fx"]), float(intr["fy"]), float(intr["cx"]), float(intr["cy"])],
            )
            ans = pycolmap.absolute_pose_estimation(p2d, p3d, camera)
            if ans is not None and ans.get("cam_from_world") is not None:
                cfw = ans["cam_from_world"]
                R_cw = cfw.rotation.matrix()
                t_cw = np.array(cfw.translation, dtype=np.float64)
                num_inliers = int(ans.get("num_inliers", num_matches))

        if R_cw is None or t_cw is None:
            # OpenCV RANSAC PnP fallback
            K = np.array(
                [[intr["fx"], 0.0, intr["cx"]], [0.0, intr["fy"], intr["cy"]], [0.0, 0.0, 1.0]],
                dtype=np.float64,
            )
            ok, rvec, tvec, inliers = cv2.solvePnPRansac(
                p3d, p2d, K, None, flags=cv2.SOLVEPNP_EPNP, reprojectionError=8.0
            )
            if not ok or inliers is None or len(inliers) < 8:
                return {
                    "localized": False,
                    "num_matches": num_matches,
                    "num_inliers": 0,
                    "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                }
            R_cw, _ = cv2.Rodrigues(rvec)
            t_cw = tvec.reshape(3)
            num_inliers = int(len(inliers))

        # Convert COLMAP World-to-Camera back to ARKit Camera-to-World
        R_wc_colmap = R_cw.T
        t_wc = -R_wc_colmap @ t_cw
        # Invert portrait basis adjustment: R_arkit = R_wc_colmap @ C_PORTRAIT^-1 (C_PORTRAIT is symmetric involutory!)
        R_wc_arkit = R_wc_colmap @ C_PORTRAIT

        pos_xyz = [round(float(c), 4) for c in t_wc]
        quat_xyzw = rotmat_to_xyzw_quat(R_wc_arkit)
        simd_matrix = make_simd_float4x4(R_wc_arkit, t_wc)

        return {
            "localized": True,
            "position": pos_xyz,
            "quaternion": quat_xyzw,
            "simd_float4x4": simd_matrix,
            "num_matches": num_matches,
            "num_inliers": num_inliers,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }


# =====================================================================
# 3. FASTAPI APPLICATION & WEBSOCKET HUB
# =====================================================================
app = FastAPI(title="Firesight VPS & Routing Brain", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

router_graph = WaypointRouter()
vps_engine = VisualPositioningEngine(FINAL_MODEL_DIR)


class TelemetryHub:
    def __init__(self) -> None:
        self.ar_clients: Set[WebSocket] = set()
        self.dashboard_clients: Set[WebSocket] = set()
        self.firefighter_pos: List[float] = [0.0, 0.0, 0.3]
        self.firefighter_quat: List[float] = [0.0, 0.0, 0.0, 1.0]
        self.current_node: str = "room_232"
        self.mayday_active: bool = False
        self.last_audio_cue: Optional[str] = None
        self.last_route: Dict[str, Any] = router_graph.compute_dijkstra(self.firefighter_pos)

    def get_full_state(self, include_pointcloud: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": "state_sync",
            "firefighter": {
                "position": self.firefighter_pos,
                "quaternion": self.firefighter_quat,
                "current_node": self.last_route["start_node"],
                "current_label": self.last_route["start_label"],
                "mayday": self.mayday_active,
            },
            "route": self.last_route,
            "nodes": list(router_graph.nodes.values()),
            "edges": router_graph.edges,
            "audio_cue": self.last_audio_cue,
            "timestamp": time.time(),
        }
        if include_pointcloud:
            payload["pointcloud"] = [
                [round(float(p[0]), 3), round(float(p[1]), 3), round(float(p[2]), 3)]
                for p in vps_engine.xyz
            ]
            payload["trajectory"] = vps_engine.trajectory
        return payload

    async def broadcast_dashboards(self, message: Dict[str, Any]) -> None:
        dead: List[WebSocket] = []
        for ws in self.dashboard_clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.dashboard_clients.discard(ws)

    async def broadcast_ar_clients(self, message: Dict[str, Any]) -> None:
        dead: List[WebSocket] = []
        for ws in self.ar_clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ar_clients.discard(ws)


hub = TelemetryHub()


@app.get("/api/state")
async def get_state() -> Dict[str, Any]:
    """REST endpoint returning full 3D map, waypoint graph, and active route."""
    return hub.get_full_state(include_pointcloud=True)


@app.post("/api/block/{node_id}")
async def block_node_rest(node_id: str) -> Dict[str, Any]:
    """REST endpoint to toggle a waypoint's Blocked status and trigger Dijkstra recalculation."""
    blocked = router_graph.toggle_block(node_id)
    hub.last_route = router_graph.compute_dijkstra(hub.firefighter_pos)
    hub.last_audio_cue = "hazard_ahead_rerouting.mp3" if blocked else "route_recalculated.mp3"

    state = hub.get_full_state(include_pointcloud=False)
    await hub.broadcast_dashboards(state)
    await hub.broadcast_ar_clients(
        {
            "type": "route_update",
            "route": hub.last_route,
            "audio_cue": hub.last_audio_cue,
            "blocked_node": node_id,
            "is_blocked": blocked,
        }
    )
    return state


@app.websocket("/ws/dashboard")
async def ws_dashboard_endpoint(ws: WebSocket) -> None:
    """
    WebSocket for the React + Three.js Commander Dashboard.
    Streams live 3D map, firefighter position, and handles interactive waypoint blocking.
    """
    await ws.accept()
    hub.dashboard_clients.add(ws)
    try:
        # Send full initial state including the 658 3D map landmarks & camera trajectory
        await ws.send_json(hub.get_full_state(include_pointcloud=True))

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "toggle_block":
                node_id = msg["node_id"]
                blocked = router_graph.toggle_block(node_id, msg.get("blocked"))
                hub.last_route = router_graph.compute_dijkstra(hub.firefighter_pos)
                hub.last_audio_cue = "hazard_ahead_rerouting.mp3" if blocked else "route_recalculated.mp3"

                await hub.broadcast_dashboards(hub.get_full_state(include_pointcloud=False))
                await hub.broadcast_ar_clients(
                    {
                        "type": "route_update",
                        "route": hub.last_route,
                        "audio_cue": hub.last_audio_cue,
                        "blocked_node": node_id,
                        "is_blocked": blocked,
                    }
                )

            elif mtype == "simulate_move":
                # Allows testing firefighter movement directly from the Commander Dashboard
                pos = msg.get("position", [0.0, 0.0, 0.3])
                hub.firefighter_pos = [round(float(x), 3) for x in pos]
                prev_node = hub.last_route["start_node"]
                hub.last_route = router_graph.compute_dijkstra(hub.firefighter_pos)
                if hub.last_route["start_node"] != prev_node:
                    if hub.last_route["start_node"] == hub.last_route["target_exit"]:
                        hub.last_audio_cue = "arrived_at_exit.mp3"
                    else:
                        hub.last_audio_cue = "proceed_to_next_waypoint.mp3"
                await hub.broadcast_dashboards(hub.get_full_state(include_pointcloud=False))
                await hub.broadcast_ar_clients(
                    {
                        "type": "pose_and_route",
                        "localized": True,
                        "position": hub.firefighter_pos,
                        "quaternion": hub.firefighter_quat,
                        "route": hub.last_route,
                        "audio_cue": hub.last_audio_cue,
                    }
                )

            elif mtype == "update_waypoint":
                router_graph.update_node_pos(msg["node_id"], msg["pos"])
                hub.last_route = router_graph.compute_dijkstra(hub.firefighter_pos)
                await hub.broadcast_dashboards(hub.get_full_state(include_pointcloud=False))

            elif mtype == "clear_mayday":
                hub.mayday_active = False
                hub.last_audio_cue = "mayday_acknowledged.mp3"
                await hub.broadcast_dashboards(hub.get_full_state(include_pointcloud=False))
                await hub.broadcast_ar_clients({"type": "audio_trigger", "audio_cue": hub.last_audio_cue})

    except WebSocketDisconnect:
        hub.dashboard_clients.discard(ws)


@app.websocket("/ws/ar")
async def ws_ar_endpoint(ws: WebSocket) -> None:
    """
    WebSocket for the iOS AR Client (Swift + ARKit).
    Receives either:
      - Raw binary compressed 640x480 grayscale JPEG frames
      - JSON messages: {"type": "frame", "image_b64": "...", "intrinsics": {...}}
      - JSON messages: {"type": "mayday", "active": true}
    Responds with 6-DoF pose, simd_float4x4 alignment transform, Dijkstra 3D path, and MP3 audio flag.
    """
    await ws.accept()
    hub.ar_clients.add(ws)
    try:
        # Send initial route immediately on connect
        await ws.send_json(
            {
                "type": "init",
                "route": hub.last_route,
                "nodes": list(router_graph.nodes.values()),
                "audio_cue": "system_online.mp3",
            }
        )

        while True:
            packet = await ws.receive()
            jpeg_bytes: Optional[bytes] = None
            intrinsics: Optional[Dict[str, float]] = None

            if packet.get("bytes") is not None:
                jpeg_bytes = packet["bytes"]
            elif packet.get("text") is not None:
                msg = json.loads(packet["text"])
                mtype = msg.get("type", "frame")

                if mtype == "mayday":
                    hub.mayday_active = bool(msg.get("active", True))
                    hub.last_audio_cue = "mayday_transmitting.mp3"
                    await hub.broadcast_dashboards(hub.get_full_state(include_pointcloud=False))
                    await ws.send_json(
                        {
                            "type": "mayday_ack",
                            "mayday": hub.mayday_active,
                            "audio_cue": "mayday_transmitting.mp3",
                        }
                    )
                    continue

                if "image_b64" in msg:
                    b64_str = msg["image_b64"].split(",")[-1]
                    jpeg_bytes = base64.b64decode(b64_str)
                    intrinsics = msg.get("intrinsics")

            if jpeg_bytes is None:
                continue

            # Run SuperPoint + LightGlue + pycolmap PnP in threadpool to keep event loop non-blocking
            loc = await asyncio.to_thread(vps_engine.localize_jpeg, jpeg_bytes, intrinsics)

            audio_cue: Optional[str] = None
            if loc.get("localized"):
                prev_node = hub.last_route["start_node"]
                hub.firefighter_pos = loc["position"]
                hub.firefighter_quat = loc["quaternion"]
                hub.last_route = router_graph.compute_dijkstra(hub.firefighter_pos)

                curr_node = hub.last_route["start_node"]
                if curr_node != prev_node:
                    if curr_node == hub.last_route["target_exit"]:
                        audio_cue = "arrived_at_exit.mp3"
                    else:
                        audio_cue = "proceed_to_next_waypoint.mp3"
                hub.last_audio_cue = audio_cue

            response = {
                "type": "pose_and_route",
                **loc,
                "current_node": hub.last_route["start_node"],
                "current_label": hub.last_route["start_label"],
                "route": hub.last_route,
                "audio_cue": audio_cue,
            }
            await ws.send_json(response)
            await hub.broadcast_dashboards(hub.get_full_state(include_pointcloud=False))

    except WebSocketDisconnect:
        hub.ar_clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
