"""Visual positioning and the phone / commander sockets.

make_router(get_world, lock) mounts /ws/ar and /ws/dashboard.
vps_locate(jpeg_bytes) is the camera fix for the particle filter:
{level, x, y, heading, confidence} or None.

Model files live in compute/final_model. torch, lightglue, and pycolmap
load on first use. If they are missing, ORB keyframes still localize;
if that cannot run either, /ws/ar keeps routing from positions the phone sends
and reports VPS as unavailable.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import json
import math
import re
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.routing import best_room_cell
from . import live

_REPO = Path(__file__).resolve().parents[2]
_COMPUTE = _REPO / "data" / "vps"
MODEL_DIR = Path(__file__).resolve().parents[2] / "data" / "vps" / "final_model"

# ARKit scan "2nd floor part 2" (origin in DCC 232) is 1:1 meters with the plan.
ARKIT_TO_DCC_X0 = 40.1
ARKIT_TO_DCC_Y0 = 60.0
ARKIT_DEFAULT_LEVEL = 2
ARKIT_FLOOR_M = 4.0

_LEVEL_RE = re.compile(r"^[Ff](\d+)")

C_PORTRAIT = np.array(
    [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]],
    dtype=np.float64,
)

_engine = None
_engine_lock = threading.RLock()
_cv2_mod = None
_cv2_tried = False
_ml_mods = None
_pycolmap_mod = None
_pycolmap_tried = False

_POSE_KEYS = ("position", "quaternion", "heading", "x", "y", "level", "dcc_xy")
_REPORT_KINDS = ("fire", "smoke", "clear", "searched", "blocked", "open")
_LIVE_KINDS = ("fire", "report", "people", "building")
_MAX_JPEG = 8_000_000


def _cv2():
    global _cv2_mod, _cv2_tried
    if not _cv2_tried:
        _cv2_tried = True
        try:
            import cv2
            _cv2_mod = cv2
        except Exception:
            _cv2_mod = None
    return _cv2_mod


def _ml():
    """(torch, LightGlue, SuperPoint) or (None, None, None). Imported once."""
    global _ml_mods
    if _ml_mods is None:
        try:
            import torch
            from lightglue import LightGlue, SuperPoint
            _ml_mods = (torch, LightGlue, SuperPoint)
        except Exception:
            _ml_mods = (None, None, None)
    return _ml_mods


def _pycolmap():
    global _pycolmap_mod, _pycolmap_tried
    if not _pycolmap_tried:
        _pycolmap_tried = True
        try:
            import pycolmap
            _pycolmap_mod = pycolmap
        except Exception:
            _pycolmap_mod = None
    return _pycolmap_mod


def arkit_to_dcc_plan(pos_xyz) -> tuple[int, float, float]:
    """ARKit [X, Y, Z] meters -> (level, plan x, plan y). Z maps to plan y."""
    x = float(pos_xyz[0]) if len(pos_xyz) > 0 else 0.0
    z = float(pos_xyz[2]) if len(pos_xyz) > 2 else 0.0
    return (
        ARKIT_DEFAULT_LEVEL,
        round(ARKIT_TO_DCC_X0 + x, 3),
        round(ARKIT_TO_DCC_Y0 + z, 3),
    )


def dcc_plan_to_arkit(x_plan: float, y_plan: float, level: int = 2) -> list[float]:
    """Plan meters -> ARKit [X, Y, Z]. Each floor above 2 is +4 m on Y."""
    y_arkit = (int(level) - ARKIT_DEFAULT_LEVEL) * ARKIT_FLOOR_M
    return [
        round(float(x_plan) - ARKIT_TO_DCC_X0, 3),
        round(float(y_arkit), 3),
        round(float(y_plan) - ARKIT_TO_DCC_Y0, 3),
    ]


def _level_from_room(room_id, default=ARKIT_DEFAULT_LEVEL) -> int:
    if not isinstance(room_id, str):
        return default
    m = _LEVEL_RE.match(room_id)
    return int(m.group(1)) if m else default


def _heading_deg(quat) -> float:
    """Plan bearing: 0 is up (-y), clockwise. Same yaw the phone uses for its arrow."""
    if not isinstance(quat, (list, tuple)) or len(quat) != 4:
        return 0.0
    try:
        qx, qy, qz, qw = (float(v) for v in quat)
    except (TypeError, ValueError):
        return 0.0
    look_x = -(2.0 * (qx * qz + qw * qy))
    look_z = -(1.0 - 2.0 * (qx * qx + qy * qy))
    if look_x * look_x + look_z * look_z < 1e-12:
        return 0.0
    return round(math.degrees(math.atan2(look_x, -look_z)) % 360.0, 1)


def _confidence(loc) -> float:
    inl = int(loc.get("num_inliers") or 0)
    return round(min(1.0, max(0.0, inl / 20.0)), 3)


def _as_str(v):
    if v is None:
        return None
    s = str(v)
    if s in ("None", "nan"):
        return None
    return s


def rotmat_to_xyzw_quat(R: np.ndarray) -> list[float]:
    """3x3 rotation -> [qx, qy, qz, qw]."""
    U, _, Vt = np.linalg.svd(R)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U = U.copy()
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


def make_simd_float4x4(R_wc: np.ndarray, t_wc: np.ndarray) -> list[float]:
    """Column-major 16 floats (Swift simd_float4x4)."""
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = R_wc
    T[:3, 3] = np.asarray(t_wc, dtype=np.float64).reshape(3)
    return [round(float(x), 6) for x in T.T.flatten()]


def crop_square_448(bgr_img: np.ndarray) -> np.ndarray:
    """Center-crop to a square, then 448x448. Matches the keyframe index."""
    cv2 = _cv2()
    h, w = bgr_img.shape[:2]
    if h != w:
        s = min(h, w)
        y0 = (h - s) // 2
        x0 = (w - s) // 2
        bgr_img = bgr_img[y0:y0 + s, x0:x0 + s]
    if bgr_img.shape[0] != 448 or bgr_img.shape[1] != 448:
        bgr_img = cv2.resize(bgr_img, (448, 448), interpolation=cv2.INTER_AREA)
    return bgr_img


def compute_global_descriptor(bgr_448: np.ndarray) -> np.ndarray:
    """876-D appearance vector. Must match the precomputed keyframe `gdes` rows."""
    cv2 = _cv2()
    lab = cv2.cvtColor(bgr_448, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    norm_bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    small = cv2.resize(norm_bgr, (64, 64), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV).astype(np.float32) / 255.0
    rgb = small.astype(np.float32) / 255.0
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.clip(np.hypot(gx, gy), 0.0, 1.0)
    hsv_edge = np.dstack([hsv[:, :, 1:3], mag[:, :, None]])
    grid_rgb = cv2.resize(rgb, (12, 12), interpolation=cv2.INTER_AREA).reshape(-1)
    grid_he = cv2.resize(hsv_edge, (12, 12), interpolation=cv2.INTER_AREA).reshape(-1)
    feat_stack = np.dstack([rgb, hsv_edge])
    floor_patch = feat_stack[42:, :, :].mean(axis=(0, 1)) * 3.0
    mid_patch = feat_stack[20:42, :, :].mean(axis=(0, 1)) * 2.0
    vec = np.concatenate([grid_rgb, grid_he, floor_patch, mid_patch]).astype(np.float32)
    norm = float(np.linalg.norm(vec)) + 1e-8
    return vec / norm


def _unavailable(error="unavailable", t0=None) -> dict:
    latency = 0.0 if t0 is None else round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "localized": False,
        "error": error,
        "num_matches": 0,
        "num_inliers": 0,
        "latency_ms": latency,
    }


class _DownEngine:
    status = "unavailable"
    xyz = np.zeros((0, 3), dtype=np.float64)
    trajectory: list = []

    def localize_jpeg(self, jpeg_bytes, custom_intrinsics=None, prev_dcc_xy=None) -> dict:
        return _unavailable()


class VisualPositioningEngine:
    """SuperPoint + LightGlue against compute/final_model, else calibrated ORB keyframes."""

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = Path(model_dir)
        self.status = "unavailable"
        self.recon = None
        self.xyz = np.zeros((0, 3), dtype=np.float64)
        self.rgb = np.zeros((0, 3), dtype=np.uint8)
        self.descriptors = np.zeros((0, 256), dtype=np.float32)
        self.keypoints = np.zeros((0, 2), dtype=np.float32)
        self.map_image_size = (448, 448)
        self.default_intrinsics = {
            "width": 448, "height": 448, "fx": 470.0, "fy": 470.0, "cx": 224.0, "cy": 224.0,
        }
        self.trajectory = []
        self.device = None
        self.extractor = None
        self.matcher = None
        self.map_feats_torch = None
        self.orb = None
        self.bf = None
        self.orb_keyframes = []
        self.gdes_matrix = None
        cv2 = _cv2()
        if cv2 is not None:
            self.orb = cv2.ORB_create(nfeatures=600)
            self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self._load_map()
        self._init_backend()

    def _load_map(self) -> None:
        npz_path = self.model_dir / "map_descriptors.npz"
        if npz_path.is_file():
            data = np.load(npz_path)
            self.xyz = data["xyz"].astype(np.float64)
            self.rgb = data["rgb"].astype(np.uint8)
            self.descriptors = data["descriptors"].astype(np.float32).T
            self.keypoints = data["keypoints"].astype(np.float32)
            img_sz = data["image_size"]
            self.map_image_size = (int(img_sz[0]), int(img_sz[1]))
            print(f"[VPS] Loaded {len(self.xyz)} 3D landmarks from {npz_path}")

        pycolmap = _pycolmap()
        if pycolmap is not None and (self.model_dir / "cameras.bin").is_file():
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
                print(f"[VPS] pycolmap.Reconstruction load warning: {exc}")

        orb_npz = self.model_dir / "orb_keyframes.npz"
        if orb_npz.is_file():
            try:
                odata = np.load(orb_npz, allow_pickle=True)
                if "trajectory" in odata:
                    for idx, pos in enumerate(odata["trajectory"], start=1):
                        self.trajectory.append({"id": idx, "pos": [round(float(v), 4) for v in pos]})
            except Exception as exc:
                print(f"[VPS] Warning loading trajectory from {orb_npz}: {exc}")

        if self.trajectory:
            return
        cam_files = _camera_frames()
        for idx, jf in enumerate(cam_files[::5], start=1):
            try:
                d = json.loads(Path(jf).read_text(encoding="utf-8"))
                self.trajectory.append({"id": idx, "pos": [round(float(v), 4) for v in d["extrinsics"]["t"]]})
            except Exception:
                pass

    def _load_orb_npz(self) -> bool:
        orb_npz = self.model_dir / "orb_keyframes.npz"
        if not orb_npz.is_file():
            return False
        try:
            odata = np.load(orb_npz, allow_pickle=True)
            names = odata["names"]
            kps_arr = odata["kps"]
            des_arr = odata["des"]
            R_arr = odata["R_arkit"]
            t_arr = odata["t_arkit"]
            gdes_arr = odata["gdes"] if "gdes" in odata.files else None
            room_ids_arr = odata["room_ids"] if "room_ids" in odata.files else None
            room_labels_arr = odata["room_labels"] if "room_labels" in odata.files else None
            dcc_xys_arr = odata["dcc_xys"] if "dcc_xys" in odata.files else None
            frames = []
            for i in range(len(names)):
                t_vec = np.asarray(t_arr[i], dtype=np.float64).reshape(3)
                _, def_x, def_y = arkit_to_dcc_plan(t_vec.tolist())
                frames.append({
                    "name": str(names[i]),
                    "kps": np.asarray(kps_arr[i], dtype=np.float32),
                    "des": np.asarray(des_arr[i], dtype=np.uint8),
                    "gdes": np.asarray(gdes_arr[i], dtype=np.float32) if gdes_arr is not None else None,
                    "R_arkit": np.asarray(R_arr[i], dtype=np.float64),
                    "t_arkit": t_vec,
                    "room_id": None if room_ids_arr is None else _as_str(room_ids_arr[i]),
                    "room_label": None if room_labels_arr is None else _as_str(room_labels_arr[i]),
                    "dcc_xy": (
                        [float(dcc_xys_arr[i][0]), float(dcc_xys_arr[i][1])]
                        if dcc_xys_arr is not None else [def_x, def_y]
                    ),
                })
            self.orb_keyframes = frames
            if gdes_arr is not None:
                self.gdes_matrix = np.asarray(gdes_arr, dtype=np.float32)
            print(f"[VPS] Loaded {len(frames)} ORB keyframes from {orb_npz.name}")
            return len(frames) > 0
        except Exception as exc:
            print(f"[VPS] Warning loading {orb_npz}: {exc}")
            return False

    def _scan_orb_images(self) -> bool:
        cv2 = _cv2()
        if cv2 is None or self.orb is None:
            return False
        print("[VPS] Building ORB keyframe index from the walk dataset...")
        for jf in _camera_frames()[::10]:
            stem = Path(jf).stem
            img_path = Path(jf).parent.parent / "rgb" / f"{stem}.jpg"
            if not img_path.is_file():
                continue
            bgr = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            bgr_448 = cv2.resize(bgr, (448, 448))
            im = cv2.cvtColor(bgr_448, cv2.COLOR_BGR2GRAY)
            kps, des = self.orb.detectAndCompute(im, None)
            if des is None or len(kps) < 16:
                continue
            try:
                d = json.loads(Path(jf).read_text(encoding="utf-8"))
                R_arkit = np.array(d["extrinsics"]["R"], dtype=np.float64)
                t_arkit = np.array(d["extrinsics"]["t"], dtype=np.float64).reshape(3)
            except Exception:
                continue
            _, def_x, def_y = arkit_to_dcc_plan(t_arkit.tolist())
            self.orb_keyframes.append({
                "name": stem,
                "kps": np.array([kp.pt for kp in kps], dtype=np.float32),
                "des": des,
                "gdes": compute_global_descriptor(bgr_448),
                "R_arkit": R_arkit,
                "t_arkit": t_arkit,
                "room_id": None,
                "room_label": None,
                "dcc_xy": [def_x, def_y],
            })
        if self.orb_keyframes and self.gdes_matrix is None:
            rows = [kf["gdes"] for kf in self.orb_keyframes if kf.get("gdes") is not None]
            if len(rows) == len(self.orb_keyframes):
                self.gdes_matrix = np.stack(rows).astype(np.float32)
        print(f"[VPS] Indexed {len(self.orb_keyframes)} ORB reference keyframes.")
        return len(self.orb_keyframes) > 0

    def _init_backend(self) -> None:
        torch, LightGlue, SuperPoint = _ml()
        if torch is None or len(self.xyz) == 0 or _cv2() is None:
            if _cv2() is not None and (self._load_orb_npz() or self._scan_orb_images()):
                self.status = "orb"
                return
            self.status = "unavailable"
            print("[VPS] unavailable (SuperPoint/LightGlue missing and no ORB index)")
            return
        try:
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
            self.status = "superpoint"
            print(f"[VPS] SuperPoint + LightGlue initialized on {self.device}")
        except Exception as exc:
            print(f"[VPS] SuperPoint init failed: {exc}")
            self.extractor = None
            self.matcher = None
            if self._load_orb_npz() or self._scan_orb_images():
                self.status = "orb"
            else:
                self.status = "unavailable"

    def localize_jpeg(self, jpeg_bytes, custom_intrinsics=None, prev_dcc_xy=None) -> dict:
        t0 = time.perf_counter()
        if self.status == "unavailable":
            return _unavailable(t0=t0)
        cv2 = _cv2()
        if cv2 is None:
            return _unavailable(t0=t0)
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return _unavailable("Invalid JPEG bytes", t0)

        h, w = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        intr = dict(self.default_intrinsics)
        if custom_intrinsics:
            intr.update(custom_intrinsics)
        else:
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

        if self.status != "superpoint" or self.extractor is None or self.matcher is None:
            return self._localize_orb(bgr, prev_dcc_xy, t0)

        torch, _, _ = _ml()
        img_tensor = torch.from_numpy(gray / 255.0).float()[None, None].to(self.device)
        with torch.no_grad():
            q_feats = self.extractor.extract(img_tensor)
            matches_out = self.matcher({"image0": q_feats, "image1": self.map_feats_torch})
        matches = matches_out["matches"][0].detach().cpu().numpy()
        num_matches = int(len(matches))
        if num_matches < 10:
            return {
                "localized": False,
                "num_matches": num_matches,
                "num_inliers": 0,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }

        p2d = q_feats["keypoints"][0].detach().cpu().numpy()[matches[:, 0]].astype(np.float64)
        p3d = self.xyz[matches[:, 1]].astype(np.float64)
        R_cw = None
        t_cw = None
        num_inliers = 0
        pycolmap = _pycolmap()
        if pycolmap is not None:
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
            K = np.array(
                [[intr["fx"], 0.0, intr["cx"]], [0.0, intr["fy"], intr["cy"]], [0.0, 0.0, 1.0]],
                dtype=np.float64,
            )
            ok, rvec, tvec, inliers = cv2.solvePnPRansac(
                p3d, p2d, K, None, flags=cv2.SOLVEPNP_EPNP, reprojectionError=8.0,
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

        R_wc_colmap = R_cw.T
        t_wc = -R_wc_colmap @ t_cw
        R_wc_arkit = R_wc_colmap @ C_PORTRAIT
        return {
            "localized": True,
            "position": [round(float(c), 4) for c in t_wc],
            "quaternion": rotmat_to_xyzw_quat(R_wc_arkit),
            "simd_float4x4": make_simd_float4x4(R_wc_arkit, t_wc),
            "num_matches": num_matches,
            "num_inliers": num_inliers,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    def _localize_orb(self, bgr, prev_dcc_xy, t0) -> dict:
        cv2 = _cv2()
        if not self.orb_keyframes or self.orb is None or self.bf is None:
            return _unavailable(t0=t0)
        bgr_448 = crop_square_448(bgr)
        q_im = cv2.cvtColor(bgr_448, cv2.COLOR_BGR2GRAY)
        q_kps, q_des = self.orb.detectAndCompute(q_im, None)
        if q_des is None or len(q_kps) < 10:
            return {
                "localized": False,
                "num_matches": 0,
                "num_inliers": 0,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }
        q_pts = np.array([kp.pt for kp in q_kps], dtype=np.float32)
        q_gdes = compute_global_descriptor(bgr_448)
        n = len(self.orb_keyframes)
        if self.gdes_matrix is not None and len(self.gdes_matrix) == n:
            global_sims = self.gdes_matrix @ q_gdes
        else:
            global_sims = np.full(n, 0.8, dtype=np.float32)

        if prev_dcc_xy is not None:
            px, py = float(prev_dcc_xy[0]), float(prev_dcc_xy[1])
            dists = np.array(
                [math.hypot(kf["dcc_xy"][0] - px, kf["dcc_xy"][1] - py) for kf in self.orb_keyframes],
                dtype=np.float32,
            )
            dist_bonus = np.clip((10.5 - dists) * 0.65, 0.0, 6.8)
        else:
            dist_bonus = np.zeros(n, dtype=np.float32)

        order = np.argsort(global_sims + dist_bonus / 40.0)[::-1][:32]
        cand = {int(i) for i in order}
        by_room: dict[str, list[int]] = {}
        for idx_kf, kf in enumerate(self.orb_keyframes):
            by_room.setdefault(kf.get("room_id", "F2-232"), []).append(idx_kf)
        for indices in by_room.values():
            arr_idx = np.array(indices, dtype=np.int32)
            top_r = arr_idx[np.argsort(global_sims[arr_idx])[::-1][:5]]
            for i_r in top_r:
                cand.add(int(i_r))

        best_kf = None
        best_score = -1.0
        best_inliers = 0
        best_matches = 0
        for i_int in cand:
            kf = self.orb_keyframes[i_int]
            des = kf.get("des")
            if des is None or len(des) == 0:
                continue
            raw_m = self.bf.match(q_des, des)
            good = [m for m in raw_m if m.distance < 54]
            inl = 0
            if len(good) >= 8:
                src_p = np.float32([q_pts[m.queryIdx] for m in good])
                dst_p = np.float32([kf["kps"][m.trainIdx] for m in good])
                _, mask = cv2.findFundamentalMat(src_p, dst_p, cv2.FM_RANSAC, 4.5, 0.99)
                inl = int(mask.sum()) if mask is not None else len(good)
            g_sim = float(global_sims[i_int])
            bonus = float(dist_bonus[i_int])
            score = float(inl) * 1.15 + 32.0 * g_sim + 0.22 * len(good) + bonus
            if score > best_score and (inl >= 7 or g_sim >= 0.91):
                best_score = score
                best_inliers = max(inl, int(round(g_sim * 15)))
                best_matches = len(good)
                best_kf = kf

        latency = round((time.perf_counter() - t0) * 1000.0, 1)
        if best_kf is None:
            return {"localized": False, "num_matches": best_matches, "num_inliers": best_inliers, "latency_ms": latency}

        t_wc = best_kf["t_arkit"]
        R_wc = best_kf["R_arkit"]
        return {
            "localized": True,
            "position": [round(float(c), 4) for c in t_wc],
            "quaternion": rotmat_to_xyzw_quat(R_wc),
            "simd_float4x4": make_simd_float4x4(R_wc, t_wc),
            "room_id": best_kf.get("room_id"),
            "room_label": best_kf.get("room_label"),
            "dcc_xy": best_kf.get("dcc_xy"),
            "num_matches": best_matches,
            "num_inliers": best_inliers,
            "latency_ms": latency,
        }


def _camera_frames() -> list[str]:
    found = sorted(glob.glob(str(_COMPUTE / "2nd floor part 2" / "camera" / "frame_*.json")))
    if not found:
        found = sorted(glob.glob(str(_COMPUTE / "2nd floor" / "camera" / "frame_*.json")))
    return found


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            try:
                _engine = VisualPositioningEngine(MODEL_DIR)
            except Exception as exc:
                print(f"[VPS] init failed: {exc}")
                _engine = _DownEngine()
        return _engine


def _localize_locked(jpeg_bytes, intrinsics, prev_dcc_xy) -> dict:
    try:
        with _engine_lock:
            eng = _get_engine()
            return eng.localize_jpeg(jpeg_bytes, intrinsics, prev_dcc_xy)
    except Exception as exc:
        return _unavailable(str(exc)[:200])


def _pose_plan(loc) -> tuple[int, float, float]:
    room_id = loc.get("room_id")
    level = _level_from_room(room_id) if room_id else ARKIT_DEFAULT_LEVEL
    xy = loc.get("dcc_xy")
    if isinstance(xy, (list, tuple)) and len(xy) >= 2:
        return level, round(float(xy[0]), 3), round(float(xy[1]), 3)
    pos = loc.get("position") or [0.0, 0.0, 0.0]
    lv, x, y = arkit_to_dcc_plan(pos)
    if room_id:
        lv = level
    return lv, x, y


def vps_locate(jpeg_bytes):
    """Camera fix in plan meters. None when VPS cannot lock this frame."""
    if not isinstance(jpeg_bytes, (bytes, bytearray)) or len(jpeg_bytes) < 16:
        return None
    try:
        with _engine_lock:
            eng = _get_engine()
            if getattr(eng, "status", "unavailable") == "unavailable":
                return None
            loc = eng.localize_jpeg(bytes(jpeg_bytes), None, None)
    except Exception:
        return None
    if not isinstance(loc, dict) or not loc.get("localized"):
        return None
    try:
        level, x, y = _pose_plan(loc)
        heading = _heading_deg(loc.get("quaternion"))
        conf = _confidence(loc)
        nums = (float(level), float(x), float(y), float(heading), float(conf))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in nums):
        return None
    return {
        "level": int(level),
        "x": round(float(x), 3),
        "y": round(float(y), 3),
        "heading": round(float(heading) % 360.0, 1),
        "confidence": round(float(conf), 3),
    }


def _find_room(world, room_id):
    if not room_id:
        return None
    g = world.grid
    info = g.room_by_id.get(room_id)
    if info is not None:
        return info
    key = str(room_id).strip()
    up = key.upper()
    for cand in g.rooms:
        if cand["id"].upper() == up:
            return cand
        head = (cand.get("label") or "").split(" ")[0]
        if head.upper() == up:
            return cand
    matches = [r for r in g.rooms if (r.get("label") or "").split(" ")[0].lower() == key.lower()]
    matches.sort(key=lambda r: "lower tier" in (r.get("label") or "").lower())
    return matches[0] if matches else None


def _locate_room(world, level, x, y) -> dict:
    g = world.grid
    fi = g.level_index.get(level, g.level_index.get(ARKIT_DEFAULT_LEVEL, 0))
    r, c = g.cell_of(x, y)
    if 0 <= r < g.rows and 0 <= c < g.cols:
        ridx = int(g.room[fi, r, c])
        if ridx >= 0:
            info = g.rooms[ridx]
            return {"room_id": info["id"], "label": info["label"], "level": info["level"], "type": info["type"]}
    cell = g.nearest_walkable(fi, x, y, max_dist=10.0)
    if cell is not None:
        ridx = int(g.room[fi, cell[0], cell[1]])
        if ridx >= 0:
            info = g.rooms[ridx]
            return {"room_id": info["id"], "label": info["label"], "level": info["level"], "type": info["type"]}
    best = None
    best_d = float("inf")
    for info in g.rooms:
        if info["level"] != level or info.get("type") == "void":
            continue
        cx, cy = info["centroid"]
        d = math.hypot(x - cx, y - cy)
        if d < best_d:
            best_d = d
            best = info
    if best is None:
        return {"room_id": "unknown", "label": "unknown", "level": level, "type": "unknown"}
    return {"room_id": best["id"], "label": best["label"], "level": best["level"], "type": best["type"]}


def _floor_index(world, level) -> int:
    g = world.grid
    if level in g.level_index:
        return g.level_index[level]
    if ARKIT_DEFAULT_LEVEL in g.level_index:
        return g.level_index[ARKIT_DEFAULT_LEVEL]
    return 0


def _empty_route(reason: str) -> dict:
    return {
        "reachable": False,
        "reason": reason,
        "path": [],
        "distance_m": None,
        "instructions": [],
        "max_smoke": 0.0,
    }


def _build_route(world, session) -> dict:
    """Occupant: route_from (any exit). Firefighter with a target: route_between to best_room_cell."""
    g = world.grid
    level = int(getattr(session, "level", None) or ARKIT_DEFAULT_LEVEL)
    xy = getattr(session, "dcc_xy", None)
    if isinstance(xy, (list, tuple)) and len(xy) >= 2:
        x, y = float(xy[0]), float(xy[1])
    else:
        level, x, y = arkit_to_dcc_plan(session.arkit_pos)
    fi = _floor_index(world, level)
    if fi < len(g.levels):
        level = int(g.levels[fi]) if level not in g.level_index else level
    loc_info = _locate_room(world, level, x, y)
    override = _find_room(world, getattr(session, "last_room_id", None))
    if override is not None:
        loc_info = {
            "room_id": override["id"],
            "label": override["label"],
            "level": override["level"],
            "type": override.get("type"),
        }

    role = getattr(session, "role", None) or "occupant"
    if role not in ("occupant", "firefighter"):
        role = "occupant"
    target_id = getattr(session, "target_room_id", None)
    try:
        statuses = world.sim.room_status()
    except Exception:
        statuses = {}
    fire_rooms = []
    smoke_rooms = []
    for info in g.rooms:
        st = statuses.get(info["id"])
        if st not in ("fire", "smoke"):
            continue
        cx, cy = info["centroid"]
        row = {
            "id": info["id"],
            "label": info["label"],
            "level": info["level"],
            "centroid": [round(float(cx), 2), round(float(cy), 2)],
        }
        if st == "fire":
            fire_rooms.append(row)
        else:
            smoke_rooms.append(row)

    target_label = "Closest Safe Exit"
    raw = _empty_route("no route")
    try:
        if role == "firefighter" and target_id:
            goal = _find_room(world, target_id)
            if goal is None:
                raw = _empty_route("unknown target room")
                target_label = str(target_id)
            else:
                target_label = goal.get("label") or str(target_id)
                gfi, cell = best_room_cell(world.field, goal)
                if cell is None:
                    raw = _empty_route("target room has no walkable floor")
                else:
                    gx, gy = g.center_of(*cell)
                    raw = world.field.route_between(fi, x, y, gfi, gx, gy)
        else:
            raw = world.field.route_from(fi, x, y)
            target_label = raw.get("exit") or "Closest Safe Exit"
    except Exception as exc:
        raw = _empty_route(str(exc)[:160])

    dcc_path = []
    for pt in raw.get("path") or []:
        if isinstance(pt, (list, tuple)) and len(pt) >= 3:
            dcc_path.append([pt[0], round(float(pt[1]), 2), round(float(pt[2]), 2)])
    coords = [dcc_plan_to_arkit(pt[1], pt[2], pt[0]) for pt in dcc_path]
    if not coords:
        coords = [list(session.arkit_pos)]
    dist = raw.get("distance_m")
    if dist is not None:
        dist = round(float(dist), 1)
    smoke = raw.get("max_smoke")
    smoke = 0.0 if smoke is None else round(float(smoke), 2)
    return {
        "role": role,
        "start_node": loc_info["room_id"],
        "start_label": loc_info["label"],
        "level": loc_info["level"],
        "x_plan": round(float(x), 3),
        "y_plan": round(float(y), 3),
        "target_exit": target_label,
        "target_room_id": target_id if role == "firefighter" else None,
        "path": dcc_path,
        "path_coords": coords,
        "total_distance_m": dist,
        "distance_m": dist,
        "max_smoke": smoke,
        "instructions": list(raw.get("instructions") or []),
        "reachable": bool(raw.get("reachable", False)),
        "reason": raw.get("reason") or "",
        "fire_rooms": fire_rooms,
        "smoke_rooms": smoke_rooms,
    }


def _floor_nodes(world) -> list[dict]:
    """Floor 2 rooms in ARKit meters, the list the phone map already draws."""
    try:
        statuses = world.sim.room_status()
    except Exception:
        statuses = {}
    searched = getattr(world, "searched", {}) or {}
    out = []
    for r in world.grid.rooms:
        if r["level"] != ARKIT_DEFAULT_LEVEL or r.get("type") in ("void", "unknown"):
            continue
        cx, cy = r["centroid"]
        st = statuses.get(r["id"], "clear")
        rid = r["id"]
        out.append({
            "id": rid,
            "label": r["label"],
            "type": r["type"],
            "level": r["level"],
            "centroid": [round(float(cx), 2), round(float(cy), 2)],
            "pos": dcc_plan_to_arkit(cx, cy, ARKIT_DEFAULT_LEVEL),
            "is_exit": r["type"] in ("stairwell", "lobby") and any(
                k in rid for k in ("218", "2ST1", "2ST6", "242", "2ST3", "2ST4")
            ),
            "status": st,
            "blocked": st == "fire",
            "searched": rid in searched,
        })
    return out


def _apply_report(world, info, kind, by) -> None:
    from . import reports
    kind = (kind or "fire").lower().strip()
    if kind not in _REPORT_KINDS:
        kind = "fire"
    raw = f"{kind} in {info['label']}"
    base = {
        "target_id": info["id"],
        "target_label": info["label"],
        "level": info["level"],
        "severity": None,
        "count": None,
        "raw": raw,
    }
    updates = [{**base, "kind": kind, "severity": "heavy" if kind == "smoke" else None}]
    if kind == "clear":
        updates.append({**base, "kind": "searched"})
    reports.apply(world, updates, by, raw)


def _consider_fix(session, loc) -> None:
    """Anti-jump: same room eases 65/35; a new room needs 2 agreeing frames, or 4 if it is far."""
    pos = loc.get("position")
    if not isinstance(pos, (list, tuple)) or len(pos) < 3:
        return
    try:
        pos = [float(pos[0]), float(pos[1]), float(pos[2])]
    except (TypeError, ValueError):
        return
    if not all(math.isfinite(v) for v in pos):
        return
    cand_label = loc.get("room_label")
    cand_id = loc.get("room_id")
    cand_xy = loc.get("dcc_xy")
    if isinstance(cand_xy, (list, tuple)) and len(cand_xy) >= 2:
        try:
            cand_xy = [float(cand_xy[0]), float(cand_xy[1])]
            if not all(math.isfinite(v) for v in cand_xy):
                cand_xy = None
        except (TypeError, ValueError):
            cand_xy = None
    else:
        cand_xy = None
    quat = loc.get("quaternion")
    if not (isinstance(quat, (list, tuple)) and len(quat) == 4):
        quat = session.arkit_quat

    accept = session.last_room_label is None or cand_label == session.last_room_label
    smooth = False
    if accept:
        smooth = session.dcc_xy is not None and cand_xy is not None
    else:
        step = 0.0
        if session.dcc_xy is not None and cand_xy is not None:
            step = math.hypot(cand_xy[0] - session.dcc_xy[0], cand_xy[1] - session.dcc_xy[1])
        need = 2 if step <= 11.5 else 4
        if cand_label == session.pending_label:
            session.pending_count += 1
        else:
            session.pending_label = cand_label
            session.pending_count = 1
        if session.pending_count < need:
            return

    if smooth:
        session.dcc_xy = [
            round(0.65 * session.dcc_xy[0] + 0.35 * cand_xy[0], 3),
            round(0.65 * session.dcc_xy[1] + 0.35 * cand_xy[1], 3),
        ]
        session.arkit_pos = [
            round(0.65 * session.arkit_pos[i] + 0.35 * pos[i], 4) for i in range(3)
        ]
    else:
        session.arkit_pos = [round(v, 4) for v in pos]
        if cand_xy is not None:
            session.dcc_xy = [round(cand_xy[0], 3), round(cand_xy[1], 3)]
        else:
            session.level, x, y = arkit_to_dcc_plan(session.arkit_pos)
            session.dcc_xy = [x, y]
    try:
        session.arkit_quat = [round(float(q), 6) for q in quat]
    except (TypeError, ValueError):
        pass
    session.level = _level_from_room(cand_id, ARKIT_DEFAULT_LEVEL)
    session.last_room_id = cand_id
    session.last_room_label = cand_label
    session.pending_label = None
    session.pending_count = 0
    if not session.dcc_xy:
        session.level, x, y = arkit_to_dcc_plan(session.arkit_pos)
        session.dcc_xy = [x, y]


def _flat_pose(msg) -> dict:
    out = {}
    imu = msg.get("imu") if isinstance(msg, dict) else None
    if isinstance(imu, dict):
        out.update(imu)
    if isinstance(msg, dict):
        for key in _POSE_KEYS:
            if msg.get(key) is not None:
                out[key] = msg[key]
    return out


def _pose_present(flat) -> bool:
    pos = flat.get("position")
    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
        return True
    if flat.get("x") is not None and flat.get("y") is not None:
        return True
    xy = flat.get("dcc_xy")
    return isinstance(xy, (list, tuple)) and len(xy) >= 2


def _take_client_pose(session, flat) -> bool:
    """Apply a phone-sent pose. Plan x/y wins over an ARKit position when both are present."""
    got = False
    quat = flat.get("quaternion")
    if isinstance(quat, (list, tuple)) and len(quat) == 4:
        try:
            session.arkit_quat = [round(float(v), 6) for v in quat]
        except (TypeError, ValueError):
            pass
    if flat.get("heading") is not None:
        try:
            session.heading = float(flat["heading"]) % 360.0
        except (TypeError, ValueError):
            pass
    if flat.get("level") is not None:
        try:
            session.level = int(flat["level"])
        except (TypeError, ValueError):
            pass
    if flat.get("x") is not None and flat.get("y") is not None:
        try:
            session.dcc_xy = [round(float(flat["x"]), 3), round(float(flat["y"]), 3)]
            session.arkit_pos = dcc_plan_to_arkit(session.dcc_xy[0], session.dcc_xy[1], session.level)
            got = True
        except (TypeError, ValueError):
            pass
    if not got and isinstance(flat.get("dcc_xy"), (list, tuple)) and len(flat["dcc_xy"]) >= 2:
        try:
            session.dcc_xy = [round(float(flat["dcc_xy"][0]), 3), round(float(flat["dcc_xy"][1]), 3)]
            session.arkit_pos = dcc_plan_to_arkit(session.dcc_xy[0], session.dcc_xy[1], session.level)
            got = True
        except (TypeError, ValueError):
            pass
    pos = flat.get("position")
    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
        try:
            session.arkit_pos = [round(float(pos[0]), 4), round(float(pos[1]), 4), round(float(pos[2]), 4)]
            if not got:
                session.level, x, y = arkit_to_dcc_plan(session.arkit_pos)
                session.dcc_xy = [x, y]
                got = True
        except (TypeError, ValueError):
            pass
    if got:
        # Phone pose is the plan position; don't keep a VPS room label that belonged somewhere else.
        session.last_room_id = None
        session.last_room_label = None
        session.pending_label = None
        session.pending_count = 0
    return got


def _client_loc(session, prior, vps_status) -> dict:
    loc = {
        "localized": True,
        "position": list(session.arkit_pos),
        "quaternion": list(session.arkit_quat),
        "dcc_xy": list(session.dcc_xy) if session.dcc_xy else None,
        "source": "client",
        "num_matches": 0,
        "num_inliers": 0,
        "latency_ms": (prior or {}).get("latency_ms", 0.0),
    }
    if vps_status == "unavailable":
        loc["vps"] = "unavailable"
        loc["error"] = "unavailable"
    return loc


def _banner(kind, data) -> str:
    if kind == "fire":
        step = data.get("t")
        if step is None:
            return "Fire update. Routes recalculated."
        return f"Fire update (step {step}). Routes recalculated."
    if kind == "report":
        changes = [str(c) for c in (data.get("changes") or []) if str(c) != "Routes updated"]
        if changes:
            return "Field report: " + "; ".join(changes[:3])
        return "Field report: " + str(data.get("text") or "update")
    if kind == "people":
        who = data.get("person_id") or "A person"
        status = data.get("status") or "moving"
        return f"{who} is {status}. Route checked."
    if kind == "building":
        return "Building map updated. Routes recalculated."
    return "Route updated."


def _cue(kind, data):
    if kind == "fire":
        return "hazard_ahead_rerouting.mp3"
    if kind == "report":
        blob = " ".join(str(c) for c in (data.get("changes") or [])) + " " + str(data.get("text") or "")
        if any(word in blob.lower() for word in ("fire", "smoke", "blocked")):
            return "hazard_ahead_rerouting.mp3"
        return "route_recalculated.mp3"
    if kind == "building":
        return "route_recalculated.mp3"
    return None


def _remember_person(session, publish: bool) -> None:
    route = session.last_route or {}
    xy = session.dcc_xy or [ARKIT_TO_DCC_X0, ARKIT_TO_DCC_Y0]
    record = {
        "person_id": session.person_id,
        "status": "trapped" if session.mayday else "evacuating",
        "level": int(route.get("level") or session.level or ARKIT_DEFAULT_LEVEL),
        "x": float(xy[0] if route.get("x_plan") is None else route["x_plan"]),
        "y": float(xy[1] if route.get("y_plan") is None else route["y_plan"]),
        "room_id": route.get("start_node") or session.last_room_id,
        "updated_at": time.time(),
    }
    try:
        from . import occupants
        with occupants.people_lock:
            occupants.people[session.person_id] = record
    except Exception:
        pass
    if not publish:
        return
    try:
        from . import store
        store.log("person_status", dict(record))
        store.save_person(dict(record))
    except Exception:
        pass
    try:
        live.publish("people", dict(record))
    except Exception:
        pass


class ARClientSession:
    def __init__(self, ws: WebSocket, idx: int) -> None:
        self.ws = ws
        self.person_id = f"phone-{idx}"
        self.role = "occupant"
        self.target_room_id = "F2-228"
        self.arkit_pos = [0.0, 0.0, 0.0]
        self.arkit_quat = [0.0, 0.0, 0.0, 1.0]
        self.heading = 0.0
        self.mayday = False
        self.last_room_id = "F2-232"
        self.last_room_label = "232 CLASSROOM"
        self.level = ARKIT_DEFAULT_LEVEL
        self.dcc_xy = [ARKIT_TO_DCC_X0, ARKIT_TO_DCC_Y0]
        self.pending_label = None
        self.pending_count = 0
        self.last_route = {}


def _blank_route() -> dict:
    return {
        "start_node": None,
        "start_label": None,
        "role": "occupant",
        "reachable": False,
        "path": [],
        "path_coords": [],
        "instructions": [],
        "fire_rooms": [],
        "smoke_rooms": [],
    }


async def _send(ws: WebSocket, payload) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


class TelemetryHub:
    def __init__(self, get_world, lock) -> None:
        self.get_world = get_world
        self.lock = lock
        self.ar_sessions: dict[WebSocket, ARClientSession] = {}
        self.dashboard_clients: set[WebSocket] = set()
        self.client_counter = 0
        self.firefighter_pos = [0.0, 0.0, 0.0]
        self.firefighter_quat = [0.0, 0.0, 0.0, 1.0]
        self.mayday_active = False
        self.last_audio_cue = None
        self.latest_frame_b64 = None
        self.last_route = _blank_route()

    def on_live(self, kind, data=None) -> None:
        if kind not in _LIVE_KINDS:
            return
        if not isinstance(data, dict):
            data = {}
        snap = dict(data)
        if isinstance(snap.get("changes"), list):
            snap["changes"] = list(snap["changes"])
        loop = live._loop
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._kick, str(kind), snap)
        except RuntimeError:
            return

    def _kick(self, kind, data) -> None:
        try:
            asyncio.get_running_loop().create_task(self._push_live(kind, data))
        except RuntimeError:
            return

    def _dashboard(self, world, include_pointcloud: bool) -> dict:
        try:
            fire_cells, smoke_cells = world.sim.cells_payload()
            room_st = world.sim.room_status()
            vert_st = world.sim.vertical_status()
            sim_t = world.sim.t
            sim_mode = world.sim.mode
            origins = list(world.sim.origins)
        except Exception:
            fire_cells, smoke_cells, room_st, vert_st = {}, {}, {}, {}
            sim_t, sim_mode, origins = 0, "simulate", []
        route = self.last_route or _blank_route()
        payload = {
            "type": "state_sync",
            "t": sim_t,
            "mode": sim_mode,
            "origins": origins,
            "rooms": room_st,
            "verticals": vert_st,
            "fire": fire_cells,
            "smoke": smoke_cells,
            "firefighter": {
                "position": self.firefighter_pos,
                "quaternion": self.firefighter_quat,
                "current_node": route.get("start_node"),
                "current_label": route.get("start_label"),
                "mayday": self.mayday_active,
            },
            "route": route,
            "nodes": _floor_nodes(world),
            "edges": [],
            "audio_cue": self.last_audio_cue,
            "latest_frame_b64": self.latest_frame_b64,
            "timestamp": time.time(),
        }
        if include_pointcloud:
            eng = _get_engine()
            payload["pointcloud"] = [
                [round(float(p[0]), 3), round(float(p[1]), 3), round(float(p[2]), 3)]
                for p in getattr(eng, "xyz", [])
            ]
            payload["trajectory"] = list(getattr(eng, "trajectory", []) or [])
        return payload

    async def broadcast_dashboards(self) -> None:
        with self.lock:
            msg = self._dashboard(self.get_world(), False)
        await self._send_dash(msg)

    async def _send_dash(self, message) -> None:
        dead = []
        for ws in list(self.dashboard_clients):
            if not await _send(ws, message):
                dead.append(ws)
        if not dead:
            return
        with self.lock:
            for ws in dead:
                self.dashboard_clients.discard(ws)

    async def broadcast_ar(self, audio_cue=None, alert_banner=None) -> None:
        with self.lock:
            world = self.get_world()
            nodes = _floor_nodes(world)
            pending = []
            for ws, sess in list(self.ar_sessions.items()):
                sess.last_route = _build_route(world, sess)
                self.last_route = sess.last_route
                msg = {
                    "type": "route_update",
                    "route": sess.last_route,
                    "nodes": nodes,
                    "audio_cue": audio_cue,
                    "alert_banner": alert_banner,
                }
                pending.append((ws, msg))
        dead = []
        for ws, msg in pending:
            if not await _send(ws, msg):
                dead.append(ws)
        if not dead:
            return
        with self.lock:
            for ws in dead:
                self.ar_sessions.pop(ws, None)

    async def _push_live(self, kind, data) -> None:
        try:
            banner = _banner(kind, data)
            cue = _cue(kind, data)
            self.last_audio_cue = cue
            await self.broadcast_ar(audio_cue=cue, alert_banner=banner)
            await self.broadcast_dashboards()
        except Exception as exc:
            print(f"[VPS] live push failed: {exc}")


def make_router(get_world, lock) -> APIRouter:
    """/ws/ar (phone) and /ws/dashboard. Routes come from the Firesight world."""
    router = APIRouter()
    hub = TelemetryHub(get_world, lock)
    live.subscribe(hub.on_live)

    def _note_loop() -> None:
        try:
            live._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    @router.websocket("/ws/dashboard")
    async def ws_dashboard(ws: WebSocket) -> None:
        await ws.accept()
        _note_loop()
        with lock:
            hub.dashboard_clients.add(ws)
        try:
            await asyncio.to_thread(_get_engine)
            with lock:
                hello = hub._dashboard(get_world(), True)
            await ws.send_json(hello)
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "toggle_block":
                    node_id = msg.get("node_id")
                    try:
                        with lock:
                            world = get_world()
                            info = _find_room(world, node_id)
                            if info is None:
                                missing = True
                            else:
                                missing = False
                                st = world.sim.room_status().get(info["id"], "clear")
                                _apply_report(world, info, "clear" if st == "fire" else "fire", "Commander")
                    except Exception as exc:
                        await _send(ws, {"type": "error", "error": str(exc)[:200]})
                        continue
                    if missing:
                        await _send(ws, {"type": "error", "error": f"Unknown room {node_id}"})
                elif mtype == "simulate_move":
                    pos = msg.get("position") or [0.0, 0.0, 0.3]
                    try:
                        pos = [round(float(v), 3) for v in pos[:3]]
                    except (TypeError, ValueError):
                        continue
                    level, x, y = arkit_to_dcc_plan(pos)
                    fake = ARClientSession(ws, 0)
                    fake.arkit_pos = pos
                    fake.level = level
                    fake.dcc_xy = [x, y]
                    fake.last_room_id = None
                    fake.last_room_label = None
                    fake.role = "occupant"
                    fake.target_room_id = None
                    with lock:
                        hub.firefighter_pos = pos
                        hub.last_route = _build_route(get_world(), fake)
                    await hub.broadcast_dashboards()
                    await hub.broadcast_ar()
                elif mtype == "clear_mayday":
                    hub.mayday_active = False
                    hub.last_audio_cue = "mayday_acknowledged.mp3"
                    await hub.broadcast_dashboards()
                    await hub.broadcast_ar(audio_cue="mayday_acknowledged.mp3")
        except WebSocketDisconnect:
            pass
        finally:
            with lock:
                hub.dashboard_clients.discard(ws)

    @router.websocket("/ws/ar")
    async def ws_ar(ws: WebSocket) -> None:
        await ws.accept()
        _note_loop()
        with lock:
            hub.client_counter += 1
            session = ARClientSession(ws, hub.client_counter)
            world = get_world()
            session.last_route = _build_route(world, session)
            nodes = _floor_nodes(world)
            hub.last_route = session.last_route
            hub.ar_sessions[ws] = session
        try:
            await ws.send_json({
                "type": "init",
                "person_id": session.person_id,
                "role": session.role,
                "route": session.last_route,
                "nodes": nodes,
                "audio_cue": "system_online.mp3",
            })
            while True:
                packet = await ws.receive()
                if packet.get("type") == "websocket.disconnect":
                    break
                await _on_ar_packet(ws, session, packet)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            with lock:
                hub.ar_sessions.pop(ws, None)

    async def _on_ar_packet(ws: WebSocket, session: ARClientSession, packet) -> None:
        jpeg = None
        intrinsics = None
        flat = {}
        if packet.get("bytes") is not None:
            jpeg = packet["bytes"]
        elif packet.get("text"):
            try:
                msg = json.loads(packet["text"])
            except json.JSONDecodeError:
                return
            if not isinstance(msg, dict):
                return
            if msg.get("role") in ("occupant", "firefighter"):
                session.role = msg["role"]
            if msg.get("target_room_id"):
                session.target_room_id = str(msg["target_room_id"])
            if msg.get("person_id"):
                session.person_id = str(msg["person_id"])
            mtype = msg.get("type") or "frame"
            flat = _flat_pose(msg)
            if mtype == "set_mode":
                with lock:
                    world = get_world()
                    session.last_route = _build_route(world, session)
                    hub.last_route = session.last_route
                    nodes = _floor_nodes(world)
                await _send(ws, {
                    "type": "route_update",
                    "route": session.last_route,
                    "nodes": nodes,
                    "audio_cue": "route_recalculated.mp3",
                })
                return
            if mtype == "report_fire":
                room_id = msg.get("room_id") or (session.last_route or {}).get("start_node") or "F2-232"
                kind = msg.get("kind") or "fire"
                info = None
                try:
                    with lock:
                        world = get_world()
                        info = _find_room(world, room_id)
                        if info is not None:
                            _apply_report(world, info, kind, session.person_id)
                except Exception as exc:
                    await _send(ws, {"type": "error", "error": str(exc)[:200]})
                    return
                if info is None:
                    await _send(ws, {"type": "error", "error": f"Unknown room {room_id}"})
                return
            if mtype == "mayday":
                session.mayday = bool(msg.get("active", True))
                hub.mayday_active = session.mayday
                hub.last_audio_cue = "mayday_transmitting.mp3"
                await _send(ws, {
                    "type": "mayday_ack",
                    "mayday": session.mayday,
                    "audio_cue": "mayday_transmitting.mp3",
                })
                _remember_person(session, publish=True)
                await hub.broadcast_dashboards()
                return
            if mtype == "relocalize":
                session.dcc_xy = None
                session.last_room_id = None
                session.last_room_label = None
                session.pending_label = None
                session.pending_count = 0
                return
            if msg.get("image_b64"):
                try:
                    b64 = str(msg["image_b64"]).split(",")[-1].strip()
                    if len(b64) <= 3_000_000:
                        hub.latest_frame_b64 = b64
                    jpeg = base64.b64decode(b64, validate=False)
                    intrinsics = msg.get("intrinsics")
                except Exception:
                    jpeg = None

        if jpeg is not None and len(jpeg) > _MAX_JPEG:
            jpeg = None
        pose = _pose_present(flat)
        if jpeg is None and not pose:
            return

        if jpeg is not None:
            prev = list(session.dcc_xy) if session.dcc_xy else None
            loc = await asyncio.to_thread(_localize_locked, jpeg, intrinsics, prev)
            vps_status = getattr(_engine, "status", "unavailable")
        else:
            loc = None
            eng = await asyncio.to_thread(_get_engine)
            vps_status = getattr(eng, "status", "unavailable")

        prev_node = (session.last_route or {}).get("start_node")
        with lock:
            if loc is not None and loc.get("localized"):
                _consider_fix(session, loc)
            elif pose:
                _take_client_pose(session, flat)
                loc = _client_loc(session, loc, vps_status)
            elif loc is None:
                return
            world = get_world()
            session.last_route = _build_route(world, session)
            hub.last_route = session.last_route
            nodes = _floor_nodes(world)
            if loc.get("localized"):
                hub.firefighter_pos = list(session.arkit_pos)
                hub.firefighter_quat = list(session.arkit_quat)
        audio = None
        curr = session.last_route.get("start_node")
        if loc.get("localized") and curr != prev_node:
            if curr and curr == session.last_route.get("target_room_id"):
                audio = "arrived_at_exit.mp3"
            else:
                audio = "proceed_to_next_waypoint.mp3"
        hub.last_audio_cue = audio
        response = {
            "type": "pose_and_route",
            **loc,
            "role": session.role,
            "current_node": session.last_route.get("start_node"),
            "current_label": session.last_route.get("start_label"),
            "route": session.last_route,
            "nodes": nodes,
            "audio_cue": audio,
        }
        await _send(ws, response)
        await hub.broadcast_dashboards()
        if loc.get("localized"):
            _remember_person(session, publish=curr != prev_node)

    return router


__all__ = ["make_router", "vps_locate", "MODEL_DIR"]
