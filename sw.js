/* Дизель-Підбір — Service Worker
 * Стратегія: network-first для навігацій (сторінка ЗАВЖДИ свіжа, коли є мережа),
 * кеш — лише офлайн-фолбек. Оновлення застосунку доходять до користувачів одразу
 * після деплою, без ручного очищення кешу.
 *
 * Після зміни логіки кешу тут — підніми VERSION, щоб старі кеші у користувачів
 * гарантовано очистилися. Для звичайних правок index.html піднімати НЕ треба:
 * network-first і так віддасть свіжий HTML.
 */
const VERSION = 'v2026-08-14-1';
const CACHE = 'dd-shell-' + VERSION;
const SHELL = ['/', '/index.html', '/manifest.json'];

self.addEventListener('install', (e) => {
  self.skipWaiting(); // новий SW бере керування не чекаючи закриття вкладок
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    // видалити всі кеші попередніх версій (у т.ч. від старих/зламаних SW)
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  // Сторонні запити (Supabase API, jsDelivr CDN, Google Fonts) — завжди напряму в мережу.
  if (url.origin !== self.location.origin) return;

  // Навігації / HTML → network-first: свіже завжди, коли є з'єднання.
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const net = await fetch(req);
        const c = await caches.open(CACHE);
        c.put('/index.html', net.clone());
        return net;
      } catch (_) {
        return (await caches.match('/index.html')) || (await caches.match('/')) || Response.error();
      }
    })());
    return;
  }

  // Свої статичні файли (іконки, manifest) → stale-while-revalidate.
  e.respondWith((async () => {
    const cached = await caches.match(req);
    const fetching = fetch(req)
      .then((net) => {
        if (net && net.ok) caches.open(CACHE).then((c) => c.put(req, net.clone()));
        return net;
      })
      .catch(() => cached);
    return cached || fetching;
  })());
});
