/* ==========================================================================
   Shared UI behaviour for the Fakhri HRMS (HR console).
   - slide-out navigation on phones
   - auto-dismissing flash messages
   - data-confirm guard on destructive forms
   - reusable webcam capture used by the face-enrolment card
   ========================================================================== */
(function () {
  "use strict";

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }

  /* ------------------------------------------------------------- navigation */
  function initDrawer() {
    var toggle = $("#menuToggle");
    var backdrop = $("#drawerBackdrop");
    if (!toggle) return;
    function close() { document.body.classList.remove("drawer-open"); }
    toggle.addEventListener("click", function () {
      document.body.classList.toggle("drawer-open");
    });
    if (backdrop) backdrop.addEventListener("click", close);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") close();
    });
    window.addEventListener("hashchange", close);
  }

  /* ------------------------------------------------------------ flash/errors */
  function initAlerts() {
    $$(".alert-ok").forEach(function (node) {
      window.setTimeout(function () {
        node.style.transition = "opacity .4s ease";
        node.style.opacity = "0";
        window.setTimeout(function () { node.remove(); }, 450);
      }, 7000);
    });
  }

  function initConfirm() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || !form.dataset || !form.dataset.confirm) return;
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    }, true);
  }

  /* ------------------------------------------------------- webcam enrolment */
  function initFaceEnrolment(options) {
    var video = $("#enrolVideo");
    var canvas = $("#enrolCanvas");
    var placeholder = $("#enrolPlaceholder");
    var startBtn = $("#enrolStart");
    var captureBtn = $("#enrolCapture");
    var stopBtn = $("#enrolStop");
    var status = $("#enrolStatus");
    if (!video || !startBtn || !captureBtn) return;

    var stream = null;
    var facing = "user";

    function setStatus(message, kind) {
      if (!status) return;
      status.textContent = message || "";
      status.style.color = kind === "error" ? "#c91d1d" : kind === "ok" ? "#0f7b52" : "";
    }

    function stop() {
      if (stream) {
        stream.getTracks().forEach(function (track) { track.stop(); });
        stream = null;
      }
      video.classList.add("hidden");
      if (placeholder) placeholder.classList.remove("hidden");
      captureBtn.disabled = true;
      if (stopBtn) stopBtn.disabled = true;
      startBtn.disabled = false;
      setStatus("Camera stopped.");
    }

    function start() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setStatus("This browser cannot open the camera. Use the file upload instead.", "error");
        return;
      }
      setStatus("Starting camera…");
      navigator.mediaDevices.getUserMedia({
        video: { facingMode: facing, width: { ideal: 1280 }, height: { ideal: 960 } },
        audio: false
      }).then(function (mediaStream) {
        stream = mediaStream;
        video.srcObject = mediaStream;
        video.classList.remove("hidden");
        if (placeholder) placeholder.classList.add("hidden");
        captureBtn.disabled = false;
        if (stopBtn) stopBtn.disabled = false;
        startBtn.disabled = true;
        setStatus("Look straight at the camera, then capture.");
      }).catch(function (error) {
        setStatus("Camera blocked (" + error.name + "). Allow camera access, or upload a photo instead.", "error");
      });
    }

    function capture() {
      if (!stream) return;
      var width = video.videoWidth || 640;
      var height = video.videoHeight || 480;
      canvas.width = width;
      canvas.height = height;
      canvas.getContext("2d").drawImage(video, 0, 0, width, height);
      setStatus("Uploading to the face service…");

      canvas.toBlob(function (blob) {
        if (!blob) { setStatus("Could not read the frame — try again.", "error"); return; }
        var data = new FormData();
        data.append("_csrf", options.csrf);
        data.append("image", blob, "enrolment.jpg");

        fetch(options.endpoint, {
          method: "POST",
          body: data,
          credentials: "same-origin",
          redirect: "follow"
        }).then(function (response) {
          // The endpoint answers with a 303 back to the employee page.
          window.location.href = response.url || options.endpoint;
        }).catch(function () {
          setStatus("Upload failed — check the connection and try again.", "error");
        });
      }, "image/jpeg", 0.9);
    }

    startBtn.addEventListener("click", start);
    captureBtn.addEventListener("click", capture);
    if (stopBtn) stopBtn.addEventListener("click", stop);
    window.addEventListener("beforeunload", stop);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initDrawer();
    initAlerts();
    initConfirm();
  });

  window.FakhriHRMS = {
    initFaceEnrolment: initFaceEnrolment
  };
})();
