/* ==========================================================================
   The bell: unread count, the notification list, and system notifications.

   With push turned on, the server reaches this device even when the app is
   closed. While a page is open, polling keeps the bell current too. Both use the
   same notification tag, so a message never appears twice.
   ========================================================================== */
(function () {
  "use strict";

  var bell = document.getElementById("bellBtn");
  if (!bell) return;
  var badge = document.getElementById("bellBadge");
  var panel = document.getElementById("inboxPanel");
  var list = document.getElementById("inboxList");
  var pushArea = document.getElementById("inboxPush");
  var toast = document.getElementById("inboxToast");
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var POLL_MS = 45000;
  var LAST_KEY = "hrms-inbox-last-notified";
  var state = { items: [], unread: 0, pushKey: null };

  function post(url, body, keepalive) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      keepalive: !!keepalive,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfMeta ? csrfMeta.content : "" },
      body: JSON.stringify(body || {})
    });
  }

  function readLast() {
    try { return parseInt(localStorage.getItem(LAST_KEY) || "", 10); } catch (error) { return NaN; }
  }
  function writeLast(value) {
    try { localStorage.setItem(LAST_KEY, String(value)); } catch (error) { /* private mode */ }
  }
  function sameSite(url) { return /^\/(?!\/)/.test(url || "") ? url : "/"; }

  function timeAgo(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return "";
    var minutes = Math.round((Date.now() - then) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return minutes + " min ago";
    if (minutes < 1440) return Math.round(minutes / 60) + " hr ago";
    return Math.round(minutes / 1440) + " d ago";
  }

  function renderBadge() {
    badge.textContent = state.unread > 9 ? "9+" : String(state.unread);
    badge.classList.toggle("hidden", !state.unread);
    bell.setAttribute("aria-label", state.unread ? "Notifications, " + state.unread + " unread" : "Notifications");
  }

  function renderList() {
    list.textContent = "";
    if (!state.items.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No notifications yet.";
      list.appendChild(empty);
      return;
    }
    state.items.forEach(function (item) {
      var row = document.createElement("a");
      row.className = "inbox-item" + (item.unread ? " is-unread" : "");
      row.href = sameSite(item.url);
      var title = document.createElement("strong");
      title.textContent = item.title;
      row.appendChild(title);
      if (item.body) {
        var body = document.createElement("span");
        body.textContent = item.body;
        row.appendChild(body);
      }
      var when = document.createElement("time");
      when.textContent = timeAgo(item.at);
      row.appendChild(when);
      list.appendChild(row);
    });
  }

  function showToast(item) {
    toast.textContent = "";
    var title = document.createElement("strong");
    title.textContent = item.title;
    toast.appendChild(title);
    if (item.body) toast.appendChild(document.createTextNode(item.body));
    toast.classList.remove("hidden");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(function () { toast.classList.add("hidden"); }, 6000);
  }

  function announce(items) {
    var last = readLast();
    var newest = items.length ? items[0].id : 0;
    if (isNaN(last)) { writeLast(newest); return; }  // first visit: old items are not news
    var fresh = items.filter(function (item) { return item.id > last; });
    if (!fresh.length) return;
    writeLast(newest);
    if (!document.hidden) { showToast(fresh[0]); return; }
    if (window.Notification && Notification.permission === "granted" && navigator.serviceWorker) {
      navigator.serviceWorker.ready.then(function (registration) {
        fresh.slice(0, 3).forEach(function (item) {
          registration.showNotification(item.title, {
            body: item.body, icon: "/static/img/icon-192.png", tag: "hrms-" + item.id,
            data: { url: sameSite(item.url) }
          });
        });
      });
    }
  }

  function refresh() {
    return fetch("/api/inbox", { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) return;
        state.items = data.items;
        state.unread = data.unread;
        state.pushKey = data.push_key;
        renderBadge();
        if (!panel.classList.contains("hidden")) renderList();
        announce(data.items);
      })
      .catch(function () { /* offline: try again on the next tick */ });
  }

  /* ----------------------------------------------------------------- push */
  function keyBytes(value) {
    var text = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4));
    return Uint8Array.from(text, function (char) { return char.charCodeAt(0); });
  }

  function pushSupported() {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  }

  function currentSubscription() {
    if (!pushSupported()) return Promise.resolve(null);
    return navigator.serviceWorker.ready.then(function (registration) {
      return registration.pushManager.getSubscription();
    });
  }

  function pushMessage(text) {
    pushArea.textContent = "";
    var note = document.createElement("div");
    note.className = "small muted";
    note.textContent = text;
    pushArea.appendChild(note);
  }

  function pushButton(label, primary, action) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm" + (primary ? " btn-primary" : "");
    button.textContent = label;
    button.addEventListener("click", action);
    pushArea.appendChild(button);
  }

  function turnOn() {
    pushMessage("Waiting for your permission…");
    Notification.requestPermission().then(function (permission) {
      if (permission !== "granted") { renderPush(); return null; }
      return navigator.serviceWorker.ready.then(function (registration) {
        return registration.pushManager.subscribe({
          userVisibleOnly: true, applicationServerKey: keyBytes(state.pushKey)
        });
      }).then(function (subscription) {
        return post("/api/push/subscribe", subscription.toJSON());
      }).then(function (response) {
        if (!response.ok) throw new Error("rejected");
        return post("/api/push/test");
      }).then(renderPush);
    }).catch(function () {
      pushMessage("Could not turn alerts on. Private or incognito windows cannot receive them. Otherwise, please try again.");
    });
  }

  function turnOff() {
    currentSubscription().then(function (subscription) {
      if (!subscription) return null;
      return post("/api/push/unsubscribe", { endpoint: subscription.endpoint }).then(function () {
        return subscription.unsubscribe();
      });
    }).then(renderPush);
  }

  function renderPush() {
    if (!window.isSecureContext) {
      pushMessage("Phone and computer alerts need the secure (https) address of this app.");
      return Promise.resolve();
    }
    if (!pushSupported()) {
      var iPhone = /iPad|iPhone|iPod/.test(navigator.userAgent);
      pushMessage(iPhone
        ? "On iPhone: tap Share, then Add to Home Screen. Open the app from there to turn on alerts."
        : "This browser cannot show system notifications. The bell still works.");
      return Promise.resolve();
    }
    if (!state.pushKey) { pushMessage("System notifications are not available on this server."); return Promise.resolve(); }
    if (Notification.permission === "denied") {
      pushMessage("Notifications are blocked. Allow them for this site in your browser settings.");
      return Promise.resolve();
    }
    return currentSubscription().then(function (subscription) {
      pushArea.textContent = "";
      if (subscription) {
        var note = document.createElement("div");
        note.className = "small muted grow";
        note.textContent = "Alerts are on for this device.";
        pushArea.appendChild(note);
        pushButton("Test", false, function () { post("/api/push/test"); });
        pushButton("Turn off", false, turnOff);
      } else {
        var hint = document.createElement("div");
        hint.className = "small muted grow";
        hint.textContent = "Get alerts even when the app is closed.";
        pushArea.appendChild(hint);
        pushButton("Turn on", true, turnOn);
      }
    });
  }

  /* -------------------------------------------------------------- the panel */
  function openPanel() {
    panel.classList.remove("hidden");
    bell.setAttribute("aria-expanded", "true");
    renderList();
    renderPush();
    if (state.items.length && state.unread) {
      post("/api/inbox/seen", { last_id: state.items[0].id });
      state.unread = 0;
      state.items.forEach(function (item) { item.unread = false; });
      renderBadge();
    }
  }
  function closePanel() {
    panel.classList.add("hidden");
    bell.setAttribute("aria-expanded", "false");
  }

  bell.addEventListener("click", function (event) {
    event.stopPropagation();
    if (panel.classList.contains("hidden")) openPanel(); else closePanel();
  });
  document.addEventListener("click", function (event) {
    if (!panel.classList.contains("hidden") && !panel.contains(event.target)) closePanel();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closePanel();
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });

  // A shared phone must stop receiving this person's alerts once they sign out.
  var logoutForm = document.querySelector('form[action="/logout"]');
  if (logoutForm) {
    logoutForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var leave = function () { logoutForm.submit(); };
      window.setTimeout(leave, 1200);
      currentSubscription().then(function (subscription) {
        if (!subscription) return null;
        post("/api/push/unsubscribe", { endpoint: subscription.endpoint }, true);
        return subscription.unsubscribe();
      }).then(leave, leave);
    });
  }

  refresh();
  window.setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
})();
