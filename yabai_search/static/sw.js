// Service worker for Yabai Search.
// Uses stale-while-revalidate for static assets: serves cached content
// instantly, then fetches fresh content in the background. API responses
// and HTML pages are never cached.

const CACHE_NAME = "yabai-static-v1";

self.addEventListener("install", (event) => {
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((names) =>
            Promise.all(
                names
                    .filter((name) => name !== CACHE_NAME)
                    .map((name) => caches.delete(name))
            )
        )
    );
    self.clients.claim();
});

self.addEventListener("fetch", (event) => {
    const url = new URL(event.request.url);

    // Only handle GET requests for static assets.
    if (event.request.method !== "GET") return;
    if (!url.pathname.startsWith("/static/") && url.pathname !== "/manifest.webmanifest") return;

    const cacheKey = url.pathname;

    event.respondWith(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.match(cacheKey).then((cached) => {
                // Fetch fresh content in the background.
                const fetchPromise = fetch(event.request).then((response) => {
                    if (response.ok) {
                        cache.put(cacheKey, response.clone());
                    }
                    return response;
                }).catch(() => cached);

                // Return cached immediately if available, otherwise wait for fetch.
                return cached || fetchPromise;
            });
        })
    );
});
