/* ==========================================================================
   Offline gate scans: keep a photo safely on this device, send it when online.

   No face templates live on the device, so a stolen tablet does not carry the
   company's biometrics. The trade-off is that a saved scan cannot show a name:
   the server identifies the person when it syncs.

   Photos are encrypted with a key the browser cannot export, and each one is
   deleted as soon as the server has finished with it.
   ========================================================================== */
(function () {
  "use strict";

  var DB_NAME = "hrms-kiosk-offline";
  var MAX_SCANS = 500;
  var OFFSET_KEY = "hrms-kiosk-clock-offset";
  var cfg = window.KIOSK_CONFIG || {};
  var available = !!(cfg.offlineEnabled && window.indexedDB && window.crypto && crypto.subtle);
  var csrf = cfg.csrf || "";
  var syncing = false;
  var dbPromise = null;

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      var request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = function () {
        request.result.createObjectStore("scans", { keyPath: "id" });
        request.result.createObjectStore("meta");
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
    return dbPromise;
  }

  function run(store, mode, action) {
    return open().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(store, mode);
        var request = action(tx.objectStore(store));
        tx.oncomplete = function () { resolve(request && request.result); };
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error); };
      });
    });
  }

  /** One AES key for this device. ``extractable: false`` keeps it out of any script's reach. */
  function deviceKey() {
    return run("meta", "readonly", function (store) { return store.get("key"); }).then(function (key) {
      if (key) return key;
      return crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"])
        .then(function (fresh) {
          return run("meta", "readwrite", function (store) { return store.put(fresh, "key"); })
            .then(function () { return fresh; });
        });
    });
  }

  function count() {
    if (!available) return Promise.resolve(0);
    return run("scans", "readonly", function (store) { return store.count(); }).catch(function () { return 0; });
  }

  function newId() {
    var bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.prototype.map.call(bytes, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }

  /** The device's clock, corrected by what the server last told us. */
  function correctedNow() {
    var offset = 0;
    try { offset = parseInt(localStorage.getItem(OFFSET_KEY) || "0", 10) || 0; } catch (error) { /* private mode */ }
    return Date.now() + offset;
  }

  function save(blob) {
    return count().then(function (total) {
      if (total >= MAX_SCANS) throw new Error("full");
      return Promise.all([deviceKey(), blob.arrayBuffer()]);
    }).then(function (parts) {
      var iv = crypto.getRandomValues(new Uint8Array(12));
      return crypto.subtle.encrypt({ name: "AES-GCM", iv: iv }, parts[0], parts[1]).then(function (cipher) {
        var scan = { id: newId(), capturedAt: correctedNow(), iv: iv, cipher: cipher };
        return run("scans", "readwrite", function (store) { return store.put(scan); });
      });
    });
  }

  function decrypt(scan) {
    return deviceKey().then(function (key) {
      return crypto.subtle.decrypt({ name: "AES-GCM", iv: scan.iv }, key, scan.cipher);
    }).then(function (plain) { return new Blob([plain], { type: "image/jpeg" }); });
  }

  function oldest() {
    return open().then(function (db) {
      return new Promise(function (resolve, reject) {
        var request = db.transaction("scans").objectStore("scans").getAll();
        request.onsuccess = function () {
          resolve(request.result.sort(function (a, b) { return a.capturedAt - b.capturedAt; }));
        };
        request.onerror = function () { reject(request.error); };
      });
    });
  }

  function send(scan) {
    return decrypt(scan).then(function (image) {
      var data = new FormData();
      data.append("image", image, "scan.jpg");
      data.append("scan_id", scan.id);
      data.append("captured_at", String(Math.round(scan.capturedAt)));
      return fetch(cfg.offlineSyncUrl, {
        method: "POST", body: data, credentials: "same-origin", redirect: "manual",
        headers: { "X-CSRF-Token": csrf }
      });
    }).then(function (response) {
      if (response.type === "opaqueredirect" || response.status === 401 || response.status === 403) return "auth";
      if (!response.ok) return "later";
      return response.json().then(function (body) { return body.status === "done" ? "done" : "later"; });
    });
  }

  /**
   * Send saved scans, oldest first, because their order decides check-in and check-out.
   * Stops at the first problem and tries again later, so nothing is lost or reordered.
   */
  function sync() {
    if (!available || syncing) return Promise.resolve({ state: "idle" });
    syncing = true;
    return oldest().then(function (scans) {
      var index = 0;
      function next() {
        if (index >= scans.length) return { state: "idle" };
        var scan = scans[index];
        return send(scan).then(function (result) {
          if (result !== "done") return { state: result };
          index += 1;
          return run("scans", "readwrite", function (store) { return store.delete(scan.id); }).then(next);
        });
      }
      return next();
    }).catch(function () {
      return { state: "later" };
    }).then(function (outcome) {
      syncing = false;
      return outcome;
    });
  }

  function serverStatus(status) {
    if (status.csrf) csrf = status.csrf;
    if (status.server_epoch_ms) {
      try { localStorage.setItem(OFFSET_KEY, String(status.server_epoch_ms - Date.now())); } catch (error) { /* private mode */ }
    }
  }

  window.KioskOffline = { available: available, save: save, count: count, sync: sync, serverStatus: serverStatus };
})();
