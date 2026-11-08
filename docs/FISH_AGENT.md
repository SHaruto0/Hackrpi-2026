# Fish Audio evacuation agent

The phone page starts this agent when someone taps Talk. The default public agent id is `b6e8fe7adad842678c869269def9344b`. A page opened as `phone.html?agent=<id>` uses that id instead.

No API key goes in the page. The agent must be public, published, and allowed for the page origin.

## Dashboard setup

1. Open the agent in the Fish Audio console.
2. Paste the system prompt and the first message below. Set the first message to fixed text, so `{{room}}` and `{{floor}}` are filled once and then spoken.
3. Add the five client tools. Type is `client`. `expects_response` is true. `parameter_mode` is `strict`. Names must match the code exactly.
4. Allow-list the dynamic variables (next section).
5. On Deploy, turn on Public access. Under Allowed origins, add every origin that will open the phone page. Matching is scheme, host, and port. For a laptop demo add `http://localhost:8000`. If you use an HTTPS tunnel, add that origin too. `localhost` and `127.0.0.1` are different origins.
6. Publish. Callers hear the published version only.

Pick a calm voice. The page does not choose the voice.

## System prompt

```text
You are a calm evacuation guide for {{building}}. You help one person leave during a fire. Speak in short, plain sentences. Stay calm.

Use your tools before you give a direction.
Call where_am_i when you need their place. If unsure is true, or there is no position, say you are not sure. Tell them to point the camera at a door number, or to follow the exit signs. Never guess a room, a floor, or a distance.
Call get_route for the next turn. If reachable is false, say there is no safe route and to follow the exit signs. Otherwise say only the turn, the distance, the exit, and the hazard the tool returned.
Call get_hazards before you send them toward stairs, and whenever they ask about fire or smoke. If a stair is blocked, say so.
Call repeat_instruction when they ask you to say it again. Read that text.
Call report_status when they say they are safe, trapped, or still leaving. Confirm the status you sent.

If a tool returns an error, tell them to follow the exit signs. Do not invent a path.

Do not give medical advice. Do not tell them to fight the fire.

They started in {{room}} on floor {{floor}}. The planned exit is {{exit}}. Those facts can go stale. Trust the tools over this opening.

If a user message starts with [system], it is an update from the app, not the person speaking. Tell them the new direction in one short sentence.
```

## First message

```text
You are in {{room}} on floor {{floor}}. I will help you get out. Follow the arrow, and ask if you need a direction.
```

## Dynamic variables

The phone sends these four names on every Talk tap. Allow-list only these names.

| Name | Value |
|---|---|
| `room` | Room label, or `unknown` |
| `floor` | Floor number as text, or `unknown` |
| `exit` | Exit name from the route, or `unknown` |
| `building` | Building name, or `this building` |

In the dashboard, open Configuration, then the Default values link under the system prompt. Add each name with a spoken default:

- `room`: `your room`
- `floor`: `this floor`
- `exit`: `the nearest exit`
- `building`: `this building`

Set missing values to use those defaults, so a gap is never spoken as `{{room}}`. The phone does not send any other custom variable.

## Client tools

Each tool's parameters object is the schema to paste. Results stay small.

### get_route

Get the live route from the person's current position.

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

Returns `reachable`, `turn`, `distance_to_next_m`, `exit`, `next_floor`, `hazard`, and `instructions` (at most 3 lines). `next_floor` is null when the next waypoint stays on this floor. On failure: `{"error":"No position yet."}` or `{"error":"Tool failed."}`.

### where_am_i

Read the latest known position. Do not guess past this result.

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

Returns `level`, `x`, `y`, `room_label`, `confidence`, and `unsure`. When `unsure` is true, or there is no position, `note` tells you what to say. Tell the person to point the camera at a door number or follow the exit signs.

### get_hazards

List fire and smoke on the person's floor, and stairs that are blocked.

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

Returns `floor`, `fire_rooms` (`id`, `label`), `smoke_rooms`, `blocked_stairs` (`id`, `name`, `status` of `fire` or `smoke`), `fire_count`, and `smoke_count`. Lists are capped at 15. Counts are the full totals. Elevators are left out.

### report_status

Save that the person is safe, trapped, or still evacuating, with their latest position.

```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": ["safe", "trapped", "evacuating"],
      "description": "safe, trapped, or evacuating."
    }
  },
  "required": ["status"],
  "additionalProperties": false
}
```

Returns `{"ok":true,"status":"safe"}` or `{"ok":false,"error":"..."}`.

### repeat_instruction

Read the direction currently on screen.

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

Returns `{"text":"..."}`. Say that text. Do not add a turn the text does not contain.

## Phone behavior

Talk starts the session from the tap, which is what the microphone permission needs. A second tap hangs up. While the agent is on, the page's own spoken directions stay quiet. The last agent line shows as a caption. If the SDK fails to load or the session fails to start, the page shows "Voice agent unavailable" and the map, arrow, and buttons keep working.

When the route changes during a live session, the page sends:

```text
[system] The route changed: <banner text>. Tell the user briefly.
```

The agent should answer that in one short spoken sentence.
