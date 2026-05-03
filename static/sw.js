// Service worker — cache the app shell so the dashboard opens instantly
// (and works offline for the static UI; data still needs network).
const CACHE = 'ffd-v1';
const SHELL = ['/', '/static/manifest.json', '/static/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Network-first for API calls so data is always fresh.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/parse-pdf') || url.pathname.startsWith('/save-') || url.pathname.startsWith('/recategorize')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }
  // Cache-first for the app shell.
  e.respondWith(
    caches.match(e.request).then((cached) => cached || fetch(e.request).then((resp) => {
      if (e.request.method === 'GET' && resp.ok) {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return resp;
    }))
  );
});
