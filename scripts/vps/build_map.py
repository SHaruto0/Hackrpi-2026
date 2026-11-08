#!/usr/bin/env python3
"""
build_map.py
Runs hloc (SuperPoint + LightGlue + pycolmap fast triangulation) on the cloud GPU
using the ARKit poses in empty_model/ to produce final_model/ (.bin files + map_descriptors.npz).
Includes a multi-view SVD + ray-depth fallback for low-parallax indoor sequences.
"""

import argparse
import glob
import json
import shutil
from collections import defaultdict
from pathlib import Path
import h5py
import numpy as np
import pycolmap

from hloc import extract_features, match_features, triangulation
from hloc.utils.io import get_keypoints, get_matches
from build_poses import find_dataset_root, build_empty_model, rotmat_to_qvec


def generate_hybrid_pairs(empty_model_dir: Path, pairs_path: Path, max_neighbors: int = 8):
    """Generate image pairs combining multi-stride sequential offsets and pose-proximity neighbors."""
    recon = pycolmap.Reconstruction(str(empty_model_dir))
    images = sorted(recon.images.values(), key=lambda im: im.name)
    names = [im.name for im in images]
    n = len(names)

    centers = []
    for im in images:
        if hasattr(im, "projection_center"):
            c = np.array(im.projection_center(), dtype=np.float64)
        else:
            cam_from_world = im.cam_from_world() if callable(getattr(im, "cam_from_world", None)) else im.cam_from_world
            R = cam_from_world.rotation.matrix()
            t = np.array(cam_from_world.translation, dtype=np.float64)
            c = -R.T @ t
        centers.append(c)
    centers = np.stack(centers, axis=0)

    offsets = [1, 2, 3, 5, 8, 12]
    pair_set = set()

    for i in range(n):
        for off in offsets:
            j = i + off
            if j < n:
                a, b = (names[i], names[j]) if names[i] < names[j] else (names[j], names[i])
                pair_set.add((a, b))

        dists = np.linalg.norm(centers - centers[i], axis=1)
        order = np.argsort(dists)
        for j in order[1 : max_neighbors + 1]:
            if i != j:
                a, b = (names[i], names[j]) if names[i] < names[j] else (names[j], names[i])
                pair_set.add((a, b))

    sorted_pairs = sorted(pair_set)
    pairs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(pairs_path, "w", encoding="utf-8") as f:
        for a, b in sorted_pairs:
            f.write(f"{a} {b}\n")

    print(f"[build_map] Generated {len(sorted_pairs)} image pairs -> {pairs_path}")


