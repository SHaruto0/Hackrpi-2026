import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  Animated,
  Easing,
  StyleSheet,
  Text,
  View,
  Image,
  TextInput,
  TouchableOpacity,
  ScrollView,
  Dimensions,
  SafeAreaView,
  Platform,
  NativeModules,
  DeviceEventEmitter,
  Vibration,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { Accelerometer, Magnetometer } from 'expo-sensors';
import Svg, { Circle, Defs, G, LinearGradient, Path, Stop, Text as SvgText } from 'react-native-svg';
import { WebView } from 'react-native-webview';
import {
  AudioModule,
  RecordingPresets,
  createAudioPlayer,
  setAudioModeAsync,
  useAudioRecorder,
} from 'expo-audio';
import * as Speech from 'expo-speech';

// Push-to-talk recording. iOS: 16 kHz mono 16-bit WAV. The bit rate must equal
// rate x depth x channels (256 kbps) or iOS fails with "unsupported data format".
// Android (and the iOS fallback): AAC .m4a, 44.1 kHz mono.
const TALK_AAC = {
  extension: '.m4a',
  sampleRate: 44100,
  numberOfChannels: 1,
  bitRate: 96000,
  android: { outputFormat: 'mpeg4', audioEncoder: 'aac' },
  ios: { outputFormat: 'aac ', audioQuality: 96 },
  web: { mimeType: 'audio/webm', bitsPerSecond: 96000 },
};
const TALK_WAV = {
  extension: '.wav',
  sampleRate: 16000,
  numberOfChannels: 1,
  bitRate: 256000,
  android: { outputFormat: 'mpeg4', audioEncoder: 'aac' },
  ios: {
    outputFormat: 'lpcm',
    audioQuality: 96,
    linearPCMBitDepth: 16,
    linearPCMIsBigEndian: false,
    linearPCMIsFloat: false,
  },
  web: { mimeType: 'audio/webm', bitsPerSecond: 96000 },
};
const TALK_RECORDING = Platform.OS === 'ios' ? TALK_WAV : TALK_AAC;
import appConfig from './app.json';
import Constants from 'expo-constants';

const { width: SCREEN_W, height: SCREEN_H } = Dimensions.get('window');

function pulseTrappedHaptic() {
  try {
    const modName = 'expo-haptics';
    const Haptics = require(modName);
    if (Haptics?.notificationAsync && Haptics?.NotificationFeedbackType) {
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
      return;
    }
  } catch (_) {}
  try {
    Vibration.vibrate(180);
  } catch (_) {}
}

// Extra correction on top of the tilt-compensated compass (0 = none).
const HEADING_OFFSET_DEG = 0;

const FALLBACK_SERVER_BASE = 'http://129.161.93.234:8000';

function defaultServerBase() {
  const raw = appConfig?.expo?.extra?.serverUrl;
  if (typeof raw === 'string' && raw.trim()) return raw.trim().replace(/\/$/, '');
  // Dev: the laptop running Metro also runs the server on :8000.
  const host = String(Constants?.expoConfig?.hostUri || Constants?.manifest2?.extra?.expoGo?.debuggerHost || '').split(':')[0];
  // Only a LAN IP works; with `expo start --tunnel` the host is the tunnel domain, not the laptop.
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) return `http://${host}:8000`;
  return FALLBACK_SERVER_BASE;
}

// Same host as /ws/ar, always http(s), no socket path.
function httpServerBase(base) {
  let b = String(base || '').trim().replace(/\/$/, '');
  if (!b) b = defaultServerBase();
  if (/^wss?:\/\//i.test(b)) b = b.replace(/^ws/i, 'http');
  else if (!/^https?:\/\//i.test(b)) b = `http://${b}`;
  return b.replace(/\/ws\/ar$/i, '');
}

// One clip at a time. A newer line for the same slot replaces a queued one.
// Identical text inside 10s is skipped. Non-200 or network failure uses expo-speech.
function createVoicePlayer(getContext) {
  const queue = [];
  const recent = new Map();
  let playing = false;
  let playingSlot = null;
  let currentSound = null;
  let cancelPlayback = null;
  let closed = false;
  let hushed = false;
  let audioReady = null;
  const PRIORITY = new Set(['mayday', 'ff-report']);

  const releasePlayer = (player) => {
    if (!player) return;
    try {
      player.remove();
    } catch (_) {}
  };

  const ensureAudio = () => {
    if (!audioReady) {
      audioReady = setAudioModeAsync({
        playsInSilentMode: true,
        shouldPlayInBackground: false,
      }).catch(() => {});
    }
    return audioReady;
  };

  const speakExpo = (text, opts) =>
    new Promise((resolve) => {
      try {
        Speech.stop();
        Speech.speak(text, {
          rate: opts?.rate ?? 1.05,
          pitch: opts?.pitch ?? 0.95,
          onDone: resolve,
          onStopped: resolve,
          onError: resolve,
        });
      } catch (_) {
        resolve();
      }
    });

  const speakFish = async (text) => {
    const { base, style } = getContext();
    const url = `${base}/api/voice/tts?text=${encodeURIComponent(text)}&style=${style}`;
    await ensureAudio();
    const res = await fetch(url);
    if (!res.ok) throw new Error(String(res.status));
    let player = null;
    try {
      await new Promise((resolve, reject) => {
        let settled = false;
        const finish = (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (err) reject(err);
          else resolve();
        };
        const timer = setTimeout(() => finish(), 30000);
        cancelPlayback = () => finish();
        try {
          player = createAudioPlayer(url);
          currentSound = player;
          player.addListener('playbackStatusUpdate', (status) => {
            if (status?.error) finish(new Error(String(status.error)));
            else if (status?.isLoaded && status.didJustFinish) finish();
          });
          if (closed || settled) {
            currentSound = null;
            finish();
            return;
          }
          player.play();
        } catch (err) {
          finish(err);
        }
      });
    } finally {
      if (currentSound === player) currentSound = null;
      cancelPlayback = null;
      releasePlayer(player);
    }
  };

  // One voice for the whole session: Fish unless it fails twice in a row,
  // then the device voice from then on. Never alternate between the two.
  let engine = 'fish';
  let fishFails = 0;
  const drain = async () => {
    if (playing || closed) return;
    const item = queue.shift();
    if (!item) return;
    playing = true;
    playingSlot = item.slot;
    try {
      if (engine === 'fish') {
        try {
          await speakFish(item.text);
          fishFails = 0;
        } catch (_) {
          fishFails += 1;
          if (fishFails >= 2) engine = 'device';
        }
      } else if (!closed) {
        await speakExpo(item.text, item.opts);
      }
    } finally {
      playing = false;
      playingSlot = null;
      if (!closed) drain();
    }
  };

  return {
    enqueue(text, slot, opts) {
      if (closed) return;
      const t = String(text || '').replace(/\s+/g, ' ').trim();
      if (!t) return;
      const now = Date.now();
      for (const [key, ts] of recent) {
        if (now - ts >= 10000) recent.delete(key);
      }
      const prev = recent.get(t);
      if (prev && now - prev < 10000) return;
      recent.set(t, now);
      for (let i = queue.length - 1; i >= 0; i--) {
        if (queue[i].slot === slot) queue.splice(i, 1);
      }
      if (hushed) return;
      // One voice at a time: the newest line replaces anything waiting,
      // and interrupts the current one unless that one is urgent.
      queue.length = 0;
      queue.push({ text: t, slot, opts });
      if (playing && !PRIORITY.has(playingSlot)) {
        if (cancelPlayback) cancelPlayback();
        else {
          try {
            Speech.stop();
          } catch (_) {}
        }
      }
      drain();
    },
    clear() {
      queue.length = 0;
      if (cancelPlayback) cancelPlayback();
      try {
        Speech.stop();
      } catch (_) {}
    },
    hush(on) {
      hushed = !!on;
      if (!hushed) return;
      queue.length = 0;
      if (cancelPlayback) cancelPlayback();
      const s = currentSound;
      if (s) {
        try { s.pause(); } catch (_) {}
      }
      try {
        Speech.stop();
      } catch (_) {}
    },
    close() {
      closed = true;
      queue.length = 0;
      if (cancelPlayback) cancelPlayback();
      const s = currentSound;
      currentSound = null;
      releasePlayer(s);
      try {
        Speech.stop();
      } catch (_) {}
    },
  };
}

// Same meters as backend/services/vps.py arkit_to_dcc_plan.
const ARKIT_TO_DCC_X0 = 40.1;
const ARKIT_TO_DCC_Y0 = 60.0;
const ARKIT_DEFAULT_LEVEL = 2;
const ARKIT_FLOOR_M = 4.0;

function arkitToPlan(pos, level) {
  const x = Number(pos?.[0]) || 0;
  const z = Number(pos?.[2]) || 0;
  const lv = Number.isFinite(Number(level)) ? Number(level) : ARKIT_DEFAULT_LEVEL;
  return {
    level: lv,
    x: Math.round((ARKIT_TO_DCC_X0 + x) * 1000) / 1000,
    y: Math.round((ARKIT_TO_DCC_Y0 + z) * 1000) / 1000,
  };
}

function planToArkit(x, y, level) {
  const lv = Number.isFinite(Number(level)) ? Number(level) : ARKIT_DEFAULT_LEVEL;
  return [
    Math.round((Number(x) - ARKIT_TO_DCC_X0) * 1000) / 1000,
    Math.round((lv - ARKIT_DEFAULT_LEVEL) * ARKIT_FLOOR_M * 1000) / 1000,
    Math.round((Number(y) - ARKIT_TO_DCC_Y0) * 1000) / 1000,
  ];
}

function errorText(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  return fallback;
}

// http(s) base, host:port, or an already-built ws URL -> /ws/ar on that host.
function arSocketUrl(base) {
  let b = String(base || '').trim().replace(/\/$/, '');
  if (!b) b = defaultServerBase();
  if (/^wss?:\/\//i.test(b)) return /\/ws\/ar$/i.test(b) ? b : `${b}/ws/ar`;
  if (!/^https?:\/\//i.test(b)) b = `http://${b}`;
  return `${b.replace(/^http/i, 'ws')}/ws/ar`;
}

// Quick-pick DCC Floor 2 rooms for Firefighter Target-Room Navigation
const QUICK_DCC_ROOMS = [
  { id: 'F2-232', short: 'Rm 232' },
  { id: 'F2-234', short: 'Rm 234' },
  { id: 'F2-236', short: 'Rm 235/236' },
  { id: 'F2-238', short: 'Rm 238' },
  { id: 'F2-239', short: 'Rm 239' },
  { id: 'F2-240', short: 'Rm 240' },
  { id: 'F2-218', short: '218 Lobby' },
  { id: 'F2-2ST6', short: 'Stair 2ST6' },
  { id: 'F2-308L', short: '308 Lecture' },
];

// Audio cue voice lines (triggered by JSON flags from centralized backend)
// ---- Demo script ---------------------------------------------------------
// Marker starts in DCC 232 and only moves along the route (steps or the Walk
// button). Facing is assumed to follow the path. It waits at the 218 doorway;
// when Command sets 218 on fire the route flips, the app says "Turn around",
// then "Go straight" down hallway 219. Set DEMO = false for real sensors.
// const DEMO = true; // scripted 232 -> 218 walk (hardcoded path), commented out
const DEMO = false;   // position comes from the camera (VPS) again
const DEMO_START = { level: 2, x: 40.06, y: 59.99 }; // 232 CLASSROOM
// Before the fire the marker stops right before 218 LOBBY (its east edge is
// x = 35.42 m) and waits there.
const DEMO_STOP_X = 35.7;
const inDemoRoom = (x) => x >= DEMO_STOP_X;
const DEMO_SPEED_MPS = 1.2; // normal walking pace; drives marker + instructions over time
const DEMO_TURN_MS = 3500;

// Firefighter destination is fixed for the demo.
const FF_TARGET_ROOM = 'F2-228';
const FF_TARGET_LABEL = '228 VESTIBULE';

const AUDIO_CUE_PHRASES = {
  'system_online.mp3': 'Firesight 3D D C C Navigation Online.',
  'hazard_ahead_rerouting.mp3': 'Warning! Fire hazard reported ahead. Rerouting path immediately.',
  'route_recalculated.mp3': 'Route updated with latest live fire data.',
  'proceed_to_next_waypoint.mp3': 'Continue along the highlighted 3D path.',
  'arrived_at_exit.mp3': 'You have reached your destination.',
  'mayday_transmitting.mp3': 'Mayday alert transmitted to Incident Command.',
  'mayday_acknowledged.mp3': 'Incident Commander acknowledged your Mayday.',
};

function wrap180(deg) {
  const d = ((Number(deg) % 360) + 360) % 360;
  return d > 180 ? d - 360 : d;
}

function norm360(deg) {
  return ((Number(deg) % 360) + 360) % 360;
}

// Plan bearing: 0 = up the plan (-y), clockwise. Matches backend nav_hints.
function bearingPlan(x1, y1, x2, y2) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  if (dx * dx + dy * dy < 1e-8) return null;
  return norm360((Math.atan2(dx, -dy) * 180) / Math.PI);
}

function turnName(heading, headingAfter) {
  if (heading == null || headingAfter == null) return 'straight';
  const delta = wrap180(headingAfter - heading);
  if (Math.abs(delta) < 30) return 'straight';
  return delta > 0 ? 'right' : 'left';
}

// Next path point at least 1.5 m ahead, or the first point on another floor.
function hintsFromPath(path, level, x, y, planNorth) {
  const pts = [];
  for (const pt of path || []) {
    if (!Array.isArray(pt) || pt.length < 3) continue;
    const pl = Number(pt[0]);
    const px = Number(pt[1]);
    const py = Number(pt[2]);
    if (!Number.isFinite(pl) || !Number.isFinite(px) || !Number.isFinite(py)) continue;
    pts.push({ pl, px, py });
  }
  const lv = Number(level);
  let chosen = null;
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i];
    const dist = Math.hypot(p.px - x, p.py - y);
    if (p.pl !== lv || dist >= 1.5) {
      chosen = { i, dist, ...p };
      break;
    }
  }
  const north = Number.isFinite(Number(planNorth)) ? Number(planNorth) : 0;
  if (!chosen) {
    return {
      next: null,
      distance_to_next_m: 0,
      turn: 'arrive',
      heading_compass_deg: norm360(north),
    };
  }
  const heading = bearingPlan(x, y, chosen.px, chosen.py);
  let turn = 'straight';
  if (chosen.pl !== lv) turn = chosen.pl > lv ? 'stairs_up' : 'stairs_down';
  else if (chosen.i + 1 >= pts.length) turn = 'arrive';
  else {
    const fol = pts[chosen.i + 1];
    turn = turnName(heading, bearingPlan(chosen.px, chosen.py, fol.px, fol.py));
  }
  return {
    next: { level: chosen.pl, x: chosen.px, y: chosen.py },
    distance_to_next_m: Math.round(chosen.dist * 10) / 10,
    turn,
    heading_compass_deg: norm360(north + (heading == null ? 0 : heading)),
  };
}

function straightLabel(meters) {
  const n = Math.round(Number(meters) || 0);
  return n > 0 ? `Go straight ${n} m` : 'Go straight';
}

function arrowFromRel(rel) {
  if (Math.abs(rel) <= 30) return 'straight';
  if (rel > 30 && rel <= 150) return 'right';
  if (rel < -30 && rel >= -150) return 'left';
  return 'around';
}

function buildArrow(nav, phoneHeading, compassOk, role) {
  if (!nav) return { state: 'finding', label: 'Finding your route...' };
  if (nav.reachable === false) {
    return { state: 'blocked', label: nav.reason ? String(nav.reason) : 'No safe route' };
  }
  const turn = nav.turn || 'straight';
  if (turn === 'stairs_up' || turn === 'stairs_down') {
    return {
      state: turn,
      label: turn === 'stairs_up' ? 'Take the stairs up' : 'Take the stairs down',
    };
  }
  if (turn === 'arrive') {
    return {
      state: 'arrive',
      label: role === 'firefighter' ? 'You have arrived' : 'You are out',
    };
  }
  const heading = Number(nav.heading_compass_deg);
  const phone = Number(phoneHeading);
  if (!compassOk || !Number.isFinite(phone) || !Number.isFinite(heading)) {
    if (turn === 'right') return { state: 'right', label: 'Turn right' };
    if (turn === 'left') return { state: 'left', label: 'Turn left' };
    return { state: 'straight', label: straightLabel(nav.distance_to_next_m) };
  }
  const state = arrowFromRel(wrap180(heading - phone));
  if (state === 'right') return { state, label: 'Turn right' };
  if (state === 'left') return { state, label: 'Turn left' };
  if (state === 'around') return { state, label: 'Turn around' };
  return { state: 'straight', label: straightLabel(nav.distance_to_next_m) };
}

// Tilt-compensated heading of where the phone "looks": the back camera when held
// upright, the top edge when flat. up = unit vector to the sky in device axes.
function headingTilt(m, up) {
  if (!m || !up) return null;
  const ux = up.x, uy = up.y, uz = up.z;
  // East = m x up, North = up x East (both horizontal).
  const ex = m.y * uz - m.z * uy, ey = m.z * ux - m.x * uz, ez = m.x * uy - m.y * ux;
  const en = Math.hypot(ex, ey, ez);
  if (!(en > 1e-6)) return null;
  const nx = uy * ez - uz * ey, ny = uz * ex - ux * ez, nz = ux * ey - uy * ex;
  // Forward: top edge when flat (0,1,0), back camera when upright (0,0,-1).
  const vx = 0, vy = uz, vz = -uy;
  const e = vx * ex + vy * ey + vz * ez;
  const n = vx * nx + vy * ny + vz * nz;
  if (Math.hypot(e, n) < 1e-6) return null;
  return norm360((Math.atan2(e / en, n / en) * 180) / Math.PI);
}

// Phone flat, portrait. Magnetometer +y is toward the top of the phone.
function headingFromMag(x, y) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  if (Math.hypot(x, y) < 1e-3) return null;
  return norm360((Math.atan2(-x, y) * 180) / Math.PI);
}

