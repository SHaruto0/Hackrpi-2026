/* Firesight phone sensors: steps, compass, camera frames. Classic script, no modules. */
(function () {
  "use strict";

  // Step: local peak above 10.6, then a dip below 9.4, at least 280 ms later.
  // Length: Weinberg 0.47 * (amax - amin)^(1/4), clamped to 0.45..0.9 m.
  var LP_ALPHA = 0.25;
  var PEAK_MS2 = 10.6;
  var DIP_MS2 = 9.4;
  var MIN_STEP_MS = 280;
  var HEADING_ALPHA = 0.3;
  var STEP_K = 0.47;
  var STEP_MIN_M = 0.45;
  var STEP_MAX_M = 0.9;
  var FRAME_LONG = 960;
  var FRAME_Q = 0.7;
  var BATCH_MS = 1000;
  var QUEUE_MAX = 200;

  var state = {
    session: "",
    apiBase: "",
    onPose: function () {},
    onStep: function () {},
    onStatus: function () {},
    running: false,
    heading: null,
    sawAbsolute: false,
    warnedNoHeading: false,
    lp: null,
    prev: null,
    peakArmed: false,
    lastStepAt: 0,
    amax: 0,
    amin: 0,
    queue: [],
    flushing: false,
    flushAgain: false,
    batchTimer: null,
    videoEl: null,
    stream: null,
    camTimer: null,
    frameBusy: false,
    frameGen: 0,
    canvas: null
  };

  function status(text) {
    try {
      state.onStatus(text);
    } catch (err) {}
  }

  function emitPose(pose) {
    try {
      state.onPose(pose);
    } catch (err) {}
  }

  function emitStep(step) {
    try {
      state.onStep(step);
    } catch (err) {}
  }

  function norm360(deg) {
    return ((deg % 360) + 360) % 360;
  }

  function init(opts) {
    opts = opts || {};
    state.session = opts.session ? String(opts.session) : "";
    state.apiBase = String(opts.apiBase || "").replace(/\/$/, "");
    state.onPose = typeof opts.onPose === "function" ? opts.onPose : function () {};
    state.onStep = typeof opts.onStep === "function" ? opts.onStep : function () {};
    state.onStatus = typeof opts.onStatus === "function" ? opts.onStatus : function () {};
  }

  // iOS: webkitCompassHeading. Android absolute: 360 - alpha. Smooth on the circle.
  function smoothHeading(deg) {
    deg = norm360(deg);
    if (state.heading == null) {
      state.heading = deg;
      return;
    }
    var delta = ((deg - state.heading + 540) % 360) - 180;
    state.heading = norm360(state.heading + HEADING_ALPHA * delta);
  }

  function applyHeading(e, fromAbs) {
    if (!e) return;
    if (typeof e.webkitCompassHeading === "number" && isFinite(e.webkitCompassHeading)) {
      smoothHeading(e.webkitCompassHeading);
      return;
    }
    var absolute = fromAbs || e.absolute === true;
    if (!absolute) return;
    if (!fromAbs && state.sawAbsolute) return;
    if (fromAbs) state.sawAbsolute = true;
    if (typeof e.alpha !== "number" || !isFinite(e.alpha)) return;
    smoothHeading(360 - e.alpha);
  }

  function onOrient(e) {
    applyHeading(e, false);
  }

  function onOrientAbs(e) {
    applyHeading(e, true);
  }

  function heading() {
    return state.heading;
  }

  function resetDetector() {
    state.lp = null;
    state.prev = null;
    state.peakArmed = false;
    state.amax = 0;
    state.amin = 0;
    state.lastStepAt = 0;
  }

  function stepLength(amax, amin) {
    var amp = amax - amin;
    if (amp < 0) amp = 0;
    var length = STEP_K * Math.pow(amp, 0.25);
    if (!isFinite(length) || length < STEP_MIN_M) length = STEP_MIN_M;
    if (length > STEP_MAX_M) length = STEP_MAX_M;
    return Math.round(length * 1000) / 1000;
  }

  function detectStep(mag, now) {
    if (state.lp == null) {
      state.lp = mag;
      state.prev = mag;
      state.amax = mag;
      state.amin = mag;
      return;
    }
    var v = LP_ALPHA * mag + (1 - LP_ALPHA) * state.lp;
    state.lp = v;
    if (v > state.amax) state.amax = v;
    if (v < state.amin) state.amin = v;

    if (state.prev != null && v < state.prev && state.prev > PEAK_MS2) {
      state.peakArmed = true;
    }

    if (state.peakArmed && v < DIP_MS2) {
      state.peakArmed = false;
      if (now - state.lastStepAt >= MIN_STEP_MS) {
        var h = heading();
        if (h == null) {
          h = 0;
          if (!state.warnedNoHeading) {
            state.warnedNoHeading = true;
            status("No compass yet. Steps use heading 0.");
          }
        }
        var step = {
          t: now,
          heading_deg: Math.round(norm360(h) * 10) / 10,
          length_m: stepLength(state.amax, state.amin)
        };
        state.queue.push(step);
        if (state.queue.length > QUEUE_MAX) {
          state.queue.splice(0, state.queue.length - QUEUE_MAX);
        }
        state.lastStepAt = now;
        state.amax = v;
        state.amin = v;
        emitStep(step);
      }
    }
    state.prev = v;
  }

  function onMotion(e) {
    if (!state.running || !e) return;
    var a = e.accelerationIncludingGravity;
    if (!a || a.x == null || a.y == null || a.z == null) return;
    var mag = Math.hypot(a.x, a.y, a.z);
    if (!isFinite(mag)) return;
    detectStep(mag, Date.now());
  }

  async function errorText(res) {
    var msg = String(res.status);
    try {
      var j = await res.json();
      if (j && j.detail != null) {
        var d = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
        if (d) msg += " " + d;
      }
    } catch (err) {}
    return msg;
  }

  async function postJSON(path, body) {
    var res = await fetch(state.apiBase + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error(await errorText(res));
    return res.json();
  }

  function hasSession() {
    if (state.session) return true;
    status("No session");
    return false;
  }

  async function flushSteps() {
    if (!state.session || !state.queue.length) return;
    if (state.flushing) {
      state.flushAgain = true;
      return;
    }
    state.flushing = true;
    try {
      while (state.queue.length) {
        var batch = state.queue.slice();
        state.queue.length = 0;
        try {
          var pose = await postJSON("/api/loc/steps", {
            session: state.session,
            steps: batch
          });
          emitPose(pose);
        } catch (err) {
          if (batch.length) {
            var merged = batch.concat(state.queue);
            state.queue = merged.length > QUEUE_MAX ? merged.slice(0, QUEUE_MAX) : merged;
          }
          status("Steps failed (" + err.message + ")");
          break;
        }
      }
    } finally {
      state.flushing = false;
      if (state.flushAgain) {
        state.flushAgain = false;
        if (state.queue.length) flushSteps();
      }
    }
  }

  // Must run in the tap handler. Both iOS prompts are started before the first await.
  async function requestPermissions() {
    try {
      var jobs = [];
      if (typeof DeviceMotionEvent !== "undefined" &&
          typeof DeviceMotionEvent.requestPermission === "function") {
        jobs.push(DeviceMotionEvent.requestPermission());
      }
      if (typeof DeviceOrientationEvent !== "undefined" &&
          typeof DeviceOrientationEvent.requestPermission === "function") {
        jobs.push(DeviceOrientationEvent.requestPermission());
      }
      if (!jobs.length) {
        status("Sensor permission not needed. Tap Walk test.");
        return true;
      }
      status("Asking for sensor access");
      var results = await Promise.all(jobs);
      var ok = results.every(function (r) { return r === "granted"; });
      status(ok ? "Sensors allowed. Tap Walk test." : "Sensor permission denied");
      return ok;
    } catch (err) {
      status("Permission failed (" + err.message + ")");
      return false;
    }
  }

  function start() {
    if (state.running) return;
    state.running = true;
    resetDetector();
    window.addEventListener("devicemotion", onMotion, { passive: true });
    window.addEventListener("deviceorientation", onOrient, { passive: true });
    window.addEventListener("deviceorientationabsolute", onOrientAbs, { passive: true });
    if (state.batchTimer) clearInterval(state.batchTimer);
    state.batchTimer = setInterval(flushSteps, BATCH_MS);
    status("Listening for steps");
  }

  function stop() {
    if (!state.running) return;
    state.running = false;
    window.removeEventListener("devicemotion", onMotion);
    window.removeEventListener("deviceorientation", onOrient);
    window.removeEventListener("deviceorientationabsolute", onOrientAbs);
    if (state.batchTimer) {
      clearInterval(state.batchTimer);
      state.batchTimer = null;
    }
    status("Stopped");
    flushSteps();
  }

  async function startAt(opts) {
    opts = opts || {};
    if (!hasSession()) return;
    try {
      var body = {
        session: state.session,
        level: Number(opts.level),
        x: Number(opts.x),
        y: Number(opts.y)
      };
      if (opts.room_id) body.room_id = String(opts.room_id);
      if (opts.sigma_m != null && opts.sigma_m !== "") body.sigma_m = Number(opts.sigma_m);
      var pose = await postJSON("/api/loc/start", body);
      emitPose(pose);
      status("Position set");
      return pose;
    } catch (err) {
      status("Start failed (" + err.message + ")");
    }
  }

  async function tookStairs(direction) {
    if (!hasSession()) return;
    try {
      var pose = await postJSON("/api/loc/stairs", {
        session: state.session,
        direction: direction
      });
      emitPose(pose);
      var label = direction === "up" ? "Stairs up sent" :
        direction === "down" ? "Stairs down sent" : "Stairs sent";
      status(label);
      return pose;
    } catch (err) {
      status("Stairs failed (" + err.message + ")");
    }
  }

  function stopCamera(silent) {
    var wasOn = !!(state.camTimer || state.stream);
    state.frameGen += 1;
    if (state.camTimer) {
      clearInterval(state.camTimer);
      state.camTimer = null;
    }
    if (state.stream) {
      try {
        state.stream.getTracks().forEach(function (t) { t.stop(); });
      } catch (err) {
        if (!silent) status("Camera failed (" + err.message + ")");
      }
      state.stream = null;
    }
    if (state.videoEl) state.videoEl.srcObject = null;
    state.frameBusy = false;
    if (!silent && wasOn) status("Camera off");
  }

  async function captureFrame() {
    if (state.frameBusy) return;
    var video = state.videoEl;
    if (!video || !video.videoWidth || !video.videoHeight) return;
    if (!state.session) {
      status("No session");
      return;
    }
    var gen = state.frameGen;
    state.frameBusy = true;
    try {
      var vw = video.videoWidth;
      var vh = video.videoHeight;
      var longSide = Math.max(vw, vh);
      var scale = longSide > FRAME_LONG ? FRAME_LONG / longSide : 1;
      var cw = Math.max(1, Math.round(vw * scale));
      var ch = Math.max(1, Math.round(vh * scale));
      if (!state.canvas) state.canvas = document.createElement("canvas");
      var canvas = state.canvas;
      canvas.width = cw;
      canvas.height = ch;
      var ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, cw, ch);
      if (typeof canvas.toBlob !== "function") throw new Error("toBlob missing");
      var blob = await new Promise(function (resolve) {
        canvas.toBlob(resolve, "image/jpeg", FRAME_Q);
      });
      if (!blob) throw new Error("empty frame");
      if (gen !== state.frameGen) return;
      var fd = new FormData();
      fd.append("session", state.session);
      var h = heading();
      if (h != null) fd.append("heading_deg", String(Math.round(norm360(h) * 10) / 10));
      fd.append("image", blob, "frame.jpg");
      var res = await fetch(state.apiBase + "/api/loc/frame", {
        method: "POST",
        body: fd
      });
      if (gen !== state.frameGen) return;
      if (!res.ok) throw new Error(await errorText(res));
      emitPose(await res.json());
    } catch (err) {
      status("Frame failed (" + err.message + ")");
    } finally {
      if (gen === state.frameGen) state.frameBusy = false;
    }
  }

  function liveVideoStream(stream) {
    if (!stream || typeof stream.getVideoTracks !== "function") return null;
    var tracks = stream.getVideoTracks();
    for (var i = 0; i < tracks.length; i++) {
      if (tracks[i] && tracks[i].readyState === "live") return stream;
    }
    return null;
  }

  // Pass existingStream to reuse a camera already opened in the tap. Omit it to call getUserMedia.
  async function enableCamera(videoEl, everyMs, existingStream) {
    if (!(everyMs > 0)) everyMs = 2500;
    var handed = arguments.length >= 3;
    var reuse = handed ? liveVideoStream(existingStream) : (videoEl ? liveVideoStream(videoEl.srcObject) : null);
    if (handed && !reuse) {
      status("Camera failed (no stream)");
      return false;
    }
    var openedHere = false;
    try {
      if (!videoEl) throw new Error("no video element");
      if (reuse && state.stream === reuse && state.videoEl === videoEl && state.camTimer) {
        status("Camera on");
        return true;
      }
      if (state.stream && state.stream !== reuse) {
        stopCamera(true);
      } else if (state.camTimer) {
        clearInterval(state.camTimer);
        state.camTimer = null;
      }
      var stream = reuse;
      if (!stream) {
        if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
          status("Camera not available");
          return false;
        }
        stream = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: { ideal: "environment" } }
        });
        openedHere = true;
      }
      state.stream = stream;
      state.videoEl = videoEl;
      videoEl.muted = true;
      videoEl.setAttribute("playsinline", "");
      videoEl.setAttribute("webkit-playsinline", "");
      if (videoEl.srcObject !== stream) videoEl.srcObject = stream;
      var playResult = videoEl.play();
      if (playResult && typeof playResult.then === "function") await playResult;
      state.camTimer = setInterval(captureFrame, everyMs);
      status("Camera on");
      return true;
    } catch (err) {
      if (openedHere) stopCamera(true);
      else if (state.camTimer) {
        clearInterval(state.camTimer);
        state.camTimer = null;
      }
      status("Camera failed (" + err.message + ")");
      return false;
    }
  }

  function disableCamera() {
    try {
      stopCamera(false);
    } catch (err) {
      status("Camera failed (" + err.message + ")");
    }
  }

  window.FiresightPDR = {
    init: init,
    requestPermissions: requestPermissions,
    start: start,
    stop: stop,
    startAt: startAt,
    tookStairs: tookStairs,
    enableCamera: enableCamera,
    disableCamera: disableCamera,
    heading: heading
  };
})();
