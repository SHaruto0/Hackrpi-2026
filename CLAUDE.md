# Firesight: project context for Claude Code

## TL;DR
HackRPI 2026 project (Nov 7 to 8, 2026, DCC at RPI, Troy NY). Coding ends **11:00 AM Sunday**; Devpost submit by **9:00 AM Sunday**, edits until 11:00 AM.

**Firesight** turns building floor plan images into a structured, multi-floor 3D map, simulates a fire, and gives every person a live escape route that reroutes as fire and smoke spread. Next: occupants point their phone camera and see the escape path drawn over the live view with an arrow, mini-map and voice, rerouting as the fire spreads. Then firefighter/commander views.

**Pitch line:** "Wayfinding apps assume the building is safe. Firesight assumes it isn't."

## Team constraints (important)
- Laptops only. No iPhone Pro (no LiDAR), no extra hardware.
- Map source = DCC architectural floor plan images (3 floors, NOT evacuation maps) → Gemini → JSON.
- Fire data = mock simulation (cellular automaton). Real sensors are "future work".
- Laptop first, cloud later (Vultr).
- Build with Cursor/Claude Code; the SpaceXSI raffle (mechanical keyboard) needs Cursor + Grok Bot usage, so mention it on Devpost.

## What exists now (working, tested)
All paths are relative to the repo root. See README.md for the user-facing overview and docs/API.md for every endpoint.
```
backend/
  server.py                 FastAPI app: building/fire/route API, editor save/check, mounts routers, serves viewer/
  core/                     pure, testable building logic
    model.py                load + validate building JSON, align floors, warnings (wall_style, plan_north_deg, door axis)
    grid.py                 3D grid (floor,row,col) at 0.5 m; walls ("centerline" thick or "faces" CAD lines), doors, stairs
    fire.py                 mock fire + smoke sim (spreads through doors, smoke rises via stairs)
    routing.py              multi-source Dijkstra from all exits -> route field; instructions text
    checks.py               map quality checks (sealed areas, reachability, stairs; door/wall checks off for CAD maps)
  mapping/                  floor plans -> building JSON
    pdf_extract.py          vector CAD PDF -> exact walls/doors/rooms/stairs (main path for DCC)
    extract_floorplans.py   images/PDF -> Gemini -> checker feedback rounds -> best JSON (fallback path)
    prompts/                Gemini extraction + fix prompts
  services/                 phone-facing routers (built by Grok agents, reviewed + tested)
    occupants.py            /api/qr, /api/qr/print, /api/nav (turn hints, compass heading, hazard), /api/rev, /api/people
    voice.py                /api/voice/tts via Fish Audio (disk cache data/tts_cache), occupant vs firefighter style
    localize.py             /api/loc/*: 500-particle filter (steps + compass, walls, stairs, OCR fixes)
    vision.py               RapidOCR read_text + match_rooms (room numbers, EXIT) for localization
    store.py                MongoDB Atlas (MONGODB_URI): async event log (fire_*, person_status, loc_pose, field_report),
                            map versions; /api/history/*; no-op when unset
    cortex.py               Snowflake Cortex (SNOWFLAKE_ACCOUNT, SNOWFLAKE_PAT): /api/brief (template fallback), /api/ask (RAG)
    reports.py              firefighter field reports: Fish ASR /api/voice/stt -> rule parser -> apply to world
                            (fire/smoke/clear/searched/blocked/open/victim) -> reroute; /api/reports*, undo
viewer/
  index.html                Three.js 3D viewer (stacked floors, fire/smoke tiles, routes)
  editor.html               2D map editor (doors, exits, walls, rooms, stairs, scale; Apply/Save/Check)
  phone.html                occupant phone page (QR start, camera arrow, mini-map, voice, safe/trapped, sensors, Talk agent)
  voice-agent.js            Fish Audio voice agent (agent-client via CDN, client tools; setup in docs/FISH_AGENT.md)
  firefighter.html          push-to-talk field reports, read-back, search tracker
  command.html              commander dashboard: Cortex brief/Q&A, people, floors, field reports, timeline
  pdr.js, pdr-test.html     phone step counting + compass + frame upload for /api/loc (test page)
data/
  buildings/                dcc.json (from PDFs, main), dcc-gemini.json (Gemini attempt), sample_building.json
  plans/                    DCC vector floor plan PDFs (darrin-1..3.pdf)
  archive/                  old Gemini rounds and hand-fix attempts
scripts/make_sample.py      generates data/buildings/sample_building.json
tests/                      test_core (5), test_tier2 (10), test_localize (9), test_data (5), test_reports (14)
docs/                       API.md, FISH_AGENT.md (agent dashboard setup), HANDOFF.md (original notes)
.team/                      (gitignored) specs/contracts for Grok agents
```

