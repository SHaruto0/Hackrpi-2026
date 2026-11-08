#!/usr/bin/env python3
"""
Builds a balanced, ground-truth-labeled visual localization index in final_model/orb_keyframes.npz:
  1. Densely indexes Room 232 from BOTH "2nd floor part 2" (frames 1..255) and "2nd floor" (frames 1..245)
     (~145 keyframes covering every wall, TV, whiteboard, desk, window, and doorway of Room 232).
  2. Densely indexes Room 235 (frames 935..1138), Room 239 (frames 1842..2115), and 242 East Lobby (2386..2569),
     while keeping Corridor 219 evenly sampled so Corridor 219 never overwhelms classroom matches.
  3. Uses CLAHE-normalized LAB + HSV + Sobel Edge spatial descriptors + 600-keypoint ORB descriptors.
"""

import glob
import json
import math
from collections import Counter
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
FINAL_MODEL = ROOT / "final_model"


def crop_square_448(bgr_img: np.ndarray) -> np.ndarray:
    """Center-crop any aspect ratio image (e.g. 4:3 iPhone photo) to a 1:1 square, then resize to 448x448."""
    h, w = bgr_img.shape[:2]
    if h != w:
        s = min(h, w)
        y0 = (h - s) // 2
        x0 = (w - s) // 2
        bgr_img = bgr_img[y0 : y0 + s, x0 : x0 + s]
    if bgr_img.shape[0] != 448 or bgr_img.shape[1] != 448:
        bgr_img = cv2.resize(bgr_img, (448, 448), interpolation=cv2.INTER_AREA)
    return bgr_img


def compute_global_descriptor(bgr_448: np.ndarray) -> np.ndarray:
    """
    Computes an L2-normalized illumination-robust global spatial appearance descriptor (876-D):
      - CLAHE-normalized LAB + HSV + Sobel edge magnitude on a 12x12 spatial grid (864 dims)
      - Floor & mid-wall color/texture signatures (12 dims)
    """
    # Normalize illumination via CLAHE on L channel so live Expo camera auto-exposure matches MiSensorKit
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

    # Floor patch (bottom 35%) + Mid wall patch (middle 35%)
    floor_patch = feat_stack[42:, :, :].mean(axis=(0, 1)) * 3.0
    mid_patch = feat_stack[20:42, :, :].mean(axis=(0, 1)) * 2.0

    vec = np.concatenate([grid_rgb, grid_he, floor_patch, mid_patch]).astype(np.float32)
    norm = float(np.linalg.norm(vec)) + 1e-8
    return vec / norm