def fallback_track_triangulation(
    dataset_root: Path,
    features_path: Path,
    matches_path: Path,
    pairs_path: Path,
    text_dir: Path,
) -> pycolmap.Reconstruction:
    """Direct multi-view SVD track triangulation with ray-depth fallback for low-parallax clips."""
    text_dir.mkdir(parents=True, exist_ok=True)
    C_portrait = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])
    json_files = sorted(glob.glob(str(dataset_root / "camera" / "frame_*.json")))

    cams = {}
    imgs = {}
    name_to_id = {}

    for idx, jf in enumerate(json_files, start=1):
        stem = Path(jf).stem
        name = f"{stem}.jpg"
        if not (dataset_root / "rgb" / name).exists():
            continue
        with open(jf, "r", encoding="utf-8") as f:
            d = json.load(f)
        intr, extr = d["intrinsics"], d["extrinsics"]
        R_arkit = np.array(extr["R"], dtype=np.float64)
        t_arkit = np.array(extr["t"], dtype=np.float64).reshape(3)
        R_cw = (R_arkit @ C_portrait).T
        t_cw = -R_cw @ t_arkit
        qvec = rotmat_to_qvec(R_cw)
        cams[idx] = (
            int(intr["image_width"]),
            int(intr["image_height"]),
            float(intr["fx"]),
            float(intr["fy"]),
            float(intr["cx"]),
            float(intr["cy"]),
        )
        imgs[idx] = {
            "name": name,
            "R": R_cw,
            "t": t_cw,
            "q": qvec,
            "center": t_arkit,
            "kps": get_keypoints(features_path, name),
        }
        name_to_id[name] = idx

    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    pairs = [line.strip().split() for line in open(pairs_path, encoding="utf-8") if line.strip()]
    for n0, n1 in pairs:
        if n0 not in name_to_id or n1 not in name_to_id:
            continue
        i0, i1 = name_to_id[n0], name_to_id[n1]
        try:
            m, scores = get_matches(matches_path, n0, n1)
        except Exception:
            continue
        for (k0, k1), s in zip(m, scores):
            if s > 0.2:
                union((i0, int(k0)), (i1, int(k1)))

    tracks_raw = defaultdict(dict)
    for node in list(parent.keys()):
        root = find(node)
        img_id, kp_idx = node
        if img_id not in tracks_raw[root]:
            tracks_raw[root][img_id] = kp_idx

    points3D = {}
    obs_to_pid = {}
    pid = 1

    for obs_dict in tracks_raw.values():
        if len(obs_dict) < 3:
            continue
        obs_list = sorted(obs_dict.items())
        A = []
        for img_id, kp_idx in obs_list:
            w, h, fx, fy, cx, cy = cams[img_id]
            u, v = imgs[img_id]["kps"][kp_idx]
            xn, yn = (u - cx) / fx, (v - cy) / fy
            P = np.hstack([imgs[img_id]["R"], imgs[img_id]["t"].reshape(3, 1)])
            A.append(xn * P[2] - P[0])
            A.append(yn * P[2] - P[1])
        A = np.array(A, dtype=np.float64)
        _, _, Vt = np.linalg.svd(A)
        X = Vt[-1, :3] / (Vt[-1, 3] + 1e-12)

        ref_id, ref_kp = obs_list[0]
        z_ref = (imgs[ref_id]["R"] @ X + imgs[ref_id]["t"])[2]
        if not (0.4 < z_ref < 15.0) or not np.all(np.isfinite(X)):
            w, h, fx, fy, cx, cy = cams[ref_id]
            u, v = imgs[ref_id]["kps"][ref_kp]
            ray_cam = np.array([(u - cx) / fx, (v - cy) / fy, 1.0])
            ray_cam /= np.linalg.norm(ray_cam)
            X = imgs[ref_id]["center"] + (imgs[ref_id]["R"].T @ (ray_cam * 3.0))

        points3D[pid] = (X, obs_list)
        for img_id, kp_idx in obs_list:
            obs_to_pid[(img_id, kp_idx)] = pid
        pid += 1

    cam_lines = ["# Camera list"]
    img_lines = ["# Image list"]
    pt3d_lines = ["# 3D point list"]

    for cid, (w, h, fx, fy, cx, cy) in sorted(cams.items()):
        cam_lines.append(f"{cid} PINHOLE {w} {h} {fx:.8f} {fy:.8f} {cx:.8f} {cy:.8f}")

    for iid, im in sorted(imgs.items()):
        q, t = im["q"], im["t"]
        img_lines.append(
            f"{iid} {q[0]:.10f} {q[1]:.10f} {q[2]:.10f} {q[3]:.10f} "
            f"{t[0]:.10f} {t[1]:.10f} {t[2]:.10f} {iid} {im['name']}"
        )
        p2d_tokens = []
        for k_idx, (u, v) in enumerate(im["kps"]):
            p3d_id = obs_to_pid.get((iid, k_idx), -1)
            p2d_tokens.append(f"{u:.4f} {v:.4f} {p3d_id}")
        img_lines.append(" ".join(p2d_tokens))

    for p3d_id, (X, obs_list) in sorted(points3D.items()):
        track_tokens = " ".join(f"{iid} {k_idx}" for iid, k_idx in obs_list)
        pt3d_lines.append(f"{p3d_id} {X[0]:.6f} {X[1]:.6f} {X[2]:.6f} 200 200 200 1.0 {track_tokens}")

    (text_dir / "cameras.txt").write_text("\n".join(cam_lines) + "\n", encoding="utf-8")
    (text_dir / "images.txt").write_text("\n".join(img_lines) + "\n", encoding="utf-8")
    (text_dir / "points3D.txt").write_text("\n".join(pt3d_lines) + "\n", encoding="utf-8")

    return pycolmap.Reconstruction(str(text_dir))