function ribbonCenter(w, h, rel, turn) {
  const cx = w * 0.5;
  const relN = Number(rel) || 0;
  const off = Math.abs(relN) > 100 || turn === 'around';
  const bend = Math.max(-1, Math.min(1, relN / 80));
  let kick = 0;
  if (turn === 'left') kick = -1;
  else if (turn === 'right') kick = 1;
  const n = 32;
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    if (off) {
      const side = relN >= 0 ? 1 : -1;
      const ang = t * Math.PI * 0.92;
      pts.push({
        x: cx + side * Math.sin(ang) * w * 0.28,
        y: h * 0.96 - Math.sin(ang) * h * 0.62,
        t,
      });
    } else {
      const kickT = t < 0.5 ? 0 : Math.pow((t - 0.5) / 0.5, 1.2);
      let u = bend * (0.25 * t + 0.75 * t * t) + kick * kickT;
      if (u > 1.05) u = 1.05;
      if (u < -1.05) u = -1.05;
      pts.push({
        x: cx + u * w * 0.36,
        y: h * (0.96 - 0.86 * Math.pow(t, 0.78)),
        t,
      });
    }
  }
  return pts;
}

function ribbonEdges(pts, w) {
  const left = [];
  const right = [];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[Math.max(0, i - 1)];
    const b = pts[Math.min(pts.length - 1, i + 1)];
    let dx = b.x - a.x;
    let dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    dx /= len;
    dy /= len;
    const px = -dy;
    const py = dx;
    const half = w * (0.22 * (1 - pts[i].t) + 0.055 * pts[i].t);
    left.push({ x: pts[i].x + px * half, y: pts[i].y + py * half });
    right.push({ x: pts[i].x - px * half, y: pts[i].y - py * half });
  }
  return { left, right };
}

function polyD(left, right) {
  const all = left.concat(right.slice().reverse());
  let d = '';
  for (let i = 0; i < all.length; i++) {
    d += `${i ? 'L' : 'M'}${all[i].x.toFixed(1)} ${all[i].y.toFixed(1)}`;
  }
  return `${d}Z`;
}

function lineD(pts) {
  let d = '';
  for (let i = 0; i < pts.length; i++) {
    d += `${i ? 'L' : 'M'}${pts[i].x.toFixed(1)} ${pts[i].y.toFixed(1)}`;
  }
  return d;
}

