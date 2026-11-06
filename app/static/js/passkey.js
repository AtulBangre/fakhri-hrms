/* ==========================================================================
   Fingerprint or Face ID sign-in (WebAuthn), for the login page and the
   account page. The device checks the finger or face. It never leaves the device.
   ========================================================================== */
(function () {
  "use strict";

  function toBytes(value) {
    var text = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4));
    return Uint8Array.from(text, function (char) { return char.charCodeAt(0); }).buffer;
  }
  function toText(buffer) {
    return btoa(String.fromCharCode.apply(null, new Uint8Array(buffer)))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function post(url, body, csrf) {
    var headers = { "Content-Type": "application/json" };
    if (csrf) headers["X-CSRF-Token"] = csrf;
    return fetch(url, { method: "POST", credentials: "same-origin", headers: headers, body: JSON.stringify(body || {}) })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) throw new Error(data.detail || "Please try again.");
          return data;
        });
      });
  }

  function friendly(error) {
    if (error && error.name === "NotAllowedError") return "Cancelled, or this device has no saved sign-in here.";
    if (error && error.name === "InvalidStateError") return "This device is already added.";
    return (error && error.message) || "Please try again.";
  }

  /* ------------------------------------------------------------ login page */
  var loginBtn = document.getElementById("passkeyLogin");
  if (loginBtn && window.PublicKeyCredential) {
    var note = document.getElementById("passkeyNote");
    fetch("/api/passkey/available").then(function (r) { return r.json(); }).then(function (data) {
      if (data.available) loginBtn.closest("[data-passkey-area]").hidden = false;
    }).catch(function () {});

    loginBtn.addEventListener("click", function () {
      note.textContent = "";
      loginBtn.disabled = true;
      post("/api/passkey/login/options").then(function (options) {
        options.challenge = toBytes(options.challenge);
        (options.allowCredentials || []).forEach(function (item) { item.id = toBytes(item.id); });
        return navigator.credentials.get({ publicKey: options });
      }).then(function (credential) {
        var next = document.querySelector('input[name="next"]');
        return post("/api/passkey/login/verify", {
          next: next ? next.value : "",
          credential: {
            id: credential.id, rawId: toText(credential.rawId), type: credential.type,
            clientExtensionResults: credential.getClientExtensionResults(),
            response: {
              clientDataJSON: toText(credential.response.clientDataJSON),
              authenticatorData: toText(credential.response.authenticatorData),
              signature: toText(credential.response.signature),
              userHandle: credential.response.userHandle ? toText(credential.response.userHandle) : null
            }
          }
        });
      }).then(function (data) {
        window.location.assign(data.redirect);
      }).catch(function (error) {
        note.textContent = friendly(error);
        loginBtn.disabled = false;
      });
    });
  }

  /* ---------------------------------------------------------- account page */
  var addBtn = document.getElementById("passkeyAdd");
  if (addBtn) {
    var status = document.getElementById("passkeyStatus");
    var meta = document.querySelector('meta[name="csrf-token"]');
    var csrf = meta ? meta.content : "";
    if (!window.PublicKeyCredential) {
      addBtn.disabled = true;
      status.textContent = "This browser cannot use fingerprint or face sign-in.";
    }
    addBtn.addEventListener("click", function () {
      status.textContent = "";
      addBtn.disabled = true;
      post("/api/passkey/register/options", {}, csrf).then(function (options) {
        options.challenge = toBytes(options.challenge);
        options.user.id = toBytes(options.user.id);
        (options.excludeCredentials || []).forEach(function (item) { item.id = toBytes(item.id); });
        return navigator.credentials.create({ publicKey: options });
      }).then(function (credential) {
        return post("/api/passkey/register/verify", {
          name: document.getElementById("passkeyName").value,
          credential: {
            id: credential.id, rawId: toText(credential.rawId), type: credential.type,
            clientExtensionResults: credential.getClientExtensionResults(),
            response: {
              clientDataJSON: toText(credential.response.clientDataJSON),
              attestationObject: toText(credential.response.attestationObject),
              transports: credential.response.getTransports ? credential.response.getTransports() : []
            }
          }
        }, csrf);
      }).then(function () {
        window.location.reload();
      }).catch(function (error) {
        status.textContent = friendly(error);
        addBtn.disabled = false;
      });
    });
  }
})();
