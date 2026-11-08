/* Firesight voice agent. Fish Audio web client, no build step. */
import { AgentSession } from "https://cdn.jsdelivr.net/npm/@fishaudio/agent-client@0.3.0/+esm";

const DEFAULT_AGENT_ID = "b6e8fe7adad842678c869269def9344b";
const ROOM_CAP = 15;

function readState(getState) {
  try {
    const value = typeof getState === "function" ? getState() : null;
    return value && typeof value === "object" ? value : {};
  } catch (err) {
    return {};
  }
}

function agentIdFromPage() {
  try {
    const raw = new URLSearchParams(window.location.search).get("agent");
    const id = raw ? String(raw).trim() : "";
    if (id) return id;
  } catch (err) {}
  return DEFAULT_AGENT_ID;
}

function hasPose(state) {
  return state.level != null && state.level !== ""
    && Number.isFinite(Number(state.x)) && Number.isFinite(Number(state.y));
}

function round1(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return Math.round(n * 10) / 10;
}

function dynamicVariables(state) {
  const exit = state.nav && state.nav.exit ? String(state.nav.exit) : "unknown";
  const floor = state.level == null || state.level === "" ? "unknown" : String(state.level);
  return {
    room: state.room_label ? String(state.room_label) : "unknown",
    floor: floor,
    exit: exit,
    building: state.building_name ? String(state.building_name) : "this building"
  };
}

