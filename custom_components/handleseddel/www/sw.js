// Gemmer appen på enheden, så den virker uden internet.
const CACHE = 'handleseddel-v13';
const FILES = ['./index.html', './manifest.webmanifest', './icon-180.png', './icon-192.png', './icon-512.png'];
const SCOPE = new URL('./', self.location).pathname;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Kun appens egne filer håndteres. API-kald og login går altid direkte til serveren.
// Nyeste udgave hentes fra serveren, men svarer den ikke inden 3 sekunder (dårligt signal), bruges den gemte udgave.
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin || !url.pathname.startsWith(SCOPE)) return;
  const fraCache = () => caches.match(e.request, { ignoreSearch: true }).then(r => r || caches.match('./index.html'));
  const net = fetch(e.request).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {}); }
    return res;
  });
  e.respondWith(new Promise(resolve => {
    let done = false;
    const brug = r => { if (!done && r) { done = true; resolve(r); } };
    const t = setTimeout(() => fraCache().then(r => r ? brug(r) : net.then(brug, () => {})), 3000);
    net.then(r => { clearTimeout(t); brug(r); }, () => { clearTimeout(t); fraCache().then(r => brug(r || Response.error())); });
  }));
});
