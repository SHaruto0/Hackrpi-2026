# Firesight API

Base URL: `http://localhost:8000`

Start the server from the repo root:

```bash
uvicorn backend.server:app --reload --port 8000
```

`FIRESIGHT_BUILDING` selects the JSON file. The default is `data/buildings/sample_building.json`.

Coordinates are meters in one shared plan frame. `x` increases to the right. `y` increases downward. `plan_north_deg` is the compass bearing of plan "up" (negative `y`), clockwise from north.

JSON error bodies use FastAPI's shape: `{"detail": "..."}`.

The app also serves `viewer/` at `/`. These pages are static files: `/` (3D viewer), `/editor.html`, `/phone.html`, `/firefighter.html`, `/command.html`, `/pdr-test.html`. `/api/*` is matched first.

Uploading a building replaces the in-memory world. That clears the fire, blocked cells, searched rooms, and the report log. Check-ins and localization sessions stay in memory and are cleared when the process stops. Localization sessions also expire after 30 minutes idle. Field reports, blocked cells, and searched rooms live on the world object and are also cleared by `POST /api/fire/reset`.

## Building

### `GET /api/building`

Returns the loaded model plus grid metadata and exit points.

Model fields:

| Field | Meaning |
|---|---|
| `building` | Name string. |
| `units` | Always `meters`. |
| `scale_method` | How the geometry was scaled. May be empty. |
| `floor_height_m` | Floor-to-floor height. Default 4. |
| `wall_style` | `centerline` or `faces`. |
| `plan_north_deg` | Compass bearing of plan up, 0 to 360. |
| `levels` | Floor numbers, low to high. |
| `floors` | One object per floor. See below. |
| `vertical_connections` | Stairs, ramps, and elevators. See below. |
| `multi_floor_spaces` | List copied from the source JSON. |
| `global_notes` | Free text. |
| `warnings` | Loader warnings plus grid warnings. |
| `grid` | `{origin_x, origin_y, cell, rows, cols, levels}`. `cell` is 0.5. |
| `exits` | `{level, door_id, x, y}` for each exterior exit cell. `x` and `y` are cell centers. |

Each floor:

| Field | Meaning |
|---|---|
| `level` | Integer floor number. |
| `alignment` | `{offset_x, offset_y, rotation_deg}` already applied to coordinates. |
| `outline` | Polygon `[[x, y], ...]`. May be empty. |
| `walls` | `{id, x1, y1, x2, y2, exterior, confidence}`. |
| `spaces` | `{id, label, type, polygon, multi_floor, confidence}`. |
| `doors` | `{id, x, y, width, axis, connects, exterior_exit, confidence}`. |
| `notes` | Free text. |

Space `type` is one of: `classroom`, `office`, `hallway`, `lobby`, `restroom`, `stairwell`, `elevator`, `mechanical`, `lecture_hall`, `storage`, `unknown`, `void`. Unknown types are stored as `unknown`.

Door `width` is clamped to 0.6 to 3.0 m. `axis` is a unit vector along the opening, or `null`. `connects` is a list of space ids.

Each vertical connection: `{id, name, type, x, y, floors, positions, confidence}`. `type` is `stairs`, `elevator`, or `ramp`. `positions` maps a floor number to `[x, y]` when the source JSON had per-floor points.

### `POST /api/building`

