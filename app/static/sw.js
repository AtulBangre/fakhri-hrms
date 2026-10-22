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
const CACHE = "fakhri-hrms-v2";
const SHELL = [
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/js/kiosk.js",
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
    event.respondWith(
      fetch(request)
        .then((response) => response)
        .catch(() =>
          caches.match(request).then((cached) => cached || caches.match("/offline"))
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