### Run
```bash
pip install -r requirements.txt
python scripts/make_sample.py                                   # synthetic building for tests
FIRESIGHT_BUILDING=data/buildings/dcc.json uvicorn backend.server:app --reload --port 8000
# http://localhost:8000 (3D), /editor.html, /phone.html?qr=F3-318, /pdr-test.html, /api/qr/print
python tests/test_core.py; python tests/test_tier2.py; python tests/test_localize.py
```
Rebuild the DCC map:
```bash
python -m backend.mapping.pdf_extract data/plans/darrin-1.pdf data/plans/darrin-2.pdf data/plans/darrin-3.pdf -o data/buildings/dcc.json
python -m backend.core.checks data/buildings/dcc.json          # remaining issues; fix in /editor.html
# Hand fixes that must survive re-extraction go in data/buildings/dcc.overrides.json (no_exit, exit, flight_goes,
# remove_flight; matched by position). OCR results are cached in data/plans/*.ocr.json and *.stairs.json.
# Gemini fallback (needs GEMINI_API_KEY; free tier: use gemini-3.8-flash):
python -m backend.mapping.extract_floorplans <f1> <f2> <f3> -o data/buildings/dcc-gemini.json --rounds 3
```
Or POST the JSON to `/api/building` to hot-swap without restarting. Voice needs FISH_API_KEY (optional FISH_VOICE_ID =
a voice MODEL id from fish.audio/m/<id>, not the agent id). World also holds `blocked` (cells reported impassable,
treated like fire by routing), `searched`, `reports`; fire reset clears them.
Phones need HTTPS for camera/motion: `cloudflared tunnel --url http://localhost:8000`.

### Viewer controls
Pick a room → Start fire → Step / Play. Click any room to get a route from there (reroutes live every step). "All routes" draws every room's evacuation path. Floor filter, floor spacing slider, labels toggle. Drag rotate, scroll zoom, right-drag pan.

