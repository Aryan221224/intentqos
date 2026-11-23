// SampleNet Home service worker: keeps the app shell available offline so it opens instantly and can say
// "you're offline" instead of a browser error. API calls always go to the network (live data only).
const SHELL = "samplenet-shell-v1";
const FILES = ["/app/", "/app/manifest.webmanifest", "/app/icon-192.png", "/app/icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.pathname.startsWith("/v1/")) return;      // live data: network only
  e.respondWith(fetch(e.request).then(r => {                                     // network first, cache as fallback
    if (r.ok && (u.pathname.startsWith("/app/") || u.pathname.startsWith("/fonts/"))) {
      const copy = r.clone(); caches.open(SHELL).then(c => c.put(e.request, copy));
    }
    return r;
  }).catch(() => caches.match(e.request).then(m => m || caches.match("/app/"))));
});
