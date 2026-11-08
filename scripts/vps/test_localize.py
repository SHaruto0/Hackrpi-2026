#!/usr/bin/env python3
"""
test_localize.py
Offline localization test: loads final_model/ (cameras.bin, images.bin, points3D.bin, map_descriptors.npz),
runs SuperPoint + LightGlue + pycolmap.absolute_pose_estimation() on test frames from rgb/,
and compares the localized 6-DoF pose against the ground-truth ARKit pose in camera/*.json.
"""

import glob
import json
from pathlib import Path
import cv2
import numpy as np
import pycolmap
import torch
from lightglue import LightGlue, SuperPoint


def load_map(final_model_dir: Path, device: torch.device):
    data = np.load(final_model_dir / "map_descriptors.npz")
    xyz = data["xyz"].astype(np.float64)  # (N, 3)
    descs = data["descriptors"].astype(np.float32).T  # (N, 256)
    kps = data["keypoints"].astype(np.float32)  # (N, 2)
    img_size = data["image_size"]

    return {
        "xyz": xyz,
        "feats": {
            "keypoints": torch.from_numpy(kps)[None].to(device),
            "descriptors": torch.from_numpy(descs)[None].to(device),
            "image_size": torch.tensor([[int(img_size[0]), int(img_size[1])]], dtype=torch.float32, device=device),
        },
    }


def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    final_dir = Path("final_model")
    map_data = load_map(final_dir, device)

    extractor = SuperPoint(max_num_keypoints=2048).eval().to(device)
    matcher = LightGlue(features="superpoint").eval().to(device)

    C_portrait = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])

    cam_files = sorted(glob.glob("misensorkit_*/camera/frame_*.json")) or sorted(glob.glob("camera/frame_*.json"))
    rgb_dir = Path(cam_files[0]).parent.parent / "rgb"

    test_indices = [0, 25, 50, 75, 100]
    errors_m = []

    print(f"Testing localization against {len(map_data['xyz'])} 3D map landmarks on {device}...")
    print("-" * 75)

    for idx in test_indices:
        if idx >= len(cam_files):
            continue
        jf = cam_files[idx]
        d = json.load(open(jf, encoding="utf-8"))
        intr, extr = d["intrinsics"], d["extrinsics"]
        gt_pos = np.array(extr["t"], dtype=np.float64)

        img_name = Path(jf).stem + ".jpg"
        img_bgr = cv2.imread(str(rgb_dir / img_name), cv2.IMREAD_GRAYSCALE)
        h, w = img_bgr.shape[:2]
        img_tensor = torch.from_numpy(img_bgr / 255.0).float()[None, None].to(device)

        with torch.no_grad():
            q_feats = extractor.extract(img_tensor)
            matches_out = matcher({"image0": q_feats, "image1": map_data["feats"]})

        matches = matches_out["matches"][0].cpu().numpy()
        if len(matches) < 12:
            print(f"{img_name}: Too few matches ({len(matches)})")
            continue

        q_kps = q_feats["keypoints"][0].cpu().numpy()[matches[:, 0]].astype(np.float64)
        p3d = map_data["xyz"][matches[:, 1]].astype(np.float64)

        camera = pycolmap.Camera(
            model="PINHOLE",
            width=int(intr["image_width"]),
            height=int(intr["image_height"]),
            params=[float(intr["fx"]), float(intr["fy"]), float(intr["cx"]), float(intr["cy"])],
        )

        ans = pycolmap.absolute_pose_estimation(q_kps, p3d, camera)
        if ans is None or not ans.get("success", True if "cam_from_world" in ans else False):
            print(f"{img_name}: PnP failed")
            continue

        cfw = ans["cam_from_world"]
        R_cw = cfw.rotation.matrix()
        t_cw = np.array(cfw.translation, dtype=np.float64)
        est_pos = -R_cw.T @ t_cw
        err = float(np.linalg.norm(est_pos - gt_pos))
        errors_m.append(err)

        inliers = ans.get("num_inliers", len(matches))
        print(
            f"{img_name} | Matches: {len(matches):3d} | Inliers: {inliers:3d} | "
            f"Est XYZ: ({est_pos[0]:+.2f}, {est_pos[1]:+.2f}, {est_pos[2]:+.2f}) | "
            f"GT XYZ: ({gt_pos[0]:+.2f}, {gt_pos[1]:+.2f}, {gt_pos[2]:+.2f}) | "
            f"Error: {err * 100:.1f} cm"
        )

    if errors_m:
        print("-" * 75)
        print(f"Mean Localization Error: {np.mean(errors_m) * 100:.2f} cm")


if __name__ == "__main__":
    main()