def label_part2_frame(frame_idx: int, t_arkit: np.ndarray):
    """
    Exact ground-truth room assignment and yaw-corrected DCC Floor 2 plan coordinates (x_plan, y_plan)
    for '2nd floor part 2' (frames 1..2569), verified directly from RGB camera images:
      - Frames 1..255: Inside 232 CLASSROOM (walking out door 232 at frame 258-262)
      - Frames 256..440: 219 CORRIDOR (OUTSIDE 232) (looking at West double doors & outside 232 glass wall)
      - Frames 441..680: 219 CORRIDOR (OUTSIDE 234)
      - Frames 681..934: 219 CORRIDOR (OUTSIDE 235)
      - Frames 935..1138: Inside 235 CLASSROOM
      - Frames 1139..1480: 219 CORRIDOR (OUTSIDE 236)
      - Frames 1481..1720: 219 CORRIDOR (OUTSIDE 238)
      - Frames 1721..1841: 219 CORRIDOR (OUTSIDE 239)
      - Frames 1842..2115: Inside 239 CLASSROOM
      - Frames 2116..2280: 219 CORRIDOR (OUTSIDE 240)
      - Frames 2281..2385: 219 CORRIDOR (EAST / STAIR 2ST6)
      - Frames 2386..2569: 242 EAST LOBBY / STAIR 2ST6 EXIT
    """
    x, _y, z = float(t_arkit[0]), float(t_arkit[1]), float(t_arkit[2])
    theta = math.radians(-3.8)
    xr = x * math.cos(theta) - z * math.sin(theta)
    zr = x * math.sin(theta) + z * math.cos(theta)

    if frame_idx <= 255:
        return "F2-232", "232 CLASSROOM", [round(40.1 + xr * 0.5, 2), round(60.0 + zr * 0.5, 2)]
    elif frame_idx <= 440:
        return "F2-219", "219 CORRIDOR (OUTSIDE 232)", [39.5, 55.0]
    elif frame_idx <= 680:
        return "F2-219", "219 CORRIDOR (OUTSIDE 234)", [45.0, 54.8]
    elif frame_idx <= 934:
        return "F2-219", "219 CORRIDOR (OUTSIDE 235)", [52.0, 55.0]
    elif frame_idx <= 1138:
        return "F2-236", "235 CLASSROOM", [55.8, 59.0]
    elif frame_idx <= 1480:
        return "F2-219", "219 CORRIDOR (OUTSIDE 236)", [58.0, 54.8]
    elif frame_idx <= 1720:
        return "F2-219", "219 CORRIDOR (OUTSIDE 238)", [62.8, 54.8]
    elif frame_idx <= 1841:
        return "F2-219", "219 CORRIDOR (OUTSIDE 239)", [67.8, 55.0]
    elif frame_idx <= 2115:
        return "F2-239", "239 CLASSROOM", [68.1, 59.3]
    elif frame_idx <= 2280:
        return "F2-219", "219 CORRIDOR (OUTSIDE 240)", [74.5, 54.8]
    elif frame_idx <= 2385:
        return "F2-219", "219 CORRIDOR (EAST / STAIR 2ST6)", [79.0, 55.0]
    else:
        return "F2-2ST6", "242 EAST LOBBY / STAIR 2ST6 EXIT", [84.0, 61.5]


def label_part1_frame(frame_idx: int, t_arkit: np.ndarray):
    """Ground-truth room assignment for original '2nd floor' dataset (frames 1..1481)."""
    x, _y, _z = float(t_arkit[0]), float(t_arkit[1]), float(t_arkit[2])
    if frame_idx <= 240:
        return "F2-232", "232 CLASSROOM", [40.1, 60.0]
    elif x < 4.0:
        return "F2-219", "219 CORRIDOR (OUTSIDE 232)", [39.8, 54.8]
    elif x < 9.0:
        return "F2-219", "219 CORRIDOR (OUTSIDE 234)", [45.0, 54.8]
    elif x < 14.0:
        return "F2-219", "219 CORRIDOR (OUTSIDE 235)", [52.0, 54.8]
    elif x < 19.5:
        return "F2-219", "219 CORRIDOR (OUTSIDE 236)", [57.8, 54.8]
    elif x < 24.5:
        return "F2-219", "219 CORRIDOR (OUTSIDE 238)", [62.5, 54.8]
    elif x < 30.0:
        return "F2-219", "219 CORRIDOR (OUTSIDE 239)", [68.0, 54.8]
    elif x < 36.5:
        return "F2-219", "219 CORRIDOR (OUTSIDE 240)", [74.5, 54.8]
    elif x < 42.0:
        return "F2-219", "219 CORRIDOR (EAST / STAIR 2ST6)", [79.5, 55.0]
    else:
        return "F2-2ST6", "242 EAST LOBBY / STAIR 2ST6 EXIT", [84.0, 61.5]


