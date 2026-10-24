/* Cliente de la API y utilidades comunes */
'use strict';
const API = {
  async req(method, url, body, opts = {}) {
    const headers = { 'X-Requested-With': 'estudio' };
    let payload = body;
    if (body !== undefined && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    const r = await fetch(url, { method, headers, body: payload, credentials: 'same-origin' });
    if (r.status === 401 && !opts.noRedirect) {
      location.href = '/?next=' + encodeURIComponent(location.pathname);
      throw new Error('No autenticado');
    }
    const ct = r.headers.get('content-type') || '';
    const data = ct.includes('json') ? await r.json() : await r.text();
    if (!r.ok) {
      const msg = (data && data.detail) ? (Array.isArray(data.detail)
        ? data.detail.map(d => d.msg).join('; ') : data.detail) : `Error ${r.status}`;
      const e = new Error(msg); e.status = r.status; throw e;
    }
    return data;
  },
  get(u, o) { return this.req('GET', u, undefined, o); },
  post(u, b, o) { return this.req('POST', u, b ?? {}, o); },
  put(u, b, o) { return this.req('PUT', u, b, o); },
  patch(u, b) { return this.req('PATCH', u, b); },
  del(u) { return this.req('DELETE', u); },
  /* Subida por fragmentos de 16 MB, con reintentos y reanudación (robusta ante proxies y cortes) */
  async uploadFile(pid, file, filename, { autoTranscribe = false, language = 'es' } = {}, onProgress) {
    const start = await this.post(`/api/projects/${pid}/uploads`, { filename, size: file.size });
    const id = start.upload_id, chunk = start.chunk;
    let offset = 0, fails = 0;
    while (offset < file.size) {
      const end = Math.min(file.size, offset + chunk);
      try {
        const base = offset;
        const r = await this.upload(`/api/uploads/${id}?offset=${offset}`, file.slice(offset, end),
          p => onProgress && onProgress((base + p * (end - base)) / file.size));
        offset = r.received; fails = 0;
      } catch (e) {
        if (++fails > 5) throw e;
        await new Promise(r => setTimeout(r, 1500 * fails));
        try { offset = (await this.get(`/api/uploads/${id}`)).received; } catch (_) { /* reintentar */ }
      }
    }
    onProgress && onProgress(1);
    return this.post(`/api/uploads/${id}/complete`, { auto_transcribe: autoTranscribe, language });
  },
  /* Subida con progreso (XHR permite medir el avance) */
  upload(url, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open('PUT', url);
      x.setRequestHeader('X-Requested-With', 'estudio');
      x.upload.onprogress = e => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
      x.onload = () => {
        let d = null; try { d = JSON.parse(x.responseText); } catch (_) { /* */ }
        if (x.status >= 200 && x.status < 300) resolve(d);
        else reject(new Error((d && d.detail) || `Error ${x.status}`));
      };
      x.onerror = () => reject(new Error('Fallo de red durante la subida'));
      x.send(blob);
    });
  },
};

function toast(msg, isErr = false, ms = 3800) {
  let box = document.getElementById('toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; document.body.appendChild(box); }
  const t = document.createElement('div');
  t.className = 'toast' + (isErr ? ' err' : '');
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function fmtTime(s, ms = false) {
  if (!isFinite(s) || s < 0) s = 0;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  const base = (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(sec).padStart(2, '0');
  return ms ? base + '.' + String(Math.floor((s % 1) * 10)) : base;
}

function parseTime(str) {
  if (typeof str === 'number') return str;
  const p = String(str).trim().replace(',', '.').split(':').map(Number);
  if (p.some(isNaN)) return NaN;
  return p.reduce((a, v) => a * 60 + v, 0);
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'html') e.innerHTML = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false)
    e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}

function uid() { return Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4); }
function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
