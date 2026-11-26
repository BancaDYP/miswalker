/* Guarda la app en el teléfono para que abra aunque la red falle */
const CACHE = "loto-v1";
const ARCHIVOS = ["./", "./index.html", "./kino.html", "./manifest.json"];
self.addEventListener("install", e=>{ self.skipWaiting(); e.waitUntil(caches.open(CACHE).then(c=>c.addAll(ARCHIVOS).catch(()=>{}))); });
self.addEventListener("activate", e=>{ e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", e=>{
  if(e.request.method!=="GET") return;
  // primero intenta la red (para tener lo más nuevo); si tarda más de 4 s o falla, usa lo guardado
  e.respondWith((async()=>{
    const cache = await caches.open(CACHE);
    const red = fetch(e.request).then(r=>{ if(r && r.ok) cache.put(e.request, r.clone()); return r; });
    const tiempo = new Promise(res=>setTimeout(res, 4000));
    try {
      const r = await Promise.race([red, tiempo.then(()=>null)]);
      if(r) return r;
    } catch(err){}
    const guardado = await cache.match(e.request, { ignoreSearch:true }) || await cache.match("./index.html");
    if(guardado) return guardado;
    return red;
  })());
});