Body: a building JSON object. Fenced ` ```json ` text is accepted only by the file loader, so this body must be a JSON object.

Replaces the current building and resets the fire, blocked cells, searched rooms, and field reports. Routes are rebuilt from the new grid.

Response: `{"ok": true, "warnings": [...]}`.

Errors: `400` when the JSON cannot be loaded (`ModelError` or `ValueError`). The detail string is the reason.

### `POST /api/building/save`

Body: `{"name": "<file>"}`.

Writes the current building to `data/buildings/<name>.json`. Alignment is stored as zeros because it is already applied. `levels` and `warnings` are omitted so the file can be loaded again.

When `MONGODB_URI` is set, the same document is stored as the next Atlas version (`version` is the previous max plus 1). A failed Atlas write does not fail the disk save. The HTTP body does not include the version.

`name` must match `[A-Za-z0-9_.-]{1,64}` and must not start with `.`. A missing `.json` suffix is added.

Response: `{"ok": true, "path": "data/buildings/<name>.json"}` (relative path).

Errors: `400` when the name is rejected.

## Fire

### `GET /api/state`

| Field | Meaning |
|---|---|
| `t` | Step count since reset. |
| `origins` | `{level, x, y}` for each ignition, in order. |
| `rooms` | Map of space id to `fire`, `smoke`, or `clear`. |
| `verticals` | Map of vertical id to `fire`, `smoke`, or `clear`. |
| `fire` | Per floor, list of `[row, col]` cells that are on fire. |
| `smoke` | Per floor, list of `[row, col, value]` for smoke at least 0.15 and not on fire. Value is rounded to 2 decimals. |

`fire` and `smoke` object keys are floor levels as strings (`"1"`, `"2"`). A room is `fire` when any of its cells are on fire, and `smoke` when the max smoke in the room is above 0.35. The same 0.35 cutoff is used for vertical connections.

Cell center in plan meters: `(origin_x + (col + 0.5) * cell, origin_y + (row + 0.5) * cell)`.

### `POST /api/fire/start`

Body is one of:

- `{"room_id": "F3-318"}`
- `{"level": 3, "x": 12.5, "y": 8.0}`

Ignites a patch of about 1 m around a walkable cell, sets smoke there to 1, refreshes routes.

Response: `{"ok": true, "t": <step>}`.

Errors:

- `404` unknown `room_id` or unknown `level`.
- `400` when neither form is complete, or the point cannot be placed on a walkable cell.

### `POST /api/fire/step`

Body: `{"n": 1}`. `n` defaults to 1 and is clamped to 1 through 50.

Each step spreads fire to walkable 4-neighbors with probability 0.30 (seeded, so a reset repeats the same spread). Smoke diffuses with decay 0.92, about 2 cells per step, and moves through vertical links (factor 0.97 upward, 0.35 downward). Routes refresh after the steps.

Response: `{"ok": true, "t": <step>}`.

### `POST /api/fire/reset`

No body. Clears fire, smoke, origins, the step counter, blocked cells, searched rooms, and the report log. Routes refresh.

Response: `{"ok": true}`.

## Routing

Routes use an 8-connected grid with no corner cutting. Fire cells and cells marked blocked by a field report are impassable. Smoke multiplies travel cost by `1 + 12 * smoke`. Stair and ramp links cost 8 m per floor, also scaled by smoke. Elevators are left out.

### `GET /api/route`

Query: `level` (int), `x` (float), `y` (float). All required.

Snaps to the nearest passable cell within 3 m, then follows the exit field.

Reachable:

| Field | Meaning |
|---|---|
| `reachable` | `true`. |
| `reason` | Empty string. |
| `path` | `[[level, x, y], ...]` from the start toward the exit. Collinear points are dropped. Coordinates are cell centers, rounded to 2 decimals. |
| `distance_m` | Walking length on one floor, rounded to 0.1 m. Stair travel is left out of this number and appears in `instructions`. |
| `max_smoke` | Highest smoke on the cells of the path, rounded to 2 decimals. |
| `exit` | Door id of the exit cell, or `null`. |
| `instructions` | Sentences such as `Walk 12 m through the hallway (317)`, `Take West Stairs down to floor 1`, `Exit the building at <door id>`. |

Unreachable:

| Field | Meaning |
|---|---|
| `reachable` | `false`. |
| `reason` | `in fire`, `not on a walkable area`, or `no safe route to any exit`. |
| `path` | `[]`. |
| `distance_m` | `null`. |
| `instructions` | `[]`. |

Errors: `404` when `level` is not a floor in the building.

### `GET /api/routes/all`

No parameters.

One route from the center of every room. Spaces of type `stairwell`, `elevator`, and `hallway` are skipped. A room with no walkable cell inside it is skipped.

Each item: `{room_id, label, level, reachable, distance_m, path}`.

## Occupants and QR

QR codes use the space id (`F3-318`). `data/qr.json` can override a code. When that file is missing or invalid, every code is resolved from the map.

An override entry needs finite `level`, `x`, and `y`. Optional fields: `heading_deg` (default 0), `room_id`, `label`.

Printed cards and the QR list skip spaces of type `stairwell`, `elevator`, and `void`.

### `GET /api/qr`

Returns a list of `{code, room_id, label, level}`. `code` and `room_id` are the space id.

### `GET /api/qr/{code}`

Returns the pose for one code.

| Field | Meaning |
|---|---|
| `code` | The requested code. |
| `room_id` | Space id, or `null` when an override names no room. |
| `label` | Room label, or the code. |
| `level` | Floor number. |
| `x`, `y` | Plan meters. For a map room, the center of the nearest walkable cell inside that room. |
| `heading_deg` | Facing in plan degrees. 0 when the pose comes from the map. |

Errors: `404` when the code is neither an override nor a space id. A room whose interior has no walkable cell also 404s.

### `GET /api/qr/print`

Returns HTML, not JSON. One card per listed room. Each QR encodes `{base}/phone.html?qr={code}`.

Query: `base` (optional). When set, it is the origin used inside the QR text, with no trailing slash. When omitted, the request's own base URL is used. Use `base` when phones must open an HTTPS tunnel host.

### `GET /api/nav`

Query: `level`, `x`, `y`. All required.

Same route object as `GET /api/route`, plus:

| Field | Meaning |
|---|---|
| `next` | `{level, x, y}` of the next waypoint, or `null`. |
| `distance_to_next_m` | Meters from the query point to `next`. `0` when there is no next point. |
| `turn` | `left`, `right`, `straight`, `stairs_up`, `stairs_down`, or `arrive`. |
| `heading_plan_deg` | Bearing toward `next` in the plan. 0 is up (negative `y`), clockwise. |
| `heading_compass_deg` | `plan_north_deg + heading_plan_deg`, wrapped to 0..360. |
| `plan_north_deg` | From the building model. |
| `hazard` | `fire_near` when fire is within 5 m, else `smoke_on_route` when `max_smoke` is above 0.35, else `null`. Fire wins over smoke. |
| `rev` | `"{world id}:{t}"`. Changes when the building object changes or the fire steps. |

The next waypoint is the first path point at least 1 m away on this floor, or the first point on another floor if the path changes floor sooner. A turn under 30 degrees is `straight`. When `reachable` is false, `next` is `null`, `distance_to_next_m` is 0, and `turn` is `arrive`.

Errors: `404` for an unknown floor.

### `GET /api/rev`

Response: `{"rev": "<world id>:<t>", "t": <step>}`.

Phones can poll this and refetch `/api/nav` when `rev` changes.

### `POST /api/people/{person_id}/status`

Body:

| Field | Meaning |
|---|---|
| `status` | `safe`, `trapped`, or `evacuating`. Required. |
| `level` | Optional int. |
| `x`, `y` | Optional floats. |
| `room_id` | Optional string. |

Stores one record per `person_id`, including `updated_at` as Unix seconds. A later post replaces the earlier one.

Response: `{"ok": true}`.

Errors: `400` when `status` is not one of the three values. `422` when the JSON does not match the body.

### `GET /api/people`

Returns the stored records: `{person_id, status, level, x, y, room_id, updated_at}`.

## Voice

Speech synthesis is Fish Audio TTS. Clips are cached at `data/tts_cache/<sha1>.mp3`. The hash covers style, voice id, and the request text. A repeat request is served from disk.

`FISH_VOICE_ID` and `FISH_VOICE_ID_FF` are voice model ids (`fish.audio/m/<id>`). They are not the Talk agent id. Agent setup is in `docs/FISH_AGENT.md`.

Speech-to-text is Fish Audio ASR (`transcribe-1-pro`) on `POST /api/voice/stt`. The same call is used by `POST /api/reports/audio`. Markers such as `<|bg|>` are stripped from the transcript. Language is English. The API key is never returned.

Occupant lines are kept short and calm. Firefighter lines are compressed (`Walk 12 m through the hallway (317)` becomes `Hallway 317, 12 meters.`). Playback speed is 0.9 for occupant and 1.15 for firefighter.

### `GET /api/voice/tts`

Query:

| Name | Meaning |
|---|---|
| `text` | Sentence to speak. Required. |
| `style` | `occupant` or `firefighter`. Required. |

Response: `audio/mpeg` bytes.

Errors:

- `400` when `style` is neither value, or the text is empty after cleanup.
- `503` when `FISH_API_KEY` is unset, the request times out (15 s), or Fish Audio returns an error or empty audio.

### `POST /api/voice/prewarm`

No body. Synthesizes a fixed list of short phrases in both styles and fills the cache.

Response: `{"cached": <count>}`. The count is the number of phrase and style pairs processed. A Fish Audio failure returns `503` and stops the batch.

### `POST /api/voice/stt`

Multipart form. Field `audio` is the recording (webm, mp4, or any bytes Fish accepts). Required.

Sends the file to Fish Audio ASR. Timeout is 30 s.

Response:

| Field | Meaning |
|---|---|
| `text` | Transcript with event markers removed, whitespace collapsed. |
| `duration` | Duration from Fish Audio, or `null` when the body has none. |

Errors:

- `400` when the file is empty (`audio is empty`).
- `503` when `FISH_API_KEY` is unset (`FISH_API_KEY is not set`), the call times out (`Fish Audio transcription timed out`), or Fish Audio fails (`Fish Audio transcription failed`).

## Localization

Sessions live in memory. Each session is a particle filter with 500 particles. A session id you have not started, or one idle for more than 30 minutes, returns `404` with detail `Unknown session`.

Every pose response:

| Field | Meaning |
|---|---|
| `session` | The session id. |
| `level` | Floor with the most particle weight. |
| `x`, `y` | Weighted mean on that floor, plan meters. |
| `spread_m` | Spread of the particles on that floor. |
| `confidence` | `1 - spread_m / 6`, clamped to 0..1. |
| `source` | `start`, `steps`, `stairs`, or `vision`. |
| `unsure` | `true` when `spread_m` is above 4. |
| `n_steps` | Step updates applied since start. |

`POST /api/loc/frame` adds `seen`, and `detail` when OCR cannot run.

Compass headings on these routes are degrees clockwise from north. The filter subtracts `plan_north_deg` to get the plan frame, where 0 is up.

### `POST /api/loc/start`

JSON body:

| Field | Meaning |
|---|---|
| `session` | Client-chosen id. Required. Starting the same id again replaces the filter. |
| `level` | Floor number. Required, and used when `room_id` is omitted. |
| `x`, `y` | Plan meters. Required. |
| `sigma_m` | Scatter in meters. Default 0.5. |
| `room_id` | Optional space id. Particles are placed on that room's walkable cells, and the room's floor is used. |

Response: the pose object. `source` is `start`.

Errors:

- `404` unknown `room_id` or unknown `level`.
- `422` when `x`, `y`, or `sigma_m` is not finite, or the body is the wrong shape.

### `POST /api/loc/steps`

JSON body:

| Field | Meaning |
|---|---|
| `session` | Required. |
| `steps` | List of `{heading_deg, length_m, t}`. `t` is optional and stored only by the client. An empty list returns the current pose. |

Each step moves particles along the heading. Length is scaled with noise. A move that crosses a wall is rejected and that particle's weight is multiplied by 0.02. `source` becomes `steps`.

Response: the pose object.

Errors: `404` unknown or expired session. `422` for a bad body.

### `POST /api/loc/stairs`

JSON body: `{"session": "...", "direction": "up"}`. `direction` is `up` or `down`.

Particles within 5 m of a stair (elevators are ignored) move to the linked floor. Particles that are not near a stair keep their floor and their weight is multiplied by 0.05. `source` becomes `stairs`.

Response: the pose object.

Errors: `400` when `direction` is not `up` or `down`. `404` for an unknown session.

### `POST /api/loc/frame`

Multipart form:

| Field | Meaning |
|---|---|
| `session` | Required. |
| `image` | JPEG file. Required. |
| `heading_deg` | Optional. Accepted and ignored. Motion stays on `/api/loc/steps`. |

RapidOCR reads the image (long side scaled to at most 960 px). Tokens are matched to room labels and ids. A one-character miss costs 0.3 score. The token `EXIT` matches exterior doors at score 0.5. The best match for each token reweights particles toward that room's door points (or its centroid). Matching uses a 3 m vision sigma. `source` becomes `vision`.

Response: the pose object plus `seen`, a list of `{text, room_id, score}`. `room_id` is `null` when the token matched no room.

If the OCR package is missing or recognition throws, the pose is unchanged, `seen` is `[]`, and `detail` explains the failure (`vision module is not available` or `vision failed: <ExceptionName>`).

Errors: `404` unknown session. `422` when `session` or `image` is missing.

### `GET /api/loc/{session}`

Returns the current pose. Refreshes the idle timer.

Errors: `404` unknown or expired session.

## Editor and check

The map editor (`/editor.html`) loads `GET /api/building`, posts edits with `POST /api/building`, saves with `POST /api/building/save`, and lists issues with `GET /api/check`.

### `GET /api/check`

No parameters. Runs the map checks on the current building. On a large map this can take a few seconds.

Response: `{"issues": ["Floor 2: ...", ...]}`.

Each issue is one sentence. Checks cover thin walls, doors off walls, rooms with no door, sealed pockets, stairwells that are not linked, floors with no stair or ramp, spaces with no walkable cells, and spaces that cannot reach an exterior exit. An empty list means the checker found nothing.

The same checks run from the command line:

```bash
python -m backend.core.checks data/buildings/dcc.json
```

## Field reports

`/firefighter.html` posts these routes. A report is speech or text. The parser returns updates. Applying them paints the grid, appends one report, and refreshes routes once. Undo restores fire, smoke, blocked cells, searched rooms, and ignition origins from the snapshot taken before that report.

A "floor N" or "level N" phrase sets the floor for later clauses until another floor is named. Clauses split on commas, semicolons, and the word "and". Room labels, room numbers, and stair names are matched against the loaded map. When `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_PAT` are set, Cortex may fill a missing `target_id`. If the rules find nothing actionable, the Cortex list is used instead. A Cortex failure leaves the rule result as-is. Rules always run.

Update object:

| Field | Meaning |
|---|---|
| `kind` | `fire`, `smoke`, `clear`, `searched`, `blocked`, `open`, `victim`, or `note`. |
| `target_id` | Space id or vertical id, or `null` when no place was named. |
| `target_label` | Room or stair label, or `null`. |
| `level` | Floor number, or `null`. |
| `severity` | `light`, `heavy`, or `null`. Set only for `smoke`. |
| `count` | Integer for `victim`, otherwise `null`. A bare "victim" counts as 1. |
| `raw` | The clause text. |

What an applied update does:

| Kind | Effect |
|---|---|
| `fire` | Ignites the room, or each stair cell. |
| `smoke` | Raises smoke to at least 0.5, or 0.9 when `severity` is `heavy`. |
| `clear` | Turns off fire, smoke, and blocked on those cells, and drops ignition origins there. |
| `blocked` | Marks cells impassable. A room uses its walkable cells. A stair uses a 1.5 m disk on each floor it serves. |
| `open` | Clears the blocked flag on those cells. |
| `searched` | Stores `{by, t, note}` on the room id. `t` is Unix seconds. `note` is the clause. |
| `victim`, `note` | Recorded on the report only. The grid does not change. |

Blocked cells are impassable on the next route, together with fire. Smoke written here shows up on `GET /api/state`.

Stored report:

| Field | Meaning |
|---|---|
| `id` | 8 hex characters. |
| `t` | Unix seconds. |
| `by` | Reporter name, or `""`. |
| `text` | The full report text. |
| `updates` | The update list. |
| `changes` | Short lines such as `Fire in 318`, `Routes updated`. |

### `POST /api/reports`

JSON body:

| Field | Meaning |
|---|---|
| `text` | Required. Whitespace is collapsed. Empty text is `400` (`text is required`). |
| `by` | Optional reporter name. |
| `dry_run` | Optional, default `false`. When `true`, the text is parsed and nothing is stored or painted. |

Response:

| Field | Meaning |
|---|---|
| `report` | On a real apply that produced updates, the stored report. On `dry_run`, the same shape with `id` and `t` set to `null` and `changes` set to `[]`. If a real apply yields no updates, nothing new is stored and `report` is the newest stored report, or `null`. |
| `changes` | Lines from the apply. `[]` on `dry_run` or when there is nothing to apply. A real apply that changes the map ends with `Routes updated`. |
| `confirmation` | Read-back, such as `Copy. 318 on fire. Routes updated.` A dry run omits `Routes updated.` An empty parse is `Copy. Nothing to add.` |

Errors: `400` when `text` is empty. `422` when the body is the wrong shape.

### `POST /api/reports/audio`

Multipart form. Field `audio` is the recording. Optional form field `by`.

Transcribes with the same ASR call as `POST /api/voice/stt`, then applies the transcript (never a dry run).

Response: the same `{report, changes, confirmation}` object as `POST /api/reports`.

Errors: `400` when the audio is empty. `503` for a missing `FISH_API_KEY` or a failed transcription. See `POST /api/voice/stt`.

### `GET /api/reports`

No parameters. The stored reports, newest first. `[]` when none have been applied.

### `GET /api/reports/searched`

No parameters. One object. Each searched room id maps to `{by, t, note}`. The key `unsearched` is a list of `{id, label, level}` for rooms not yet searched. Spaces of type `stairwell`, `elevator`, and `void` are left out of that list.

### `POST /api/reports/undo`

No body. Reverses the newest report and refreshes routes.

Response: `{"ok": true|false, "changes": ["..."]}`.

| Case | `ok` | `changes` |
|---|---|---|
| A snapshot was restored | `true` | `Undid report <id>`, `Routes updated` |
| The report had no snapshot | `true` | `Removed report <id>`, `Routes updated` |
| The log is empty | `false` | `No report to undo` |
| The grid shape no longer matches | `false` | `Could not undo the last report` (the report stays) |

This route returns HTTP 200 in every case above.

## Commander brief

`/command.html` calls these routes. Facts come from the live fire, exits, check-ins, and routes. The system prompt tells the model to use only those facts, to say Unknown when a fact is missing, to avoid inventing people or positions, and to say that fire data is simulated.

`GET /api/brief` works without Snowflake. `POST /api/ask` does not.

Cortex is configured when `SNOWFLAKE_ACCOUNT` matches `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` and `SNOWFLAKE_PAT` is set. The model is `SNOWFLAKE_MODEL` or `claude-sonnet-4-5`. Timeout is 30 s. The token is never returned.

`facts` on the brief:

| Field | Meaning |
|---|---|
| `t` | Fire step count. |
| `fire_rooms` | `{id, label, level}` for rooms whose status is `fire`. |
| `smoke_rooms` | Same shape, status `smoke`. |
| `blocked_stairs` | Stairs and ramps that are not `clear`: `{id, name, type, status, floors}`. Elevators are left out. `status` is `fire` or `smoke`. |
| `exits` | `{id, level, x, y}` sorted by floor, then id. |
| `people_counts` | Counts by check-in status. Keys include `evacuating`, `trapped`, and `safe`. |
| `trapped` | `{person_id, room_id, room_label, level}` for people whose status is `trapped`. |
| `no_safe_route` | `{id, label, level, reason}` for rooms with no exit route. Types `stairwell`, `elevator`, and `void` are skipped. |

### `GET /api/brief`

Query: `style`. `commander` (default) or `radio`. `radio` asks Cortex for terse radio phrasing. The template text does not change with style.

Response: `{"text": "...", "model": "<name or template>", "facts": { ... }}`.

`model` is `template` when Cortex is off or the call fails. Otherwise it is the configured model name. `text` is then the template briefing, which always says that fire data is simulated.

Errors: `400` when `style` is neither value (`style must be commander or radio`).

### `POST /api/ask`

JSON body: `{"question": "..."}`. Whitespace-only is rejected.

The server picks up to 8 rooms whose labels, ids, types, or door ids match the question, then asks Cortex. Questions that mention stairs or exits also include those rooms.

Response:

| Field | Meaning |
|---|---|
| `answer` | Cortex text. |
| `model` | Configured model name. |
| `sources` | Room ids sent as context, in rank order. |

Errors:

- `400` when `question` is empty (`question is required`).
- `503` when Cortex is not configured (`Snowflake Cortex is not configured`) or the call fails (`Snowflake Cortex request failed`).
- `422` when the body is the wrong shape.

## History

MongoDB Atlas. Collections: `events` and `buildings`. Indexes: `(kind, ts)` on events, `(name, version)` on buildings.

`MONGODB_URI` empty: `log` and map-version saves do nothing. `GET /api/history/status` still returns 200. The other history routes return `503` with detail `History needs MongoDB`.

`MONGODB_DB` defaults to `firesight`.

Events are inserted on a background thread. Each stored event has `kind`, `ts` (UTC), the fields that were logged, and `building` when the payload had `building` or `name`. `loc_pose` is kept to one insert per session per 2 seconds.

Kinds the server logs: `building_upload`, `building_save`, `fire_start`, `fire_step`, `fire_reset`, `person_status`, `loc_pose`, `field_report`.

Public events use a string `_id` and an ISO-8601 `ts` ending in `Z`.

### `GET /api/history/status`

No parameters. Always available.

Response: `{"enabled": true|false, "db": "<name>"}`. `enabled` is true when `MONGODB_URI` is set. This route does not open a connection.

### `GET /api/history/events`

Query:

| Name | Meaning |
|---|---|
| `kind` | Optional. When set, only that kind. |
| `limit` | Optional. Default 100. Minimum 1, maximum 1000. |

Response: a list of events, newest first.

Errors: `503` when history is off or the query fails. `422` when `limit` is outside 1 to 1000.

### `GET /api/history/timeline`

Query: `since` (optional ISO-8601 timestamp). A trailing `Z` is accepted.

Returns `fire_*` events and `person_status` events, oldest first. `since` is inclusive. Other kinds are left out, including `field_report` and `loc_pose`.

Errors: `400` when `since` is not an ISO timestamp (`since must be an ISO timestamp`). `503` when history is off or the query fails.

### `GET /api/history/buildings`

No parameters. Latest version of each saved map: `{name, version, saved_at}`. `saved_at` is ISO-8601 UTC. Sorted by name.

Errors: `503` when history is off or the query fails.

### `GET /api/history/buildings/{name}`

Query: `version` (optional integer). Omit it to load the latest version.

Response: `{"name", "version", "saved_at", "doc"}`. `doc` is the building JSON stored at that version.

Errors: `404` when that name or version is missing (`Unknown building <name>`). `503` when history is off or the query fails.
