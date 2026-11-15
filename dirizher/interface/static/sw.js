// Сервис-воркер «Дирижёра»: только чтобы страницу можно было установить как приложение и открыть
// демо без сети. Регистрируется лишь по HTTPS (GitHub Pages); на приборе по http:// не включается.
//
// Правила (принцип: экран не должен показывать устаревшее состояние прибора):
//  - запросы /api/… и всё, кроме GET своего адреса, НЕ трогаем — они всегда идут на прибор;
//  - остальное — «сначала сеть»: свежий ответ кладём в кэш, из кэша берём только без сети.
"use strict";
const CACHE = "dirizher-shell-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.includes("/api/")) {
    return;  // мимо воркера — как будто его нет
  }
  event.respondWith(
    fetch(req)
      .then((resp) => {
        if (resp.ok && resp.type === "basic") {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return resp;
      })
      .catch(() => caches.match(req).then((hit) => hit || Response.error()))
  );
});