def main() -> None:
    orb = cv2.ORB_create(nfeatures=650)
    names, kps_list, des_list, gdes_list = [], [], [], []
    R_list, t_list, room_ids, room_labels, dcc_xys = [], [], [], [], []
    trajectory = []

    # 1. Index "2nd floor part 2":
    #    - Densely sample inside classrooms (Room 232: stride 3, Room 235: stride 3, Room 239: stride 3, 242 Lobby: stride 3)
    #    - Sparsely sample Corridor 219 (stride 12) so Corridor 219 does NOT outnumber the classrooms!
    p2_files = sorted(glob.glob(str(ROOT / "2nd floor part 2" / "camera" / "frame_*.json")))
    for idx, jf in enumerate(p2_files, start=1):
        d = json.loads(Path(jf).read_text(encoding="utf-8"))
        t_vec = np.array(d["extrinsics"]["t"], dtype=np.float64).reshape(3)
        if idx % 5 == 1:
            trajectory.append(t_vec.tolist())

        in_classroom_or_lobby = (
            (idx <= 255)
            or (935 <= idx <= 1138)
            or (1842 <= idx <= 2115)
            or (idx >= 2386)
        )
        stride = 3 if in_classroom_or_lobby else 12
        if idx % stride != 1:
            continue

        stem = Path(jf).stem
        img_path = Path(jf).parent.parent / "rgb" / f"{stem}.jpg"
        if not img_path.exists():
            continue
        bgr = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        bgr_448 = crop_square_448(bgr)
        gray = cv2.cvtColor(bgr_448, cv2.COLOR_BGR2GRAY)
        kps, des = orb.detectAndCompute(gray, None)
        if des is None or len(kps) < 12:
            continue

        gdes = compute_global_descriptor(bgr_448)
        R_mat = np.array(d["extrinsics"]["R"], dtype=np.float64)
        rid, rlabel, dxy = label_part2_frame(idx, t_vec)

        names.append(f"p2_{stem}")
        kps_list.append(np.array([kp.pt for kp in kps], dtype=np.float32))
        des_list.append(des)
        gdes_list.append(gdes)
        R_list.append(R_mat)
        t_list.append(t_vec)
        room_ids.append(rid)
        room_labels.append(rlabel)
        dcc_xys.append(dxy)

    # 2. Index Room 232 frames (1..240, stride 4) from original "2nd floor" as well,
    #    giving Room 232 complete 360-degree coverage from two separate walks!
    p1_files = sorted(glob.glob(str(ROOT / "2nd floor" / "camera" / "frame_*.json")))
    for idx, jf in enumerate(p1_files, start=1):
        stride = 4 if idx <= 240 else 25
        if idx % stride != 1:
            continue
        # Skip doorway transition frames 241..295 in part 1
        if 240 < idx <= 295:
            continue
        stem = Path(jf).stem
        img_path = Path(jf).parent.parent / "rgb" / f"{stem}.jpg"
        if not img_path.exists():
            continue
        bgr = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        bgr_448 = crop_square_448(bgr)
        gray = cv2.cvtColor(bgr_448, cv2.COLOR_BGR2GRAY)
        kps, des = orb.detectAndCompute(gray, None)
        if des is None or len(kps) < 12:
            continue

        d = json.loads(Path(jf).read_text(encoding="utf-8"))
        t_vec = np.array(d["extrinsics"]["t"], dtype=np.float64).reshape(3)
        R_mat = np.array(d["extrinsics"]["R"], dtype=np.float64)
        gdes = compute_global_descriptor(bgr_448)
        rid, rlabel, dxy = label_part1_frame(idx, t_vec)

        names.append(f"p1_{stem}")
        kps_list.append(np.array([kp.pt for kp in kps], dtype=np.float32))
        des_list.append(des)
        gdes_list.append(gdes)
        R_list.append(R_mat)
        t_list.append(t_vec)
        room_ids.append(rid)
        room_labels.append(rlabel)
        dcc_xys.append(dxy)

    out_path = FINAL_MODEL / "orb_keyframes.npz"
    np.savez_compressed(
        out_path,
        names=np.array(names, dtype=object),
        kps=np.array(kps_list, dtype=object),
        des=np.array(des_list, dtype=object),
        gdes=np.stack(gdes_list, axis=0).astype(np.float32),
        R_arkit=np.stack(R_list, axis=0).astype(np.float64),
        t_arkit=np.stack(t_list, axis=0).astype(np.float64),
        room_ids=np.array(room_ids, dtype=object),
        room_labels=np.array(room_labels, dtype=object),
        dcc_xys=np.array(dcc_xys, dtype=np.float64),
        trajectory=np.array(trajectory, dtype=np.float64),
    )

    counts = Counter(room_labels)
    print(f"[upgrade_keyframes] Saved {len(names)} calibrated keyframes to {out_path}")
    for k, v in sorted(counts.items()):
        print(f"  - {k}: {v} keyframes")


if __name__ == "__main__":
    main()