## Data model (Gemini output = input to the loader)
Shared frame for all floors: meters, origin top-left of the lowest floor footprint, x right, y down. Key fields:
- `floors[].level`, `alignment {offset_x, offset_y, rotation_deg}` (applied by the loader: rotate about origin, then offset)
- `floors[].outline`, `walls[] {x1,y1,x2,y2,exterior}`, `spaces[] {id,label,type,polygon,multi_floor}`, `doors[] {x,y,width,connects,exterior_exit}`
- `vertical_connections[] {id,name,type: stairs|elevator|ramp, x,y,floors}`
- every element has `confidence`
Loader behavior: drops invalid elements with warnings, renames duplicate ids, clamps door width 0.6 to 3.0 m, unknown space types become `unknown`, accepts JSON wrapped in ``` fences. Warns if no exterior exits or no stairs on multi-floor buildings.

## Key algorithms / constants
- Grid: `CELL=0.5` m, wall half-thickness 0.3 m, doors carve openings (radius = width/2 + 0.25). Cells outside the floor outline are blocked.
- Stairs: link nearest walkable cell to (x,y) on consecutive served floors; cost `STAIR_COST_M=8` per floor. **Elevators excluded from evacuation routes.**
- Routing: 8-connected, no corner cutting, fire cells impassable, smoke multiplies cost by `1 + 12*smoke`. One Dijkstra from all exits gives routes for everyone; recomputed after every fire change (~10 ms on the sample).
- Fire sim: fire spreads to 4-neighbors with p=0.30/step (seeded, deterministic); smoke diffuses (decay 0.92/cell, 2 cells/step), rises up stairs (x0.97) and weakly down (x0.35). Replace `FireSim.step` with real sensor input later by writing to `sim.fire` / `sim.smoke`.
- Room status: `fire` if any fire cell, `smoke` if max smoke > 0.35.

## API
| Method | Path | Body / params | Returns |
|---|---|---|---|
| GET | `/api/building` | | normalized model + `grid` meta + `exits` + `warnings` |
| POST | `/api/building` | raw building JSON | replaces building, resets fire |
| GET | `/api/state` | | `t`, `rooms` status, `verticals` status, `fire`/`smoke` cells per level |
| POST | `/api/fire/start` | `{room_id}` or `{level,x,y}` | |
| POST | `/api/fire/step` | `{n}` (1 to 50) | |
| POST | `/api/fire/reset` | | |
| GET | `/api/route` | `level,x,y` | `reachable, path [[level,x,y]...], distance_m, exit, max_smoke, instructions[]` |
| GET | `/api/routes/all` | | route from every room |

Grid cell (r,c) center = `(origin_x + (c+.5)*cell, origin_y + (r+.5)*cell)`. JSON keys for per-level dicts are strings ("1").

## Viewer notes (viewer/index.html)
Three.js 0.160 from jsdelivr via importmap. Plan (x,y) → world (x, levelY, y); floors stacked on world Y with `gap` spacing. Room slabs = ShapeGeometry (y flipped, rotateX -90°). Walls are BoxGeometry split around door positions. Fire/smoke = InstancedMesh tiles per floor. Route = TubeGeometry over a CurvePath of LineCurve3. Click picking uses raycast on room slabs/floor plates.

## Camera navigation (occupant phone app) - core feature to build next
A person opens Firesight on their phone, points the camera ahead, and sees the escape path drawn over the live camera view, plus voice directions. It reroutes live as the fire spreads.

### User flow
1. Scan the QR code posted in the room (or tap "I'm in room ___"). QR gives exact position AND facing direction.
2. Camera view opens. Overlay shows: big arrow toward the next waypoint, distance to the next turn, banner ("Head to WEST STAIRS, east stairs blocked"), and a mini-map in the corner with their dot + route (2D floor, same data as the 3D viewer).
3. As they walk, position updates from the camera (see localization), the arrow turns, waypoints tick off.
4. Hazard warnings: "Turn around" if facing toward fire; "Smoke ahead, stay low" if route passes smoke.
5. Fire spreads → server pushes new route → arrow and path change immediately + voice "East stairs blocked, use west stairs."
6. At stairs: "Take West Stairs down to floor 1", floor auto-switches after stair detection or tap.
7. Outside: "You're out. Go to the assembly point." + "I'm safe" button. Also an "I'm trapped" button that sends last known room.

### Showing the path on the camera
- **v1 (works on every phone, build first):** 2D overlay on the camera feed. Arrow rotation = bearing to next waypoint minus phone heading. Heading from `deviceorientationabsolute` (Android) / `webkitCompassHeading` (iOS, needs permission tap). Map coordinates are in the building frame, so add the building bearing (from OpenStreetMap, measured once) to convert to compass degrees. Draw a "ribbon" of chevrons in perspective at the bottom of the screen pointing the arrow direction so it looks like a path on the floor.
- **v2 (Android Chrome, stretch):** WebXR `immersive-ar` with `local-floor` + hit-test to place real 3D path tiles/arrows on the floor, anchored at the QR start pose. iOS Safari WebXR support is limited, so iPhones stay on v1.

### Localization (no LiDAR, no GPS)
GPS doesn't work indoors. Combine, in order of trust:
1. **QR / AprilTag anchors** (room doors, stairwell doors, hallway corners): exact pose. Each QR encodes `{building, level, x, y, heading}`.
2. **Door numbers seen by the camera:** every ~2 s send a frame to `POST /api/localize`; Gemini (or OCR) reads room numbers like "204" and snaps to that door.
3. **Image retrieval:** during setup, film each hallway, extract a frame every 0.5 s, label frames with map positions. Embed with DINOv2 (small), search with FAISS. Return top matches + confidence. Runs on laptop CPU.
4. **Motion between fixes:** WebXR tracking on Android (v2), otherwise step counting from the accelerometer + compass heading (dead reckoning).
5. **Map matching:** snap the estimate to walkable cells and only allow moves to neighboring cells (simple particle filter or nearest walkable cell on the route).
6. **Confidence rule:** if unsure, never guess. Show "Point your camera at a door number or scan the nearest QR." ("No position beats a wrong one", lesson from marko.)

### Backend additions needed
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/localize` | body: jpeg frame + last known `{level,x,y}`; returns `{level,x,y,confidence,source}` (qr/door_number/retrieval) |
| GET | `/api/qr/{code}` | resolve a QR id to `{level,x,y,heading}` |
| GET | `/api/route` | already exists; phone calls it on every position update |
| WS | `/ws/routes` | push "hazards changed, refetch route" to phones (polling every 1 to 2 s is fine for v1) |
| POST | `/api/people/{id}/status` | safe / trapped / evacuating + last position (commander view) |
| GET | `/api/qr/print` | printable sheet of QR codes per room/door for the demo |

Also add `next_waypoint` and `turn` hints to the route response: distance to next turn, turn direction (left/right/straight/stairs down/up), so the phone doesn't recompute.

### Phone app files to create
```
viewer/phone.html     camera view (getUserMedia, facingMode: "environment"), arrow + chevron ribbon overlay (canvas),
                      mini-map (2D canvas of the current floor + route), banner, voice, I'm safe / trapped buttons
backend/localize.py   QR lookup, door-number reading (Gemini), DINOv2 + FAISS retrieval, confidence
data/frames/          labeled reference frames (setup step)
scripts/build_index.py  extract frames from walkthrough videos, embed, save FAISS index + labels
```
Phones need HTTPS for camera + compass: run behind an HTTPS tunnel (e.g. cloudflared or ngrok) or deploy on Vultr with a cert.

