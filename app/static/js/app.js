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
    function setOpen(open) {
      document.body.classList.toggle("drawer-open", open);
      var toggle = $("#menuToggle");
      if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    function close() { setOpen(false); }

    document.addEventListener("click", function (event) {
      if (event.target.closest("#drawerBackdrop") || event.target.closest("[data-close-drawer]")) {
        event.preventDefault();
        close();
        return;
      }
      if (event.target.closest("#menuToggle")) {
        event.preventDefault();
        setOpen(!document.body.classList.contains("drawer-open"));
        return;
      }
      if (event.target.closest("[data-open-drawer]")) {
        event.preventDefault();
        setOpen(true);
        return;
      }
      if (event.target.closest("aside.drawer nav.nav a, aside.drawer a")) {
        close();
      }
    });

    document.addEventListener("touchstart", function (event) {
      if (event.target.closest("#drawerBackdrop") || event.target.closest("[data-close-drawer]")) {
        close();
      }
    }, { passive: true });

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

  /* A slow network must not turn one tap into two submissions. */
  function initSubmitFeedback() {
    function release(form) {
      delete form.dataset.busy;
      $$(".is-busy", form).forEach(function (button) {
        button.classList.remove("is-busy");
        button.disabled = false;
      });
    }
    // Runs after every form's own handlers, so forms sent by script are skipped.
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (event.defaultPrevented || (form.getAttribute("method") || "").toLowerCase() !== "post") return;
      if (form.dataset.busy) { event.preventDefault(); return; }
      form.dataset.busy = "1";
      var button = event.submitter || $("button[type=submit]", form);
      // Disabled on the next tick, or the browser would drop the button's own value.
      window.setTimeout(function () {
        if (button) { button.disabled = true; button.classList.add("is-busy"); }
      }, 0);
      // A response that downloads a file never leaves the page.
      window.setTimeout(function () { release(form); }, 15000);
    });
    // Coming back with the browser's Back button restores the page as it was left.
    window.addEventListener("pageshow", function () { $$("form[data-busy]").forEach(release); });
  }

  function initOfflineNotice() {
    var banner = document.createElement("div");
    banner.className = "offline-banner hidden";
    banner.setAttribute("role", "status");
    banner.textContent = "You are offline. Changes cannot be saved until you reconnect.";
    document.body.appendChild(banner);
    function update() { banner.classList.toggle("hidden", navigator.onLine !== false); }
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    update();
  }

  /* Portal password: fill the box with a suggestion, and copy the saved one. */
  function initPasswordHelpers() {
    document.addEventListener("click", function (event) {
      var fill = event.target.closest("[data-fill-password]");
      if (fill) {
        var input = document.getElementById(fill.getAttribute("data-fill-password"));
        if (input) input.value = fill.getAttribute("data-value") || "";
        return;
      }
      var copy = event.target.closest("[data-copy-from]");
      if (!copy) return;
      var source = document.getElementById(copy.getAttribute("data-copy-from"));
      if (!source) return;
      function done(label) { copy.textContent = label; }
      // The clipboard API needs HTTPS or localhost; elsewhere ask for a manual copy.
      if (navigator.clipboard) {
        navigator.clipboard.writeText(source.textContent).then(function () { done("Copied"); }, function () { done("Select and copy it"); });
      } else {
        done("Select and copy it");
      }
    });
  }

  /* Ask HR steps aside while reading or typing, so it never covers a button. */
  function initFloatingButton() {
    var fab = $("#openAskHrBtn");
    if (!fab) return;
    var last = window.scrollY;
    window.addEventListener("scroll", function () {
      var now = window.scrollY;
      if (Math.abs(now - last) < 8) return;
      fab.classList.toggle("is-away", now > last && now > 60);
      last = now;
    }, { passive: true });
    document.addEventListener("focusin", function (event) {
      if (event.target.matches("main input, main select, main textarea")) fab.classList.add("is-away");
    });
    document.addEventListener("focusout", function (event) {
      if (event.target.matches("main input, main select, main textarea")) fab.classList.remove("is-away");
    });
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

  /* --------------------------------------------------- photo upload resize */
  var UPLOAD_MAX_SIDE = 1280;

  /** Phone photos are often several MB. If anything fails, the original is sent. */
  function initImageDownscale() {
    document.querySelectorAll("form[data-downscale-image]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        var input = form.querySelector("input[type=file]");
        var file = input && input.files[0];
        if (form.dataset.resized || !file || !window.createImageBitmap || !window.DataTransfer) return;
        event.preventDefault();
        var send = function () { form.dataset.resized = "1"; form.requestSubmit(); };
        createImageBitmap(file).then(function (bitmap) {
          var scale = UPLOAD_MAX_SIDE / Math.max(bitmap.width, bitmap.height);
          if (scale >= 1) return send();
          var canvas = document.createElement("canvas");
          canvas.width = Math.round(bitmap.width * scale);
          canvas.height = Math.round(bitmap.height * scale);
          canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
          canvas.toBlob(function (blob) {
            if (blob) {
              var transfer = new DataTransfer();
              transfer.items.add(new File([blob], "face.jpg", { type: "image/jpeg" }));
              input.files = transfer.files;
            }
            send();
          }, "image/jpeg", 0.92);
        }).catch(send);
      });
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
      var shiftStart = form.getAttribute("data-shift-start") || "";
      var shiftEnd = form.getAttribute("data-shift-end") || "";
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
      // Manual entry: default times follow the shift of the employee picked.
      if (target && target.name === "employee_id" && target.tagName === "SELECT") {
        var entryForm = target.closest("form.attendance-edit-form");
        if (entryForm) {
          applyEmployeeShift(entryForm, target);
          var inField = $("input[name='check_in']", entryForm);
          var outField = $("input[name='check_out']", entryForm);
          if (inField) inField.value = "";
          if (outField) outField.value = "";
          syncForm(entryForm, false);
        }
      }
    });

    function applyEmployeeShift(form, select) {
      var option = select.options[select.selectedIndex];
      if (!option || !option.getAttribute("data-shift-start")) return;
      form.setAttribute("data-shift-start", option.getAttribute("data-shift-start"));
      form.setAttribute("data-shift-end", option.getAttribute("data-shift-end") || "");
    }

    $$("form.attendance-edit-form").forEach(function (form) {
      var employeeSelect = $("select[name='employee_id']", form);
      if (employeeSelect) applyEmployeeShift(form, employeeSelect);
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

  /* -------------------------------------------------- Zero Data Loss Autosave Engine */
  function initAutoDraftSaver() {
    var TARGET_FORMS = [
      '#leave-apply-form',
      '#loan-apply-dialog form',
      '#loan-modal form',
      '#request-correction form',
      'form.attendance-edit-form',
      'form[data-autosave]'
    ];
    var forms = $$(TARGET_FORMS.join(', '));
    if (!forms.length) return;

    function getStorageKey(form) {
      var id = form.id || form.getAttribute('action') || 'form';
      return 'hrms_draft_' + window.location.pathname.replace(/\/$/, '') + '_' + id;
    }

    function saveDraft(form) {
      var key = getStorageKey(form);
      var data = {};
      var elements = form.elements;
      var hasMeaningfulData = false;
      for (var i = 0; i < elements.length; i++) {
        var el = elements[i];
        if (!el.name || el.type === 'hidden' || el.type === 'password' || el.type === 'file' || el.name === '_csrf') continue;
        if (el.type === 'checkbox' || el.type === 'radio') {
          if (el.checked) {
            data[el.name] = el.value;
            hasMeaningfulData = true;
          }
        } else if (el.value && el.value.trim().length > 0) {
          data[el.name] = el.value;
          hasMeaningfulData = true;
        }
      }
      if (hasMeaningfulData) {
        try {
          localStorage.setItem(key, JSON.stringify({ ts: Date.now(), fields: data }));
        } catch (e) {}
      } else {
        try { localStorage.removeItem(key); } catch (e) {}
      }
    }

    function showToast(msg) {
      var toast = document.createElement('div');
      toast.className = 'ios-draft-toast';
      toast.innerHTML = '<span class="pulse-dot"></span><span>' + msg + '</span><button type="button" aria-label="Dismiss">✕</button>';
      document.body.appendChild(toast);
      window.setTimeout(function() { toast.classList.add('visible'); }, 50);
      var timer = window.setTimeout(function() {
        toast.classList.remove('visible');
        window.setTimeout(function() { toast.remove(); }, 300);
      }, 4000);
      toast.querySelector('button').addEventListener('click', function() {
        clearTimeout(timer);
        toast.classList.remove('visible');
        window.setTimeout(function() { toast.remove(); }, 200);
      });
    }

    function restoreDraft(form) {
      var key = getStorageKey(form);
      var raw;
      try { raw = localStorage.getItem(key); } catch (e) { return; }
      if (!raw) return;
      try {
        var payload = JSON.parse(raw);
        if (!payload || !payload.fields) return;
        // Expire drafts older than 7 days
        if (Date.now() - (payload.ts || 0) > 7 * 86400 * 1000) {
          localStorage.removeItem(key);
          return;
        }
        var fields = payload.fields;
        var restoredCount = 0;
        for (var name in fields) {
          if (!fields.hasOwnProperty(name)) continue;
          var val = fields[name];
          var el = form.elements[name];
          if (!el) continue;
          if (el.type === 'checkbox') {
            if (!el.checked && (val === '1' || val === el.value || val === true)) {
              el.checked = true;
              restoredCount++;
            }
          } else if (el.type === 'radio') {
            var radios = form.querySelectorAll('input[type=radio][name="' + name + '"]');
            radios.forEach(function(r) {
              if (r.value === val) { r.checked = true; restoredCount++; }
            });
          } else {
            if (!el.value || el.value === el.defaultValue) {
              el.value = val;
              restoredCount++;
            }
          }
        }
        if (restoredCount > 0) {
          showToast('Draft restored from your last visit');
        }
      } catch (e) {}
    }

    forms.forEach(function(form) {
      restoreDraft(form);
      var debounceTimer = null;
      function onInput() {
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function() { saveDraft(form); }, 350);
      }
      form.addEventListener('input', onInput);
      form.addEventListener('change', onInput);
      form.addEventListener('submit', function() {
        try { localStorage.removeItem(getStorageKey(form)); } catch (e) {}
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initDrawer();
    initAlerts();
    initConfirm();
    initRowToggles();
    initBulk();
    initAttendanceForms();
    initImageDownscale();
    initSubmitFeedback();
    initAutoDraftSaver();
    initOfflineNotice();
    initFloatingButton();
    initPasswordHelpers();
  });

  window.FakhriHRMS = {
    initFaceEnrolment: initFaceEnrolment
  };
})();