function samplePoint(pts, t) {
  if (!pts.length) return { x: 0, y: 0, ang: 0 };
  const x = Math.max(0, Math.min(0.999, t)) * (pts.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = pts[i];
  const b = pts[Math.min(i + 1, pts.length - 1)];
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  return {
    x: a.x + dx * f,
    y: a.y + dy * f,
    ang: (Math.atan2(dx, -dy) * 180) / Math.PI,
  };
}

function stepTurnIcon(line) {
  const s = String(line || '').toLowerCase();
  if (s.includes('turn slightly left')) return '↖';
  if (s.includes('turn slightly right')) return '↗';
  if (s.includes('turn around')) return '↶';
  if (s.includes('turn left')) return '↰';
  if (s.includes('turn right')) return '↱';
  if (s.includes('stairs up') || /\btake\b[\s\S]*\bup\b/.test(s)) return 'stairs-up';
  if (s.includes('down')) return 'stairs-down';
  if (s.includes('to exit') || s.includes('arrive')) return '⬤';
  return '↑';
}

function StairsGlyph({ up }) {
  const d = up ? 'M3 24 V18 H10 V12 H17 V6 H25' : 'M3 4 V10 H10 V16 H17 V22 H25';
  return (
    <Svg width={28} height={28} viewBox="0 0 28 28">
      <Path d={d} stroke="#67e8f9" strokeWidth={7} fill="none" strokeLinecap="round" strokeLinejoin="round" opacity={0.35} />
      <Path d={d} stroke="#67e8f9" strokeWidth={2.6} fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </Svg>
  );
}

function StepChipIcon({ icon }) {
  const scale = useRef(new Animated.Value(1)).current;
  const prev = useRef(icon);
  useEffect(() => {
    if (prev.current === icon) return;
    prev.current = icon;
    scale.setValue(0.7);
    Animated.spring(scale, { toValue: 1, friction: 4, tension: 180, useNativeDriver: true }).start();
  }, [icon, scale]);
  return (
    <Animated.View style={{ transform: [{ scale }] }}>
      {icon === 'stairs-up' || icon === 'stairs-down' ? (
        <StairsGlyph up={icon === 'stairs-up'} />
      ) : (
        <Text style={styles.stepChipIcon}>{icon}</Text>
      )}
    </Animated.View>
  );
}

function FloorRibbon({ rel, turn, pulseOn, offCourse, width, height }) {
  const [frame, setFrame] = useState({ flowT: 0, glow: 0.92 });
  useEffect(() => {
    let alive = true;
    let raf = 0;
    const t0 = Date.now();
    const tick = () => {
      if (!alive) return;
      const now = Date.now() - t0;
      const flowT = (now % 1500) / 1500;
      const glow = pulseOn ? 0.4 + 0.6 * (0.5 + 0.5 * Math.sin(now / 140)) : 0.92;
      setFrame({ flowT, glow });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
    };
  }, [pulseOn]);

  const geom = useMemo(() => {
    const pts = ribbonCenter(width, height, rel, turn);
    const edges = ribbonEdges(pts, width);
    return { fill: polyD(edges.left, edges.right), spine: lineD(pts), pts };
  }, [width, height, rel, turn]);

  const pulses = [];
  for (let i = 0; i < 4; i++) {
    const t = (frame.flowT + i * 0.25) % 1;
    const p = samplePoint(geom.pts, 0.06 + t * 0.9);
    const fade = 0.4 + 0.6 * Math.sin((1 - t) * Math.PI);
    const r = 8 + (1 - t) * 12;
    pulses.push(
      <G key={i}>
        <Circle cx={p.x} cy={p.y} r={r * 2.4} fill="#67e8f9" opacity={0.14 * fade * frame.glow} />
        <Circle cx={p.x} cy={p.y} r={r} fill="#f0fdff" opacity={0.82 * fade * frame.glow} />
      </G>
    );
  }

  const glowW = Math.max(28, width * 0.16);
  return (
    <Svg width={width} height={height}>
      <Defs>
        <LinearGradient id="ribbonFade" x1="0" y1="1" x2="0" y2="0">
          <Stop offset="0" stopColor="#a5f3fc" stopOpacity="0.55" />
          <Stop offset="0.35" stopColor="#22d3ee" stopOpacity="0.32" />
          <Stop offset="0.72" stopColor="#06b6d4" stopOpacity="0.12" />
          <Stop offset="1" stopColor="#22d3ee" stopOpacity="0" />
        </LinearGradient>
      </Defs>
      <Path d={geom.spine} stroke="#67e8f9" strokeWidth={glowW} fill="none" strokeLinecap="round" opacity={0.16 * frame.glow} />
      <Path d={geom.fill} fill="url(#ribbonFade)" opacity={0.92 * frame.glow} />
      <Path d={geom.spine} stroke="#22d3ee" strokeWidth={glowW * 0.42} fill="none" strokeLinecap="round" opacity={0.28 * frame.glow} />
      <Path d={geom.spine} stroke="#ecfeff" strokeWidth={3.5} fill="none" strokeLinecap="round" opacity={0.72 * frame.glow} />
      {pulses}
      {offCourse ? (
        <SvgText
          x={width / 2}
          y={height * 0.3}
          fill="#fde68a"
          fontSize="16"
          fontWeight="600"
          textAnchor="middle"
          opacity={0.9}
        >
          Turn around
        </SvgText>
      ) : null}
    </Svg>
  );
}

export default function App() {
  const [permission, requestPermission] = useCameraPermissions();
  const audioRecorder = useAudioRecorder(TALK_RECORDING);
  const [cameraReady, setCameraReady] = useState(false);
  const [liveFrameUri, setLiveFrameUri] = useState(null);
  const [useStreamOverlay, setUseStreamOverlay] = useState(true);
  const [serverBase, setServerBase] = useState(defaultServerBase);
  const [connected, setConnected] = useState(false);
  const [localized, setLocalized] = useState(false);

  // Task 1 ('occupant' -> Escape to Closest Exit) vs Task 2 ('firefighter' -> Navigate to Target Room + Report Fire)
  const [role, setRole] = useState('occupant');
  const [targetRoomId, setTargetRoomId] = useState(FF_TARGET_ROOM);
  const [alertBanner, setAlertBanner] = useState(null);

  const [position, setPosition] = useState([0.0, 0.0, 0.0]);
  const [currentRoomId, setCurrentRoomId] = useState('F2-232');
  const [currentLabel, setCurrentLabel] = useState('232 CLASSROOM');
  const [inliers, setInliers] = useState(0);
  const [latencyMs, setLatencyMs] = useState(0);
  const [nodes, setNodes] = useState([]);
  const [route, setRoute] = useState(null);
  const [nav, setNav] = useState(null);
  const [phoneHeading, setPhoneHeading] = useState(null);
  const [compassOk, setCompassOk] = useState(false);
  const [mayday, setMayday] = useState(false);
  const [trappedSent, setTrappedSent] = useState(false);
  const [showUrlBar, setShowUrlBar] = useState(false);
  const [unitName, setUnitName] = useState('Unit 1');
  const [talking, setTalking] = useState(false);
  const [hudOpen, setHudOpen] = useState(true);
  const upRef = useRef(null); // low-passed "up" vector in device axes
  const [barOpen, setBarOpen] = useState(true);
  const [reportText, setReportText] = useState('');
  const [reportNote, setReportNote] = useState(null);
  const [toast, setToast] = useState('');
  const [qrScan, setQrScan] = useState(false);
  const [mapOpen, setMapOpen] = useState(false);
  const [mapError, setMapError] = useState('');

  const cameraRef = useRef(null);
  const webviewRef = useRef(null);
  const wsRef = useRef(null);
  const isCapturingRef = useRef(false);
  const roleRef = useRef(role);
  const targetRoomRef = useRef(targetRoomId);
  const unitRef = useRef('Unit 1');
  const planRef = useRef(
    DEMO ? { ...DEMO_START } : { level: ARKIT_DEFAULT_LEVEL, x: ARKIT_TO_DCC_X0, y: ARKIT_TO_DCC_Y0 }
  );
  const navRef = useRef(null);
  const demoRef = useRef({ firstExit: null, holding: false, rerouted: false, turnUntil: 0, facing: null, timer: null, fetchAt: 0 });
  const [demoWalking, setDemoWalking] = useState(false);
  const [demoTurning, setDemoTurning] = useState(false);
  const recRef = useRef(null);
  const recStartRef = useRef(null);
  const scanLockRef = useRef(0);
  const toastTimerRef = useRef(null);
  const motionAskRef = useRef(async () => {});
  const serverHttpRef = useRef(httpServerBase(defaultServerBase()));
  const voiceRef = useRef(null);
  const fetchNavRef = useRef(async () => {});
  const planNorthRef = useRef(null);
  const navSeqRef = useRef(0);
  const spokenStepRef = useRef(null);
  const phoneHeadingRef = useRef(null);
  const personIdRef = useRef(`occ-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`);
  const roomIdRef = useRef(currentRoomId);
  const trappedFill = useRef(new Animated.Value(0)).current;
  const trappedSentRef = useRef(false);
  const trappedGen = useRef(0);
  const commitTrappedRef = useRef(async () => {});
  roomIdRef.current = currentRoomId;
  const mapPathRef = useRef([]);
  const mapPathKeyRef = useRef('');
  if (!voiceRef.current) {
    voiceRef.current = createVoicePlayer(() => ({
      base: serverHttpRef.current,
      style: roleRef.current === 'firefighter' ? 'firefighter' : 'occupant',
    }));
  }

  useEffect(() => {
    roleRef.current = role;
  }, [role]);

  useEffect(() => {
    targetRoomRef.current = targetRoomId;
  }, [targetRoomId]);

  useEffect(() => {
    unitRef.current = unitName;
  }, [unitName]);

  const showToast = (text) => {
    const line = String(text || '').replace(/\s+/g, ' ').trim();
    setToast(line);
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(''), 3200);
  };

  const postToMap = (payload) => {
    const node = webviewRef.current;
    if (!node || typeof node.postMessage !== 'function') return;
    try {
      node.postMessage(JSON.stringify(payload));
    } catch (_) {}
  };

  const postMapPose = () => {
    const { level, x, y } = planRef.current;
    const heading = Number(phoneHeadingRef.current);
    postToMap({
      type: 'pose',
      level,
      x,
      y,
      heading_deg: Number.isFinite(heading) ? Math.round(heading * 10) / 10 : 0,
    });
  };

  const rememberPlan = (level, x, y, src) => {
    if (DEMO && src !== 'demo') return; // demo: only the scripted walk moves the marker
    const next = {
      level: Number.isFinite(Number(level)) ? Number(level) : ARKIT_DEFAULT_LEVEL,
      x: Number(x),
      y: Number(y),
    };
    if (!Number.isFinite(next.x) || !Number.isFinite(next.y)) return;
    planRef.current = next;
    postMapPose();
  };

  const pushPlan = (level, x, y, extra) => {
    rememberPlan(level, x, y);
    const next = planRef.current;
    setPosition(planToArkit(next.x, next.y, next.level));
    setLocalized(true);
    if (extra?.label) setCurrentLabel(String(extra.label));
    if (extra?.roomId) setCurrentRoomId(String(extra.roomId));
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      const payload = {
        type: 'pose',
        level: next.level,
        x: next.x,
        y: next.y,
        role: roleRef.current,
        target_room_id: targetRoomRef.current,
      };
      if (Number.isFinite(Number(extra?.heading))) payload.heading = Number(extra.heading);
      ws.send(JSON.stringify(payload));
    }
    fetchNavRef.current();
  };

  // Connect to Centralized FastAPI /ws/ar on the server base URL.
  const connectWebSocket = (base) => {
    serverHttpRef.current = httpServerBase(base);
    const urlToConnect = arSocketUrl(base);
    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch (_) {}
    }
    try {
      const ws = new WebSocket(urlToConnect);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        ws.send(
          JSON.stringify({
            type: 'set_mode',
            role: roleRef.current,
            target_room_id: targetRoomRef.current,
          })
        );
      };
      ws.onclose = () => {
        setConnected(false);
      };
      ws.onerror = () => {
        setConnected(false);
      };
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.nodes) setNodes(msg.nodes);
          if (msg.route) {
            setRoute(msg.route);
            if (msg.route.start_node) setCurrentRoomId(msg.route.start_node);
            if (msg.route.start_label) setCurrentLabel(msg.route.start_label);
            if (msg.route.x_plan != null && msg.route.y_plan != null) {
              rememberPlan(msg.route.level, msg.route.x_plan, msg.route.y_plan);
            }
          }
          if (msg.pose) rememberPlan(msg.pose.level, msg.pose.x, msg.pose.y);
          if (msg.loc) rememberPlan(msg.loc.level, msg.loc.x, msg.loc.y);
          if (msg.type === 'route_update') fetchNavRef.current();
          if (msg.alert_banner) {
            setAlertBanner(msg.alert_banner);
          }
          if (msg.localized) {
            setLocalized(true);
            if (msg.position) setPosition(msg.position);
            if (Array.isArray(msg.dcc_xy) && msg.dcc_xy.length >= 2) {
              rememberPlan(msg.level ?? planRef.current.level, msg.dcc_xy[0], msg.dcc_xy[1]);
            } else if (msg.x != null && msg.y != null) {
              rememberPlan(msg.level ?? planRef.current.level, msg.x, msg.y);
            } else if (msg.position) {
              const plan = arkitToPlan(msg.position, msg.level ?? planRef.current.level);
              rememberPlan(plan.level, plan.x, plan.y);
            }
            if (msg.num_inliers !== undefined) setInliers(msg.num_inliers);
            if (msg.latency_ms !== undefined) setLatencyMs(msg.latency_ms);
          } else if (msg.localized === false) {
            setLocalized(false);
            if (msg.latency_ms !== undefined) setLatencyMs(msg.latency_ms);
          }
          // Route cues duplicate the spoken step line; only mayday cues are voiced.
          if (msg.audio_cue && /^mayday_/.test(msg.audio_cue) && AUDIO_CUE_PHRASES[msg.audio_cue]) {
            voiceRef.current.enqueue(AUDIO_CUE_PHRASES[msg.audio_cue], 'cue', {
              rate: 1.05,
              pitch: 0.95,
            });
          }
        } catch (_) {}
      };
    } catch (_) {
      setConnected(false);
    }
  };

  useEffect(() => {
    connectWebSocket(defaultServerBase());
    return () => {
      if (wsRef.current) wsRef.current.close();
      voiceRef.current.close();
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
      const rec = recRef.current;
      recRef.current = null;
      if (rec) rec.stop().catch(() => {});
    };
  }, []);

  // Dead reckoning: each step moves the marker along the compass heading.
  // Camera (VPS) fixes from /ws/ar still overwrite planRef, so they correct drift.
  useEffect(() => {
    let sub = null;
    const det = { lp: null, prev: null, armed: false, last: 0, amax: 0, amin: 0 };
    const onStep = (lengthM) => {
      if (DEMO) {
        demoAdvanceRef.current(lengthM);
        return;
      }
      const h = Number(phoneHeadingRef.current);
      if (!Number.isFinite(h)) return;
      const north = Number(planNorthRef.current) || 0;
      const b = ((h - north) * Math.PI) / 180;
      const p = planRef.current;
      if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return;
      pushPlan(p.level, p.x + lengthM * Math.sin(b), p.y - lengthM * Math.cos(b), { heading: h });
    };
    try {
      Accelerometer.setUpdateInterval(50);
      sub = Accelerometer.addListener((a) => {
        if (!a) return;
        const mag = Math.hypot(a.x || 0, a.y || 0, a.z || 0); // in g
        // iOS reports gravity (points down), Android reports the reaction (points up).
        const sgn = Platform.OS === 'ios' ? -1 : 1;
        const g = upRef.current;
        const k = g ? 0.1 : 1;
        const gx = sgn * (a.x || 0), gy = sgn * (a.y || 0), gz = sgn * (a.z || 0);
        const nx = g ? g.x + k * (gx - g.x) : gx, ny = g ? g.y + k * (gy - g.y) : gy, nz = g ? g.z + k * (gz - g.z) : gz;
        const gl = Math.hypot(nx, ny, nz) || 1;
        upRef.current = { x: nx / gl, y: ny / gl, z: nz / gl };
        if (!Number.isFinite(mag)) return;
        if (det.lp == null) { det.lp = det.prev = det.amax = det.amin = mag; return; }
        const v = 0.25 * mag + 0.75 * det.lp;
        det.lp = v;
        det.amax = Math.max(det.amax, v);
        det.amin = Math.min(det.amin, v);
        const now = Date.now();
        if (v < det.prev && det.prev > 1.08) det.armed = true;
        if (det.armed && v < 0.96) {
          det.armed = false;
          if (now - det.last >= 280) {
            det.last = now;
            const amp = Math.max(0, (det.amax - det.amin) * 9.81);
            const len = Math.min(0.9, Math.max(0.5, 0.47 * Math.pow(amp, 0.25)));
            onStep(len);
          }
          det.amax = det.amin = v;
        }
        det.prev = v;
      });
    } catch (_) {}
    return () => { if (sub) sub.remove(); };
  }, []);

  // Steps from the phone IMU. Same peak/dip detector as viewer/pdr.js.
  useEffect(() => {
    const det = {
      heading: null,
      sawAbsolute: false,
      lp: null,
      prev: null,
      peakArmed: false,
      lastStepAt: 0,
      amax: 0,
      amin: 0,
    };
    const norm360 = (deg) => ((deg % 360) + 360) % 360;
    const smoothHeading = (deg) => {
      const d = norm360(deg);
      if (det.heading == null) {
        det.heading = d;
        return;
      }
      const delta = ((d - det.heading + 540) % 360) - 180;
      det.heading = norm360(det.heading + 0.3 * delta);
    };
    const onOrient = (e, fromAbs) => {
      if (!e) return;
      if (typeof e.webkitCompassHeading === 'number' && Number.isFinite(e.webkitCompassHeading)) {
        smoothHeading(e.webkitCompassHeading);
        return;
      }
      const absolute = fromAbs || e.absolute === true;
      if (!absolute) return;
      if (!fromAbs && det.sawAbsolute) return;
      if (fromAbs) det.sawAbsolute = true;
      if (typeof e.alpha !== 'number' || !Number.isFinite(e.alpha)) return;
      smoothHeading(360 - e.alpha);
    };
    const stepLength = (amax, amin) => {
      let amp = amax - amin;
      if (amp < 0) amp = 0;
      let length = 0.47 * Math.pow(amp, 0.25);
      if (!Number.isFinite(length) || length < 0.45) length = 0.45;
      if (length > 0.9) length = 0.9;
      return Math.round(length * 1000) / 1000;
    };
    const sendStep = (heading, length) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(
        JSON.stringify({
          type: 'imu',
          heading_deg: heading,
          length_m: length,
          t: Date.now(),
          role: roleRef.current,
          target_room_id: targetRoomRef.current,
        })
      );
    };
    const onMotion = (e) => {
      const a = e && e.accelerationIncludingGravity;
      if (!a || a.x == null || a.y == null || a.z == null) return;
      const mag = Math.hypot(a.x, a.y, a.z);
      if (!Number.isFinite(mag)) return;
      const now = Date.now();
      if (det.lp == null) {
        det.lp = mag;
        det.prev = mag;
        det.amax = mag;
        det.amin = mag;
        return;
      }
      const v = 0.25 * mag + 0.75 * det.lp;
      det.lp = v;
      if (v > det.amax) det.amax = v;
      if (v < det.amin) det.amin = v;
      if (det.prev != null && v < det.prev && det.prev > 10.6) det.peakArmed = true;
      if (det.peakArmed && v < 9.4 && now - det.lastStepAt >= 280) {
        det.peakArmed = false;
        det.lastStepAt = now;
        const h = det.heading == null ? 0 : det.heading;
        sendStep(Math.round(norm360(h) * 10) / 10, stepLength(det.amax, det.amin));
        det.amax = v;
        det.amin = v;
      } else if (det.peakArmed && v < 9.4) {
        det.peakArmed = false;
      }
      det.prev = v;
    };

    const unlisten = [];
    const host = typeof globalThis !== 'undefined' ? globalThis : null;
    const listen = (type, fn) => {
      try {
        if (!host || typeof host.addEventListener !== 'function') return;
        host.addEventListener(type, fn, { passive: true });
        unlisten.push(() => host.removeEventListener(type, fn));
      } catch (_) {}
    };
    listen('devicemotion', onMotion);
    listen('deviceorientation', (e) => onOrient(e, false));
    listen('deviceorientationabsolute', (e) => onOrient(e, true));

    try {
      const motionNative = NativeModules && NativeModules.ExponentDeviceMotion;
      const magNative = NativeModules && NativeModules.ExponentMagnetometer;
      if (motionNative && typeof motionNative.setUpdateInterval === 'function') {
        motionNative.setUpdateInterval(50);
        const sub = DeviceEventEmitter.addListener('deviceMotionDidUpdate', onMotion);
        unlisten.push(() => sub.remove());
      }
      if (magNative && typeof magNative.setUpdateInterval === 'function') {
        magNative.setUpdateInterval(100);
        const sub = DeviceEventEmitter.addListener('magnetometerDidUpdate', (m) => {
          if (!m || !Number.isFinite(m.x) || !Number.isFinite(m.y)) return;
          smoothHeading((Math.atan2(m.y, m.x) * 180) / Math.PI);
        });
        unlisten.push(() => sub.remove());
      }
    } catch (_) {}

    motionAskRef.current = async () => {
      try {
        const jobs = [];
        const Motion = host && host.DeviceMotionEvent;
        const Orient = host && host.DeviceOrientationEvent;
        if (Motion && typeof Motion.requestPermission === 'function') jobs.push(Motion.requestPermission());
        if (Orient && typeof Orient.requestPermission === 'function') jobs.push(Orient.requestPermission());
        if (jobs.length) await Promise.all(jobs);
      } catch (_) {}
    };

    return () => {
      motionAskRef.current = async () => {};
      unlisten.forEach((fn) => {
        try {
          fn();
        } catch (_) {}
      });
    };
  }, []);

  // Compass heading, degrees clockwise from north. Smoothed. Falls back to server turn text.
  useEffect(() => {
    let sub = null;
    let alive = true;
    const smooth = { heading: null };
    const push = (deg) => {
      if (!alive || !Number.isFinite(deg) || DEMO) return;
      const d = norm360(deg + HEADING_OFFSET_DEG);
      if (smooth.heading == null) smooth.heading = d;
      else {
        const delta = wrap180(d - smooth.heading);
        smooth.heading = norm360(smooth.heading + 0.35 * delta);
      }
      phoneHeadingRef.current = smooth.heading;
      setPhoneHeading(smooth.heading);
      setCompassOk(true);
    };
    (async () => {
      try {
        if (typeof Magnetometer.requestPermissionsAsync === 'function') {
          const perm = await Magnetometer.requestPermissionsAsync();
          if (perm && perm.status === 'denied') return;
        }
        const available = await Magnetometer.isAvailableAsync();
        if (!alive || !available) return;
        Magnetometer.setUpdateInterval(100);
        sub = Magnetometer.addListener((m) => {
          const tilt = headingTilt(m, upRef.current);
          const deg = tilt != null ? tilt : headingFromMag(m?.x, m?.y);
          if (deg == null) return;
          push(deg);
        });
      } catch (_) {
        if (alive) setCompassOk(false);
      }
    })();
    return () => {
      alive = false;
      try {
        if (sub) sub.remove();
      } catch (_) {}
    };
  }, []);

  // Server route: occupant /api/nav, firefighter /api/route_to. Every 1s and on route_update.
  useEffect(() => {
    let alive = true;
    const ensureNorth = async (base) => {
      if (planNorthRef.current != null) return planNorthRef.current;
      try {
        const res = await fetch(`${base}/api/building`);
        const data = await res.json().catch(() => ({}));
        if (res.ok && data && data.plan_north_deg != null) {
          planNorthRef.current = Number(data.plan_north_deg) || 0;
        }
      } catch (_) {}
      return planNorthRef.current;
    };
    const pullNav = async () => {
      const seq = ++navSeqRef.current;
      const { level, x, y } = planRef.current;
      if (!Number.isFinite(Number(x)) || !Number.isFinite(Number(y))) return;
      const base = serverHttpRef.current;
      const target = targetRoomRef.current;
      const roleNow = roleRef.current;
      if (roleNow === 'firefighter' && !target) {
        setNav(null);
        return;
      }
      const firefighter = roleNow === 'firefighter';
      const q = `level=${encodeURIComponent(level)}&x=${encodeURIComponent(x)}&y=${encodeURIComponent(y)}`;
      const url = firefighter
        ? `${base}/api/route_to?${q}&to_room=${encodeURIComponent(target)}`
        : `${base}/api/nav?${q}`;
      try {
        const res = await fetch(url);
        const data = await res.json().catch(() => ({}));
        if (!alive || seq !== navSeqRef.current || !res.ok || !data) return;
        if (roleRef.current !== roleNow) return;
        if (data.plan_north_deg != null) planNorthRef.current = Number(data.plan_north_deg) || 0;
        if (firefighter) {
          const north = await ensureNorth(base);
          if (!alive || seq !== navSeqRef.current) return;
          const hints = hintsFromPath(data.path, level, x, y, north);
          if (data.reachable === false) {
            hints.turn = 'blocked';
            hints.next = null;
            hints.distance_to_next_m = 0;
          }
          setNav({ ...data, ...hints, _role: roleNow });
        } else {
          setNav({ ...data, _role: roleNow });
        }
      } catch (_) {}
    };
    fetchNavRef.current = pullNav;
    pullNav();
    const id = setInterval(pullNav, 1000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // 3D map card: pose every 1s and whenever heading or plan changes. Route when the path changes.
  useEffect(() => {
    postMapPose();
    const id = setInterval(postMapPose, 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    phoneHeadingRef.current = phoneHeading;
    postMapPose();
  }, [phoneHeading]);

  useEffect(() => {
    const raw = Array.isArray(nav?.path) ? nav.path : Array.isArray(route?.path) ? route.path : [];
    // Start the drawn path at the marker itself.
    const me = planRef.current;
    const path = raw.length && me && Number.isFinite(me.x) && Number.isFinite(me.y)
      ? [[me.level, me.x, me.y], ...raw]
      : raw;
    const key = JSON.stringify(path);
    if (key === mapPathKeyRef.current) return;
    mapPathKeyRef.current = key;
    mapPathRef.current = path;
    postToMap({ type: 'route', path });
  }, [nav, route]);

  // Switch between Task 1 (Occupant Escape) and Task 2 (Firefighter Target Room)
  const switchMode = (newRole, _ignored) => {
    const newTargetRoom = FF_TARGET_ROOM;
    if (newRole !== roleRef.current) {
      voiceRef.current.clear();
      spokenStepRef.current = '';
      setNav(null);
      setRoute(null);
    }
    setRole(newRole);
    roleRef.current = newRole;
    if (newTargetRoom) {
      setTargetRoomId(newTargetRoom);
      targetRoomRef.current = newTargetRoom;
    }
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: 'set_mode',
          role: newRole,
          target_room_id: newTargetRoom,
        })
      );
    }
    fetchNavRef.current();
  };

  // Report live fire or clear status from Firefighter phone -> updates Commander webpage + all phones
  const sendFireReport = (kind, customRoomId = null) => {
    const roomToReport = customRoomId || currentRoomId || 'F2-232';
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: 'report_fire',
          room_id: roomToReport,
          kind,
        })
      );
    }
  };

  // Periodically grab 640x480 frame from camera, update live screen feed, and send to centralized server
  useEffect(() => {
    const interval = setInterval(async () => {
      if (!cameraRef.current || isCapturingRef.current || recRef.current) {
        return;
      }
      isCapturingRef.current = true;
      try {
        const photo = await cameraRef.current.takePictureAsync({
          quality: 0.35,
          base64: true,
          shutterSound: false,
        });
        if (photo?.uri) {
          setLiveFrameUri(photo.uri);
        } else if (photo?.base64) {
          setLiveFrameUri(`data:image/jpeg;base64,${photo.base64}`);
        }
        if (photo?.base64 && wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(
            JSON.stringify({
              type: 'frame',
              image_b64: photo.base64,
              role: roleRef.current,
              target_room_id: targetRoomRef.current,
            })
          );
        }
      } catch (_) {
        // Ignore transient camera frame busy errors
      } finally {
        isCapturingRef.current = false;
      }
    }, 450);

    return () => clearInterval(interval);
  }, [connected, cameraReady]);

  const showReport = (data, fallbackText) => {
    const transcript = (data?.report && data.report.text) || fallbackText || '';
    const confirmation = data?.confirmation || 'Report sent';
    setReportNote({ transcript: String(transcript), confirmation: String(confirmation) });
    voiceRef.current.enqueue(confirmation, 'ff-report');
    return confirmation;
  };

  const postJson = async (path, body) => {
    const res = await fetch(`${serverHttpRef.current}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(errorText(data, 'Request failed'));
    return data;
  };

  const startTalk = async () => {
    if (recRef.current || recStartRef.current) return;
    setTalking(true);
    voiceRef.current.hush(true);
    const job = (async () => {
      const perm = await AudioModule.requestRecordingPermissionsAsync();
      if (!perm.granted) throw new Error('Mic permission needed');
      await setAudioModeAsync({
        allowsRecording: true,
        playsInSilentMode: true,
      });
      try {
        await audioRecorder.prepareToRecordAsync(TALK_RECORDING);
      } catch (first) {
        if (Platform.OS !== 'ios') throw first;
        await audioRecorder.prepareToRecordAsync(TALK_AAC);
      }
      audioRecorder.record();
      recRef.current = audioRecorder;
    })();
    recStartRef.current = job;
    try {
      await job;
    } catch (err) {
      recRef.current = null;
      recStartRef.current = null;
      setTalking(false);
      voiceRef.current.hush(false);
      showToast(err?.message || 'Could not record');
    }
  };

  const stopTalk = async () => {
    const pending = recStartRef.current;
    recStartRef.current = null;
    if (pending) {
      try {
        await pending;
      } catch (_) {
        setTalking(false);
        voiceRef.current.hush(false);
        return;
      }
    }
    const recording = recRef.current;
    recRef.current = null;
    setTalking(false);
    voiceRef.current.hush(false);
    if (!recording) return;
    try {
      await recording.stop();
      await setAudioModeAsync({
        allowsRecording: false,
        playsInSilentMode: true,
      });
      let uri = recording.uri;
      if (uri && !/^[a-z]+:\/\//i.test(uri)) uri = `file://${uri}`;
      if (!uri) throw new Error('No audio captured');
      const form = new FormData();
      const wav = /\.wav$/i.test(uri) || /\.wav$/i.test(String(uri).split('?')[0]);
      form.append('audio', { uri, name: wav ? 'report.wav' : 'report.m4a', type: wav ? 'audio/wav' : 'audio/mp4' });
      form.append('by', unitRef.current || '');
      // Expo's global fetch rejects React Native file parts ({ uri }) with
      // "Unsupported FormDataPart implementation". XMLHttpRequest still uses the
      // native uploader, which streams the file from its uri.
      const { ok, data } = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', `${serverHttpRef.current}/api/reports/audio`);
        xhr.timeout = 45000;
        xhr.onload = () => {
          let body = {};
          try {
            body = JSON.parse(xhr.responseText || '{}');
          } catch (_) {}
          resolve({ ok: xhr.status >= 200 && xhr.status < 300, data: body });
        };
        xhr.onerror = () => reject(new Error('Upload failed. Is the server reachable?'));
        xhr.ontimeout = () => reject(new Error('Upload timed out'));
        xhr.send(form);
      });
      if (!ok) throw new Error(errorText(data, 'Report failed'));
      showReport(data, '');
    } catch (err) {
      showToast(err?.message || 'Report failed');
    }
  };

  const sendTextReport = async () => {
    const text = reportText.replace(/\s+/g, ' ').trim();
    if (!text) return;
    try {
      const data = await postJson('/api/reports', { text, by: unitRef.current || '' });
      setReportText('');
      showReport(data, text);
    } catch (err) {
      showToast(err?.message || 'Report failed');
    }
  };

  const reportFireHere = async () => {
    const { level, x, y } = planRef.current;
    try {
      await postJson('/api/fire/start', { level, x, y });
      const data = await postJson('/api/reports', {
        text: 'Fire at firefighter position',
        by: unitRef.current || '',
      });
      showReport(data, 'Fire at firefighter position');
      showToast('Fire reported here');
    } catch (err) {
      showToast(err?.message || 'Could not report fire');
    }
  };

  const onBarcodeScanned = async ({ data }) => {
    const raw = String(data || '').trim();
    const match = raw.match(/^FIRESIGHT:(.+)$/i);
    if (!match) return;
    const code = match[1].trim();
    if (!code) return;
    const now = Date.now();
    if (now - scanLockRef.current < 2500) return;
    scanLockRef.current = now;
    try {
      const res = await fetch(`${serverHttpRef.current}/api/qr/${encodeURIComponent(code)}`);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(errorText(body, 'Unknown QR'));
      pushPlan(body.level, body.x, body.y, {
        label: body.label,
        roomId: body.room_id,
        heading: body.heading_deg,
      });
      setQrScan(false);
      showToast(`Start set: ${body.label || code}`);
    } catch (err) {
      showToast(err?.message || 'QR failed');
    }
  };

  commitTrappedRef.current = async () => {
    if (trappedSentRef.current) return;
    trappedSentRef.current = true;
    trappedGen.current += 1;
    trappedFill.setValue(1);
    setTrappedSent(true);
    setMayday(true);
    pulseTrappedHaptic();
    const { level, x, y } = planRef.current;
    const id = personIdRef.current;
    try {
      await postJson(`/api/people/${encodeURIComponent(id)}/status`, {
        status: 'trapped',
        level,
        x,
        y,
        room_id: roomIdRef.current || null,
      });
    } catch (err) {
      showToast(err?.message || 'Could not send trapped alert');
    }
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'mayday', active: true }));
    }
    voiceRef.current.enqueue('Help is coming.', 'mayday');
  };

  const beginTrappedHold = () => {
    if (trappedSentRef.current) return;
    const gen = ++trappedGen.current;
    trappedFill.stopAnimation((value) => {
      if (gen !== trappedGen.current || trappedSentRef.current) return;
      const from = Number(value) || 0;
      Animated.timing(trappedFill, {
        toValue: 1,
        duration: Math.max(16, Math.round(2000 * (1 - from))),
        easing: Easing.linear,
        useNativeDriver: false,
      }).start(({ finished }) => {
        if (finished && gen === trappedGen.current) commitTrappedRef.current();
      });
    });
  };

  const cancelTrappedHold = () => {
    if (trappedSentRef.current) return;
    const gen = ++trappedGen.current;
    trappedFill.stopAnimation((value) => {
      if (gen !== trappedGen.current || trappedSentRef.current) return;
      const from = Number(value) || 0;
      if (from <= 0.001) {
        trappedFill.setValue(0);
        return;
      }
      Animated.timing(trappedFill, {
        toValue: 0,
        duration: Math.max(160, Math.round(2000 * from)),
        easing: Easing.linear,
        useNativeDriver: false,
      }).start();
    });
  };

  const trappedFillWidth = trappedFill.interpolate({
    inputRange: [0, 1],
    outputRange: ['0%', '100%'],
  });

  const arrowLive = useMemo(
    () => buildArrow(nav, phoneHeading, compassOk, role),
    [nav, phoneHeading, compassOk, role]
  );
  // Demo: during the scripted reroute the arrow always says turn around.
  const arrow = demoTurning ? { state: 'around', label: 'Turn around' } : arrowLive;

  // ---- Demo engine: walk along the route, face along it, scripted turn-around.
  const demoAdvanceRef = useRef(() => {});
  demoAdvanceRef.current = (dist) => {
    const d = demoRef.current;
    const n = navRef.current;
    if (!DEMO || d.holding || Date.now() < d.turnUntil || !n || n.reachable === false) return;
    const pts = (n.path || [])
      .filter((p) => Array.isArray(p) && Number(p[0]) === Number(planRef.current.level))
      .map((p) => ({ x: Number(p[1]), y: Number(p[2]) }));
    if (pts.length < 2) return;
    let { x, y } = planRef.current;
    // Start from the path point nearest to the marker, then walk forward.
    let k = 0;
    let best = Infinity;
    pts.forEach((p, i) => {
      const dd = Math.hypot(p.x - x, p.y - y);
      if (dd < best) {
        best = dd;
        k = i;
      }
    });
    let left = dist;
    for (let i = k + 1; i < pts.length && left > 0; i++) {
      const seg = Math.hypot(pts[i].x - x, pts[i].y - y);
      let nx;
      let ny;
      if (seg <= left) {
        nx = pts[i].x;
        ny = pts[i].y;
      } else {
        nx = x + ((pts[i].x - x) * left) / seg;
        ny = y + ((pts[i].y - y) * left) / seg;
      }
      if (!d.rerouted && !inDemoRoom(nx)) {
        // Stop right before entering 218 and wait for the fire.
        d.holding = true;
        setDemoWalking(false);
        break;
      }
      left -= Math.min(seg, left);
      x = nx;
      y = ny;
    }
    const arrived = left > 0 && !d.holding;
    rememberPlan(planRef.current.level, x, y, 'demo');
    if (arrived) {
      setDemoWalking(false);
      voiceRef.current.enqueue("You're out. Go to the assembly point.", 'demo');
    }
    // Instructions follow the marker: refresh the route twice a second while walking.
    const now = Date.now();
    if (arrived || d.holding || now - d.fetchAt >= 500) {
      d.fetchAt = now;
      fetchNavRef.current();
    }
  };

  const resetDemo = () => {
    const d = demoRef.current;
    if (d.timer) clearTimeout(d.timer);
    demoRef.current = { firstExit: null, holding: false, rerouted: false, turnUntil: 0, facing: null, timer: null, fetchAt: 0 };
    setDemoWalking(false);
    setDemoTurning(false);
    spokenStepRef.current = '';
    voiceRef.current.clear();
    rememberPlan(DEMO_START.level, DEMO_START.x, DEMO_START.y, 'demo');
    fetchNavRef.current();
  };

  useEffect(() => {
    if (!DEMO || !demoWalking) return undefined;
    const id = setInterval(() => demoAdvanceRef.current(DEMO_SPEED_MPS * 0.1), 100);
    return () => clearInterval(id);
  }, [demoWalking]);

  // Facing follows the route. A reroute that reverses it holds the old facing
  // for a moment ("Turn around"), then turns ("Go straight").
  useEffect(() => {
    navRef.current = nav;
    if (!DEMO || !nav || nav.reachable === false) return;
    const d = demoRef.current;
    const target = Number(nav.heading_compass_deg);
    if (!Number.isFinite(target)) return;
    if (d.firstExit == null) {
      d.firstExit = nav.exit;
      setDemoWalking(true); // start walking on its own, at walking pace
    }
    const now = Date.now();
    if (now < d.turnUntil) return;
    if (!d.rerouted && d.holding && nav.exit && d.firstExit && nav.exit !== d.firstExit) {
      d.rerouted = true;
      d.holding = false;
      if (d.facing != null && Math.abs(wrap180(target - d.facing)) > 90) {
        d.turnUntil = now + DEMO_TURN_MS;
        setDemoTurning(true);
        setDemoWalking(false);
        voiceRef.current.enqueue('Fire in 218 ahead. Route blocked. Turn around.', 'demo');
        d.timer = setTimeout(() => {
          const t2 = Number(navRef.current && navRef.current.heading_compass_deg);
          d.facing = Number.isFinite(t2) ? t2 : target;
          phoneHeadingRef.current = d.facing;
          setPhoneHeading(d.facing);
          d.turnUntil = 0;
          setDemoTurning(false);
          const first = navRef.current && navRef.current.instructions && navRef.current.instructions[0];
          spokenStepRef.current = first ? String(first).replace(/\s+/g, ' ').trim() : '';
          voiceRef.current.enqueue('Go straight down hallway 219 to the exit.', 'demo');
          setDemoWalking(true);
        }, DEMO_TURN_MS);
        return;
      }
      setDemoWalking(true); // small change of direction: just keep walking the new route
    }
    d.facing = target;
    phoneHeadingRef.current = target;
    setPhoneHeading(target);
    setCompassOk(true);
  }, [nav]);

  useEffect(() => {
    const line =
      nav && Array.isArray(nav.instructions) && nav.instructions[0]
        ? String(nav.instructions[0]).replace(/\s+/g, ' ').trim()
        : '';
    // Speak only directions computed for the current mode.
    if (!nav || nav._role !== roleRef.current) return;
    if (DEMO && Date.now() < demoRef.current.turnUntil) {
      spokenStepRef.current = line; // the scripted lines own the voice during the turn
      return;
    }
    if (!line || spokenStepRef.current === line) return;
    spokenStepRef.current = line;
    voiceRef.current.enqueue(line, 'nav-step');
  }, [nav]);

  const fireRoomIds = useMemo(() => {
    const s = new Set();
    (route?.fire_rooms || []).forEach((r) => s.add(r.id));
    return s;
  }, [route]);

  if (!permission) {
    return <View style={styles.container} />;
  }

  if (!permission.granted) {
    return (
      <SafeAreaView style={styles.permissionContainer}>
        <Text style={styles.permTitle}>FIRESIGHT // 3D DCC NAVIGATION</Text>
        <Text style={styles.permSub}>
          Firesight uses your camera to localize inside the 3D model of Darrin Communications Center (DCC) and route around live fires.
        </Text>
        <TouchableOpacity
          style={styles.permButton}
          onPress={() => {
            motionAskRef.current();
            requestPermission();
          }}
        >
          <Text style={styles.permButtonText}>ENABLE AR CAMERA</Text>
        </TouchableOpacity>
      </SafeAreaView>
    );
  }

  // First line is relative to where the marker faces: "Turn left, then walk 6 m ...".
  const serverLine =
    nav && Array.isArray(nav.instructions) && nav.instructions[0] ? String(nav.instructions[0]) : '';
  const facingTurn = compassOk && ['left', 'right', 'around'].includes(arrow.state);
  const primaryInstruction = !serverLine
    ? arrow.label
    : facingTurn
    ? `${arrow.label}, then ${serverLine.charAt(0).toLowerCase()}${serverLine.slice(1)}`
    : serverLine;
  const unreachable = (route && route.reachable === false) || (nav && nav.reachable === false);
  const targetLabel = role === 'firefighter' ? FF_TARGET_LABEL : route?.target_exit || nav?.exit || 'EXIT';
  const routeMeters = route?.total_distance_m ?? route?.distance_m ?? nav?.distance_m;
  const targetDeg = Number(
    nav && (nav.target_compass_deg != null ? nav.target_compass_deg : nav.heading_compass_deg)
  );
  const rel =
    compassOk && Number.isFinite(Number(phoneHeading)) && Number.isFinite(targetDeg)
      ? wrap180(targetDeg - phoneHeading)
      : 0;
  let pathTurn = 'straight';
  if (nav) {
    const t = String(nav.turn || '');
    if (t === 'left' || t === 'right' || t === 'straight' || t === 'stairs_up' || t === 'stairs_down') {
      pathTurn = t;
    }
  }
  if (arrow.state === 'around' || primaryInstruction.toLowerCase().includes('turn around')) pathTurn = 'around';
  const offCourse = Math.abs(rel) > 100 || pathTurn === 'around';
  const stepLine =
    nav && Array.isArray(nav.instructions) && nav.instructions[0]
      ? String(nav.instructions[0]).replace(/\s+/g, ' ').trim()
      : '';
  const legM = nav && nav.distance_to_next_m != null && Number.isFinite(Number(nav.distance_to_next_m))
    ? Math.max(0, Math.round(Number(nav.distance_to_next_m)))
    : null;
  const turnClose =
    !!nav &&
    Number(nav.distance_to_next_m) <= 3 &&
    pathTurn !== 'straight' &&
    pathTurn !== 'arrive' &&
    arrow.state !== 'blocked';
  const showPath = !!(nav && nav.reachable !== false && arrow.state !== 'blocked' && arrow.state !== 'finding');
  const mapUri = `${httpServerBase(serverBase)}/index.html?embed=1&bare=1`;
  const ribbonH = role === 'firefighter' ? SCREEN_H * 0.42 : SCREEN_H * 0.52;
  const chipM = legM != null ? legM : routeMeters != null ? Math.round(Number(routeMeters)) : null;
  const chipDist = chipM != null ? `${chipM} m · ${Math.max(1, Math.round(chipM / DEMO_SPEED_MPS))} s` : '';
  const noteMapError = (nativeEvent) => {
    const desc = (nativeEvent && (nativeEvent.description || nativeEvent.title)) || 'Map failed to load';
    const code = nativeEvent && nativeEvent.statusCode;
    const text = code ? `${desc} (${code})` : String(desc);
    setMapError(`${text}\n${mapUri}`);
  };

  return (
    <View style={styles.container}>
      <StatusBar style="light" />

      {/* 1A. LIVE FULLSCREEN BACK CAMERA (640x480 fast VGA preset) */}
      <CameraView
        ref={cameraRef}
        style={styles.cameraFull}
        facing="back"
        mode="picture"
        pictureSize="640x480"
        active={true}
        animateShutter={false}
        barcodeScannerSettings={qrScan ? { barcodeTypes: ['qr'] } : undefined}
        onBarcodeScanned={qrScan ? onBarcodeScanned : undefined}
        onCameraReady={() => setCameraReady(true)}
      />

      {/* 1B. GUARANTEED LIVE CAMERA FEED OVERLAY */}
      {useStreamOverlay && liveFrameUri && (
        <Image
          source={{ uri: liveFrameUri }}
          style={StyleSheet.absoluteFillObject}
          resizeMode="cover"
          fadeDuration={0}
        />
      )}

      {/* 2. FLOOR PATH */}
      <View style={[styles.pathLayer, role === 'firefighter' && styles.pathLayerFf]} pointerEvents="none">
        {showPath ? (
          <FloorRibbon
            rel={rel}
            turn={offCourse ? 'around' : pathTurn}
            offCourse={offCourse}
            pulseOn={turnClose}
            width={SCREEN_W}
            height={ribbonH}
          />
        ) : null}
        {stepLine || chipDist ? (
          <View style={styles.stepChip}>
            {stepLine ? <StepChipIcon icon={stepTurnIcon(stepLine)} /> : null}
            {stepLine ? (
              <Text style={styles.stepChipText} numberOfLines={2}>
                {stepLine}
              </Text>
            ) : null}
            {chipDist ? <Text style={styles.stepChipDist}>{chipDist}</Text> : null}
          </View>
        ) : null}
      </View>

      {/* 3. TOP TACTICAL VISOR HUD */}
      <SafeAreaView style={styles.topHudWrapper}>
        <View
          style={[
            styles.hudCard,
            role === 'firefighter' && styles.hudCardFirefighter,
            mayday && styles.hudCardMayday,
          ]}
        >
          {/* MODE SWITCHER: Task 1 (Occupant Escape) vs Task 2 (Firefighter Target Room) */}
          {hudOpen ? (
          <View style={styles.modeSwitchRow}>
            <TouchableOpacity
              style={[styles.modeBtn, role === 'occupant' && styles.modeBtnOccupantActive]}
              onPress={() => switchMode('occupant')}
            >
              <Text style={styles.modeBtnText}>🏃 EVACUATE TO EXIT</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.modeBtn, role === 'firefighter' && styles.modeBtnFirefighterActive]}
              onPress={() => switchMode('firefighter', targetRoomId)}
            >
              <Text style={styles.modeBtnText}>👨‍🚒 FIREFIGHTER MODE</Text>
            </TouchableOpacity>
          </View>
          ) : null}

          <View style={styles.stepHud}>
            <Text style={styles.stepHudText} numberOfLines={2}>
              {primaryInstruction}
            </Text>
          </View>

          {hudOpen ? (<>
          {/* Connection & VPS Lock Header */}
          <View style={styles.hudHeaderRow}>
            <View style={styles.statusBadge}>
              <View
                style={[
                  styles.statusDot,
                  { backgroundColor: connected ? (localized ? '#10b981' : '#f59e0b') : '#ef4444' },
                ]}
              />
              <Text style={styles.statusText}>
                {connected
                  ? localized
                    ? `3D LOCKED (${inliers} PTS • ${Math.round(latencyMs)}ms)`
                    : 'SCANNING DCC...'
                  : 'SERVER OFFLINE'}
              </Text>
            </View>

            <View style={{ flexDirection: 'row', gap: 6 }}>
              <TouchableOpacity
                onPress={() => setUseStreamOverlay((v) => !v)}
                style={styles.cfgBtn}
              >
                <Text style={styles.cfgBtnText}>{useStreamOverlay ? 'Cam: Live' : 'Cam: Raw'}</Text>
              </TouchableOpacity>
              <TouchableOpacity onPress={() => setShowUrlBar((s) => !s)} style={styles.cfgBtn}>
                <Text style={styles.cfgBtnText}>{showUrlBar ? 'Hide URL' : 'Server'}</Text>
              </TouchableOpacity>
            </View>
          </View>

          {showUrlBar && (
            <View style={styles.urlRow}>
              <TextInput
                style={styles.urlInput}
                value={serverBase}
                onChangeText={setServerBase}
                autoCapitalize="none"
                autoCorrect={false}
                placeholder="http://host:8000"
                placeholderTextColor="#64748b"
              />
              <TouchableOpacity
                style={styles.connectBtn}
                onPress={() => {
                  motionAskRef.current();
                  connectWebSocket(serverBase);
                }}
              >
                <Text style={styles.connectBtnText}>Connect</Text>
              </TouchableOpacity>
            </View>
          )}

          {/* LIVE FIRE ALERT BANNER (Synced with Commander Webpage & Firefighter Reports) */}
          {(alertBanner || (route?.fire_rooms && route.fire_rooms.length > 0)) && (
            <View style={styles.fireBanner}>
              <Text style={styles.fireBannerText} numberOfLines={1}>
                {route?.fire_rooms && route.fire_rooms.length > 0
                  ? `🔥 ACTIVE FIRE: ${route.fire_rooms.map((r) => r.label).join(', ')}`
                  : alertBanner}
              </Text>
            </View>
          )}

          {/* Pinpointed Room / Hallway Location in 3D DCC Model */}
          <Text style={styles.locationLabel}>CURRENT LOCATION (DCC FLOOR 2)</Text>
          <Text style={styles.roomTitle}>{currentLabel.toUpperCase()}</Text>

          {/* TASK 2: FIREFIGHTER TARGET (fixed) */}
          {role === 'firefighter' && (
            <View style={styles.targetSelectorWrap}>
              <Text style={styles.targetSelectorLabel}>
                TARGET ROOM: {FF_TARGET_LABEL}{fireRoomIds.has(FF_TARGET_ROOM) ? '  🔥' : ''}
              </Text>
            </View>
          )}

          {/* Active 3D Route Bar (Closest Safe Exit for Occupants, Target Room for Firefighters) */}
          <View style={styles.egressRow}>
            <View style={{ flex: 1, paddingRight: 8 }}>
              <Text
                style={[
                  styles.egressTarget,
                  role === 'firefighter' && { color: '#fb923c' },
                  unreachable && { color: '#ef4444' },
                ]}
              >
                {!route && !nav
                  ? 'Finding your route...'
                  : unreachable
                  ? `⚠️ ${(route && route.reason) || (nav && nav.reason) || 'NO SAFE ROUTE'}`
                  : role === 'firefighter'
                  ? `🎯 TARGET ROOM: ${String(targetLabel).toUpperCase()}`
                  : `🚪 CLOSEST SAFE EXIT: ${String(targetLabel).toUpperCase()}`}
              </Text>
              <Text style={styles.turnInstruction} numberOfLines={2}>
                {primaryInstruction}
              </Text>
            </View>
            <Text
              style={[
                styles.distanceBadge,
                role === 'firefighter' && { backgroundColor: 'rgba(249, 115, 22, 0.25)' },
              ]}
            >
              {routeMeters != null ? `${routeMeters}m` : '—'}
            </Text>
          </View>
          </>) : null}
          <TouchableOpacity style={styles.foldHandle} onPress={() => setHudOpen((v) => !v)} hitSlop={8}>
            <Text style={styles.foldText}>{hudOpen ? '▲ hide' : '▼ more'}</Text>
          </TouchableOpacity>
        </View>
      </SafeAreaView>

      {/* 4. LIVE DCC 200 LEVEL MINI-MAP (Shows Rooms, Active Fires in Red, and User Dot) */}
      {toast ? (
        <View style={styles.toast} pointerEvents="none">
          <Text style={styles.toastText}>{toast}</Text>
        </View>
      ) : null}

      {DEMO ? (
        <View style={styles.demoPills}>
          <TouchableOpacity
            style={[styles.demoPill, demoWalking && styles.demoPillOn]}
            onPress={() => setDemoWalking((v) => !v)}
          >
            <Text style={styles.demoPillText}>{demoWalking ? '■ Stop' : '▶ Walk'}</Text>
          </TouchableOpacity>
          <TouchableOpacity style={styles.demoPill} onPress={resetDemo}>
            <Text style={styles.demoPillText}>↺ 232</Text>
          </TouchableOpacity>
        </View>
      ) : null}

      {/* 5. BOTTOM ACTION BAR: Firefighter Live Fire Reporting OR Occupant Trapped/Mayday */}
      <View style={styles.bottomBar}>
        <TouchableOpacity style={styles.foldHandleBar} onPress={() => setBarOpen((v) => !v)} hitSlop={8}>
          <Text style={styles.foldText}>{barOpen ? '▼ hide' : role === 'firefighter' ? '▲ reports' : "▲ I'm trapped"}</Text>
        </TouchableOpacity>
        {!barOpen ? null : role === 'firefighter' ? (
          <View style={styles.ffStack}>
            <View style={styles.unitRow}>
              <Text style={styles.unitLabel}>UNIT</Text>
              <TextInput
                style={styles.unitInput}
                value={unitName}
                onChangeText={setUnitName}
                placeholder="Unit name"
                placeholderTextColor="#64748b"
                autoCorrect={false}
              />
              <TouchableOpacity
                style={[styles.scanBtn, qrScan && styles.scanBtnOn]}
                onPress={() => setQrScan((on) => !on)}
              >
                <Text style={styles.scanBtnText}>{qrScan ? 'Scanning QR' : 'Scan QR'}</Text>
              </TouchableOpacity>
            </View>
            {reportNote ? (
              <Text style={styles.reportNote} numberOfLines={3}>
                {reportNote.transcript ? `${reportNote.transcript}. ` : ''}
                {reportNote.confirmation}
              </Text>
            ) : null}
            <TouchableOpacity
              activeOpacity={0.85}
              onPressIn={startTalk}
              onPressOut={stopTalk}
              style={[styles.talkBtn, talking && styles.talkBtnOn]}
            >
              <Text style={styles.reportBtnText}>{talking ? 'Release to send' : 'Hold to talk'}</Text>
            </TouchableOpacity>
            <View style={styles.textReportRow}>
              <TextInput
                style={styles.textReportInput}
                value={reportText}
                onChangeText={setReportText}
                placeholder="Or type a report"
                placeholderTextColor="#64748b"
                onSubmitEditing={sendTextReport}
              />
              <TouchableOpacity style={styles.sendReportBtn} onPress={sendTextReport}>
                <Text style={styles.sendReportText}>Send</Text>
              </TouchableOpacity>
            </View>
            <View style={styles.ffActionRow}>
              <TouchableOpacity
                activeOpacity={0.85}
                onPress={reportFireHere}
                style={styles.reportFireBtn}
              >
                <Text style={styles.reportBtnText}>Report fire here</Text>
                <Text style={styles.reportBtnSub}>{currentLabel}</Text>
              </TouchableOpacity>

              <TouchableOpacity
                activeOpacity={0.85}
                onPress={() => sendFireReport('clear')}
                style={styles.reportClearBtn}
              >
                <Text style={styles.reportBtnText}>Room clear</Text>
                <Text style={styles.reportBtnSub}>Mark searched</Text>
              </TouchableOpacity>
            </View>
          </View>
        ) : (
          <TouchableOpacity
            activeOpacity={0.85}
            disabled={trappedSent}
            onPressIn={beginTrappedHold}
            onPressOut={cancelTrappedHold}
            style={[styles.maydayBtn, trappedSent && styles.maydayBtnSent]}
          >
            <Animated.View pointerEvents="none" style={[styles.maydayFill, { width: trappedFillWidth }]} />
            <Text style={styles.maydayText}>{trappedSent ? 'Help is coming' : "I'm trapped"}</Text>
          </TouchableOpacity>
        )}
      </View>

      <View style={mapOpen ? styles.mapFull : [styles.minimapCard, role === 'firefighter' && styles.minimapCardFf]}>
        <TouchableOpacity
          style={[styles.mapBar, mapOpen && styles.mapBarOpen]}
          onPress={() => setMapOpen((open) => !open)}
        >
          <Text style={[styles.minimapTitle, mapOpen && styles.mapTitleOpen]}>
            {mapOpen ? 'TAP TO SHRINK' : 'DCC 3D MAP  ·  TAP'}
          </Text>
        </TouchableOpacity>
        <View style={mapOpen ? styles.mapWebFull : styles.mapWebClip}>
          <WebView
            ref={webviewRef}
            key={mapUri}
            source={{ uri: mapUri }}
            style={mapOpen ? styles.mapWebFull : styles.mapWeb}
            pointerEvents={mapOpen ? 'auto' : 'none'}
            originWhitelist={['*']}
            javaScriptEnabled
            domStorageEnabled
            allowsInlineMediaPlayback
            mixedContentMode="always"
            scrollEnabled={!!mapOpen}
            bounces={false}
            setSupportMultipleWindows={false}
            onError={(e) => noteMapError(e?.nativeEvent)}
            onHttpError={(e) => noteMapError(e?.nativeEvent)}
            onLoad={() => {
              setMapError('');
              postMapPose();
              postToMap({ type: 'route', path: mapPathRef.current || [] });
            }}
          />
          {mapError ? (
            <View style={styles.mapErrorBox} pointerEvents="none">
              <Text style={styles.mapErrorText}>{mapError}</Text>
            </View>
          ) : null}
          {mapOpen ? null : <TouchableOpacity style={styles.mapTap} onPress={() => setMapOpen(true)} />}
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#050811' },
  cameraFull: {
    flex: 1,
    width: SCREEN_W,
    height: SCREEN_H,
  },
  permissionContainer: {
    flex: 1,
    backgroundColor: '#070b14',
    justifyContent: 'center',
    alignItems: 'center',
    padding: 28,
  },
  permTitle: { color: '#f97316', fontSize: 22, fontWeight: '900', marginBottom: 12 },
  permSub: { color: '#cbd5e1', fontSize: 15, textAlign: 'center', marginBottom: 24, lineHeight: 22 },
  permButton: {
    backgroundColor: '#f97316',
    paddingVertical: 14,
    paddingHorizontal: 28,
    borderRadius: 12,
  },
  permButtonText: { color: '#fff', fontWeight: '800', fontSize: 16 },
  arFloorContainer: {
    position: 'absolute',
    bottom: SCREEN_H * 0.21,
    left: 0,
    right: 0,
    alignItems: 'center',
    justifyContent: 'center',
  },
  pathLayer: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 86,
    height: SCREEN_H * 0.52,
    alignItems: 'center',
  },
  pathLayerFf: { bottom: 268, height: SCREEN_H * 0.42 },
  stepChip: {
    position: 'absolute',
    bottom: 6,
    maxWidth: SCREEN_W - 36,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    backgroundColor: 'rgba(2, 6, 23, 0.62)',
    borderWidth: 1,
    borderColor: 'rgba(34, 211, 238, 0.45)',
    borderRadius: 999,
    paddingHorizontal: 12,
    paddingVertical: 5,
  },
  stepChipIcon: {
    color: '#67e8f9',
    fontSize: 28,
    lineHeight: 32,
    fontWeight: '800',
    textShadowColor: 'rgba(103, 232, 249, 0.95)',
    textShadowOffset: { width: 0, height: 0 },
    textShadowRadius: 10,
  },
  stepChipText: { color: '#ecfeff', fontSize: 13, fontWeight: '700', flexShrink: 1 },
  stepChipDist: { color: '#67e8f9', fontSize: 13, fontWeight: '800' },
  stepHud: {
    backgroundColor: 'rgba(8, 47, 73, 0.72)',
    borderWidth: 1,
    borderColor: 'rgba(34, 211, 238, 0.85)',
    borderRadius: 8,
    paddingVertical: 6,
    paddingHorizontal: 10,
    marginBottom: 8,
  },
  stepHudText: { color: '#ecfeff', fontSize: 15, fontWeight: '800' },
  topHudWrapper: {
    position: 'absolute',
    top: Platform.OS === 'android' ? 32 : 0,
    left: 10,
    right: 10,
  },
  hudCard: {
    backgroundColor: 'rgba(11, 15, 25, 0.86)',
    borderRadius: 16,
    padding: 12,
    borderWidth: 1.5,
    borderColor: 'rgba(34, 211, 238, 0.55)',
  },
  hudCardFirefighter: {
    borderColor: 'rgba(249, 115, 22, 0.75)',
  },
  hudCardMayday: {
    borderColor: '#ef4444',
    backgroundColor: 'rgba(69, 10, 10, 0.92)',
  },
  modeSwitchRow: {
    flexDirection: 'row',
    gap: 8,
    marginBottom: 8,
  },
  modeBtn: {
    flex: 1,
    paddingVertical: 7,
    borderRadius: 8,
    backgroundColor: '#1e293b',
    alignItems: 'center',
    borderWidth: 1,
    borderColor: '#334155',
  },
  modeBtnOccupantActive: {
    backgroundColor: '#0891b2',
    borderColor: '#22d3ee',
  },
  modeBtnFirefighterActive: {
    backgroundColor: '#ea580c',
    borderColor: '#fb923c',
  },
  modeBtnText: {
    color: '#ffffff',
    fontSize: 11,
    fontWeight: '900',
    letterSpacing: 0.4,
  },
  hudHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 6,
  },
  statusBadge: { flexDirection: 'row', alignItems: 'center' },
  statusDot: { width: 9, height: 9, borderRadius: 5, marginRight: 6 },
  statusText: { color: '#e2e8f0', fontSize: 11, fontWeight: '800', letterSpacing: 0.3 },
  cfgBtn: {
    backgroundColor: '#1e293b',
    paddingHorizontal: 9,
    paddingVertical: 4,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: '#475569',
  },
  cfgBtnText: { color: '#94a3b8', fontSize: 10, fontWeight: '700' },
  urlRow: { flexDirection: 'row', gap: 8, marginBottom: 8 },
  urlInput: {
    flex: 1,
    backgroundColor: '#0f172a',
    color: '#f8fafc',
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#334155',
    fontSize: 12,
  },
  connectBtn: {
    backgroundColor: '#f97316',
    paddingHorizontal: 14,
    justifyContent: 'center',
    borderRadius: 8,
  },
  connectBtnText: { color: '#fff', fontWeight: '800', fontSize: 12 },
  fireBanner: {
    backgroundColor: 'rgba(220, 38, 38, 0.9)',
    paddingVertical: 5,
    paddingHorizontal: 10,
    borderRadius: 8,
    marginBottom: 6,
    borderWidth: 1,
    borderColor: '#fca5a5',
  },
  fireBannerText: {
    color: '#ffffff',
    fontSize: 11,
    fontWeight: '900',
    letterSpacing: 0.3,
  },
  locationLabel: { color: '#94a3b8', fontSize: 10, fontWeight: '800', letterSpacing: 0.7 },
  roomTitle: { color: '#ffffff', fontSize: 21, fontWeight: '900', marginTop: 1 },
  targetSelectorWrap: {
    marginTop: 8,
    paddingTop: 6,
    borderTopWidth: 1,
    borderTopColor: 'rgba(148, 163, 184, 0.2)',
  },
  targetSelectorLabel: {
    color: '#fdba74',
    fontSize: 10,
    fontWeight: '800',
    marginBottom: 5,
  },
  roomChipScroll: {
    flexDirection: 'row',
  },
  roomChip: {
    backgroundColor: '#1e293b',
    paddingHorizontal: 11,
    paddingVertical: 6,
    borderRadius: 8,
    marginRight: 6,
    borderWidth: 1,
    borderColor: '#475569',
  },
  roomChipSelected: {
    backgroundColor: '#ea580c',
    borderColor: '#fed7aa',
  },
  roomChipFire: {
    backgroundColor: '#991b1b',
    borderColor: '#f87171',
  },
  roomChipText: {
    color: '#ffffff',
    fontSize: 12,
    fontWeight: '800',
  },
  egressRow: {
    marginTop: 8,
    paddingTop: 8,
    borderTopWidth: 1,
    borderTopColor: 'rgba(148, 163, 184, 0.2)',
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  egressTarget: { color: '#22d3ee', fontSize: 12, fontWeight: '900' },
  turnInstruction: { color: '#f8fafc', fontSize: 14, fontWeight: '800', marginTop: 2 },
  distanceBadge: {
    color: '#fff',
    fontSize: 19,
    fontWeight: '900',
    backgroundColor: 'rgba(34, 211, 238, 0.22)',
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 8,
    overflow: 'hidden',
  },
  toast: {
    position: 'absolute',
    left: 24,
    right: 24,
    bottom: 280,
    backgroundColor: 'rgba(15, 23, 42, 0.94)',
    borderRadius: 10,
    paddingVertical: 8,
    paddingHorizontal: 12,
    borderWidth: 1,
    borderColor: '#fb923c',
    alignItems: 'center',
  },
  toastText: { color: '#fff', fontSize: 13, fontWeight: '800', textAlign: 'center' },
  minimapCard: {
    position: 'absolute',
    right: 12,
    bottom: 102,
    backgroundColor: 'rgba(11, 15, 25, 0.88)',
    borderRadius: 12,
    padding: 8,
    borderWidth: 1,
    borderColor: '#334155',
  },
  minimapTitle: {
    color: '#a5f3fc',
    fontSize: 10,
    fontWeight: '800',
    textAlign: 'center',
    letterSpacing: 0.4,
  },
  minimapCardFf: { bottom: 268 },
  mapFull: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    zIndex: 50,
    backgroundColor: '#050811',
  },
  mapBar: { alignItems: 'center', paddingVertical: 4 },
  mapBarOpen: {
    paddingTop: Platform.OS === 'android' ? 36 : 52,
    paddingBottom: 8,
    backgroundColor: '#070b14',
  },
  mapTitleOpen: { fontSize: 14, color: '#ecfeff', paddingVertical: 4 },
  mapWebClip: { width: 168, height: 118, borderRadius: 8, overflow: 'hidden' },
  mapWeb: { width: 168, height: 118, backgroundColor: '#0b1020' },
  mapWebFull: { flex: 1, backgroundColor: '#050811' },
  mapTap: { position: 'absolute', left: 0, right: 0, top: 0, bottom: 0 },
  mapErrorBox: {
    position: 'absolute',
    left: 4,
    right: 4,
    top: 4,
    bottom: 4,
    justifyContent: 'center',
  },
  mapErrorText: { color: '#fecaca', fontSize: 9, fontWeight: '700', textAlign: 'center' },
  foldHandle: { alignSelf: 'center', paddingTop: 4, paddingHorizontal: 14 },
  foldHandleBar: {
    alignSelf: 'center',
    marginBottom: 6,
    paddingVertical: 3,
    paddingHorizontal: 14,
    borderRadius: 999,
    backgroundColor: 'rgba(11, 15, 25, 0.8)',
  },
  foldText: { color: '#94a3b8', fontSize: 11, fontWeight: '700' },
  bottomBar: {
    position: 'absolute',
    bottom: 24,
    left: 14,
    right: 14,
  },
  ffStack: { gap: 6 },
  unitRow: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  unitLabel: { color: '#fdba74', fontSize: 10, fontWeight: '800' },
  unitInput: {
    flex: 1,
    backgroundColor: '#0f172a',
    color: '#f8fafc',
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#475569',
    paddingHorizontal: 8,
    paddingVertical: 6,
    fontSize: 12,
  },
  scanBtn: {
    backgroundColor: '#1e293b',
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#475569',
    paddingHorizontal: 10,
    paddingVertical: 8,
  },
  scanBtnOn: { backgroundColor: '#ea580c', borderColor: '#fed7aa' },
  scanBtnText: { color: '#fff', fontSize: 11, fontWeight: '800' },
  reportNote: { color: '#e2e8f0', fontSize: 11, fontWeight: '700' },
  talkBtn: {
    backgroundColor: '#9a3412',
    paddingVertical: 10,
    borderRadius: 12,
    alignItems: 'center',
    borderWidth: 2,
    borderColor: '#fdba74',
  },
  talkBtnOn: { backgroundColor: '#c2410c', borderColor: '#fff' },
  textReportRow: { flexDirection: 'row', gap: 6 },
  textReportInput: {
    flex: 1,
    backgroundColor: '#0f172a',
    color: '#f8fafc',
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#475569',
    paddingHorizontal: 8,
    paddingVertical: 6,
    fontSize: 12,
  },
  sendReportBtn: {
    backgroundColor: '#ea580c',
    borderRadius: 8,
    paddingHorizontal: 12,
    justifyContent: 'center',
  },
  sendReportText: { color: '#fff', fontSize: 12, fontWeight: '800' },
  ffActionRow: {
    flexDirection: 'row',
    gap: 10,
  },
  reportFireBtn: {
    flex: 1.2,
    backgroundColor: '#dc2626',
    paddingVertical: 12,
    borderRadius: 14,
    alignItems: 'center',
    borderWidth: 2,
    borderColor: '#fca5a5',
  },
  reportClearBtn: {
    flex: 1,
    backgroundColor: '#15803d',
    paddingVertical: 12,
    borderRadius: 14,
    alignItems: 'center',
    borderWidth: 2,
    borderColor: '#86efac',
  },
  reportBtnText: {
    color: '#ffffff',
    fontSize: 13,
    fontWeight: '900',
  },
  reportBtnSub: {
    color: 'rgba(255,255,255,0.85)',
    fontSize: 10,
    fontWeight: '700',
    marginTop: 2,
  },
  demoPills: { position: 'absolute', left: 14, bottom: 150, flexDirection: 'row', gap: 6 },
  demoPill: {
    paddingVertical: 6,
    paddingHorizontal: 12,
    borderRadius: 999,
    backgroundColor: 'rgba(11, 15, 25, 0.85)',
    borderWidth: 1,
    borderColor: 'rgba(148, 163, 184, 0.5)',
  },
  demoPillOn: { backgroundColor: '#0e7490', borderColor: '#22d3ee' },
  demoPillText: { color: '#e2e8f0', fontSize: 12, fontWeight: '700' },
  maydayBtn: {
    backgroundColor: '#b91c1c',
    paddingVertical: 16,
    borderRadius: 16,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 2,
    borderColor: '#f87171',
    overflow: 'hidden',
  },
  maydayBtnActive: {
    backgroundColor: '#ef4444',
    borderColor: '#ffffff',
  },
  maydayBtnSent: {
    backgroundColor: '#dc2626',
    borderColor: '#dc2626',
  },
  maydayFill: {
    position: 'absolute',
    left: 0,
    top: 0,
    bottom: 0,
    backgroundColor: '#ef4444',
  },
  maydayText: {
    color: '#ffffff',
    fontSize: 15,
    fontWeight: '900',
    letterSpacing: 0.5,
    zIndex: 1,
  },
});
