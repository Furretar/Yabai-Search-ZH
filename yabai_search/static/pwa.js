// The service worker has been removed: it served the home-page shell from a
// stale-while-revalidate cache, which could delay updates and briefly show the
// previous page. Standard HTTP caching now handles repeat visits (versioned
// assets are immutable), so this script only cleans up registrations and
// caches left behind by older versions.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker
    .getRegistrations()
    .then((registrations) =>
      Promise.all(registrations.map((registration) => registration.unregister())),
    )
    .catch(() => {
      // Unregistering is best-effort; a failure must never break the page.
    });
}
if (window.caches) {
  caches
    .keys()
    .then((names) => Promise.all(names.map((name) => caches.delete(name))))
    .catch(() => {
      // Cache cleanup is best-effort too.
    });
}
