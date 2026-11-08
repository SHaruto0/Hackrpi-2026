"""Generate a fake 3-floor building in the exact Gemini output schema.

Use it to develop/test before the real DCC JSON exists:
    python scripts/make_sample.py   ->  data/buildings/sample_building.json
Layout per floor (meters): 40 x 24, central hallway y=10..14,
west stairs NW corner, east stairs SE corner, elevator on the south side.
"""
import json
import os

W, H = 40, 24


def floor(level):
    walls = [
        (0, 0, W, 0, True), (W, 0, W, H, True), (W, H, 0, H, True), (0, H, 0, 0, True),
        (0, 10, W, 10, False), (0, 14, W, 14, False),
    ]
    north = [(0, 4, "stairwell", "West Stairs"), (4, 12, "classroom", None), (12, 20, "office", None),
             (20, 28, "classroom", None), (28, 36, "classroom", None), (36, 40, "restroom", "Restroom")]
    south = [(0, 8, "classroom", None), (8, 16, "office", None), (16, 20, "elevator", "Elevator"),
             (20, 28, "classroom", None), (28, 36, "storage", None), (36, 40, "stairwell", "East Stairs")]
    if level == 3:  # a big lecture hall on the top floor
        north = [north[0], north[1], north[2], (20, 36, "lecture_hall", "Lecture Hall 3A"), north[5]]

    spaces, doors = [], []
    n = 1
    for (x0, x1, t, label), (y0, y1, ydoor) in (
        [(r, (0, 10, 10)) for r in north] + [(r, (14, 24, 14)) for r in south]
    ):
        if x0 > 0:
            walls.append((x0, y0, x0, y1, False))
        lbl = label or f"{level}{n:02d}"
        sid = f"F{level}-{lbl.replace(' ', '')}"
        n += 1
        spaces.append({"id": sid, "label": lbl, "type": t,
                       "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                       "multi_floor": False, "confidence": 0.9})
        doors.append({"id": f"F{level}-D-{lbl.replace(' ', '')}", "x": (x0 + x1) / 2, "y": ydoor,
                      "width": 1.8 if t in ("stairwell", "lecture_hall") else 0.9,
                      "connects": [sid, f"F{level}-HALL"], "exterior_exit": False, "confidence": 0.85})

    spaces.append({"id": f"F{level}-HALL", "label": f"Hall {level}", "type": "hallway",
                   "polygon": [[0, 10], [W, 10], [W, 14], [0, 14]], "multi_floor": False, "confidence": 0.95})
    if level == 1:
        doors += [
            {"id": "EXIT-WEST", "x": 0, "y": 12, "width": 1.8, "connects": ["F1-HALL"],
             "exterior_exit": True, "confidence": 0.95},
            {"id": "EXIT-EAST", "x": W, "y": 12, "width": 1.8, "connects": ["F1-HALL"],
             "exterior_exit": True, "confidence": 0.95},
        ]

    # dedupe partition walls
    uniq, seen = [], set()
    for w in walls:
        k = tuple(round(v, 3) for v in w[:4])
        if k not in seen:
            seen.add(k); uniq.append(w)

    return {
        "level": level,
        "alignment": {"offset_x": 0, "offset_y": 0, "rotation_deg": 0},
        "outline": [[0, 0], [W, 0], [W, H], [0, H]],
        "walls": [{"id": f"F{level}-W{i}", "x1": a, "y1": b, "x2": c, "y2": d,
                   "exterior": e, "confidence": 0.9} for i, (a, b, c, d, e) in enumerate(uniq)],
        "spaces": spaces,
        "doors": doors,
        "notes": "synthetic sample",
    }


building = {
    "building": "Sample Hall (synthetic)",
    "units": "meters",
    "scale_method": "synthetic",
    "floor_height_m": 4.0,
    "floors": [floor(1), floor(2), floor(3)],
    "vertical_connections": [
        {"id": "STAIR-WEST", "name": "West Stairs", "type": "stairs", "x": 2, "y": 5, "floors": [1, 2, 3], "confidence": 0.95},
        {"id": "STAIR-EAST", "name": "East Stairs", "type": "stairs", "x": 38, "y": 19, "floors": [1, 2, 3], "confidence": 0.95},
        {"id": "ELEVATOR-1", "name": "Elevator", "type": "elevator", "x": 18, "y": 19, "floors": [1, 2, 3], "confidence": 0.9},
    ],
    "multi_floor_spaces": [],
    "global_notes": "Synthetic test building, not DCC.",
}

if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "buildings", "sample_building.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(building, fh, indent=2)
    print("wrote", out)
