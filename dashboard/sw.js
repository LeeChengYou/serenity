// Serenity Signal Service Worker
// 版本字串：升版時更改此值以觸發 activate 清除舊快取
const CACHE_VERSION = 'serenity-v2-fundpool';

// 安裝時預快取的靜態資產
const STATIC_ASSETS = [
  '/index.html',
  '/app.js',
  '/styles.css',
  '/manifest.json',
  '/icons/icon.svg',
];

// ── install：預快取靜態資產 ────────────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => {
      // addAll 若有任一失敗也不中斷 SW 安裝（用 Promise.allSettled 包住）
      return Promise.allSettled(
        STATIC_ASSETS.map((url) =>
          cache.add(url).catch(() => {
            // 單一資產快取失敗不阻斷整體安裝
          })
        )
      );
    })
  );
  // 立即接管，不等待舊 SW 失效
  self.skipWaiting();
});

// ── activate：清除舊版快取 ────────────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_VERSION)
          .map((key) => caches.delete(key))
      )
    )
  );
  // 立即接管所有 clients
  self.clients.claim();
});

// ── fetch：/api/* 一律 network-only；其餘 cache-first ─────────────────────────
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 金融 API 資料：不快取，永遠走網路（避免離線時提供過時訊號誤導投資決策）
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // 靜態資產：cache-first，快取不中才走網路並更新快取
  event.respondWith(
    caches.open(CACHE_VERSION).then((cache) =>
      cache.match(event.request).then((cached) => {
        if (cached) {
          return cached;
        }
        return fetch(event.request).then((response) => {
          // 只快取成功且同源的回應
          if (
            response &&
            response.status === 200 &&
            response.type === 'basic'
          ) {
            cache.put(event.request, response.clone());
          }
          return response;
        });
      })
    )
  );
});