### Demo for camera nav
Judge scans the QR taped to the demo table ("Room 204, floor 2"). Their phone shows the camera with an arrow and path toward the west stairs. We start a fire near the east stairs on the laptop; the judge's arrow swings and the voice says the stairs are blocked. Point the camera at a door number: position snaps and the panel shows "located by door number 205".

## Remaining tasks (priority order)
**Tier 1 (do first)**
1. Run extraction on the 3 real DCC floor plan images; inspect `data/dcc.raw.json` and warnings; fix by hand. Gemini coordinates will be rough. Check: stairwells line up across floors, every space has a door, exterior exits marked.
2. Map editor (React or plain canvas) to fix walls/doors/labels/exits and set scale (click a door = 0.9 m). Save back to JSON. Hand-drawn fallback map by hour 4 so routing work is never blocked.
3. Optional CV assist for geometry (OpenCV: perspective warp, adaptive threshold walls, connected components rooms), with Gemini only for labels/semantics. Gemini is good at meaning, weak at precise coordinates.
**Tier 2**
4. **Camera navigation v1** (see "Camera navigation" section): `phone.html` with camera + arrow/chevron path overlay + mini-map, QR anchors, compass heading, route polling, turn hints in the route API.
5. Voice: ElevenLabs TTS. Occupants: calm, simple ("Exit the room, turn left"). Firefighters: terse radio style ("Room 204. West stairs, floor 2, third door left"). Cache common phrases.
6. Camera localization: `/api/localize` with door-number reading (Gemini) first, then DINOv2 + FAISS retrieval; confidence check with QR fallback.
7. Stretch: camera navigation v2 with WebXR AR path on the floor (Android Chrome) + continuous tracking, map-matched with a particle filter.
**Tier 3 (pick 1 to 2)**
8. Firefighter mode: route *to* a room, mark rooms searched, commander dashboard (who's safe/trapped from self check-ins).
9. Snowflake Cortex: building knowledge RAG ("where's the gas shutoff?") + commander briefings.
10. Evacuation simulation: many agents + bottleneck heatmap (ML/Data Science track).
11. Persistence: MongoDB Atlas (buildings, rooms, people), Tiger Data (fire/smoke/sensor time series, positions). Deploy on Vultr.

**Cut order if behind:** 7 → 10 → 9 → 6 (fall back to QR/tap) → 8. Never cut 1, 2, routing, fire sim, 3D viewer.

## Checkpoints
| Hour | Must be true |
|---|---|
| 4 | Real (or hand-fixed) DCC map loads; route on screen |
| 10 | Fire spreads, routes reroute live in 3D |
| 16 | Phone page opens via QR, camera view shows arrow/path + mini-map + voice, reroutes live |
| 20 | Feature freeze, polish + demo only |

## Demo script (2 min)
1. "Here are DCC's floor plans." Gemini turns them into this 3D building (rotate it).
2. Judge scans a QR → their phone shows the way out from their "room".
3. Start a fire near the east stairs → smoke rises floor by floor → every route flips to the west stairs live.
4. Voice reads directions. Elevator is labeled not for evacuation.
5. Close: limits + future (real sensors, alarm panel integration, firefighter mode).

## Prize tracks to target
Grand ($1,000 / $750), In the Clouds (Sony headphones), ML/Data Science ($400), Gemini API, ElevenLabs (earbuds), MongoDB Atlas (M5Stack IoT kit), Tiger Data (Stream Deck Mini), Vultr, Snowflake API, SpaceXSI raffle (keyboard), Ethical AI ($300, if we add explainable routing + privacy). Check the per-team track limit on Devpost. Judging: practicality, creativity, technical difficulty, effort.

## Prior art to differentiate from
**marko** (BigRed//Hacks 2026 winner): AR indoor wayfinding from floor plans + LiDAR scan, used rented GPUs for visual localization (SuperPoint/LightGlue/NetVLAD), Dijkstra on 0.5 m grid, compass + OSM building bearing. Their lessons: lighting changes break camera localization (54% matched at night); "no position beats a wrong one"; fuse signals. Our difference: dynamic hazards (fire/smoke change the map live), multi-floor smoke physics, evacuation-first, works without LiDAR or GPUs.

## Honest limits (say these to judges)
- Fire data is simulated; production plugs into alarm panels/IoT sensors.
- Gemini maps need human review (minutes per floor, done once before any emergency).
- Cameras fail in heavy smoke; production would fuse IMU, Wi-Fi/BLE/UWB.

## Conventions
- Python 3.11+, FastAPI, numpy/scipy. Keep backend pure and testable; add tests to `tests/test_core.py` for new logic.
- Viewer is a single static HTML file; keep it dependency-free beyond the Three.js CDN.
- Never commit API keys; use env vars (`GEMINI_API_KEY`, `GEMINI_MODEL`, `FIRESIGHT_BUILDING`).
- Writing style for UI copy and docs: short, plain, no em dashes.
