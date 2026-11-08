# Firesight — 3D Visual Positioning & AR Evacuation System (DCC 200 Level)

Real-time indoor Visual Positioning System (VPS), dynamic Dijkstra evacuation routing, 3D Incident Commander Dashboard, and Mobile AR Navigation client mapped to **RPI Darrin Communications Center (DCC) 200 Level**.

---

## Repository Structure

```text
├── final_model/               # Compiled COLMAP 3D map (5,231 3D points + SuperPoint & ORB descriptors)
│   ├── cameras.bin
│   ├── frames.bin
│   ├── images.bin
│   ├── points3D.bin
│   ├── rigs.bin
│   ├── map_descriptors.npz    # 256-D SuperPoint descriptors + 3D XYZ landmarks
│   └── orb_keyframes.npz      # Precomputed OpenCV ORB keyframes + 46.3m trajectory
├── backend/                   # Component 1: FastAPI WebSocket & VPS + Dijkstra Server ("The Brain")
│   ├── main.py
│   ├── requirements.txt
│   └── start.bat
├── dashboard/                 # Component 2: Incident Commander 3D Web Dashboard (React + Three.js)
│   ├── public/dcc_200_floorplan.png
│   └── src/App.jsx
├── mobile-app/                # Component 3: React Native (Expo) Mobile AR Client
│   ├── App.js
│   └── package.json
├── ios/                       # Native SwiftUI + ARKit + RealityKit iOS implementation
│   └── FiresightARApp.swift
├── build_poses.py             # Converts MiSensorKit ARKit extrinsics/intrinsics to COLMAP model
├── build_map.py               # SuperPoint + LightGlue + multi-view SVD 3D triangulation pipeline
├── run_vultr.sh               # One-click cloud GPU/CPU mapping script
└── map_preview.html           # Standalone interactive 3D point-cloud & trajectory viewer
```

---

## Quick Start for Teammates

### 1. Start the FastAPI Backend ("The Brain")
```bash
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```
- Loads `final_model/` (5,231 3D landmarks + 117 reference keyframes across the 46.3m DCC 200 walkthrough).
- Exposes WebSockets at `ws://<YOUR_LAPTOP_IP>:8000/ws/ar` and `ws://localhost:8000/ws/dashboard`.

### 2. Start the Commander 3D Dashboard
```bash
cd dashboard
npm install
npm run dev
```
- Open `http://localhost:5173`.
- Click any 3D waypoint sphere in **Corridor 219** to toggle a fire hazard (`Blocked`) and watch Dijkstra dynamically reroute the firefighter in real time.

### 3. Start the Mobile AR App (React Native / Expo)
```bash
cd mobile-app
npm install --legacy-peer-deps
npx expo login --no-browser
npx expo start --tunnel
```
- Open **Expo Go** on iOS or Android (signed into the same Expo account) and scan/open the tunnel URL.
- Tap **Server IP** in the top-right HUD of the phone screen and set `ws://<YOUR_LAPTOP_IP>:8000/ws/ar`.
