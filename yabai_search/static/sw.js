// Service worker for Yabai Search.
//
// Caches the home-page shell together with its versioned static assets so a
// reload paints instantly. The search results, API responses, and other HTML
// pages always go to the network.

const CACHE_NAME = "yabai-shell-v8";

// Fetch the home page, cache it under "/", and pre-cache the /static/*?v=*
// assets it references. Then prune any versioned assets the new shell no
// longer uses, so the cache stays small and consistent.
async function refreshShell() {
    const response = await fetch("/", { cache: "no-store" });
    if (!response.ok) return;
    const cache = await caches.open(CACHE_NAME);
    await cache.put("/", response.clone());

    const html = await response.text();
    const urls = new Set();
    const re = /(?:src|href)="(\/static\/[^"]+\?v=[^"]+)"/g;
    let match;
    while ((match = re.exec(html)) !== null) {
        urls.add(match[1]);
    }
    await Promise.all(
        [...urls].map(async (url) => {
            if (await cache.match(url)) return;
            try {
                await cache.add(url);
            } catch (error) {
                // A single missing asset should not fail the whole refresh.
            }
        })
    );

    // Prune versioned assets that the new shell no longer references.
    const keys = await cache.keys();
    await Promise.all(
        keys.map(async (request) => {
            const path = new URL(request.url).pathname;
            if (path.startsWith("/static/") && request.url.includes("?v=")) {
                if (![...urls].some((u) => u.startsWith(path))) {
                    await cache.delete(request);
                }
            }
        })
    );
}

self.addEventListener("install", (event) => {
    event.waitUntil(refreshShell().catch(() => {}));
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
    if (event.request.method !== "GET") return;
    if (url.origin !== self.location.origin) return;

    // Home page navigations: serve the cached shell instantly, then refresh.
    if (event.request.mode === "navigate" && url.pathname === "/") {
        event.respondWith(
            caches.match("/").then((cached) => {
                event.waitUntil(refreshShell().catch(() => {}));
                return cached || fetch(event.request);
            })
        );
        return;
    }

    // Versioned static assets: network-first so updates appear immediately,
    // falling back to cache when offline.
    if (url.pathname.startsWith("/static/") && url.searchParams.has("v")) {
        event.respondWith(
            fetch(event.request, { cache: "no-store" })
                .then((response) => {
                    if (response.ok) {
                        const clone = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
                    }
                    return response;
                })
                .catch(() => caches.match(event.request).then((cached) => cached || new Response("Offline", { status: 503 })))
        );
        return;
    }

    // Other static assets: stale-while-revalidate.
    if (url.pathname.startsWith("/static/")) {
        event.respondWith(
            caches.open(CACHE_NAME).then((cache) =>
                cache.match(event.request).then((cached) => {
                    const fetched = fetch(event.request).then((response) => {
                        if (response.ok) cache.put(event.request, response.clone());
                        return response;
                    }).catch(() => cached);
                    return cached || fetched;
                })
            )
        );
        return;
    }

    // Everything else (API, about, word, reports, manifest) goes to network.
});
