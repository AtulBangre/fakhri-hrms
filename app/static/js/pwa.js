/* ==========================================================================
   PWA plumbing, shared by the HR console, the login page and the gate scanner.

   Two jobs:
     1. register the service worker (offline app shell)
     2. keep the device FRESH — ask for an update on every load, and reload once
        when a new worker takes over

   Why that second job matters: an installed PWA can otherwise keep running the
   CSS and JS it saw on install day. That looks exactly like "the UI is broken"
   when in fact the server is serving a perfectly good new stylesheet.
   ========================================================================== */
(function () {
  "use strict";

  if (!("serviceWorker" in navigator)) return;

  var hadController = !!navigator.serviceWorker.controller;
  var reloading = false;

  navigator.serviceWorker.addEventListener("controllerchange", function () {
    // Only reload for a genuine update. On the very first install the worker
    // claims the page, which would otherwise cause one pointless reload.
    if (!hadController || reloading) return;
    reloading = true;
    window.location.reload();
  });

  window.addEventListener("load", function () {
    navigator.serviceWorker
      .register("/sw.js")
      .then(function (registration) {
        // Ask the network whether a newer worker exists, right now. The worker
        // calls skipWaiting() during install, so a new one activates at once and
        // the controllerchange handler above refreshes the page.
        registration.update().catch(function () {});
      })
      .catch(function () {
        /* offline support is a bonus; the app works fine without it */
      });
  });
})();
