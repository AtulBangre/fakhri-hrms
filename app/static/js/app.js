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
    $$("[data-open-drawer]").forEach(function (link) {
      link.addEventListener("click", function (event) {
        event.preventDefault();
        document.body.classList.add("drawer-open");
      });
    });
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

  /* --------------------------------------------- expandable table detail rows */
  function initRowToggles() {
    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-toggle-row]");
      if (!trigger) return;
      event.preventDefault();
      var row = document.getElementById(trigger.getAttribute("data-toggle-row"));
      if (!row) return;
      var open = row.classList.toggle("hidden") === false;
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        var first = row.querySelector("input, select, textarea");
        if (first) first.focus();
      }
    });
  }

  /* -------------------------------------------- select-all + bulk action bar */
  function initBulk() {
    $$("[data-bulk]").forEach(function (scope) {
      var all = $("[data-bulk-all]", scope);
      var bar = $(".bulk-bar", scope);
      var counter = $("[data-bulk-count]", scope);
      function boxes() { return $$("input[data-bulk-item]", scope); }
      function sync() {
        var picked = boxes().filter(function (box) { return box.checked; });
        boxes().forEach(function (box) {
          var tr = box.closest("tr");
          if (tr) tr.classList.toggle("is-selected", box.checked);
        });
        if (bar) bar.classList.toggle("is-visible", picked.length > 0);
        if (counter) counter.textContent = picked.length;
        if (all) {
          all.checked = picked.length > 0 && picked.length === boxes().length;
          all.indeterminate = picked.length > 0 && picked.length < boxes().length;
        }
      }
      if (all) all.addEventListener("change", function () {
        boxes().forEach(function (box) { box.checked = all.checked; });
        sync();
      });
      scope.addEventListener("change", function (event) {
        if (event.target.matches("input[data-bulk-item]")) sync();
      });
      sync();
    });
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

  /* -------------------------------------- dynamic attendance form synchronizer */
  function initAttendanceForms() {
    function syncForm(form, preserveUserInputs) {
      var statusSelect = $("select[name='status']", form);
      if (!statusSelect) return;
      var status = statusSelect.value;
      var isToday = form.getAttribute("data-is-today") === "true";
      var shiftStart = form.getAttribute("data-shift-start") || "09:30";
      var shiftEnd = form.getAttribute("data-shift-end") || "18:30";
      var originalIn = form.getAttribute("data-original-in") || "";
      var originalOut = form.getAttribute("data-original-out") || "";

      var inInput = $("input[name='check_in']", form);
      var outInput = $("input[name='check_out']", form);
      var leaveSelect = $("select[name='leave_type']", form);
      var inField = inInput ? inInput.closest(".field") : null;
      var outField = outInput ? outInput.closest(".field") : null;
      var leaveField = leaveSelect ? leaveSelect.closest(".field") : null;
      var leaveLabel = leaveField ? $("label", leaveField) : null;

      if (status === "present") {
        if (inInput) {
          inInput.disabled = false;
          if (!preserveUserInputs && !inInput.value) {
            inInput.value = originalIn || shiftStart;
          }
          if (inField) inField.classList.remove("field-disabled");
        }
        if (outInput) {
          outInput.disabled = false;
          if (!preserveUserInputs && !outInput.value) {
            if (originalOut) {
              outInput.value = originalOut;
            } else if (!isToday) {
              outInput.value = shiftEnd;
            } else {
              outInput.value = "";
            }
          }
          if (outField) outField.classList.remove("field-disabled");
        }
        if (leaveSelect) {
          leaveSelect.value = "";
          leaveSelect.disabled = true;
          if (leaveField) leaveField.classList.add("field-disabled");
          if (leaveLabel) leaveLabel.textContent = "Leave type (N/A)";
        }
      } else if (status === "half_day") {
        if (inInput) {
          inInput.disabled = false;
          if (!preserveUserInputs && !inInput.value) {
            inInput.value = originalIn || shiftStart;
          }
          if (inField) inField.classList.remove("field-disabled");
        }
        if (outInput) {
          outInput.disabled = false;
          if (!preserveUserInputs && !outInput.value && originalOut) {
            outInput.value = originalOut;
          }
          if (outField) outField.classList.remove("field-disabled");
        }
        if (leaveSelect) {
          leaveSelect.disabled = false;
          if (leaveField) leaveField.classList.remove("field-disabled");
          if (leaveLabel) leaveLabel.textContent = "Leave deduction (half day)";
        }
      } else if (status === "absent") {
        if (inInput) {
          inInput.value = "";
          inInput.disabled = true;
          if (inField) inField.classList.add("field-disabled");
        }
        if (outInput) {
          outInput.value = "";
          outInput.disabled = true;
          if (outField) outField.classList.add("field-disabled");
        }
        if (leaveSelect) {
          leaveSelect.value = "";
          leaveSelect.disabled = true;
          if (leaveField) leaveField.classList.add("field-disabled");
          if (leaveLabel) leaveLabel.textContent = "Leave type (N/A)";
        }
      } else if (status === "leave") {
        if (inInput) {
          inInput.value = "";
          inInput.disabled = true;
          if (inField) inField.classList.add("field-disabled");
        }
        if (outInput) {
          outInput.value = "";
          outInput.disabled = true;
          if (outField) outField.classList.add("field-disabled");
        }
        if (leaveSelect) {
          leaveSelect.disabled = false;
          if (!leaveSelect.value) {
            for (var i = 0; i < leaveSelect.options.length; i++) {
              if (leaveSelect.options[i].value) {
                leaveSelect.selectedIndex = i;
                break;
              }
            }
          }
          if (leaveField) leaveField.classList.remove("field-disabled");
          if (leaveLabel) leaveLabel.textContent = "Leave type *";
        }
      } else if (status === "week_off" || status === "holiday") {
        if (inInput) {
          inInput.disabled = false;
          if (inField) inField.classList.remove("field-disabled");
        }
        if (outInput) {
          outInput.disabled = false;
          if (outField) outField.classList.remove("field-disabled");
        }
        if (leaveSelect) {
          leaveSelect.value = "";
          leaveSelect.disabled = true;
          if (leaveField) leaveField.classList.add("field-disabled");
          if (leaveLabel) leaveLabel.textContent = "Leave type (N/A)";
        }
      }
    }

    document.addEventListener("change", function (event) {
      var target = event.target;
      if (target && target.name === "status") {
        var form = target.closest("form.attendance-edit-form, form[action*='/attendance/mark']");
        if (form) syncForm(form, false);
      }
    });

    $$("form.attendance-edit-form").forEach(function (form) {
      syncForm(form, true);
    });

    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-toggle-row]");
      if (!trigger) return;
      var targetId = trigger.getAttribute("data-toggle-row");
      var row = document.getElementById(targetId);
      if (row) {
        var form = $("form.attendance-edit-form, form[action*='/attendance/mark']", row);
        if (form) syncForm(form, true);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initDrawer();
    initAlerts();
    initConfirm();
    initRowToggles();
    initBulk();
    initAttendanceForms();
  });

  window.FakhriHRMS = {
    initFaceEnrolment: initFaceEnrolment
  };
})();
