/* Fakhri HRMS service worker — offline app shell.
 *
 * Strategies:
 *   navigations  → network first, fall back to the cached page, then /offline
 *   /static/*    → network first, fall back to cache when offline
 *   /api/*, POST → never cached; attendance must never come from cache
 *
 * Why network-first for assets: an installed kiosk or phone must pick up a new
 * stylesheet or script on the next load. Cache-first (the obvious "faster"
 * choice) silently pins every device to the CSS it saw on install day, which is
 * exactly how a redesign ends up invisible in the field. Assets are small and
 * already versioned with ?v=<hash>, so this costs almost nothing.
 *
 * A stale attendance screen is worse than an error, so nothing carrying data is
 * ever served from cache.
 */
const CACHE = "fakhri-hrms-v4";
const SHELL = [
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/js/kiosk.js",
  "/static/js/kiosk-offline.js",
  "/static/js/pwa.js",
  "/static/img/icon-192.png",
  "/static/img/icon-512.png",
  "/offline"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;              // live data only

  if (request.mode === "navigate") {
    // The scanner is the one page kept for offline use. It carries no names or data.
    const isKiosk = url.pathname === "/kiosk";
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (isKiosk && response.ok && !response.redirected) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put("/kiosk", copy));
          }
          return response;
        })
        .catch(() =>
          caches.match(isKiosk ? "/kiosk" : request).then((cached) => cached || caches.match("/offline"))
        )
    );
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    // network first, cache as the offline fallback
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
  }
});

/* ------------------------------------------------- system notifications */
function samePath(value) {
  // Only ever open a page of this app.
  try {
    const url = new URL(value || "/", self.location.origin);
    return url.origin === self.location.origin ? url.pathname + url.search : "/";
  } catch (error) {
    return "/";
  }
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    data = {};
  }
  // A dot on the app icon. The page sets the exact count once it opens.
  if (self.navigator.setAppBadge) self.navigator.setAppBadge().catch(() => {});
  event.waitUntil(
    self.registration.showNotification(data.title || "Fakhri HRMS", {
      body: data.body || "",
      icon: "/static/img/icon-192.png",
      badge: "/static/img/icon-192.png",
      // The same tag means the app's own polling cannot show this one twice.
      tag: data.id ? `hrms-${data.id}` : undefined,
      data: { url: samePath(data.url) },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = samePath(event.notification.data && event.notification.data.url);
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if ("focus" in client) {
          return client.focus().then((focused) => ("navigate" in focused ? focused.navigate(target) : focused));
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