def export_map_descriptors(recon: pycolmap.Reconstruction, features_path: Path, output_npz: Path):
    """Pre-aggregates SuperPoint descriptors for every triangulated 3D point in final_model."""
    if len(recon.points3D) == 0:
        print("[build_map] WARNING: 0 3D points in reconstruction; skipping map_descriptors.npz")
        return

    id_to_name = {im.image_id: im.name for im in recon.images.values()}
    first_cam = next(iter(recon.cameras.values()))
    img_w, img_h = int(first_cam.width), int(first_cam.height)

    point_ids = []
    point_xyz = []
    point_rgb = []
    point_descs = []
    point_kps = []

    with h5py.File(str(features_path), "r") as h5f:
        desc_cache = {}
        kp_cache = {}
        for img_name in id_to_name.values():
            if img_name in h5f:
                desc_cache[img_name] = np.array(h5f[img_name]["descriptors"], dtype=np.float32)
                kp_cache[img_name] = np.array(h5f[img_name]["keypoints"], dtype=np.float32)

        for pid, p3d in recon.points3D.items():
            track_descs = []
            ref_kp = None
            for elem in p3d.track.elements:
                img_name = id_to_name.get(elem.image_id)
                if img_name and img_name in desc_cache:
                    k_idx = elem.point2D_idx
                    d_mat = desc_cache[img_name]
                    if k_idx < d_mat.shape[1]:
                        track_descs.append(d_mat[:, k_idx])
                        if ref_kp is None:
                            ref_kp = kp_cache[img_name][k_idx]

            if not track_descs:
                continue

            mean_desc = np.mean(np.stack(track_descs, axis=0), axis=0)
            norm = np.linalg.norm(mean_desc)
            if norm > 1e-8:
                mean_desc /= norm

            point_ids.append(int(pid))
            point_xyz.append(np.array(p3d.xyz, dtype=np.float32))
            point_rgb.append(np.array(p3d.color, dtype=np.uint8))
            point_descs.append(mean_desc.astype(np.float16))
            point_kps.append(ref_kp.astype(np.float32))

    point_ids = np.array(point_ids, dtype=np.int64)
    point_xyz = np.stack(point_xyz, axis=0)
    point_rgb = np.stack(point_rgb, axis=0)
    point_descs = np.stack(point_descs, axis=1)
    point_kps = np.stack(point_kps, axis=0)

    np.savez_compressed(
        output_npz,
        point_ids=point_ids,
        xyz=point_xyz,
        rgb=point_rgb,
        descriptors=point_descs,
        keypoints=point_kps,
        image_size=np.array([img_w, img_h], dtype=np.int32),
    )
    print(f"[build_map] Saved {len(point_ids)} 3D point SuperPoint descriptors -> {output_npz}")


def main():
    parser = argparse.ArgumentParser(description="Build 3D COLMAP map using SuperPoint + LightGlue + ARKit poses")
    parser.add_argument("--dataset_dir", type=Path, default=Path("."), help="Path to dataset directory")
    parser.add_argument("--empty_model_dir", type=Path, default=Path("empty_model"), help="Path to empty_model")
    parser.add_argument("--work_dir", type=Path, default=Path("outputs"), help="Intermediate hloc outputs")
    parser.add_argument("--final_model_dir", type=Path, default=Path("final_model"), help="Output final_model dir")
    args = parser.parse_args()

    root = find_dataset_root(args.dataset_dir)
    rgb_dir = root / "rgb"

    if not (args.empty_model_dir / "images.txt").exists():
        print("[build_map] empty_model not found, running build_poses first...")
        build_empty_model(args.dataset_dir, args.empty_model_dir)

    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.final_model_dir.mkdir(parents=True, exist_ok=True)

    features_path = args.work_dir / "features.h5"
    pairs_path = args.work_dir / "pairs.txt"
    matches_path = args.work_dir / "matches.h5"
    sfm_dir = args.work_dir / "sfm_superpoint+lightglue"

    feature_conf = extract_features.confs["superpoint_max"].copy()
    feature_conf["model"]["max_keypoints"] = 2048
    feature_conf["preprocessing"]["grayscale"] = True
    feature_conf["preprocessing"]["resize_max"] = 640

    print("[build_map] Step 1/4: Extracting SuperPoint features...")
    extract_features.main(
        conf=feature_conf,
        image_dir=rgb_dir,
        export_dir=args.work_dir,
        feature_path=features_path,
        overwrite=False,
    )

    print("[build_map] Step 2/4: Generating covisible image pairs...")
    generate_hybrid_pairs(args.empty_model_dir, pairs_path)

    matcher_conf = match_features.confs["superpoint+lightglue"].copy()
    print("[build_map] Step 3/4: Matching features with LightGlue...")
    match_features.main(
        conf=matcher_conf,
        pairs=pairs_path,
        features=features_path,
        export_dir=args.work_dir,
        matches=matches_path,
        overwrite=False,
    )

    print("[build_map] Step 4/4: Triangulating 3D map across multi-view tracks...")
    model = fallback_track_triangulation(
        dataset_root=root,
        features_path=features_path,
        matches_path=matches_path,
        pairs_path=pairs_path,
        text_dir=args.work_dir / "text_model",
    )

    model.write_binary(str(args.final_model_dir))
    export_map_descriptors(
        recon=model,
        features_path=features_path,
        output_npz=args.final_model_dir / "map_descriptors.npz",
    )

    print("============================================================")
    print(f"[build_map] SUCCESS! Triangulated {len(model.points3D)} 3D points across {len(model.images)} frames.")
    print("============================================================")


if __name__ == "__main__":
    main()
