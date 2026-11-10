/* ==========================================================================
   Face Recognition Sign-In for the Login Page.
   Captures a camera frame, checks liveness & 1:N match with FaceAPI,
   and logs the employee into their portal (/me).
   ========================================================================== */
(function () {
  "use strict";

  var faceOption = document.getElementById("faceLoginOption");
  var openBtn = document.getElementById("openFaceScanBtn");
  var closeBtn = document.getElementById("closeFaceScanBtn");
  var scanBtn = document.getElementById("triggerFaceScanBtn");
  var passForm = document.getElementById("passwordLoginForm");
  var instantOpts = document.getElementById("instantAuthOptions");
  var faceView = document.getElementById("faceLoginView");
  var video = document.getElementById("faceVideo");
  var oval = document.getElementById("faceOvalGuide");
  var status = document.getElementById("faceLoginStatus");

  if (!openBtn || !video || !status) return;

  var stream = null;
  var busy = false;
  var warmupTimer = null;
  var canvas = document.createElement("canvas");

  function setStatus(text, kind) {
    if (!status) return;
    status.textContent = text || "";
    status.className = "face-status-text" + (kind ? " " + kind : "");
  }

  function setOval(kind) {
    if (!oval) return;
    oval.className = "face-oval-guide" + (kind ? " " + kind : "");
  }

  function stopCamera() {
    if (warmupTimer) {
      clearTimeout(warmupTimer);
      warmupTimer = null;
    }
    if (stream) {
      stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
    }
    video.srcObject = null;
    busy = false;
  }

  function closeFaceView() {
    stopCamera();
    if (faceView) {
      faceView.hidden = true;
      faceView.style.display = "none";
    }
    if (passForm) {
      passForm.hidden = false;
      passForm.style.display = "";
    }
    if (instantOpts) {
      instantOpts.hidden = false;
      instantOpts.style.display = "";
    }
    setOval("");
    setStatus("Position your face in the oval");
    if (scanBtn) scanBtn.disabled = false;
  }

  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus("This browser cannot open the camera. Please use your password.", "bad");
      return;
    }

    setStatus("Starting camera…");
    setOval("scanning");
    if (scanBtn) scanBtn.disabled = true;

    navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 960 }, height: { ideal: 720 } },
      audio: false
    }).then(function (mediaStream) {
      stream = mediaStream;
      video.srcObject = mediaStream;
      setStatus("Looking for face — hold steady…");

      // Once the video starts playing and exposure settles, trigger automatic scan
      video.onloadedmetadata = function () {
        video.play().catch(function () {});
        warmupTimer = setTimeout(function () {
          scanNow(true);
        }, 800);
      };
    }).catch(function (error) {
      setOval("bad");
      if (error && (error.name === "NotAllowedError" || error.name === "PermissionDeniedError")) {
        setStatus("Camera permission denied. Please allow camera access in your browser or sign in with password.", "bad");
      } else {
        setStatus("Cannot open camera (" + (error ? error.name : "error") + "). Please use your password.", "bad");
      }
      if (scanBtn) scanBtn.disabled = false;
    });
  }

  function scanNow(isAuto) {
    if (!stream || busy) return;
    busy = true;
    if (scanBtn) scanBtn.disabled = true;

    setOval("scanning");
    setStatus("Verifying face…");

    var width = video.videoWidth || 640;
    var height = video.videoHeight || 480;
    canvas.width = width;
    canvas.height = height;

    var ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, width, height);

    canvas.toBlob(function (blob) {
      if (!blob) {
        busy = false;
        if (scanBtn) scanBtn.disabled = false;
        setOval("warn");
        setStatus("Could not capture frame. Tap 'Scan Face' to try again.", "warn");
        return;
      }

      var formData = new FormData();
      formData.append("image", blob, "face_login.jpg");

      var nextInput = document.querySelector('input[name="next"]');
      if (nextInput && nextInput.value) {
        formData.append("next", nextInput.value);
      }

      fetch("/api/auth/face-login", {
        method: "POST",
        body: formData,
        credentials: "same-origin"
      }).then(function (response) {
        return response.json().then(function (data) {
          return { ok: response.ok, status: response.status, data: data };
        });
      }).then(function (res) {
        busy = false;
        if (scanBtn) scanBtn.disabled = false;

        if (res.ok && res.data && res.data.ok) {
          setOval("ok");
          setStatus("✓ Welcome, " + (res.data.name || "Employee") + "! Logging you in…", "ok");
          stopCamera();
          setTimeout(function () {
            window.location.assign(res.data.redirect || "/me");
          }, 600);
        } else {
          var detail = (res.data && res.data.detail) || "Face not recognised. Please try again.";
          setOval("warn");
          setStatus(detail, "warn");
        }
      }).catch(function () {
        busy = false;
        if (scanBtn) scanBtn.disabled = false;
        setOval("bad");
        setStatus("Network error. Please try again or use your password.", "bad");
      });
    }, "image/jpeg", 0.92);
  }

  // Check if camera & FaceAPI are available
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    fetch("/api/auth/face-available").then(function (r) {
      return r.json();
    }).then(function (data) {
      if (data && data.available && faceOption) {
        faceOption.hidden = false;
      }
    }).catch(function () {});
  }

  // Event listeners
  openBtn.addEventListener("click", function () {
    if (passForm) {
      passForm.hidden = true;
      passForm.style.display = "none";
    }
    if (instantOpts) {
      instantOpts.hidden = true;
      instantOpts.style.display = "none";
    }
    if (faceView) {
      faceView.hidden = false;
      faceView.style.display = "flex";
    }
    startCamera();
  });

  if (closeBtn) {
    closeBtn.addEventListener("click", closeFaceView);
  }

  if (scanBtn) {
    scanBtn.addEventListener("click", function () {
      scanNow(false);
    });
  }

  window.addEventListener("beforeunload", stopCamera);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden && stream) {
      stopCamera();
    }
  });
})();

