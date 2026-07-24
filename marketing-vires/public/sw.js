// Self-destructing service worker — DO NOT DELETE THIS FILE.
//
// Before 2026-07-18 the Vires app lived at this domain's root and registered a
// PWA service worker at /sw.js with scope "/". The /app rebase (nousergon/vires
// #135) moved the app and its worker to /app/sw.js, and this Pages project took
// over the root — which broke every returning pre-rebase browser: their stale
// root-scope worker kept serving the old cached app shell for ALL navigations
// (scope "/" covers /app/), and its update check fetched /sw.js, got this
// site's HTML fallback (wrong MIME), failed, and never unregistered — a failed
// update does not remove a worker; only real JS at this URL can. Diagnosed
// live 2026-07-20 after a day of unexplainable login loops.
//
// This file is that real JS: the canonical kill-switch. The stale worker's
// next update check installs it; on activate it wipes this origin's caches,
// unregisters itself, and reloads open tabs — which then load fresh from the
// network (marketing at root, the live app at /app/ with its own scoped
// worker). It must stay here indefinitely: a pre-rebase browser can come back
// months later.
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.map((name) => caches.delete(name)));
      await self.registration.unregister();
      const windows = await self.clients.matchAll({ type: "window" });
      for (const client of windows) {
        client.navigate(client.url);
      }
    })(),
  );
});
