/* ==========================================================================
   Gate scanner — deliberately minimal.

   On screen: the camera, a short anonymous result flash, two controls
   (flip camera, sound) and sign-out. Nothing else.

   No names, no counters, no "who scanned before you": the gate is a shared
   device and one person's attendance must not be visible to the next. If your
   gate staff need the name to verify a match, set HRMS_KIOSK_SHOW_NAME=true.

   Behaviour
     * auto-scan loop (~1.4 s), in-flight guard, server-side debounce
     * green = marked, amber = look again, red = problem
     * optional beep, tap-the-camera to scan now, wake lock, tiny status dot
   ========================================================================== */
(function () {
  "use strict";

  var cfg = window.KIOSK_CONFIG || {};
  var $ = function (id) { return document.getElementById(id); };

  var video = $("video");
  var cam = $("cam");
  var guide = $("guide");
  var flash = $("flash");
  var flashIco = $("flashIco");
  var flashTxt = $("flashTxt");
  var flashTime = $("flashTime");
  var startBtn = $("startBtn");
  var flipBtn = $("flipBtn");
  var soundBtn = $("soundBtn");
  var soundIco = $("soundIco");
  var soundLbl = $("soundLbl");
  var liveDot = $("liveDot");
  var clock = $("clock");
  var stateDot = $("stateDot");

  var canvas = document.createElement("canvas");
  var stream = null;
  var facing = "user";
  var soundOn = true;
  var busy = false;
  var running = false;
  var pausedUntil = 0;
  var loopTimer = null;
  var pollTimer = null;
  var clockTimer = null;
  var wakeLock = null;
  var flashTimer = null;

  var SCAN_INTERVAL_MS = 1400;
  var HOLD_SUCCESS_MS = 2600;
  var HOLD_NOTICE_MS = 1700;
  var HOLD_ERROR_MS = 2200;

  /* ------------------------------------------------------------------ audio */
  var audioCtx = null;
  function beep(kind) {
    if (!soundOn) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      var osc = audioCtx.createOscillator();
      var gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.type = "sine";
      var t = audioCtx.currentTime;
      if (kind === "ok") {
        osc.frequency.setValueAtTime(880, t);
        osc.frequency.setValueAtTime(1320, t + 0.09);
        gain.gain.setValueAtTime(0.15, t);
        gain.gain.exponentialRampToValueAtTime(0.001, t + 0.26);
        osc.start(t); osc.stop(t + 0.27);
      } else if (kind === "warn") {
        osc.frequency.setValueAtTime(560, t);
        gain.gain.setValueAtTime(0.1, t);
        gain.gain.exponentialRampToValueAtTime(0.001, t + 0.2);
        osc.start(t); osc.stop(t + 0.21);
      } else {
        osc.frequency.setValueAtTime(300, t);
        gain.gain.setValueAtTime(0.13, t);
        gain.gain.exponentialRampToValueAtTime(0.001, t + 0.32);
        osc.start(t); osc.stop(t + 0.33);
      }
    } catch (error) { /* sound is a nicety */ }
  }

  /* ---------------------------------------------------------------- feedback */
  function nowClock() {
    return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function setGuide(state) {
    guide.className = "kiosk-guide" + (state ? " " + state : "");
    cam.className = "kiosk-cam" + (state ? " " + state : "");
  }

  /** kind: ok | warn | bad | "" (idle) */
  function show(kind, text, time, holdMs) {
    window.clearTimeout(flashTimer);
    flashIco.textContent = kind === "ok" ? "✓" : (kind === "warn" ? "!" : (kind === "bad" ? "✕" : "•"));
    flashTxt.textContent = text;
    flashTime.textContent = time || "";
    flash.hidden = false;
    flash.className = "kiosk-flash" + (kind ? " " + kind : "");
    setGuide(kind === "ok" ? "ok" : (kind === "warn" ? "warn" : (kind === "bad" ? "bad" : "")));
    if (holdMs) {
      flashTimer = window.setTimeout(function () {
        flash.hidden = true;
        setGuide("");
      }, holdMs);
    }
  }

  function clearFlash() {
    window.clearTimeout(flashTimer);
    flash.hidden = true;
    setGuide("");
  }

  /* ---------------------------------------------------------------- capture */
  function capture() {
    if (!stream || busy || !running || Date.now() < pausedUntil) return;
    var w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return;

    // ~720 px on the long edge is plenty for YuNet and keeps the upload small.
    var scale = Math.min(1, 720 / Math.max(w, h));
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);

    busy = true;
    canvas.toBlob(function (blob) {
      if (!blob) { busy = false; return; }
      var data = new FormData();
      data.append("_csrf", cfg.csrf);
      data.append("image", blob, "scan.jpg");

      fetch(cfg.markUrl, { method: "POST", body: data, credentials: "same-origin" })
        .then(function (response) {
          return response.json().then(function (body) { return { ok: response.ok, body: body }; });
        })
        .then(function (out) { handle(out.body, out.ok); })
        .catch(function () {
          show("bad", "Connection problem", "server did not answer", HOLD_ERROR_MS);
          setState("bad");
          pausedUntil = Date.now() + HOLD_ERROR_MS;
        })
        .then(function () { busy = false; });
    }, "image/jpeg", 0.85);
  }

  function handle(body, httpOk) {
    if (!body || typeof body !== "object") {
      show("bad", "Unexpected response", "", HOLD_ERROR_MS);
      return;
    }
    var name = cfg.showName && body.employee ? (body.employee.name || "") : "";

    switch (body.action) {
      case "check_in":
        show("ok", "IN" + (name ? " · " + name : ""), (body.check_in || nowClock()).slice(0, 5), HOLD_SUCCESS_MS);
        beep("ok");
        pausedUntil = Date.now() + HOLD_SUCCESS_MS;
        break;
      case "check_out":
        show("ok", "OUT" + (name ? " · " + name : ""), (body.check_out || nowClock()).slice(0, 5), HOLD_SUCCESS_MS);
        beep("ok");
        pausedUntil = Date.now() + HOLD_SUCCESS_MS;
        break;
      case "duplicate":
        show("warn", "Already scanned", "wait a moment", HOLD_NOTICE_MS);
        beep("warn");
        pausedUntil = Date.now() + HOLD_NOTICE_MS;
        break;
      case "already_done":
        show("warn", "Already done today", (body.check_out || "").slice(0, 5), HOLD_NOTICE_MS);
        beep("warn");
        pausedUntil = Date.now() + HOLD_NOTICE_MS;
        break;
      case "blocked":
        show("warn", "On leave today", "", HOLD_NOTICE_MS);
        beep("warn");
        pausedUntil = Date.now() + HOLD_NOTICE_MS;
        break;
      case "inactive":
        show("warn", "Not active", "ask HR", HOLD_NOTICE_MS);
        beep("warn");
        pausedUntil = Date.now() + HOLD_NOTICE_MS;
        break;
      case "no_face":
        show("warn", "No face found", "step into the oval", HOLD_NOTICE_MS);
        pausedUntil = Date.now() + 1200;
        break;
      case "unknown_face":
        show("bad", "Not recognised", "ask HR to enrol", HOLD_ERROR_MS);
        beep("bad");
        pausedUntil = Date.now() + HOLD_ERROR_MS;
        break;
      case "service_down":
      case "service_error":
        show("bad", "Scanner offline", "use the HR console", HOLD_ERROR_MS);
        beep("bad");
        setState("bad");
        pausedUntil = Date.now() + 4000;
        break;
      default:
        show(httpOk ? "warn" : "bad", body.action || "Notice", "", HOLD_NOTICE_MS);
        pausedUntil = Date.now() + HOLD_NOTICE_MS;
    }

    setState(body.action === "service_down" || body.action === "service_error" ? "bad" : "ok");
  }

  /* ----------------------------------------------------------------- camera */
  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      show("bad", "No camera access", "use HTTPS", 4000);
      startBtn.querySelector("strong").textContent = "Camera needs HTTPS";
      startBtn.querySelector("small").textContent = "open this page over https://";
      return;
    }
    if (cfg.faceRequired && !cfg.faceHealthy) {
      show("bad", "Scanner disabled", "face service offline", 4000);
      return;
    }

    startBtn.querySelector("strong").textContent = "Starting…";
    startBtn.querySelector("small").textContent = "allow the camera when asked";

    navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: facing }, width: { ideal: 1280 }, height: { ideal: 960 } },
      audio: false
    }).then(function (mediaStream) {
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
      stream = mediaStream;
      video.srcObject = mediaStream;
      cam.classList.toggle("mirror", facing === "user");
      running = true;
      startBtn.hidden = true;
      liveDot.hidden = false;
      setState("ok");
      clearFlash();
      requestWakeLock();
      if (cfg.showName) show("", "Ready", "", 0);
      if (!loopTimer) loopTimer = window.setInterval(capture, SCAN_INTERVAL_MS);
    }).catch(function (error) {
      running = false;
      startBtn.hidden = false;
      startBtn.querySelector("strong").textContent = "Tap to retry";
      startBtn.querySelector("small").textContent =
        error && error.name === "NotAllowedError" ? "camera permission was denied" : "camera unavailable";
      show("bad", "Camera blocked", (error && error.name) || "", 3500);
      setState("bad");
    });
  }

  function stopCamera() {
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    video.srcObject = null;
    running = false;
    liveDot.hidden = true;
    startBtn.hidden = false;
    clearFlash();
    releaseWakeLock();
  }

  function setState(state) {
    stateDot.dataset.state = state; // ok | bad | idle
  }

  /* --------------------------------------------------------------- wake lock */
  function requestWakeLock() {
    if (!("wakeLock" in navigator)) return;
    navigator.wakeLock.request("screen").then(function (lock) {
      wakeLock = lock;
      lock.addEventListener("release", function () { wakeLock = null; });
    }).catch(function () { /* not critical */ });
  }
  function releaseWakeLock() {
    if (wakeLock) { wakeLock.release().catch(function () {}); wakeLock = null; }
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && stream) requestWakeLock();
  });

  /* ------------------------------------------------------------ status dot */
  function poll() {
    fetch(cfg.statusUrl, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.server_time) clock.textContent = data.server_time;
        var face = data.face_service || {};
        setState(face.ok ? (running ? "ok" : "idle") : "bad");
        stateDot.title = face.ok ? "Scanner ready" : ("Face service: " + (face.detail || "offline"));
      })
      .catch(function () {
        setState("bad");
        stateDot.title = "Cannot reach the server";
      });
  }

  /* -------------------------------------------------------------- controls */
  document.addEventListener("DOMContentLoaded", function () {
    // One gesture starts the camera (required by iOS, and for the permission prompt).
    startBtn.addEventListener("click", startCamera);

    // Tapping the camera scans immediately instead of waiting for the next tick.
    cam.addEventListener("click", function () {
      if (!running) { startCamera(); return; }
      pausedUntil = 0;
      capture();
    });

    flipBtn.addEventListener("click", function () {
      facing = facing === "user" ? "environment" : "user";
      cam.classList.toggle("mirror", facing === "user");
      flipBtn.classList.add("is-active");
      window.setTimeout(function () { flipBtn.classList.remove("is-active"); }, 350);
      if (running) startCamera();          // restart on the other camera
      var label = flipBtn.querySelector(".lbl");
      if (label) label.textContent = facing === "user" ? "Front" : "Back";
    });

    soundBtn.addEventListener("click", function () {
      soundOn = !soundOn;
      soundBtn.classList.toggle("is-on", soundOn);
      soundIco.textContent = soundOn ? "🔔" : "🔕";
      soundLbl.textContent = soundOn ? "Sound" : "Muted";
      if (soundOn) beep("warn");
    });

    poll();
    pollTimer = window.setInterval(poll, 20000);
    clockTimer = window.setInterval(function () {
      clock.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }, 1000);

    window.addEventListener("beforeunload", function () {
      stopCamera();
      if (loopTimer) window.clearInterval(loopTimer);
      if (pollTimer) window.clearInterval(pollTimer);
      if (clockTimer) window.clearInterval(clockTimer);
    });

    // Kiosk devices usually already granted the camera: start without a tap.
    if (navigator.permissions && navigator.permissions.query) {
      navigator.permissions.query({ name: "camera" }).then(function (status) {
        if (status.state === "granted") startCamera();
      }).catch(function () { /* Safari: wait for the tap */ });
    }
  });
})();
