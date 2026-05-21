const CACHE = 'asistia-v1';

// Solo cacheamos el shell estático — nunca la API ni WebSockets
const SHELL = [
  '/',
  '/static/kiosk.html',
  '/static/manifest.json',
  '/static/logo-ipa-192.png',
  '/static/logo-ipa-96.png',
  '/static/logo-ipa-48.png',
  '/static/IPA-512X512.png',
  '/static/favicon.ico',
  '/static/flash.mp3',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Nunca interceptar WebSockets, API, ni peticiones cross-origin
  if (e.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/ws/')) return;
  if (url.origin !== self.location.origin) return;

  // Cache-first para assets estáticos, network-first para la raíz
  if (url.pathname === '/' || url.pathname === '/static/kiosk.html') {
    e.respondWith(
      fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }).catch(() => caches.match(e.request))
    );
  } else {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request))
    );
  }
});
