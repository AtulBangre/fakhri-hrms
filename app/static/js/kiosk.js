/* ==========================================================================
   Gate scanner — deliberately minimal.

   On screen: the camera, a result flash or confirmation card, two controls
   (flip camera, sound) and sign-out. Nothing else.

   No counters and no "who scanned before you". The page itself never carries
   a name: with Settings → Gate confirmation card = full, the scan response
   brings the card of the person who just scanned and it disappears after 3.5 s;
   with "anonymous" only IN/OUT and the time are shown.

   Behaviour
     * tap mode (default): camera off until a tap, up to 3 tries, then off again
     * continuous mode: auto-scan loop (~1.4 s), in-flight guard, server-side debounce
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
  var hud = $("hud");
  var hudTxt = $("hudTxt");
  var card = $("card");
  var cardPunch = $("cardPunch");
  var cardAvatar = $("cardAvatar");
  var cardName = $("cardName");
  var cardMeta = $("cardMeta");
  var cardWhen = $("cardWhen");
  var cardLate = $("cardLate");
  var tapMode = cfg.scanMode !== "continuous";
  var session = false;   // a tap-mode scan is in progress
  var cardTimer = null;
  var startLabel = startBtn.querySelector("strong").textContent;
  var startHint = startBtn.querySelector("small").textContent;

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
  var HOLD_CARD_MS = 3500;
  var TAP_WARMUP_MS = 700;      // let exposure settle before the first frame
  var TAP_RETRY_MS = 900;
  var TAP_ATTEMPTS = 3;

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

  /** Full-screen confirmation card; returns false when the response has none. */
  function showCard(info) {
    if (!info) return false;
    window.clearTimeout(cardTimer);
    clearFlash();
    card.className = "kiosk-card" + (info.punch === "OUT" ? " out" : "");
    cardPunch.textContent = info.punch;
    if (info.avatar_url) {
      cardAvatar.innerHTML = '<img src="' + info.avatar_url + '" alt="' + (info.name || '') + '" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">';
    } else {
      cardAvatar.textContent = info.initials || "?";
    }
    cardName.textContent = info.name || "";
    cardMeta.textContent = [info.code, info.designation, info.department].filter(Boolean).join(" · ");
    cardWhen.textContent = info.time + " · " + info.date;
    cardLate.textContent = info.late || "";
    cardLate.hidden = !info.late;
    card.hidden = false;
    cardTimer = window.setTimeout(hideCard, HOLD_CARD_MS);
    return true;
  }

  function hideCard() {
    window.clearTimeout(cardTimer);
    card.hidden = true;
  }

  /* ---------------------------------------------------------------- capture */
  /** Send one frame. ``done(body)`` is called once with the response (or null). */
  function capture(done) {
    done = done || function () {};
    if (!stream || busy || !running || Date.now() < pausedUntil) return done(null);
    var w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return done(null);

    // ~480 px on the long edge is optimal for fast YuNet detection and tiny payload (<20KB).
    var scale = Math.min(1, 480 / Math.max(w, h));
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);

    busy = true;
    canvas.toBlob(function (blob) {
      if (!blob) { busy = false; return done(null); }
      var data = new FormData();
      data.append("_csrf", cfg.csrf);
      data.append("image", blob, "scan.jpg");

      fetch(cfg.markUrl, { method: "POST", body: data, credentials: "same-origin" })
        .then(function (response) {
          return response.json().then(function (body) { return { ok: response.ok, body: body }; });
        })
        .then(function (out) { return out.body; })
        .catch(function () { return { action: "connection" }; })
        .then(function (body) { busy = false; done(body); });
    }, "image/jpeg", 0.75);
  }

  function handle(body) {
    if (!body || typeof body !== "object") {
      show("bad", "Unexpected response", "", HOLD_ERROR_MS);
      return;
    }
    var name = cfg.showName && body.employee ? (body.employee.name || "") : "";

    switch (body.action) {
      case "check_in":
      case "check_out":
        if (showCard(body.card)) {
          beep("ok");
          pausedUntil = Date.now() + HOLD_CARD_MS;
          break;
        }
        if (body.action === "check_out") {
          show("ok", "OUT" + (name ? " · " + name : ""), (body.check_out || nowClock()).slice(0, 5), HOLD_SUCCESS_MS);
          beep("ok");
          pausedUntil = Date.now() + HOLD_SUCCESS_MS;
          break;
        }
        show("ok", "IN" + (name ? " · " + name : ""), (body.check_in || nowClock()).slice(0, 5), HOLD_SUCCESS_MS);
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
      case "spoof_detected":
        show("bad", "Real Face Required", "photos or screens rejected", HOLD_ERROR_MS + 1000);
        beep("bad");
        pausedUntil = Date.now() + HOLD_ERROR_MS + 1000;
        break;
      case "connection":
        show("bad", "Connection problem", "server did not answer", HOLD_ERROR_MS);
        setState("bad");
        pausedUntil = Date.now() + HOLD_ERROR_MS;
        return;
      case "service_down":
      case "service_error":
        show("bad", "Scanner offline", "use the HR console", HOLD_ERROR_MS);
        beep("bad");
        setState("bad");
        pausedUntil = Date.now() + 4000;
        break;
      default:
        show(body.kind === "error" ? "bad" : "warn", body.message || body.action || "Notice", "", HOLD_NOTICE_MS);
        pausedUntil = Date.now() + HOLD_NOTICE_MS;
    }

    setState(body.action === "service_down" || body.action === "service_error" ? "bad" : "ok");
  }

  /** Holding time of whatever ``handle`` just put on screen. */
  function holdFor(body) {
    if (!body) return HOLD_NOTICE_MS;
    if (body.card) return HOLD_CARD_MS;
    if (body.action === "check_in" || body.action === "check_out") return HOLD_SUCCESS_MS;
    return body.kind === "error" || body.action === "connection" ? HOLD_ERROR_MS : HOLD_NOTICE_MS;
  }

  /* --------------------------------------------------------------- tap mode */
  function tapScan() {
    if (session) return;
    session = true;
    hideCard();
    clearFlash();
    startBtn.hidden = true;
    hudTxt.textContent = "Verifying identity…";
    hud.hidden = false;
    startCamera(function (started) {
      if (!started) { hud.hidden = true; session = false; return; }
      window.setTimeout(function () { attempt(1); }, TAP_WARMUP_MS);
    });

    function attempt(n) {
      pausedUntil = 0;
      capture(function (body) {
        var retry = !body || body.action === "no_face";
        if (retry && n < TAP_ATTEMPTS) {
          hudTxt.textContent = "Look straight at the camera…";
          window.setTimeout(function () { attempt(n + 1); }, TAP_RETRY_MS);
          return;
        }
        hud.hidden = true;
        stopCamera(true);                 // camera off as soon as we have an answer
        if (body) handle(body);
        else show("warn", "No face found", "tap and look into the oval", HOLD_NOTICE_MS);
        window.setTimeout(function () {
          session = false;
          startBtn.hidden = false;
        }, holdFor(body));
      });
    }
  }

  /* ----------------------------------------------------------------- camera */
  function startCamera(onReady) {
    onReady = onReady || function () {};
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      show("bad", "No camera access", "use HTTPS", 4000);
      startBtn.hidden = false;
      startBtn.querySelector("strong").textContent = "Camera needs HTTPS";
      startBtn.querySelector("small").textContent = "open this page over https://";
      return onReady(false);
    }
    if (cfg.faceRequired && !cfg.faceHealthy) {
      show("bad", "Scanner disabled", "face service offline", 4000);
      startBtn.hidden = false;
      return onReady(false);
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
      startBtn.querySelector("strong").textContent = startLabel;
      startBtn.querySelector("small").textContent = startHint;
      liveDot.hidden = false;
      setState("ok");
      clearFlash();
      requestWakeLock();
      if (tapMode) return onReady(true);
      if (cfg.showName) show("", "Ready", "", 0);
      if (!loopTimer) {
        loopTimer = window.setInterval(function () {
          capture(function (body) { if (body) handle(body); });
        }, SCAN_INTERVAL_MS);
      }
      onReady(true);
    }).catch(function (error) {
      running = false;
      startBtn.hidden = false;
      startBtn.querySelector("strong").textContent = "Tap to retry";
      startBtn.querySelector("small").textContent =
        error && error.name === "NotAllowedError" ? "camera permission was denied" : "camera unavailable";
      show("bad", "Camera blocked", (error && error.name) || "", 3500);
      setState("bad");
      onReady(false);
    });
  }

  /** ``keepScreen``: leave the result on screen (tap mode turns the camera off under it). */
  function stopCamera(keepScreen) {
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    video.srcObject = null;
    running = false;
    liveDot.hidden = true;
    if (!keepScreen) {
      startBtn.hidden = false;
      clearFlash();
    }
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
    // Tap mode: every tap is one scan. Continuous: the tap starts the loop, later
    // taps scan immediately instead of waiting for the next tick.
    cam.addEventListener("click", function (event) {
      if (event.target.closest && event.target.closest("#card")) { hideCard(); return; }
      if (tapMode) { tapScan(); return; }
      if (!running) { startCamera(); return; }
      pausedUntil = 0;
      capture(function (body) { if (body) handle(body); });
    });

    flipBtn.addEventListener("click", function () {
      facing = facing === "user" ? "environment" : "user";
      cam.classList.toggle("mirror", facing === "user");
      flipBtn.classList.add("is-active");
      window.setTimeout(function () { flipBtn.classList.remove("is-active"); }, 350);
      if (running && !tapMode) startCamera();   // restart on the other camera
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

    // Continuous kiosks usually already granted the camera: start without a tap.
    // Tap mode keeps the camera off until someone taps.
    if (!tapMode && navigator.permissions && navigator.permissions.query) {
      navigator.permissions.query({ name: "camera" }).then(function (status) {
        if (status.state === "granted") startCamera();
      }).catch(function () { /* Safari: wait for the tap */ });
    }
  });
})();
