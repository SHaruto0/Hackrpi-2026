import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  StyleSheet,
  Text,
  View,
  TextInput,
  TouchableOpacity,
  Dimensions,
  SafeAreaView,
  Platform,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { CameraView, useCameraPermissions } from 'expo-camera';
import * as Speech from 'expo-speech';

const { width: SCREEN_W, height: SCREEN_H } = Dimensions.get('window');

// Default to your laptop's detected Wi-Fi IP on port 8000
const DEFAULT_WS_URL = 'ws://129.161.52.222:8000/ws/ar';

// Audio cue voice lines (triggered by JSON flags from backend)
const AUDIO_CUE_PHRASES = {
  'system_online.mp3': 'Firesight Visual Positioning Online.',
  'hazard_ahead_rerouting.mp3': 'Warning! Fire hazard reported ahead. Rerouting evacuation path.',
  'route_recalculated.mp3': 'Evacuation route updated.',
  'proceed_to_next_waypoint.mp3': 'Continue along Corridor 219 toward exit.',
  'arrived_at_exit.mp3': 'You have reached the stairwell exit. Proceed to safety.',
  'mayday_transmitting.mp3': 'Mayday alert transmitted to Incident Command.',
  'mayday_acknowledged.mp3': 'Incident Commander acknowledged your Mayday.',
};

// Compute relative yaw angle (in degrees) from camera pose to next waypoint in ARKit world (X, Z)
function computeTurnAngleDeg(camPos, camQuat, nextWaypoint) {
  if (!camPos || !nextWaypoint) return 0;
  const dx = nextWaypoint[0] - camPos[0];
  const dz = nextWaypoint[2] - camPos[2];
  if (Math.hypot(dx, dz) < 0.25) return 0;

  // Target bearing in world (X, Z) where -Z is forward
  const targetYaw = Math.atan2(dx, -dz);

  // Extract camera forward vector (-Z axis of camera in world) from quaternion [qx, qy, qz, qw]
  if (camQuat && camQuat.length === 4) {
    const [qx, qy, qz, qw] = camQuat;
    const lookX = -(2 * (qx * qz + qw * qy));
    const lookZ = -(1 - 2 * (qx * qx + qy * qy));
    const camYaw = Math.atan2(lookX, -lookZ);
    let diff = ((targetYaw - camYaw) * 180) / Math.PI;
    while (diff > 180) diff -= 360;
    while (diff < -180) diff += 360;
    return Math.max(-65, Math.min(65, diff));
  }
  return 0;
}

