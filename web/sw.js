// Service worker: it keeps the app shell, so that the home screen app opens fast and
// shows the last page without a connection. Data (/api/) always comes from the network.
const SHELL = "chief-shell-v1";
const FILES = ["/", "/app.js", "/style.css", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Network first, so that a new version shows at once. The cache is the fallback.
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  e.respondWith(fetch(e.request)
    .then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(e.request, copy));
      }
      return res;
    })
    .catch(() => caches.match(e.request)));
});