async function fetchJSON(url, options) {
  const res = await fetch(url, Object.assign({ cache: "no-store" }, options || {}));
  if (!res.ok) {
    const err = new Error("http " + res.status);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function firstInstructions(nav) {
  const list = nav && Array.isArray(nav.instructions) ? nav.instructions : [];
  const out = [];
  for (const line of list) {
    if (line == null || line === "") continue;
    out.push(String(line));
    if (out.length >= 3) break;
  }
  return out;
}

function nextFloor(nav, level) {
  const next = nav && nav.next;
  if (!next || next.level == null || next.level === "") return null;
  if (Number(next.level) === Number(level)) return null;
  return next.level;
}

function indexById(list) {
  const map = new Map();
  if (!Array.isArray(list)) return map;
  for (const item of list) {
    if (item && item.id != null) map.set(String(item.id), item);
  }
  return map;
}

function bannerFromNav(nav) {
  if (!nav) return "Finding a route.";
  if (nav.reachable === false) return "No safe route. Follow the exit signs.";
  const bits = [];
  if (nav.hazard === "fire_near") bits.push("Fire ahead. Turn around.");
  else if (nav.hazard === "smoke_on_route") bits.push("Smoke ahead. Stay low.");
  if (nav.turn) bits.push(String(nav.turn).replaceAll("_", " "));
  if (nav.exit) bits.push(String(nav.exit));
  return bits.join(" ") || "Finding a route.";
}

function clientTools(getState) {
  return {
    get_route: async function () {
      const state = readState(getState);
      if (!hasPose(state)) return { error: "No position yet." };
      const url = "/api/nav?level=" + encodeURIComponent(state.level)
        + "&x=" + encodeURIComponent(Number(state.x).toFixed(2))
        + "&y=" + encodeURIComponent(Number(state.y).toFixed(2));
      const nav = await fetchJSON(url);
      return {
        reachable: nav.reachable !== false,
        turn: nav.turn || "arrive",
        distance_to_next_m: round1(nav.distance_to_next_m) == null ? 0 : round1(nav.distance_to_next_m),
        exit: nav.exit || null,
        next_floor: nextFloor(nav, state.level),
        hazard: nav.hazard || null,
        instructions: firstInstructions(nav)
      };
    },

    where_am_i: async function () {
      const state = readState(getState);
      const unsure = state.unsure === true;
      const out = {
        level: state.level == null || state.level === "" ? null : state.level,
        x: round1(state.x),
        y: round1(state.y),
        room_label: state.room_label ? String(state.room_label) : "",
        confidence: Number.isFinite(Number(state.confidence))
          ? Math.round(Number(state.confidence) * 100) / 100
          : null,
        unsure: unsure
      };
      if (out.level == null || out.x == null || out.y == null) {
        out.note = "No position yet. Ask them to scan a room code or pick their room.";
      } else if (unsure) {
        out.note = "Position is unsure. Do not guess. Tell them to point the camera at a door number or follow the exit signs.";
      }
      return out;
    },

    get_hazards: async function () {
      const state = readState(getState);
      if (state.level == null || state.level === "") return { error: "No floor yet." };
      const data = await fetchJSON("/api/state");
      const known = indexById(state.floor_rooms);
      const useKnown = known.size > 0;
      const prefix = "F" + state.level + "-";
      const rooms = data.rooms && typeof data.rooms === "object" ? data.rooms : {};
      const fireRooms = [];
      const smokeRooms = [];
      for (const id of Object.keys(rooms)) {
        const status = rooms[id];
        if (status !== "fire" && status !== "smoke") continue;
        const meta = known.get(id);
        if (useKnown && !meta) continue;
        if (!useKnown && id.indexOf(prefix) !== 0) continue;
        const row = { id: id, label: meta && meta.label ? String(meta.label) : id };
        if (status === "fire") fireRooms.push(row);
        else smokeRooms.push(row);
      }
      const verts = indexById(state.verticals);
      const verticals = data.verticals && typeof data.verticals === "object" ? data.verticals : {};
      const blocked = [];
      for (const id of Object.keys(verticals)) {
        const status = verticals[id];
        if (status !== "fire" && status !== "smoke") continue;
        const meta = verts.get(id);
        const type = meta && meta.type ? String(meta.type) : "";
        if (type === "elevator") continue;
        if (type && type !== "stairs" && type !== "ramp") continue;
        blocked.push({
          id: id,
          name: meta && meta.name ? String(meta.name) : id,
          status: status
        });
      }
      return {
        floor: state.level,
        fire_rooms: fireRooms.slice(0, ROOM_CAP),
        smoke_rooms: smokeRooms.slice(0, ROOM_CAP),
        blocked_stairs: blocked.slice(0, ROOM_CAP),
        fire_count: fireRooms.length,
        smoke_count: smokeRooms.length
      };
    },

    report_status: async function (params) {
      const status = params && params.status != null ? String(params.status) : "";
      if (status !== "safe" && status !== "trapped" && status !== "evacuating") {
        return { ok: false, error: "Status must be safe, trapped, or evacuating." };
      }
      const state = readState(getState);
      if (!state.session_id) return { ok: false, error: "No session." };
      const body = { status: status };
      if (state.level != null && state.level !== "" && Number.isFinite(Number(state.level))) {
        body.level = Math.trunc(Number(state.level));
      }
      if (Number.isFinite(Number(state.x))) body.x = Number(Number(state.x).toFixed(2));
      if (Number.isFinite(Number(state.y))) body.y = Number(Number(state.y).toFixed(2));
      if (state.room_id) body.room_id = String(state.room_id);
      await fetchJSON("/api/people/" + encodeURIComponent(state.session_id) + "/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      return { ok: true, status: status };
    },

    repeat_instruction: async function () {
      const state = readState(getState);
      const text = state.banner ? String(state.banner) : bannerFromNav(state.nav);
      return { text: text || "Finding a route." };
    }
  };
}

function wrapTools(tools) {
  const wrapped = {};
  for (const name of Object.keys(tools)) {
    const handler = tools[name];
    wrapped[name] = async function (params, ctx) {
      try {
        return await handler(params || {}, ctx || {});
      } catch (err) {
        return { error: "Tool failed." };
      }
    };
  }
  return wrapped;
}

export function createFiresightAgent({ getState, onStatus, onTranscript } = {}) {
  let session = null;
  let running = false;
  let starting = false;
  let generation = 0;

  function emitStatus(mode) {
    try {
      if (typeof onStatus === "function") onStatus(mode);
    } catch (err) {}
  }

  function emitLine(message) {
    try {
      if (typeof onTranscript === "function") onTranscript(message);
    } catch (err) {}
  }

  async function start() {
    if (running || starting) return;
    starting = true;
    const gen = ++generation;
    try {
      const state = readState(getState);
      let dropped = false;
      const created = await AgentSession.start({
        agentId: agentIdFromPage(),
        dynamicVariables: dynamicVariables(state),
        clientTools: wrapTools(clientTools(getState)),
        callbacks: {
          onModeChange(mode) {
            if (gen !== generation || dropped) return;
            emitStatus(mode);
          },
          onMessage(message) {
            if (gen !== generation || dropped || !message) return;
            emitLine({ role: message.role, text: message.text });
          },
          onDisconnect() {
            dropped = true;
            if (session !== created) return;
            session = null;
            running = false;
            emitStatus("off");
          }
        }
      });
      if (gen !== generation || dropped) {
        try { await created.end(); } catch (err) {}
        if (gen === generation) emitStatus("off");
        return;
      }
      session = created;
      running = true;
      emitStatus(created.mode || "listening");
    } catch (err) {
      if (gen === generation) {
        session = null;
        running = false;
      }
      throw err;
    } finally {
      if (gen === generation) starting = false;
    }
  }

  async function stop() {
    generation += 1;
    starting = false;
    const current = session;
    session = null;
    running = false;
    emitStatus("off");
    if (current) {
      try { await current.end(); } catch (err) {}
    }
  }

  function say(text) {
    if (!session || text == null || text === "") return;
    try {
      session.sendUserMessage(String(text), { audio: true });
    } catch (err) {}
  }

  return {
    start: start,
    stop: stop,
    say: say,
    get active() { return running; }
  };
}
