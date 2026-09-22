'use strict';

const CACHE_NAME = 'streamly-shell-v21';
const SHELL_ASSETS = [
  '/',
  '/app.css',
  '/app.js',
  '/manifest.json',
  '/icon.svg',
  '/vendor/hls.min.js',
];

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k.startsWith('streamly-shell-') && k !== CACHE_NAME).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/s/') ||
      url.pathname.startsWith('/v/') ||
      url.pathname.startsWith('/media/')) {
    return;
  }

  event.respondWith(
    caches.match(request).then(cached => {
      return cached || fetch(request).then(response => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => {
          if (response.ok) cache.put(request, copy);
        }).catch(() => {});
        return response;
      }).catch(() => cached);
    })
  );
});
