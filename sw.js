// Stroomtip service worker: altijd eerst de verse pagina, de laatste versie als reserve zonder netwerk.
const CACHE = 'stroomtip-v1';
const BESTANDEN = ['./', './manifest.webmanifest', './icons/icon-192.png', './icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(BESTANDEN)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request)
      .then(resp => {
        if (resp.ok && new URL(e.request.url).origin === location.origin) {
          const kopie = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, kopie));
        }
        return resp;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('./')))
  );
});