export default function App() {
  const [permission, requestPermission] = useCameraPermissions();
  const [wsUrl, setWsUrl] = useState(DEFAULT_WS_URL);
  const [connected, setConnected] = useState(false);
  const [localized, setLocalized] = useState(false);
  const [position, setPosition] = useState([0.1, 0.0, 0.5]);
  const [quaternion, setQuaternion] = useState([0, 0, 0, 1]);
  const [currentLabel, setCurrentLabel] = useState('Room 232 (Scanning...)');
  const [inliers, setInliers] = useState(0);
  const [latencyMs, setLatencyMs] = useState(0);
  const [nodes, setNodes] = useState([]);
  const [route, setRoute] = useState({
    start_node: 'room_232',
    start_label: 'Room 232 (Inside)',
    target_exit: 'exit_west',
    path_ids: ['room_232', 'door_232', 'corr_232_w', 'exit_west'],
    path_coords: [
      [0.1, 0.0, 0.5],
      [-1.1, 0.0, -0.8],
      [-2.0, 0.0, -4.3],
      [-5.5, 0.0, -4.8],
    ],
    total_distance_m: 9.4,
    reachable: true,
  });
  const [mayday, setMayday] = useState(false);
  const [pulsePhase, setPulsePhase] = useState(0);
  const [showUrlBar, setShowUrlBar] = useState(false);

  const cameraRef = useRef(null);
  const wsRef = useRef(null);
  const isCapturingRef = useRef(false);

  // Animate glowing floor arrows
  useEffect(() => {
    const id = setInterval(() => {
      setPulsePhase((p) => (p + 1) % 3);
    }, 350);
    return () => clearInterval(id);
  }, []);

  // Connect to FastAPI /ws/ar WebSocket
  const connectWebSocket = (urlToConnect) => {
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
            setCurrentLabel(msg.route.start_label || 'DCC 200 Level');
          }
          if (msg.localized) {
            setLocalized(true);
            if (msg.position) setPosition(msg.position);
            if (msg.quaternion) setQuaternion(msg.quaternion);
            if (msg.num_inliers !== undefined) setInliers(msg.num_inliers);
            if (msg.latency_ms !== undefined) setLatencyMs(msg.latency_ms);
          } else if (msg.localized === false) {
            setLocalized(false);
            if (msg.latency_ms !== undefined) setLatencyMs(msg.latency_ms);
          }
          if (msg.audio_cue && AUDIO_CUE_PHRASES[msg.audio_cue]) {
            try {
              Speech.stop();
              Speech.speak(AUDIO_CUE_PHRASES[msg.audio_cue], { rate: 1.05, pitch: 0.95 });
            } catch (_) {}
          }
        } catch (_) {}
      };
    } catch (_) {
      setConnected(false);
    }
  };

  useEffect(() => {
    connectWebSocket(wsUrl);
    return () => {
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  // Periodically grab frame from camera, compress to low-bandwidth JPEG, and send to backend
  useEffect(() => {
    const interval = setInterval(async () => {
      if (
        !connected ||
        !cameraRef.current ||
        isCapturingRef.current ||
        !wsRef.current ||
        wsRef.current.readyState !== WebSocket.OPEN
      ) {
        return;
      }
      isCapturingRef.current = true;
      try {
        const photo = await cameraRef.current.takePictureAsync({
          quality: 0.35,
          base64: true,
          skipProcessing: true,
          shutterSound: false,
        });
        if (photo?.base64 && wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(
            JSON.stringify({
              type: 'frame',
              image_b64: photo.base64,
            })
          );
        }
      } catch (_) {
        // Ignore transient camera frame busy errors
      } finally {
        isCapturingRef.current = false;
      }
    }, 1350);

    return () => clearInterval(interval);
  }, [connected]);

  const sendMayday = (active) => {
    setMayday(active);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'mayday', active }));
    }
    if (active) {
      try {
        Speech.stop();
        Speech.speak(AUDIO_CUE_PHRASES['mayday_transmitting.mp3'], { rate: 1.08 });
      } catch (_) {}
    }
  };

  // Next waypoint along Dijkstra route
  const nextWaypoint = useMemo(() => {
    if (!route?.path_coords || route.path_coords.length === 0) return null;
    return route.path_coords.length > 1 ? route.path_coords[1] : route.path_coords[0];
  }, [route]);

  const turnDeg = useMemo(
    () => computeTurnAngleDeg(position, quaternion, nextWaypoint),
    [position, quaternion, nextWaypoint]
  );

  if (!permission) {
    return <View style={styles.container} />;
  }

  if (!permission.granted) {
    return (
      <SafeAreaView style={styles.permissionContainer}>
        <Text style={styles.permTitle}>FIRESIGHT // AR NAVIGATION</Text>
        <Text style={styles.permSub}>
          Firesight needs camera access to match live frames against the 3D mathematical map of DCC 200 Level.
        </Text>
        <TouchableOpacity style={styles.permButton} onPress={requestPermission}>
          <Text style={styles.permButtonText}>ENABLE AR CAMERA</Text>
        </TouchableOpacity>
      </SafeAreaView>
    );
  }

  const shiftX = (turnDeg / 65) * (SCREEN_W * 0.25);
  const mapX = (x) => 8 + ((x + 7) / 53) * 138;
  const mapZ = (z) => 10 + ((z + 7) / 9) * 48;

  return (
    <View style={styles.container}>
      <StatusBar style="light" />

      {/* 1. LIVE FULLSCREEN BACK CAMERA */}
      <CameraView
        ref={cameraRef}
        style={StyleSheet.absoluteFillObject}
        facing="back"
        animateShutter={false}
      />

      {/* 2. 3D PERSPECTIVE AR FLOOR ARROWS OVERLAY (Pure RN 3D Transforms - Crash Proof) */}
      <View style={styles.arFloorContainer} pointerEvents="none">
        {[2, 1, 0].map((idx) => {
          const isLit = pulsePhase === idx;
          const scale = 1.0 - idx * 0.22;
          const offsetX = shiftX * ((idx + 1) / 3);
          const color = !route.reachable
            ? '#ef4444'
            : isLit
            ? '#22d3ee'
            : 'rgba(6, 182, 212, 0.45)';

          return (
            <View
              key={idx}
              style={[
                styles.chevronRow,
                {
                  marginBottom: 18,
                  transform: [
                    { perspective: 420 },
                    { translateX: offsetX },
                    { rotateX: '58deg' },
                    { rotateZ: `${turnDeg * 0.45}deg` },
                    { scale },
                  ],
                },
              ]}
            >
              <View
                style={[
                  styles.chevronBox,
                  {
                    borderTopColor: color,
                    borderRightColor: color,
                    shadowColor: color,
                    shadowOpacity: isLit ? 0.9 : 0.3,
                  },
                ]}
              />
            </View>
          );
        })}
      </View>

      {/* 3. TOP TACTICAL VISOR HUD */}
      <SafeAreaView style={styles.topHudWrapper}>
        <View style={[styles.hudCard, mayday && styles.hudCardMayday]}>
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
                    ? `VPS LOCKED (${inliers} PTS • ${Math.round(latencyMs)}ms)`
                    : 'VPS SCANNING...'
                  : 'SERVER OFFLINE'}
              </Text>
            </View>

            <TouchableOpacity onPress={() => setShowUrlBar((s) => !s)} style={styles.cfgBtn}>
              <Text style={styles.cfgBtnText}>{showUrlBar ? 'Hide IP' : 'Server IP'}</Text>
            </TouchableOpacity>
          </View>

          {showUrlBar && (
            <View style={styles.urlRow}>
              <TextInput
                style={styles.urlInput}
                value={wsUrl}
                onChangeText={setWsUrl}
                autoCapitalize="none"
                autoCorrect={false}
                placeholder="ws://129.161.52.222:8000/ws/ar"
                placeholderTextColor="#64748b"
              />
              <TouchableOpacity
                style={styles.connectBtn}
                onPress={() => connectWebSocket(wsUrl)}
              >
                <Text style={styles.connectBtnText}>Connect</Text>
              </TouchableOpacity>
            </View>
          )}

          {/* Pinpointed Room / Hallway Location */}
          <Text style={styles.locationLabel}>PINPOINTED LOCATION (DCC 200)</Text>
          <Text style={styles.roomTitle}>{currentLabel.toUpperCase()}</Text>
          <Text style={styles.coordsText}>
            XYZ: ({position[0].toFixed(2)}m, {position[1].toFixed(2)}m, {position[2].toFixed(2)}m)
          </Text>

          {/* Active Dijkstra Egress Bar */}
          <View style={styles.egressRow}>
            <View>
              <Text style={styles.egressTarget}>
                {route.reachable
                  ? `EXIT VIA: ${(route.target_exit || '').toUpperCase()}`
                  : '⚠️ ALL ROUTES BLOCKED'}
              </Text>
              <Text style={styles.turnInstruction}>
                {Math.abs(turnDeg) < 15
                  ? '⬆ PROCEED STRAIGHT AHEAD'
                  : turnDeg > 0
                  ? `↗ VEER RIGHT (${Math.round(turnDeg)}°)`
                  : `↖ VEER LEFT (${Math.round(Math.abs(turnDeg))}°)`}
              </Text>
            </View>
            <Text style={styles.distanceBadge}>
              {route.total_distance_m ? `${route.total_distance_m}m` : '—'}
            </Text>
          </View>
        </View>
      </SafeAreaView>

      {/* 4. LIVE DCC 200 LEVEL MINI-MAP (Bottom Right, Pure RN Views) */}
      <View style={styles.minimapCard}>
        <Text style={styles.minimapTitle}>DCC 200 FLOOR MAP</Text>
        <View style={styles.minimapCanvas}>
          {/* Corridor 219 bar */}
          <View
            style={{
              position: 'absolute',
              left: mapX(-6),
              top: mapZ(-5) - 2,
              width: mapX(44) - mapX(-6),
              height: 5,
              backgroundColor: '#334155',
              borderRadius: 3,
            }}
          />
          {/* Waypoint nodes */}
          {nodes.map((n) => (
            <View
              key={n.id}
              style={{
                position: 'absolute',
                left: mapX(n.pos[0]) - 3,
                top: mapZ(n.pos[2]) - 3,
                width: n.is_exit ? 7 : 5,
                height: n.is_exit ? 7 : 5,
                borderRadius: 4,
                backgroundColor: n.blocked ? '#ef4444' : n.is_exit ? '#10b981' : '#64748b',
              }}
            />
          ))}
          {/* Live Firefighter Position Dot */}
          <View
            style={{
              position: 'absolute',
              left: mapX(position[0]) - 5,
              top: mapZ(position[2]) - 5,
              width: 10,
              height: 10,
              borderRadius: 5,
              backgroundColor: '#f97316',
              borderWidth: 1.5,
              borderColor: '#ffffff',
            }}
          />
        </View>
      </View>

      {/* 5. BOTTOM PUSH-TO-TALK MAYDAY BUTTON */}
      <View style={styles.bottomBar}>
        <TouchableOpacity
          activeOpacity={0.85}
          onPressIn={() => sendMayday(true)}
          onPressOut={() => sendMayday(false)}
          style={[styles.maydayBtn, mayday && styles.maydayBtnActive]}
        >
          <Text style={styles.maydayText}>
            {mayday ? '🚨 TRANSMITTING MAYDAY TO COMMAND...' : '🎙️ HOLD FOR MAYDAY (PTT)'}
          </Text>
        </TouchableOpacity>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#050811' },
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
    bottom: SCREEN_H * 0.19,
    left: 0,
    right: 0,
    alignItems: 'center',
    justifyContent: 'center',
  },
  chevronRow: {
    alignItems: 'center',
    justifyContent: 'center',
  },
  chevronBox: {
    width: 68,
    height: 68,
    borderTopWidth: 14,
    borderRightWidth: 14,
    transform: [{ rotate: '-45deg' }],
    borderRadius: 6,
  },
  topHudWrapper: {
    position: 'absolute',
    top: Platform.OS === 'android' ? 34 : 0,
    left: 12,
    right: 12,
  },
  hudCard: {
    backgroundColor: 'rgba(11, 15, 25, 0.86)',
    borderRadius: 16,
    padding: 14,
    borderWidth: 1.5,
    borderColor: 'rgba(249, 115, 22, 0.55)',
  },
  hudCardMayday: {
    borderColor: '#ef4444',
    backgroundColor: 'rgba(69, 10, 10, 0.90)',
  },
  hudHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 8,
  },
  statusBadge: { flexDirection: 'row', alignItems: 'center' },
  statusDot: { width: 9, height: 9, borderRadius: 5, marginRight: 6 },
  statusText: { color: '#e2e8f0', fontSize: 11, fontWeight: '800', letterSpacing: 0.4 },
  cfgBtn: {
    backgroundColor: '#1e293b',
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: '#475569',
  },
  cfgBtnText: { color: '#94a3b8', fontSize: 11, fontWeight: '700' },
  urlRow: { flexDirection: 'row', gap: 8, marginBottom: 10 },
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
  locationLabel: { color: '#94a3b8', fontSize: 10, fontWeight: '800', letterSpacing: 0.8 },
  roomTitle: { color: '#ffffff', fontSize: 22, fontWeight: '900', marginTop: 2 },
  coordsText: { color: '#38bdf8', fontSize: 12, fontWeight: '700', marginTop: 2 },
  egressRow: {
    marginTop: 10,
    paddingTop: 10,
    borderTopWidth: 1,
    borderTopColor: 'rgba(148, 163, 184, 0.2)',
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  egressTarget: { color: '#22d3ee', fontSize: 12, fontWeight: '800' },
  turnInstruction: { color: '#f8fafc', fontSize: 15, fontWeight: '900', marginTop: 2 },
  distanceBadge: {
    color: '#fff',
    fontSize: 20,
    fontWeight: '900',
    backgroundColor: 'rgba(34, 211, 238, 0.2)',
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 8,
    overflow: 'hidden',
  },
  minimapCard: {
    position: 'absolute',
    right: 14,
    bottom: 104,
    backgroundColor: 'rgba(11, 15, 25, 0.88)',
    borderRadius: 12,
    padding: 8,
    borderWidth: 1,
    borderColor: '#334155',
  },
  minimapTitle: {
    color: '#94a3b8',
    fontSize: 9,
    fontWeight: '800',
    textAlign: 'center',
    marginBottom: 2,
  },
  minimapCanvas: {
    width: 155,
    height: 70,
    position: 'relative',
  },
  bottomBar: {
    position: 'absolute',
    bottom: 28,
    left: 18,
    right: 18,
  },
  maydayBtn: {
    backgroundColor: '#b91c1c',
    paddingVertical: 17,
    borderRadius: 16,
    alignItems: 'center',
    borderWidth: 2,
    borderColor: '#f87171',
  },
  maydayBtnActive: {
    backgroundColor: '#ef4444',
    borderColor: '#ffffff',
  },
  maydayText: {
    color: '#ffffff',
    fontSize: 16,
    fontWeight: '900',
    letterSpacing: 0.6,
  },
});
