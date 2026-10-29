// Minimal service worker — just enough to satisfy "installable PWA"
// requirements. Deliberately does NOT cache API responses (payment status
// must always be fresh), only the static app shell.

const CACHE_NAME = "thibitifare-shell-v3";
const SHELL_FILES = ["/Manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);

  // Never intercept API calls - always go to the network for those.
  if (url.origin !== self.location.origin) return;

  // Pages and hashed assets always come from the network, so a new deploy
  // never serves a stale index.html. Only the manifest is cache-backed.
  if (url.pathname !== "/Manifest.json") return;
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
