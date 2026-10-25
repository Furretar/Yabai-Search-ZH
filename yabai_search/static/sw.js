// Minimal service worker for Yabai Search.
// It makes the site installable without caching API responses, which must
// always stay live. Requests without respondWith fall through to the network.
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", () => {
  // Intentionally empty: network handles every request.
});
