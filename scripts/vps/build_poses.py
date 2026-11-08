#!/usr/bin/env python3
"""
build_poses.py
Converts MiSensorKit ARKit JSON poses (camera/*.json + rgb/*.jpg)
into a COLMAP empty_model (cameras.txt, images.txt, points3D.txt).
"""

import argparse
import glob
import json
import os
from pathlib import Path
import numpy as np


def rotmat_to_qvec(R: np.ndarray) -> np.ndarray:
    """Convert a 3x3 rotation matrix to COLMAP Hamilton quaternion [qw, qx, qy, qz]."""
    # Ensure strict orthogonality via SVD
    U, _, Vt = np.linalg.svd(R)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt

    trace = np.trace(R)
    if trace > 0.0:
        s = 0.5 / np.sqrt(trace + 1.0)
        qw = 0.25 / s
        qx = (R[2, 1] - R[1, 2]) * s
        qy = (R[0, 2] - R[2, 0]) * s
        qz = (R[1, 0] - R[0, 1]) * s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        qw = (R[2, 1] - R[1, 2]) / s
        qx = 0.25 * s
        qy = (R[0, 1] + R[1, 0]) / s
        qz = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        qw = (R[0, 2] - R[2, 0]) / s
        qx = (R[0, 1] + R[1, 0]) / s
        qy = 0.25 * s
        qz = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        qw = (R[1, 0] - R[0, 1]) / s
        qx = (R[0, 2] + R[2, 0]) / s
        qy = (R[1, 2] + R[2, 1]) / s
        qz = 0.25 * s

    qvec = np.array([qw, qx, qy, qz], dtype=np.float64)
    qvec /= np.linalg.norm(qvec)
    return qvec


def find_dataset_root(base_dir: Path) -> Path:
    """Locate the directory containing 'rgb' and 'camera' subfolders (preferring '2nd floor part 2')."""
    if (base_dir / "rgb").is_dir() and (base_dir / "camera").is_dir():
        return base_dir
    for preferred in ("2nd floor part 2", "2nd floor"):
        cand = base_dir / preferred
        if cand.is_dir() and (cand / "rgb").is_dir() and (cand / "camera").is_dir():
            return cand
    for sub in sorted(base_dir.iterdir()):
        if sub.is_dir() and (sub / "rgb").is_dir() and (sub / "camera").is_dir():
            return sub
    raise FileNotFoundError(
        f"Could not find 'rgb/' and 'camera/' inside {base_dir.resolve()}"
    )


def build_empty_model(dataset_dir: Path, output_dir: Path, stride: int = 1):
    root = find_dataset_root(dataset_dir)
    camera_dir = root / "camera"
    rgb_dir = root / "rgb"

    output_dir.mkdir(parents=True, exist_ok=True)

    json_files = sorted(glob.glob(str(camera_dir / "frame_*.json")))
    if not json_files:
        raise RuntimeError(f"No frame_*.json files found in {camera_dir}")

    # MiSensorKit saves upright portrait RGB frames while extrinsics.R is ARKit landscape camera-to-world:
    #   +X_colmap (right)   = +Y_arkit (col 1)
    #   +Y_colmap (down)    = +X_arkit (col 0)
    #   +Z_colmap (forward) = -Z_arkit (-col 2)
    C_portrait = np.array(
        [
            [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, -1.0],
        ],
        dtype=np.float64,
    )

    cameras_lines = [
        "# Camera list with one line of data per camera:",
        "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]",
    ]
    images_lines = [
        "# Image list with two lines of data per image:",
        "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME",
        "#   POINTS2D[] as (X, Y, POINT3D_ID)",
    ]

    valid_count = 0
    if stride <= 0:
        stride = max(1, len(json_files) // 300)
        print(f"[build_poses] Auto-selected stride={stride} for {len(json_files)} total frames")
    selected_files = json_files[:: max(1, stride)]

    for idx, jf in enumerate(selected_files, start=1):
        stem = Path(jf).stem
        img_name = f"{stem}.jpg"
        img_path = rgb_dir / img_name
        if not img_path.exists():
            continue

        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)

        intr = data["intrinsics"]
        extr = data["extrinsics"]

        width = int(intr["image_width"])
        height = int(intr["image_height"])
        fx = float(intr["fx"])
        fy = float(intr["fy"])
        cx = float(intr["cx"])
        cy = float(intr["cy"])

        R_arkit = np.array(extr["R"], dtype=np.float64)
        t_arkit = np.array(extr["t"], dtype=np.float64).reshape(3)

        # Camera-to-world in COLMAP convention -> invert for World-to-Camera
        R_wc_colmap = R_arkit @ C_portrait
        R_cw = R_wc_colmap.T
        t_cw = -R_cw @ t_arkit

        qvec = rotmat_to_qvec(R_cw)

        cam_id = idx
        img_id = idx
        cameras_lines.append(
            f"{cam_id} PINHOLE {width} {height} {fx:.8f} {fy:.8f} {cx:.8f} {cy:.8f}"
        )
        images_lines.append(
            f"{img_id} {qvec[0]:.10f} {qvec[1]:.10f} {qvec[2]:.10f} {qvec[3]:.10f} "
            f"{t_cw[0]:.10f} {t_cw[1]:.10f} {t_cw[2]:.10f} {cam_id} {img_name}"
        )
        images_lines.append("")  # Empty 2D points line required by COLMAP format
        valid_count += 1

    (output_dir / "cameras.txt").write_text("\n".join(cameras_lines) + "\n", encoding="utf-8")
    (output_dir / "images.txt").write_text("\n".join(images_lines) + "\n", encoding="utf-8")
    (output_dir / "points3D.txt").write_text(
        "# 3D point list with one line of data per point:\n"
        "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)\n",
        encoding="utf-8",
    )

    print(f"[build_poses] Successfully wrote {valid_count} camera poses to {output_dir}")
    print(f"[build_poses] RGB image folder resolved at: {rgb_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert MiSensorKit poses to COLMAP empty_model")
    parser.add_argument(
        "--dataset_dir",
        type=Path,
        default=Path("."),
        help="Path to dataset folder (or parent containing misensorkit_*)",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=Path("empty_model"),
        help="Output directory for COLMAP empty_model",
    )
    parser.add_argument(
        "--stride",
        type=int,
        default=0,
        help="Frame stride (0 = auto ~300 frames, 1 = every frame, 5 = every 5th frame)",
    )
    args = parser.parse_args()
    build_empty_model(args.dataset_dir, args.output_dir, args.stride)
