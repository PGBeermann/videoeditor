/* Editor de video educativo — lógica de cliente
   Modelo: el proyecto es un JSON (clips, capas, zooms, música, capítulos, preguntas, ajustes)
   que se guarda automáticamente; el servidor renderiza con FFmpeg a partir de ese JSON. */
'use strict';

const PID = location.pathname.split('/').filter(Boolean).pop();
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

const S = {
  data: null, name: '', version: 0, role: 'owner',
  media: {},          // id -> medio
  transcripts: {},    // media_id -> [{w,s,e,p}]
  txMeta: {},         // media_id -> {language, translated}
  sel: null,          // {kind:'clip'|'overlay'|'zoom'|'music'|'chapter'|'quiz'|'comment', id}
  t: 0, playing: false, pxs: 40,
  undo: [], redo: [],
  saveTimer: null, saving: false, dirty: false,
  words: [], cues: [], txSel: null, fillers: [],
  jobs: [], jobsTimer: null, knownDone: new Set(),
  comments: [], exports: [],
  pendingAdd: [],     // medios que se añadirán al terminar de procesarse
  segs: [], segIdx: -1, segClock: 0, playToken: 0,
  musicEls: {}, layerEls: {}, assetUrl: {},
};

const canEdit = () => S.role !== 'revisor';
const D = () => S.data;
const W0 = () => D().settings.width;
const H0 = () => D().settings.height;
const FPS = () => D().settings.fps || 30;

/* ================================================================= Modelo */
function clipDur(c) { return Math.max(0, c.out - c.in); }
function segments() {
  let t = 0;
  return D().clips.map((c, i) => { const s = t; t += clipDur(c); return { clip: c, i, start: s, end: t }; });
}
function duration() { return D().clips.reduce((a, c) => a + clipDur(c), 0); }
function locate(t) {
  const segs = segments();
  for (const sg of segs) if (t >= sg.start && t < sg.end) return { ...sg, src: sg.clip.in + (t - sg.start) };
  if (segs.length && Math.abs(t - segs[segs.length - 1].end) < 1e-6) { const sg = segs[segs.length - 1]; return { ...sg, src: sg.clip.out }; }
  return null;
}
function findItem(kind, id) {
  const coll = { clip: 'clips', overlay: 'overlays', zoom: 'zooms', music: 'music', chapter: 'chapters', quiz: 'quizzes' }[kind];
  if (kind === 'comment') return S.comments.find(c => c.id === id);
  return coll ? D()[coll].find(x => x.id === id) : null;
}

function snapshot() { return JSON.stringify(D()); }
function change(fn, { silent = false } = {}) {
  if (!canEdit()) { toast('Solo lectura: usted es revisor de este proyecto', true); return; }
  S.undo.push(snapshot()); if (S.undo.length > 200) S.undo.shift();
  S.redo = [];
  fn(D());
  afterChange(silent);
}
/* Edición continua (sliders, campos de texto): un solo paso de deshacer por gesto */
function liveEdit(fn, fin) {
  if (!canEdit()) return;
  if (!S.editing) { S.undo.push(snapshot()); S.redo = []; S.editing = true; }
  fn();
  if (fin) { S.editing = false; afterChange(true); }
}
function afterChange(silent) {
  S.dirty = true; setSave('pending');
  clearTimeout(S.saveTimer); S.saveTimer = setTimeout(save, 1200);
  computeWords();
  if (!silent) renderAll(); else { renderTimeline(); updateStage(); }
}
function undo() {
  if (!S.undo.length || !canEdit()) return;
  S.redo.push(snapshot()); S.data = JSON.parse(S.undo.pop());
  if (S.sel && !findItem(S.sel.kind, S.sel.id)) S.sel = null;
  afterChange(false); seek(Math.min(S.t, duration()));
}
function redo() {
  if (!S.redo.length || !canEdit()) return;
  S.undo.push(snapshot()); S.data = JSON.parse(S.redo.pop());
  if (S.sel && !findItem(S.sel.kind, S.sel.id)) S.sel = null;
  afterChange(false); seek(Math.min(S.t, duration()));
}

function setSave(state) {
  const b = $('#saveState');
  const map = { pending: ['Cambios sin guardar', 'warn'], saving: ['Guardando…', 'warn'], ok: ['Guardado', 'ok'], err: ['Error al guardar', 'err'], ro: ['Solo lectura', ''] };
  const [txt, cls] = map[state]; b.textContent = txt; b.className = 'badge ' + cls;
}

async function save() {
  if (!S.dirty || !canEdit()) return;
  if (S.saving) { clearTimeout(S.saveTimer); S.saveTimer = setTimeout(save, 800); return; }
  S.saving = true; S.dirty = false; setSave('saving');
  try {
    const r = await API.put('/api/projects/' + PID, { data: D(), version: S.version, name: S.name });
    S.version = r.version; setSave(S.dirty ? 'pending' : 'ok');
  } catch (e) {
    S.dirty = true; setSave('err');
    if (e.status === 409) { if (confirm(e.message + '\n\n¿Recargar ahora? (se perderán los cambios locales no guardados)')) location.reload(); }
    else toast('No se pudo guardar: ' + e.message, true);
  } finally { S.saving = false; }
}
window.addEventListener('beforeunload', e => { if (S.dirty) { save(); e.preventDefault(); e.returnValue = ''; } });

/* ================================================================= Carga */
async function load() {
  const p = await API.get('/api/projects/' + PID);
  S.data = p.data; S.name = p.name; S.version = p.version; S.role = p.role;
  S.txMeta = p.transcripts;
  p.media.forEach(m => S.media[m.id] = m);
  $('#projName').value = p.name;
  document.title = p.name + ' — Editor';
  $('#btnRecord').href = '/grabar/' + PID;
  if (!canEdit()) {
    setSave('ro'); $('#roleBadge').textContent = 'Revisor'; $('#roleBadge').classList.remove('hidden');
    ['#btnRecord', '#btnExport', '#btnShare', '#drop'].forEach(s => $(s).classList.add('hidden'));
    $('#projName').disabled = true;
  } else setSave('ok');
  if (p.role !== 'owner') $('#btnShare').classList.add('hidden');
  await Promise.all(Object.keys(S.txMeta).map(loadTranscript));
  const add = new URLSearchParams(location.search).get('add');
  if (add && S.media[add]) { S.pendingAdd.push({ mid: add, at: 'end' }); history.replaceState(null, '', location.pathname); }
  computeWords(); fitStage(); renderAll(); seek(0); loadComments(); loadExports();
  for (const pa of [...S.pendingAdd]) {
    const m = S.media[pa.mid];
    if (m && m.status === 'ready') { S.pendingAdd.splice(S.pendingAdd.indexOf(pa), 1); addMediaToTimeline(m, pa.at); }
  }
  pollJobs();
}

async function loadTranscript(mid) {
  try { const t = await API.get(`/api/media/${mid}/transcript`); S.transcripts[mid] = t.words; S.txMeta[mid] = { language: t.language, translated: !!t.translated }; }
  catch (_) { /* aún no existe */ }
}

/* ================================================================= Palabras y subtítulos (igual que el servidor) */
function computeWords() {
  const out = []; let t0 = 0;
  for (const c of D().clips) {
    const dur = clipDur(c), words = S.transcripts[c.media_id] || [];
    words.forEach((w, idx) => {
      const mid = (w.s + w.e) / 2;
      if (mid >= c.in && mid <= c.out) {
        const s = t0 + Math.max(0, w.s - c.in), e = t0 + Math.min(dur, w.e - c.in);
        out.push({ w: w.w, s, e: Math.max(e, s + 0.05), p: w.p ?? 1, mid: c.media_id, idx, clipId: c.id });
      }
    });
    t0 += dur;
  }
  S.words = out; S.cues = buildCues(out);
}
function buildCues(words) {
  const cues = []; let cur = [];
  const flush = () => { if (cur.length) { cues.push({ s: cur[0].s, e: cur[cur.length - 1].e, text: cur.map(w => w.w.trim()).join(' ') }); cur = []; } };
  for (const w of words) {
    if (cur.length) {
      const len = cur.map(x => x.w.trim()).join(' ').length + 1 + w.w.trim().length;
      if (len > 42 || w.s - cur[cur.length - 1].e > 0.7 || w.e - cur[0].s > 6) flush();
    }
    cur.push(w);
    if (/[.?!…]$/.test(w.w.trim())) flush();
  }
  flush();
  cues.forEach((c, i) => { const nxt = i + 1 < cues.length ? cues[i + 1].s : c.e + 1; if (c.e - c.s < 1) c.e = Math.min(c.s + 1, nxt - 0.04); });
  return cues;
}
const FILLERS = new Set(['eh', 'ehh', 'ehhh', 'em', 'emm', 'mm', 'mmm', 'hmm', 'ah', 'ahh', 'eeh']);
function isFiller(words, i) {
  const w = words[i].w.toLowerCase().replace(/[.,;:¿?¡!…"']/g, '');
  if (FILLERS.has(w)) return true;
  if (w === 'este' || w === 'esteee' || w === 'o sea') {           // solo si está aislada por pausas
    const prev = words[i - 1], next = words[i + 1];
    return (!prev || words[i].s - prev.e > 0.25) && (!next || next.s - words[i].e > 0.25);
  }
  return false;
}

/* ================================================================= Edición de la secuencia */
function cutRange(a, b, opts = {}) {
  if (b - a < 0.02) return;
  const run = data => {
    const d = b - a, out = []; let t0 = 0;
    for (const c of data.clips) {
      const s = t0, e = t0 + clipDur(c); t0 = e;
      if (e <= a + 1e-6 || s >= b - 1e-6) { out.push(c); continue; }
      if (a > s) out.push({ ...c, out: c.in + (a - s) });
      if (b < e) out.push({ ...c, id: a > s ? uid() : c.id, in: c.in + (b - s) });
    }
    data.clips = out.filter(c => clipDur(c) >= 1 / 30);
    rippleItems(data, a, b, -d);
  };
  if (opts.inPlace) run(D()); else change(run);
}
/* Ajusta capas, zooms, música y marcas cuando se elimina [a,b] (delta<0) o se inserta en a (delta>0). */
function rippleItems(data, a, b, delta) {
  if (delta > 0) {
    for (const o of [...data.overlays, ...data.zooms]) if (o.start >= a - 1e-6) { o.start += delta; o.end += delta; }
    for (const m of data.music) if (m.start >= a - 1e-6) m.start += delta;
    for (const k of [...data.chapters, ...data.quizzes]) if (k.t >= a - 1e-6) k.t += delta;
    return;
  }
  const d = -delta;
  const span = list => list.filter(o => {
    if (o.end <= a) return true;
    if (o.start >= b) { o.start -= d; o.end -= d; return true; }
    o.start = o.start < a ? o.start : a; o.end = o.end > b ? o.end - d : a;
    return o.end - o.start >= 0.1;
  });
  data.overlays = span(data.overlays); data.zooms = span(data.zooms);
  for (const m of data.music) { if (m.start >= b) m.start -= d; else if (m.start > a) { m.in = (m.in || 0) + (b - m.start); m.start = a; } }
  for (const k of [...data.chapters, ...data.quizzes]) { if (k.t >= b) k.t -= d; else if (k.t > a) k.t = a; }
}
function splitAt(t) {
  const loc = locate(t);
  if (!loc || t - loc.start < 0.05 || loc.end - t < 0.05) { toast('Ubique el cabezal dentro de un clip para dividirlo'); return; }
  change(data => {
    const c = data.clips[loc.i];
    data.clips.splice(loc.i, 1, { ...c, out: loc.src }, { ...c, id: uid(), in: loc.src });
  });
}
function insertClipAt(t, clip) {
  change(data => {
    const segs = segments();
    let idx = data.clips.length;
    const loc = locate(t);
    if (loc && t > loc.start + 0.05 && t < loc.end - 0.05) {
      const c = data.clips[loc.i];
      data.clips.splice(loc.i, 1, { ...c, out: loc.src }, { ...c, id: uid(), in: loc.src });
      idx = loc.i + 1;
    } else if (loc) idx = Math.abs(t - loc.start) < 0.06 ? loc.i : loc.i + 1;
    if (!segs.length) idx = 0;
    const startT = segments()[idx]?.start ?? duration();
    data.clips.splice(idx, 0, clip);
    rippleItems(data, startT, startT, clipDur(clip));
  });
}

function addMediaToTimeline(m, where = 'playhead') {
  if (m.status !== 'ready') { S.pendingAdd.push({ mid: m.id, at: where }); toast('Se añadirá cuando termine de procesarse'); return; }
  if (m.kind === 'audio') {
    change(data => data.music.push({ id: uid(), media_id: m.id, start: where === 'start' ? 0 : S.t, in: 0, out: m.duration, volume: 0.22, duck: true }));
    toast('Música añadida (pista «Música»)');
    return;
  }
  const dur = m.kind === 'image' ? (m._dur || 5) : m.duration;
  const clip = { id: uid(), media_id: m.id, in: 0, out: dur, fit: 'contain' };
  const t = where === 'start' ? 0 : where === 'end' ? duration() : S.t;
  insertClipAt(t, clip);
  S.sel = { kind: 'clip', id: clip.id }; renderAll();
}

function deleteSelected() {
  if (!S.sel) return;
  const { kind, id } = S.sel;
  if (kind === 'comment') return;
  change(data => {
    if (kind === 'clip') {
      const sg = segments().find(s => s.clip.id === id);
      data.clips = data.clips.filter(c => c.id !== id);
      if (sg) rippleItems(data, sg.start, sg.end, -(sg.end - sg.start));
    } else {
      const coll = { overlay: 'overlays', zoom: 'zooms', music: 'music', chapter: 'chapters', quiz: 'quizzes' }[kind];
      data[coll] = data[coll].filter(x => x.id !== id);
    }
  });
  S.sel = null; renderAll();
}

/* ================================================================= Vista previa */
const vid = $('#vid'), still = $('#still');
function proxyUrl(mid) { return `/api/media/${mid}/proxy`; }

function fitStage() {
  const wrap = $('#stageWrap'), st = $('#stage');
  const aw = wrap.clientWidth - 28, ah = wrap.clientHeight - 28;
  const r = W0() / H0();
  let w = aw, h = aw / r; if (h > ah) { h = ah; w = ah * r; }
  st.style.width = Math.max(50, w) + 'px'; st.style.height = Math.max(30, h) + 'px';
  $('#caption').style.fontSize = Math.max(10, h * (H0() > W0() ? 0.032 : 0.045)) + 'px';
}
new ResizeObserver(() => S.data && fitStage()).observe($('#stageWrap'));

function waitEvent(elm, ev, ms = 4000) {
  return new Promise(res => { let done = false; const f = () => { if (!done) { done = true; elm.removeEventListener(ev, f); res(); } }; elm.addEventListener(ev, f); setTimeout(f, ms); });
}
async function showFrame(loc) {
  const token = ++S.playToken;
  const m = S.media[loc.clip.media_id];
  if (!m) return;
  if (m.kind === 'image') {
    still.src = proxyUrl(m.id); still.classList.remove('hidden'); vid.classList.add('hidden'); vid.pause();
    still.style.objectFit = loc.clip.fit === 'cover' ? 'cover' : 'contain';
    return;
  }
  still.classList.add('hidden'); vid.classList.remove('hidden');
  vid.style.objectFit = loc.clip.fit === 'cover' ? 'cover' : 'contain';
  vid.muted = !!loc.clip.mute;
  vid.volume = clamp(Math.pow(10, (loc.clip.gain_db || 0) / 20), 0, 1);
  if (vid.dataset.mid !== m.id) {
    vid.dataset.mid = m.id; vid.src = proxyUrl(m.id);
    await waitEvent(vid, 'loadedmetadata');
    if (token !== S.playToken) return;
  }
  if (Math.abs(vid.currentTime - loc.src) > 0.04) { vid.currentTime = loc.src; await waitEvent(vid, 'seeked', 3000); }
}

function seek(t) {
  S.t = clamp(t, 0, duration());
  const loc = locate(S.t);
  $('#stageEmpty').classList.toggle('hidden', !!D().clips.length);
  if (loc) showFrame(loc);
  else { vid.removeAttribute('src'); vid.dataset.mid = ''; vid.load(); still.classList.add('hidden'); }
  if (S.playing) { S.segs = segments(); startSegment(); }
  updateStage();
}

function play() {
  const T = duration();
  if (!T) return;
  if (S.t >= T - 0.05) S.t = 0;
  S.playing = true; $('#btnPlay').textContent = '❚❚';
  S.segs = segments(); startSegment(); requestAnimationFrame(tick);
}
function pause() {
  S.playing = false; S.playToken++; $('#btnPlay').textContent = '▶';
  vid.pause(); Object.values(S.musicEls).forEach(a => a.pause());
}
async function startSegment() {
  const loc = locate(S.t);
  if (!loc) { pause(); return; }
  S.segIdx = loc.i;
  const m = S.media[loc.clip.media_id];
  if (m && m.kind === 'image') { await showFrame(loc); S.segClock = performance.now() - (S.t - loc.start) * 1000 / rate(); return; }
  await showFrame(loc);
  if (S.playing && S.segIdx === loc.i) { vid.playbackRate = rate(); vid.play().catch(() => { }); }
}
function rate() { return parseFloat($('#rate').value) || 1; }
function tick() {
  if (!S.playing) return;
  const sg = S.segs[S.segIdx];
  if (sg) {
    const m = S.media[sg.clip.media_id];
    let t;
    if (m && m.kind === 'image') t = sg.start + (performance.now() - S.segClock) / 1000 * rate();
    else if (!vid.paused && !vid.seeking && vid.dataset.mid === sg.clip.media_id) t = sg.start + (vid.currentTime - sg.clip.in);
    else t = S.t;
    if (t >= sg.end - 0.5 / FPS()) {
      if (S.segIdx + 1 >= S.segs.length) { S.t = sg.end; pause(); updateStage(); return; }
      S.t = S.segs[S.segIdx + 1].start; startSegment();
    } else S.t = Math.max(sg.start, t);
  }
  updateStage();
  requestAnimationFrame(tick);
}

function zoomAt(t) {
  let z = 1, ksum = 0, cx = 0, cy = 0;
  for (const zm of D().zooms) {
    const r = Math.min(0.45, (zm.end - zm.start) / 2);
    let k = Math.min(clamp((t - zm.start) / r, 0, 1), clamp((zm.end - t) / r, 0, 1));
    k = k * k * (3 - 2 * k);
    if (k > 0) { z += (zm.scale - 1) * k; ksum += k; cx += zm.x * k; cy += zm.y * k; }
  }
  cx += 0.5 * (1 - Math.min(1, ksum)); cy += 0.5 * (1 - Math.min(1, ksum));
  return { z, cx, cy };
}

function updateStage() {
  const t = S.t, T = duration();
  $('#tcNow').textContent = fmtTime(t, true); $('#tcDur').textContent = fmtTime(T);
  $('#commentAt').textContent = fmtTime(t);
  // zoom
  const { z, cx, cy } = zoomAt(t);
  const zm = $('#zoomer');
  if (z > 1.001) {
    const left = clamp(cx - 0.5 / z, 0, 1 - 1 / z), top = clamp(cy - 0.5 / z, 0, 1 - 1 / z);
    zm.style.transformOrigin = '0 0';
    zm.style.transform = `translate(${-left * z * 100}%, ${-top * z * 100}%) scale(${z})`;
  } else zm.style.transform = '';
  // capas
  for (const ov of D().overlays) {
    const img = S.layerEls[ov.id];
    if (img) img.style.display = (t >= ov.start && t < ov.end) || (S.sel && S.sel.id === ov.id && !S.playing) ? '' : 'none';
  }
  // subtítulos
  const capEl = $('#caption');
  const cue = $('#showCaps').checked ? S.cues.find(c => t >= c.s && t < c.e) : null;
  if (cue) { capEl.innerHTML = '<span>' + esc(cue.text) + '</span>'; capEl.classList.remove('hidden'); capEl.style.color = D().captions.color || '#fff'; }
  else capEl.classList.add('hidden');
  // música (vista previa aproximada; el ducking real se aplica en el render)
  for (const mu of D().music) {
    const a = S.musicEls[mu.id]; if (!a) continue;
    const len = (mu.out ?? S.media[mu.media_id]?.duration ?? 0) - (mu.in || 0);
    const inside = t >= mu.start && t < mu.start + len;
    if (S.playing && inside) {
      const want = (mu.in || 0) + (t - mu.start);
      if (Math.abs(a.currentTime - want) > 0.35) a.currentTime = want;
      a.volume = clamp(mu.volume, 0, 1); a.playbackRate = rate();
      if (a.paused) a.play().catch(() => { });
    } else if (!a.paused) a.pause();
  }
  // cabezal
  $('#playhead').style.left = (70 + t * S.pxs) + 'px';
  if (S.playing) {
    const sc = $('#tlScroll'), x = 70 + t * S.pxs;
    if (x > sc.scrollLeft + sc.clientWidth - 60 || x < sc.scrollLeft + 70) sc.scrollLeft = x - 200;
  }
  highlightWord();
}

function syncLayers() {
  const box = $('#layersBox');
  const keep = new Set();
  for (const ov of D().overlays) {
    keep.add(ov.id);
    let img = S.layerEls[ov.id];
    if (!img) {
      img = el('div', { class: 'layer' }, el('img', { draggable: 'false', style: { width: '100%', display: 'block' } }));
      img.dataset.id = ov.id;
      img.addEventListener('pointerdown', e => layerPointer(e, ov.id));
      box.appendChild(img); S.layerEls[ov.id] = img;
    }
    const url = ov.asset_id ? (S.assetUrl[ov.asset_id] || `/api/assets/${ov.asset_id}`) : null;
    const im = img.querySelector('img');
    if (url && im.dataset.src !== url) { im.src = url; im.dataset.src = url; }
    img.style.left = ov.x * 100 + '%'; img.style.top = ov.y * 100 + '%'; img.style.width = ov.w * 100 + '%';
    img.style.opacity = ov.opacity ?? 1;
    const selected = S.sel && S.sel.id === ov.id;
    img.classList.toggle('sel', selected); img.classList.toggle('editable', canEdit());
    img.classList.toggle('pending', !ov.asset_id);
    let rs = img.querySelector('.rs');
    if (selected && canEdit() && !rs) { rs = el('div', { class: 'rs' }); img.appendChild(rs); }
    if (!selected && rs) rs.remove();
  }
  for (const id of Object.keys(S.layerEls)) if (!keep.has(id)) { S.layerEls[id].remove(); delete S.layerEls[id]; }
  // logo
  let logo = $('#logoPrev');
  const b = D().brand;
  if (b.logo_asset_id) {
    if (!logo) { logo = el('img', { id: 'logoPrev', class: 'layer', style: { pointerEvents: 'none' } }); box.appendChild(logo); }
    logo.src = S.assetUrl[b.logo_asset_id] || `/api/assets/${b.logo_asset_id}`;
    const m = 2.5;
    Object.assign(logo.style, { width: b.size * 100 + '%', opacity: b.opacity, left: '', right: '', top: '', bottom: '' });
    logo.style[b.position.includes('l') ? 'left' : 'right'] = m + '%';
    logo.style[b.position.includes('t') ? 'top' : 'bottom'] = m * W0() / H0() + '%';
  } else if (logo) logo.remove();
  updateStage();
}

function layerPointer(e, id) {
  if (S.playing) pause();
  const ov = D().overlays.find(o => o.id === id);
  if (!ov) return;
  if (!S.sel || S.sel.id !== id) { select('overlay', id); return; }
  if (!canEdit()) return;
  e.preventDefault();
  const st = $('#stage').getBoundingClientRect();
  const resizing = e.target.classList.contains('rs');
  const x0 = e.clientX, y0 = e.clientY, ox = ov.x, oy = ov.y, ow = ov.w;
  const before = snapshot(); let moved = false;
  const node = S.layerEls[id];
  const move = ev => {
    moved = true;
    const dx = (ev.clientX - x0) / st.width, dy = (ev.clientY - y0) / st.height;
    if (resizing) { ov.w = clamp(ow + dx, 0.03, 1); node.style.width = ov.w * 100 + '%'; }
    else { ov.x = clamp(ox + dx, -0.2, 0.98); ov.y = clamp(oy + dy, -0.2, 0.98); node.style.left = ov.x * 100 + '%'; node.style.top = ov.y * 100 + '%'; }
  };
  const up = () => {
    window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
    if (!moved) return;
    S.undo.push(before); S.redo = [];
    if (resizing) rasterize(ov);
    afterChange(true); renderInspector();
  };
  window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
}

/* Rasteriza la capa y la sube como PNG */
const _rasterQueue = {};
async function rasterize(ov) {
  const token = (_rasterQueue[ov.id] || 0) + 1; _rasterQueue[ov.id] = token;
  try {
    const { blob, w, h } = await rasterizeLayer(ov, W0(), H0());
    if (_rasterQueue[ov.id] !== token) return;
    const local = URL.createObjectURL(blob);
    const node = S.layerEls[ov.id]; if (node) node.querySelector('img').src = local;
    const r = await API.req('POST', `/api/projects/${PID}/assets`, blob);
    if (_rasterQueue[ov.id] !== token) return;
    S.assetUrl[r.id] = local;
    const cur = D().overlays.find(o => o.id === ov.id);
    if (cur) { cur.asset_id = r.id; cur.h = (h / w) * cur.w * W0() / H0(); afterChange(true); syncLayers(); }
    ov._err = null;
  } catch (e) {
    ov._err = e.message; toast(e.message, true);
    const box = $('#layerErr'); if (box) box.textContent = e.message;
  }
}
const rasterizeDebounced = {};
function rasterizeSoon(ov) {
  clearTimeout(rasterizeDebounced[ov.id]);
  rasterizeDebounced[ov.id] = setTimeout(() => rasterize(D().overlays.find(o => o.id === ov.id) || ov), 450);
}

/* ================================================================= Selección */
function select(kind, id, { seekTo = false } = {}) {
  S.sel = kind ? { kind, id } : null;
  if (seekTo && kind) {
    const it = findItem(kind, id);
    if (it && 'start' in it) seek(it.start + 0.01);
    else if (it && 't' in it) seek(it.t);
    else if (kind === 'clip') { const sg = segments().find(s => s.clip.id === id); if (sg) seek(sg.start); }
  }
  renderTimeline(); renderInspector(); syncLayers();
}

/* ================================================================= Línea de tiempo */
function tlX(t) { return 70 + t * S.pxs; }
function renderTimeline() {
  const T = duration();
  const inner = $('#tlInner');
  const width = Math.max($('#tlScroll').clientWidth, 70 + (T + 30) * S.pxs);
  inner.style.width = width + 'px';
  // regla
  const ruler = $('#tlRuler'); ruler.innerHTML = '';
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const major = steps.find(s => s * S.pxs >= 80) || 600;
  const minor = steps.slice().reverse().find(s => s * S.pxs >= 12 && s < major) || major / 2;
  const maxT = (width - 70) / S.pxs;
  for (let t = 0; t <= maxT; t += minor) {
    const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
    ruler.appendChild(el('div', { class: 'tick' + (isMajor ? '' : ' minor'), style: { left: (t * S.pxs) + 'px' } }, isMajor ? fmtTime(t) : ''));
  }
  const tracks = {}; $$('.tl-track').forEach(tr => { tr.querySelectorAll('.tl-item,.tl-mark').forEach(n => n.remove()); tracks[tr.dataset.track] = tr; });
  // clips
  for (const sg of segments()) {
    const c = sg.clip, m = S.media[c.media_id] || {};
    const d = el('div', { class: 'tl-item clip' + (m.kind === 'image' ? ' img' : '') + (isSel('clip', c.id) ? ' sel' : ''), title: m.filename || '' },
      el('span', { class: 'nm' }, (m.filename || 'medio') + (c.mute ? ' 🔇' : '')),
      el('div', { class: 'h l' }), el('div', { class: 'h r' }));
    Object.assign(d.style, { left: (sg.start * S.pxs) + 'px', width: Math.max(4, clipDur(c) * S.pxs) + 'px' });
    if (m.has_waveform && m.duration) {
      d.style.backgroundImage = `url(/api/media/${m.id}/waveform)`;
      d.style.backgroundSize = `${m.duration * S.pxs}px 55%`;
      d.style.backgroundPosition = `${-c.in * S.pxs}px 100%`;
    }
    d.addEventListener('pointerdown', e => dragClip(e, c.id));
    tracks.clips.appendChild(d);
  }
  // capas (con carriles cuando se superponen)
  laneItems(D().overlays).forEach(({ it, lane, lanes }) => tracks.overlays.appendChild(spanItem('overlay', it, LAYER_TYPES[it.type]?.label || 'Capa', lane, lanes)));
  laneItems(D().zooms).forEach(({ it, lane, lanes }) => tracks.zooms.appendChild(spanItem('zoom', it, `Zoom ${it.scale.toFixed(1)}×`, lane, lanes)));
  for (const mu of D().music) {
    const m = S.media[mu.media_id] || {};
    const len = (mu.out ?? m.duration ?? 0) - (mu.in || 0);
    const it = { id: mu.id, start: mu.start, end: mu.start + len };
    const d = spanItem('music', it, (m.filename || 'música') + ` · ${Math.round(mu.volume * 100)}%`, 0, 1);
    if (m.has_waveform && m.duration) { d.style.backgroundImage = `url(/api/media/${m.id}/waveform)`; d.style.backgroundSize = `${m.duration * S.pxs}px 70%`; d.style.backgroundPosition = `${-(mu.in || 0) * S.pxs}px 100%`; }
    tracks.music.appendChild(d);
  }
  // marcas
  for (const ch of D().chapters) tracks.marks.appendChild(markEl('chapter', ch.id, ch.t, 'Capítulo: ' + ch.title));
  for (const q of D().quizzes) tracks.marks.appendChild(markEl('quiz', q.id, q.t, 'Pregunta: ' + q.question));
  for (const c of S.comments) tracks.marks.appendChild(markEl('comment', c.id, c.t, `${c.author || c.username}: ${c.text}`, c.resolved));
  $('#playhead').style.left = tlX(S.t) + 'px';
}
function isSel(kind, id) { return S.sel && S.sel.kind === kind && S.sel.id === id; }
function laneItems(items) {
  const sorted = [...items].sort((a, b) => a.start - b.start), ends = [], res = [];
  for (const it of sorted) {
    let lane = ends.findIndex(e => e <= it.start + 1e-6);
    if (lane < 0) { lane = ends.length; ends.push(it.end); } else ends[lane] = it.end;
    res.push({ it, lane });
  }
  const lanes = Math.max(1, ends.length);
  return res.map(r => ({ ...r, lanes }));
}
function spanItem(kind, it, label, lane, lanes) {
  const d = el('div', { class: `tl-item ${kind}` + (isSel(kind, it.id) ? ' sel' : ''), title: label }, label, el('div', { class: 'h l' }), el('div', { class: 'h r' }));
  const h = 28 / lanes;
  Object.assign(d.style, { left: it.start * S.pxs + 'px', width: Math.max(6, (it.end - it.start) * S.pxs) + 'px', top: (3 + lane * h) + 'px', bottom: 'auto', height: (h - 1) + 'px' });
  d.addEventListener('pointerdown', e => dragSpan(e, kind, it.id));
  return d;
}
function markEl(kind, id, t, title, done) {
  const d = el('div', { class: `tl-mark ${kind}` + (done ? ' done' : ''), title });
  d.style.left = (t * S.pxs) + 'px';
  d.addEventListener('pointerdown', e => {
    e.stopPropagation();
    if (kind === 'comment') { pause(); seek(t); openTab('comments'); return; }
    select(kind, id, { seekTo: true }); if (!canEdit()) return;
    const x0 = e.clientX, item = findItem(kind, id), t0 = item.t, before = snapshot(); let moved = false;
    const mv = ev => { moved = true; item.t = clamp(t0 + (ev.clientX - x0) / S.pxs, 0, duration()); d.style.left = item.t * S.pxs + 'px'; };
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); if (moved) { S.undo.push(before); S.redo = []; afterChange(false); } };
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
  });
  return d;
}

function snapT(t, exclude) {
  const cands = [S.t, 0, duration(), ...segments().map(s => s.end)];
  const thr = 6 / S.pxs;
  for (const c of cands) if (Math.abs(c - t) < thr && c !== exclude) return c;
  return t;
}

function dragClip(e, id) {
  e.stopPropagation();
  if (S.playing) pause();
  if (!isSel('clip', id)) select('clip', id);
  if (!canEdit()) return;
  const handle = e.target.classList.contains('h') ? (e.target.classList.contains('l') ? 'l' : 'r') : null;
  const c = D().clips.find(x => x.id === id), m = S.media[c.media_id];
  const x0 = e.clientX, in0 = c.in, out0 = c.out, before = snapshot();
  let moved = false, dropIdx = null;
  const ind = el('div', { class: 'tl-drop-ind hidden' }); $('.tl-main').appendChild(ind);
  const maxOut = m.kind === 'image' ? 3600 : m.duration;
  const mv = ev => {
    const dt = (ev.clientX - x0) / S.pxs;
    if (Math.abs(ev.clientX - x0) > 3) moved = true;
    if (!moved) return;
    if (handle === 'l') { c.in = clamp(in0 + dt, 0, c.out - 0.1); renderTimeline(); }
    else if (handle === 'r') { c.out = clamp(out0 + dt, c.in + 0.1, maxOut); renderTimeline(); }
    else {
      const x = ev.clientX - $('.tl-main').getBoundingClientRect().left - 70;
      const segs = segments().filter(s => s.clip.id !== id);
      let acc = 0; dropIdx = segs.length;
      for (let i = 0; i < segs.length; i++) { const mid = (acc + clipDur(segs[i].clip) / 2) * S.pxs; if (x < mid) { dropIdx = i; break; } acc += clipDur(segs[i].clip); }
      let px = 0; for (let i = 0; i < dropIdx; i++) px += clipDur(segs[i].clip) * S.pxs;
      ind.classList.remove('hidden'); ind.style.left = (px + 70) + 'px';
    }
  };
  const up = () => {
    window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); ind.remove();
    if (!moved) return;
    if (!handle && dropIdx !== null) {
      const arr = D().clips, from = arr.findIndex(x => x.id === id);
      const [it] = arr.splice(from, 1); arr.splice(dropIdx, 0, it);
    }
    S.undo.push(before); S.redo = []; afterChange(false); seek(S.t);
  };
  window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
}

function dragSpan(e, kind, id) {
  e.stopPropagation();
  if (S.playing) pause();
  if (!isSel(kind, id)) select(kind, id);
  if (!canEdit()) return;
  const handle = e.target.classList.contains('h') ? (e.target.classList.contains('l') ? 'l' : 'r') : null;
  const it = findItem(kind, id), before = snapshot(), x0 = e.clientX;
  let moved = false;
  if (kind === 'music') {
    const m = S.media[it.media_id]; const s0 = it.start, in0 = it.in || 0, out0 = it.out ?? m.duration;
    const mv = ev => {
      const dt = (ev.clientX - x0) / S.pxs; if (Math.abs(ev.clientX - x0) > 3) moved = true; if (!moved) return;
      if (handle === 'l') { const ni = clamp(in0 + dt, 0, out0 - 0.5); it.start = Math.max(0, s0 + (ni - in0)); it.in = ni; }
      else if (handle === 'r') it.out = clamp(out0 + dt, in0 + 0.5, m.duration);
      else it.start = Math.max(0, snapT(s0 + dt));
      renderTimeline();
    };
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); if (moved) { S.undo.push(before); S.redo = []; afterChange(false); } };
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
    return;
  }
  const s0 = it.start, e0 = it.end;
  const mv = ev => {
    const dt = (ev.clientX - x0) / S.pxs; if (Math.abs(ev.clientX - x0) > 3) moved = true; if (!moved) return;
    if (handle === 'l') it.start = clamp(snapT(s0 + dt), 0, it.end - 0.2);
    else if (handle === 'r') it.end = Math.max(it.start + 0.2, snapT(e0 + dt));
    else { const ns = Math.max(0, snapT(s0 + dt)); it.start = ns; it.end = ns + (e0 - s0); }
    renderTimeline(); updateStage();
  };
  const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); if (moved) { S.undo.push(before); S.redo = []; afterChange(false); } };
  window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
}

function scrub(e) {
  const r = $('#tlInner').getBoundingClientRect();
  const go = ev => { const t = (ev.clientX - r.left - 70) / S.pxs; seek(clamp(t, 0, duration())); };
  if (S.playing) pause();
  go(e);
  const up = () => { window.removeEventListener('pointermove', go); window.removeEventListener('pointerup', up); };
  window.addEventListener('pointermove', go); window.addEventListener('pointerup', up);
}
$('#tlRuler').addEventListener('pointerdown', scrub);
$$('.tl-track').forEach(tr => tr.addEventListener('pointerdown', e => { if (e.target === tr) { select(null); scrub(e); } }));
$('#tlZoom').addEventListener('input', e => { S.pxs = +e.target.value; renderTimeline(); });
$('#tlFit').onclick = () => { const T = duration() || 30; S.pxs = clamp(($('#tlScroll').clientWidth - 120) / T, 2, 200); $('#tlZoom').value = S.pxs; renderTimeline(); };
$('#tlScroll').addEventListener('wheel', e => {
  if (e.ctrlKey || e.metaKey) { e.preventDefault(); S.pxs = clamp(S.pxs * (e.deltaY < 0 ? 1.15 : 0.87), 2, 200); $('#tlZoom').value = S.pxs; renderTimeline(); }
}, { passive: false });

/* ================================================================= Inspector */
function field(label, input, hint) { return el('label', { class: 'f' }, el('span', {}, label), input, hint ? el('span', { class: 'hint' }, hint) : null); }
function num(val, step, onchange, attrs = {}) {
  const i = el('input', { type: 'number', value: (+val).toFixed(step < 0.1 ? 2 : 1).replace(/\.0+$/, ''), step, ...attrs });
  i.addEventListener('change', () => { const v = parseFloat(i.value); if (!isNaN(v)) onchange(v); });
  return i;
}
function timeIn(val, onchange) {
  const i = el('input', { type: 'text', value: fmtTime(val, true), class: 'mono' });
  i.addEventListener('change', () => { const v = parseTime(i.value); if (!isNaN(v)) onchange(v); else i.value = fmtTime(val, true); });
  return i;
}
function txt(val, onchange, area = false, attrs = {}) {
  const i = el(area ? 'textarea' : 'input', area ? { rows: 3, ...attrs } : { type: 'text', ...attrs });
  i.value = val ?? '';
  i.addEventListener('input', () => onchange(i.value, false));
  i.addEventListener('change', () => onchange(i.value, true));
  return i;
}
function chk(label, val, onchange) { const i = el('input', { type: 'checkbox' }); i.checked = !!val; i.onchange = () => onchange(i.checked); return el('label', { class: 'chk' }, i, label); }
function sel(opts, val, onchange) {
  const s = el('select', {}, ...opts.map(([v, l]) => el('option', { value: v }, l))); s.value = val; s.onchange = () => onchange(s.value); return s;
}
function color(val, onchange) { const i = el('input', { type: 'color', value: val }); i.addEventListener('input', () => onchange(i.value, false)); i.addEventListener('change', () => onchange(i.value, true)); return i; }
function range(min, max, step, val, onchange, fmt) {
  const out = el('b', {}, fmt ? fmt(val) : val);
  const i = el('input', { type: 'range', min, max, step, value: val });
  i.addEventListener('input', () => { out.textContent = fmt ? fmt(+i.value) : i.value; onchange(+i.value, false); });
  i.addEventListener('change', () => onchange(+i.value, true));
  return [i, out];
}

function renderInspector() {
  const box = $('#inspector'); box.innerHTML = '';
  const it = S.sel && findItem(S.sel.kind, S.sel.id);
  if (!it || S.sel.kind === 'comment') {
    box.append(el('div', { class: 'insp-empty muted' }, el('h4', { style: { color: 'var(--text)' } }, 'Inspector'),
      el('p', { class: 'small' }, 'Seleccione un clip, capa, zoom o música en la línea de tiempo para editar sus propiedades.'),
      statsCard()));
    return;
  }
  const sec = el('div', { class: 'insp-sec' });
  const upd = (fn, re = false) => { change(fn, { silent: !re }); if (re) renderInspector(); };
  const k = S.sel.kind;
  if (k === 'clip') {
    const m = S.media[it.media_id] || {};
    sec.append(el('h4', {}, 'Clip · ' + (m.filename || '')),
      el('div', { class: 'grid2' },
        field('Entrada (s)', num(it.in, 0.01, v => upd(d => { const c = d.clips.find(x => x.id === it.id); c.in = clamp(v, 0, c.out - 0.05); }, true), { min: 0 })),
        field('Salida (s)', num(it.out, 0.01, v => upd(d => { const c = d.clips.find(x => x.id === it.id); c.out = clamp(v, c.in + 0.05, m.kind === 'image' ? 3600 : m.duration); }, true)))),
      el('div', { class: 'hint' }, `Duración en la secuencia: ${fmtTime(clipDur(it), true)}` + (m.kind !== 'image' ? ` · Original: ${fmtTime(m.duration)}` : '')),
      field('Encuadre', sel([['contain', 'Encajar (barras si la proporción difiere)'], ['cover', 'Llenar el cuadro (recorta)']], it.fit || 'contain', v => upd(d => { d.clips.find(x => x.id === it.id).fit = v; }, true))));
    if (m.kind === 'video' && m.has_audio) {
      const [r, o] = range(-20, 12, 1, it.gain_db || 0, (v, fin) => liveEdit(() => { it.gain_db = v; }, fin), v => v + ' dB');
      sec.append(chk('Silenciar audio de este clip', it.mute, v => upd(d => { d.clips.find(x => x.id === it.id).mute = v; }, true)),
        el('label', { class: 'f' }, el('span', {}, 'Ganancia: ', o), r));
    }
    sec.append(el('div', { class: 'row' }, el('button', { class: 'btn sm', onclick: () => splitAt(S.t) }, '✂ Dividir en cabezal'), el('button', { class: 'btn sm danger', onclick: deleteSelected }, 'Eliminar')));
  } else if (k === 'overlay') {
    const def = LAYER_TYPES[it.type];
    const p = it.params;
    const setP = (key, re) => (v, fin = true) => {
      liveEdit(() => { it.params[key] = v; }, fin);
      rasterizeSoon(it);
      if (re) renderInspector();
    };
    sec.append(el('h4', {}, 'Capa · ' + def.label));
    switch (it.type) {
      case 'text':
        sec.append(field('Texto', txt(p.text, setP('text'), true)),
          el('div', { class: 'grid2' }, field('Tamaño (% alto)', num(p.size, 0.5, setP('size'), { min: 1, max: 20 })), field('Alineación', sel([['left', 'Izquierda'], ['center', 'Centro'], ['right', 'Derecha']], p.align, setP('align')))),
          el('div', { class: 'row' }, field('Color', color(p.color, setP('color'))), field('Fondo', color(p.bg, setP('bg'))), el('div', { class: 'grow' }, field('Opacidad fondo', range(0, 1, 0.05, p.bgOpacity, setP('bgOpacity'))[0]))),
          chk('Negrita', p.bold, setP('bold')));
        break;
      case 'lower':
        sec.append(field('Nombre', txt(p.title, setP('title'))), field('Cargo / unidad', txt(p.subtitle, setP('subtitle'))));
        break;
      case 'latex': case 'chem':
        sec.append(field(it.type === 'chem' ? 'Fórmula o reacción (sintaxis mhchem)' : 'Expresión LaTeX', txt(p.tex, setP('tex'), true, { class: 'mono', spellcheck: 'false' }),
          it.type === 'chem' ? 'Ej.: CH3CH2OH, Fe^{3+}, 2H2 + O2 -> 2H2O, A <=> B, ^{14}_{6}C' : 'Ej.: \\Delta G = \\Delta H - T\\Delta S, \\frac{d[A]}{dt} = -k[A]'),
          el('div', { class: 'row' }, field('Color', color(p.color, setP('color'))), field('Fondo', color(p.bg, setP('bg'))), el('div', { class: 'grow' }, field('Opacidad fondo', range(0, 1, 0.05, p.bgOpacity, setP('bgOpacity'))[0]))));
        sec.append(el('div', { class: 'row wrap', style: { gap: '4px' } }, ...(it.type === 'chem'
          ? [['Combustión', 'CH4 + 2O2 -> CO2 + 2H2O'], ['Equilibrio', 'N2 + 3H2 <=> 2NH3'], ['Ácido', 'HCl_{(ac)} -> H+ + Cl-'], ['Isótopo', '^{14}_{6}C']]
          : [['Gases', 'PV = nRT'], ['Gibbs', '\\Delta G = \\Delta H - T\\Delta S'], ['Arrhenius', 'k = A\\,e^{-E_a/RT}'], ['Nernst', 'E = E^\\circ - \\frac{RT}{nF}\\ln Q']]
        ).map(([l, v]) => el('button', { class: 'btn sm', onclick: () => { change(() => { it.params.tex = v; }, { silent: true }); rasterize(it); renderInspector(); } }, l))));
        break;
      case 'molecule':
        sec.append(field('SMILES', txt(p.smiles, setP('smiles'), false, { class: 'mono', spellcheck: 'false' }), 'Ej.: CCO (etanol), c1ccccc1 (benceno), CC(=O)O (ác. acético)'),
          field('Leyenda', txt(p.legend, setP('legend'))),
          chk('Tarjeta blanca de fondo', p.card, setP('card')), chk('Mostrar hidrógenos explícitos', p.explicitH, setP('explicitH')));
        sec.append(el('div', { class: 'row wrap', style: { gap: '4px' } }, ...[['Benceno', 'c1ccccc1'], ['Glucosa', 'OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O'], ['Cafeína', 'Cn1cnc2c1c(=O)n(C)c(=O)n2C'], ['Aspirina', 'CC(=O)Oc1ccccc1C(=O)O']]
          .map(([l, v]) => el('button', { class: 'btn sm', onclick: () => { change(() => { it.params.smiles = v; it.params.legend = l; }, { silent: true }); rasterize(it); renderInspector(); } }, l))));
        break;
      case 'arrow':
        sec.append(el('div', { class: 'grid2' }, field('Color', color(p.color, setP('color'))), field('Grosor', num(p.thickness, 1, setP('thickness'), { min: 2, max: 40 }))),
          field('Ángulo (°)', range(-180, 180, 5, p.angle, setP('angle'))[0]));
        break;
      case 'box':
        sec.append(el('div', { class: 'grid2' }, field('Color', color(p.color, setP('color'))), field('Grosor', num(p.thickness, 1, setP('thickness'), { min: 2, max: 30 }))),
          el('div', { class: 'grid2' }, field('Forma', sel([['rect', 'Rectángulo'], ['ellipse', 'Elipse']], p.shape, setP('shape'))), field('Proporción (ancho/alto)', num(p.aspect || 1.6, 0.1, setP('aspect'), { min: 0.2, max: 8 }))),
          field('Relleno translúcido', range(0, 0.6, 0.05, p.fill || 0, setP('fill'))[0]));
        break;
      case 'image': {
        const f = el('input', { type: 'file', accept: 'image/*', hidden: true });
        f.onchange = () => { const file = f.files[0]; if (!file) return; const rd = new FileReader(); rd.onload = () => { change(() => { it.params.src = rd.result; it.params.name = file.name; }, { silent: true }); rasterize(it); renderInspector(); }; rd.readAsDataURL(file); };
        sec.append(el('label', { class: 'btn sm' }, p.name ? 'Cambiar imagen' : 'Elegir imagen', f), p.name ? el('span', { class: 'hint' }, p.name) : null);
        break;
      }
    }
    sec.append(el('div', { id: 'layerErr', class: 'hint', style: { color: 'var(--red-hi)' } }, it._err || ''));
    sec.append(el('div', { class: 'grid2' },
      field('Inicio', timeIn(it.start, v => upd(d => { const o = d.overlays.find(x => x.id === it.id); o.start = clamp(v, 0, o.end - 0.1); }, true))),
      field('Fin', timeIn(it.end, v => upd(d => { const o = d.overlays.find(x => x.id === it.id); o.end = Math.max(o.start + 0.1, v); }, true)))),
      el('div', { class: 'grid3' },
        field('X %', num(it.x * 100, 0.5, v => upd(d => { d.overlays.find(x => x.id === it.id).x = v / 100; }, true))),
        field('Y %', num(it.y * 100, 0.5, v => upd(d => { d.overlays.find(x => x.id === it.id).y = v / 100; }, true))),
        field('Ancho %', num(it.w * 100, 0.5, v => { upd(d => { d.overlays.find(x => x.id === it.id).w = clamp(v / 100, 0.02, 1); }, true); rasterize(D().overlays.find(x => x.id === it.id)); }))),
      el('div', { class: 'row wrap', style: { gap: '4px' } },
        el('button', { class: 'btn sm', onclick: () => upd(d => { const o = d.overlays.find(x => x.id === it.id); o.x = (1 - o.w) / 2; }, true) }, 'Centrar H'),
        el('button', { class: 'btn sm', onclick: () => upd(d => { const o = d.overlays.find(x => x.id === it.id); o.y = (1 - (o.h || 0.1)) / 2; }, true) }, 'Centrar V'),
        el('button', { class: 'btn sm', onclick: () => upd(d => { const o = d.overlays.find(x => x.id === it.id); o.y = 1 - (o.h || 0.1) - 0.06; }, true) }, 'Abajo')),
      el('label', { class: 'f' }, el('span', {}, 'Opacidad'), range(0.1, 1, 0.05, it.opacity ?? 1, (v, fin) => { liveEdit(() => { it.opacity = v; }, fin); S.layerEls[it.id].style.opacity = v; })[0]),
      field('Fundido de entrada/salida (s)', num(it.fade ?? 0.25, 0.05, v => upd(d => { d.overlays.find(x => x.id === it.id).fade = clamp(v, 0, 2); }), { min: 0, max: 2 })),
      el('div', { class: 'row' },
        el('button', { class: 'btn sm', onclick: () => { const copy = JSON.parse(JSON.stringify(it)); copy.id = uid(); copy.start = S.t; copy.end = S.t + (it.end - it.start); change(d => d.overlays.push(copy)); select('overlay', copy.id); } }, 'Duplicar en cabezal'),
        el('button', { class: 'btn sm danger', onclick: deleteSelected }, 'Eliminar')));
  } else if (k === 'zoom') {
    sec.append(el('h4', {}, 'Zoom animado'),
      field('Ampliación', range(1.1, 4, 0.1, it.scale, (v, fin) => { liveEdit(() => { it.scale = v; }, fin); updateStage(); renderTimeline(); }, v => v.toFixed(1) + '×')[0]),
      el('button', { class: 'btn sm', onclick: () => pickZoomPoint(it) }, '⌖ Elegir punto en la vista previa'),
      el('div', { class: 'grid2' },
        field('Centro X %', num(it.x * 100, 1, v => upd(d => { d.zooms.find(x => x.id === it.id).x = clamp(v / 100, 0, 1); }, true))),
        field('Centro Y %', num(it.y * 100, 1, v => upd(d => { d.zooms.find(x => x.id === it.id).y = clamp(v / 100, 0, 1); }, true)))),
      el('div', { class: 'grid2' },
        field('Inicio', timeIn(it.start, v => upd(d => { const z = d.zooms.find(x => x.id === it.id); z.start = clamp(v, 0, z.end - 0.5); }, true))),
        field('Fin', timeIn(it.end, v => upd(d => { const z = d.zooms.find(x => x.id === it.id); z.end = Math.max(z.start + 0.5, v); }, true)))),
      el('div', { class: 'hint' }, 'La transición de entrada y salida es suave (0,45 s).'),
      el('button', { class: 'btn sm danger', onclick: deleteSelected }, 'Eliminar zoom'));
  } else if (k === 'music') {
    const m = S.media[it.media_id] || {};
    sec.append(el('h4', {}, 'Música · ' + (m.filename || '')),
      el('label', { class: 'f' }, el('span', {}, 'Volumen'), range(0, 1, 0.01, it.volume, (v, fin) => { liveEdit(() => { it.volume = v; }, fin); }, v => Math.round(v * 100) + '%')[0]),
      chk('Bajar automáticamente cuando hay voz (ducking)', it.duck !== false, v => upd(d => { d.music.find(x => x.id === it.id).duck = v; })),
      el('div', { class: 'grid2' },
        field('Empieza en', timeIn(it.start, v => upd(d => { d.music.find(x => x.id === it.id).start = Math.max(0, v); }, true))),
        field('Desde (s del audio)', num(it.in || 0, 0.1, v => upd(d => { const mu = d.music.find(x => x.id === it.id); mu.in = clamp(v, 0, (mu.out ?? m.duration) - 0.5); }, true)))),
      el('div', { class: 'hint' }, 'Fundidos de entrada y salida automáticos. La vista previa no reproduce el ducking; el render sí.'),
      el('button', { class: 'btn sm danger', onclick: deleteSelected }, 'Quitar música'));
  } else if (k === 'chapter' || k === 'quiz') { openTab('marks'); sec.append(el('h4', {}, k === 'chapter' ? 'Capítulo' : 'Pregunta'), el('p', { class: 'hint' }, 'Edítelo en la pestaña «Lección».')); }
  box.append(sec);
  if (!canEdit()) box.querySelectorAll('input,select,textarea,button').forEach(n => n.disabled = true);
}

function statsCard() {
  const d = D();
  const tx = Object.keys(S.transcripts).length;
  const items = [['Duración', fmtTime(duration())], ['Clips', d.clips.length], ['Capas', d.overlays.length], ['Zooms', d.zooms.length],
    ['Capítulos', d.chapters.length], ['Preguntas', d.quizzes.length], ['Palabras en subtítulos', S.words.length], ['Medios transcritos', tx],
    ['Lienzo', `${W0()}×${H0()} · ${FPS()} fps`]];
  return el('div', { class: 'card proj-stats' }, ...items.flatMap(([a, b]) => [el('span', {}, a), el('b', {}, String(b))]));
}

function pickZoomPoint(z) {
  const ov = $('#zoomPick'); ov.classList.remove('hidden');
  const prev = $('#zoomer').style.transform; $('#zoomer').style.transform = '';
  ov.onclick = e => {
    const r = $('#stage').getBoundingClientRect();
    ov.classList.add('hidden'); $('#zoomer').style.transform = prev;
    change(d => { const zz = d.zooms.find(x => x.id === z.id); zz.x = clamp((e.clientX - r.left) / r.width, 0, 1); zz.y = clamp((e.clientY - r.top) / r.height, 0, 1); });
    seek(z.start + Math.min(1, (z.end - z.start) / 2));
  };
}

/* ================================================================= Panel: medios */
function renderMedia() {
  const box = $('#mediaList'); box.innerHTML = '';
  const list = Object.values(S.media).sort((a, b) => a.created_at - b.created_at);
  if (!list.length) box.append(el('p', { class: 'muted small' }, 'Aún no hay medios en este proyecto.'));
  for (const m of list) {
    const job = S.jobs.find(j => j.media_id === m.id && (j.status === 'running' || j.status === 'queued'));
    const thumb = el('div', { class: 'thumb' });
    if (m.status === 'ready') {
      if (m.kind === 'video') thumb.append(el('video', { src: proxyUrl(m.id) + '#t=1', preload: 'metadata', muted: true }));
      else if (m.kind === 'image') thumb.style.backgroundImage = `url(${proxyUrl(m.id)})`;
      else thumb.textContent = '♪ audio';
    } else thumb.textContent = m.status === 'error' ? '⚠' : '…';
    const tx = S.txMeta[m.id];
    let status;
    if (m.status === 'processing') status = el('span', { class: 'badge warn' }, job && job.type === 'ingest' ? `Procesando ${Math.round(job.progress * 100)}%` : 'En cola');
    else if (m.status === 'error') status = el('span', { class: 'badge err' }, 'Error');
    else if (job && job.type === 'transcribe') status = el('span', { class: 'badge warn' }, `Transcribiendo ${Math.round(job.progress * 100)}%`);
    else if (tx) status = el('span', { class: 'badge ok', title: 'Idioma: ' + tx.language }, 'Transcrito' + (tx.translated ? ' + EN' : ''));
    else status = null;
    const dur = m.kind === 'image' ? 'Imagen' : fmtTime(m.duration);
    const actions = el('div', { class: 'row', style: { gap: '2px' } });
    if (canEdit() && m.status === 'ready') {
      actions.append(el('button', { class: 'btn sm icon', title: m.kind === 'audio' ? 'Añadir como música' : 'Añadir en el cabezal', onclick: () => addMediaToTimeline(m) }, '+'));
      if (m.has_audio && m.kind !== 'image') actions.append(el('button', { class: 'btn sm icon', title: tx ? 'Volver a transcribir / traducir' : 'Transcribir', onclick: () => transcribeDialog(m) }, 'Tx'));
    }
    if (canEdit()) actions.append(el('button', { class: 'btn sm icon ghost', title: 'Eliminar medio', onclick: async () => {
      const used = D().clips.some(c => c.media_id === m.id) || D().music.some(x => x.media_id === m.id);
      if (!confirm(`¿Eliminar «${m.filename}»?` + (used ? '\nSe quitará también de la línea de tiempo.' : ''))) return;
      await API.del('/api/media/' + m.id);
      delete S.media[m.id]; delete S.transcripts[m.id];
      if (used) change(d => { d.clips = d.clips.filter(c => c.media_id !== m.id); d.music = d.music.filter(x => x.media_id !== m.id); });
      renderMedia();
    } }, '×'));
    const item = el('div', { class: 'mitem', ondblclick: () => canEdit() && addMediaToTimeline(m) }, thumb,
      el('div', { style: { minWidth: 0 } }, el('div', { class: 'nm', title: m.filename }, m.filename),
        el('div', { class: 'sub row', style: { gap: '6px' } }, el('span', {}, dur + (m.width ? ` · ${m.width}×${m.height}` : '')), status)),
      actions);
    box.append(item);
  }
}

function transcribeDialog(m) {
  const lang = prompt('Idioma del audio (es, en, pt, fr… o «auto»). Escriba «es+en» para transcribir en español y traducir al inglés:', 'es');
  if (!lang) return;
  const translate = lang.includes('+en');
  API.post(`/api/media/${m.id}/transcribe`, { language: lang.replace('+en', '').trim() || 'es', translate })
    .then(() => { toast('Transcripción en cola'); pollJobs(); }).catch(e => toast(e.message, true));
}

async function uploadFiles(files) {
  const auto = $('#autoTx').checked ? 1 : 0;
  for (const f of files) {
    const row = el('div', { class: 'job' }, el('div', { class: 't' }, el('span', { class: 'grow' }, 'Subiendo ' + f.name)), el('div', { class: 'progress' }, el('i')));
    showJobsBox(); $('#jobsBox').prepend(row);
    try {
      const r = await API.uploadFile(PID, f, f.name, { autoTranscribe: !!auto },
        p => row.querySelector('i').style.width = (p * 100) + '%');
      S.media[r.id] = { id: r.id, filename: f.name, kind: f.type.startsWith('audio') ? 'audio' : f.type.startsWith('image') ? 'image' : 'video', status: 'processing', duration: 0, created_at: Date.now() / 1000 };
      renderMedia();
    } catch (e) { toast(`${f.name}: ${e.message}`, true); }
    row.remove();
  }
  pollJobs();
}
$('#fileInput').onchange = e => { uploadFiles(Array.from(e.target.files)); e.target.value = ''; };
const drop = $('#drop');
['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => uploadFiles(Array.from(e.dataTransfer.files)));

$('#btnTitleCard').onclick = () => { const f = $('#titleForm'); f.title.value = S.name; $('#dlgTitle').showModal(); };
$('#dlgTitle').addEventListener('close', async e => {
  if (e.target.returnValue !== 'ok') return;
  const f = $('#titleForm');
  const blob = await renderTitleCard({ title: f.title.value, subtitle: f.subtitle.value, author: f.author.value, institution: f.institution.value }, W0(), H0());
  try {
    const r = await API.upload(`/api/projects/${PID}/media?filename=portada.png`, blob);
    S.media[r.id] = { id: r.id, filename: 'portada.png', kind: 'image', status: 'processing', duration: 0, created_at: Date.now() / 1000, _dur: +f.dur.value };
    S.pendingAdd.push({ mid: r.id, at: 'start', dur: +f.dur.value });
    renderMedia(); pollJobs(); toast('Portada creada; se añadirá al inicio en unos segundos');
  } catch (err) { toast(err.message, true); }
});

/* ================================================================= Panel: texto (edición por transcripción) */
function renderTranscript() {
  const box = $('#transcript'); box.innerHTML = '';
  if (!S.words.length) {
    const any = D().clips.length;
    box.append(el('p', { class: 'muted small' }, any ? 'Los clips de la línea de tiempo aún no tienen transcripción. Use el botón «Tx» de cada medio en «Medios».' : 'Añada clips a la línea de tiempo para ver aquí su transcripción.'));
    return;
  }
  const thr = parseFloat($('#silenceSec').value) || 0.8;
  const q = $('#txSearch').value.trim().toLowerCase();
  const frag = document.createDocumentFragment();
  let lastClip = null;
  S.words.forEach((w, i) => {
    if (w.clipId !== lastClip) {
      if (lastClip !== null) frag.append(el('span', { class: 'clip-sep' }, `— corte · ${fmtTime(w.s)} —`));
      lastClip = w.clipId;
    } else if (i > 0) {
      const gap = w.s - S.words[i - 1].e;
      if (gap >= thr) { const g = el('span', { class: 'gap', title: 'Clic para eliminar este silencio' }, `⋯ ${gap.toFixed(1)} s `); g.dataset.gap = i; frag.append(g); }
      else frag.append(' ');
    }
    const s = el('span', { class: 'w' + (S.fillers.includes(i) ? ' filler' : '') + (w.p < 0.5 ? ' lowconf' : '') + (q && w.w.toLowerCase().includes(q) ? ' hit' : '') }, w.w);
    s.dataset.i = i;
    frag.append(s);
  });
  box.append(frag);
  paintTxSel(); highlightWord(true);
}
let _curWord = -1;
function highlightWord(force) {
  if (!S.words.length) return;
  let lo = 0, hi = S.words.length - 1, idx = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (S.words[mid].s <= S.t) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
  if (idx >= 0 && S.t > S.words[idx].e + 0.3) idx = -1;
  if (idx === _curWord && !force) return;
  const box = $('#transcript');
  box.querySelector('.w.cur')?.classList.remove('cur');
  _curWord = idx;
  if (idx >= 0) {
    const n = box.querySelector(`.w[data-i="${idx}"]`);
    if (n) { n.classList.add('cur'); if (S.playing && !$('[data-panel="text"]').classList.contains('hidden')) { const r = n.getBoundingClientRect(), pr = box.parentElement.getBoundingClientRect(); if (r.top < pr.top + 60 || r.bottom > pr.bottom - 30) n.scrollIntoView({ block: 'center', behavior: 'smooth' }); } }
  }
}
function paintTxSel() {
  const box = $('#transcript');
  box.querySelectorAll('.w.sel').forEach(n => n.classList.remove('sel'));
  if (!S.txSel) return;
  const [a, b] = [Math.min(S.txSel.a, S.txSel.b), Math.max(S.txSel.a, S.txSel.b)];
  for (let i = a; i <= b; i++) box.querySelector(`.w[data-i="${i}"]`)?.classList.add('sel');
}
let _txDrag = false;
$('#transcript').addEventListener('pointerdown', e => {
  const g = e.target.closest('.gap');
  if (g && canEdit()) { const i = +g.dataset.gap; const a = S.words[i - 1].e + 0.12, b = S.words[i].s - 0.12; if (b > a) { cutRange(a, b); toast(`Silencio de ${(b - a).toFixed(1)} s eliminado`); } return; }
  const w = e.target.closest('.w'); if (!w) return;
  const i = +w.dataset.i;
  if (e.shiftKey && S.txSel) S.txSel.b = i; else S.txSel = { a: i, b: i };
  _txDrag = true; paintTxSel();
  if (S.playing) pause();
  seek(S.words[i].s + 0.001);
});
$('#transcript').addEventListener('pointerover', e => { if (!_txDrag) return; const w = e.target.closest('.w'); if (w && S.txSel) { S.txSel.b = +w.dataset.i; paintTxSel(); } });
window.addEventListener('pointerup', () => { _txDrag = false; });
$('#transcript').addEventListener('dblclick', async e => {
  const w = e.target.closest('.w'); if (!w || !canEdit()) return;
  const word = S.words[+w.dataset.i];
  const nv = prompt('Corregir palabra (afecta los subtítulos):', word.w);
  if (nv === null || !nv.trim() || nv === word.w) return;
  const arr = S.transcripts[word.mid]; arr[word.idx].w = nv.trim();
  try { await API.put(`/api/media/${word.mid}/transcript`, { words: arr }); computeWords(); renderTranscript(); updateStage(); }
  catch (err) { toast(err.message, true); }
});
function cutTxSelection() {
  if (!S.txSel) { toast('Seleccione palabras en la transcripción'); return; }
  const a = Math.min(S.txSel.a, S.txSel.b), b = Math.max(S.txSel.a, S.txSel.b);
  const prevE = a > 0 ? S.words[a - 1].e : 0, nextS = b + 1 < S.words.length ? S.words[b + 1].s : duration();
  const t0 = Math.max(prevE, S.words[a].s - 0.04), t1 = Math.min(nextS, S.words[b].e + 0.04);
  S.txSel = null; cutRange(t0, t1); seek(Math.max(0, t0 - 0.01));
  toast(`Eliminado ${(t1 - t0).toFixed(1)} s`);
}
$('#btnCutSel').onclick = cutTxSelection;
$('#btnFillers').onclick = () => {
  S.fillers = S.words.map((_, i) => i).filter(i => isFiller(S.words, i));
  renderTranscript();
  if (!S.fillers.length) { toast('No se encontraron muletillas'); return; }
  if (confirm(`Se encontraron ${S.fillers.length} muletillas (subrayadas). ¿Eliminarlas todas del video?\n(Puede deshacer con Ctrl+Z)`)) {
    const ranges = S.fillers.map(i => { const w = S.words[i]; const p = i > 0 ? S.words[i - 1].e : 0; const n = i + 1 < S.words.length ? S.words[i + 1].s : duration(); return [Math.max(p, w.s - 0.03), Math.min(n, w.e + 0.03)]; });
    change(() => { for (const [a, b] of ranges.reverse()) cutRange(a, b, { inPlace: true }); });
    S.fillers = []; renderAll();
  }
};
$('#btnSilences').onclick = () => {
  if (!S.words.length) { toast('Se necesita la transcripción para detectar silencios', true); return; }
  const thr = parseFloat($('#silenceSec').value) || 0.8, pad = 0.15, ranges = [];
  const ws = S.words;
  if (ws[0].s > thr) ranges.push([0, ws[0].s - pad]);
  for (let i = 1; i < ws.length; i++) { const g = ws[i].s - ws[i - 1].e; if (g > thr) ranges.push([ws[i - 1].e + pad, ws[i].s - pad]); }
  const T = duration(); if (T - ws[ws.length - 1].e > thr) ranges.push([ws[ws.length - 1].e + pad + 0.3, T]);
  if (!ranges.length) { toast('No hay silencios mayores a ' + thr + ' s'); return; }
  const total = ranges.reduce((a, [x, y]) => a + (y - x), 0);
  change(() => { for (const [a, b] of ranges.reverse()) cutRange(a, b, { inPlace: true }); });
  toast(`${ranges.length} silencios eliminados (${total.toFixed(1)} s). Ctrl+Z para deshacer.`);
  seek(Math.min(S.t, duration()));
};
$('#txSearch').addEventListener('input', debounce(renderTranscript, 200));
$('#silenceSec').addEventListener('change', renderTranscript);

/* ================================================================= Panel: capas y logo */
const LAYER_ICONS = { text: 'T', lower: '▭', latex: '∑', chem: '⚗', molecule: '⬡', arrow: '➜', box: '▢', image: '🖼' };
for (const [type, def] of Object.entries(LAYER_TYPES)) {
  $('#layerButtons').append(el('button', { class: 'btn', onclick: () => addLayer(type) }, el('i', {}, LAYER_ICONS[type]), def.label));
}
function addLayer(type) {
  if (!D().clips.length) { toast('Primero añada un clip a la línea de tiempo', true); return; }
  const def = LAYER_TYPES[type];
  const ov = { id: uid(), type, params: JSON.parse(JSON.stringify(def.defaults)), start: S.t, end: Math.min(S.t + 5, Math.max(duration(), S.t + 5)), x: 0.05, y: 0.08, w: def.w, h: 0.1, opacity: 1, fade: 0.25, asset_id: null };
  if (type === 'lower') { ov.x = 0.05; ov.y = 0.72; }
  if (type === 'latex' || type === 'chem' || type === 'molecule') ov.x = 0.6;
  if (type === 'arrow' || type === 'box') { ov.x = 0.45; ov.y = 0.4; ov.fade = 0.15; }
  // evita apilar capas nuevas exactamente en el mismo lugar
  const busy = D().overlays.filter(o => o.start < ov.end && o.end > ov.start && Math.abs(o.x - ov.x) < 0.05).length;
  ov.y = clamp(ov.y + busy * 0.14, 0, 0.85);
  change(d => d.overlays.push(ov));
  select('overlay', ov.id);
  if (type === 'image') { toast('Elija la imagen en el inspector'); return; }
  rasterize(D().overlays.find(o => o.id === ov.id));
}
$('#btnZoom').onclick = () => {
  if (!D().clips.length) return;
  const z = { id: uid(), start: S.t, end: Math.min(S.t + 4, duration()), x: 0.5, y: 0.5, scale: 1.8 };
  if (z.end - z.start < 1) { toast('Ubique el cabezal al menos 1 s antes del final', true); return; }
  change(d => d.zooms.push(z)); select('zoom', z.id); pickZoomPoint(z);
};
$('#logoInput').onchange = async e => {
  const f = e.target.files[0]; e.target.value = ''; if (!f) return;
  const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = URL.createObjectURL(f); });
  const scale = Math.min(1, 900 / img.naturalWidth);
  const c = document.createElement('canvas'); c.width = Math.round(img.naturalWidth * scale); c.height = Math.round(img.naturalHeight * scale);
  c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
  const blob = await new Promise(r => c.toBlob(r, 'image/png'));
  const r = await API.req('POST', `/api/projects/${PID}/assets`, blob);
  S.assetUrl[r.id] = URL.createObjectURL(blob);
  change(d => { d.brand.logo_asset_id = r.id; });
};
$('#logoPos').onchange = e => change(d => { d.brand.position = e.target.value; });
$('#logoSize').oninput = e => { liveEdit(() => { D().brand.size = +e.target.value; }, false); syncLayers(); };
$('#logoSize').onchange = e => liveEdit(() => { D().brand.size = +e.target.value; }, true);
$('#logoOpacity').oninput = e => { liveEdit(() => { D().brand.opacity = +e.target.value; }, false); syncLayers(); };
$('#logoOpacity').onchange = e => liveEdit(() => { D().brand.opacity = +e.target.value; }, true);
$('#logoRemove').onclick = () => change(d => { d.brand.logo_asset_id = null; });

/* ================================================================= Panel: audio */
function renderAudioPanel() {
  const a = D().audio, c = D().captions;
  $('#aDenoise').value = a.denoise; $('#aHighpass').checked = a.highpass; $('#aDeess').checked = a.deess; $('#aCompress').checked = a.compress;
  $('#aGain').value = a.voice_gain_db; $('#aGainV').textContent = a.voice_gain_db; $('#aLoud').value = String(a.loudness);
  $('#cSoft').checked = c.soft; $('#cBurn').checked = c.burn; $('#cColor').value = c.color || '#ffffff';
  const b = D().brand; $('#logoPos').value = b.position; $('#logoSize').value = b.size; $('#logoOpacity').value = b.opacity;
  $('#logoRemove').classList.toggle('hidden', !b.logo_asset_id);
  const ml = $('#musicList'); ml.innerHTML = '';
  for (const mu of D().music) {
    const m = S.media[mu.media_id] || {};
    ml.append(el('div', { class: 'list-item' }, el('div', { class: 'head' }, el('span', { class: 'time', onclick: () => seek(mu.start) }, fmtTime(mu.start)), el('span', { class: 'grow small' }, m.filename || 'música'),
      el('button', { class: 'btn sm', onclick: () => select('music', mu.id) }, 'Editar'))));
  }
}
$('#aDenoise').onchange = e => change(d => { d.audio.denoise = e.target.value; });
$('#aHighpass').onchange = e => change(d => { d.audio.highpass = e.target.checked; });
$('#aDeess').onchange = e => change(d => { d.audio.deess = e.target.checked; });
$('#aCompress').onchange = e => change(d => { d.audio.compress = e.target.checked; });
$('#aGain').oninput = e => { $('#aGainV').textContent = e.target.value; };
$('#aGain').onchange = e => change(d => { d.audio.voice_gain_db = +e.target.value; });
$('#aLoud').onchange = e => change(d => { d.audio.loudness = +e.target.value; });
$('#cSoft').onchange = e => change(d => { d.captions.soft = e.target.checked; });
$('#cBurn').onchange = e => change(d => { d.captions.burn = e.target.checked; });
$('#cColor').onchange = e => change(d => { d.captions.color = e.target.value; });

/* ================================================================= Panel: lección (capítulos, preguntas, teleprompter) */
function renderMarks() {
  const cl = $('#chapterList'); cl.innerHTML = '';
  const chs = [...D().chapters].sort((a, b) => a.t - b.t);
  if (!chs.length) cl.append(el('p', { class: 'muted small', style: { margin: 0 } }, 'Sin capítulos. Se incrustan en el MP4 y aparecen en el reproductor.'));
  for (const ch of chs) {
    const t = el('input', { type: 'text', value: ch.title, class: 'grow' });
    t.onchange = () => change(d => { d.chapters.find(x => x.id === ch.id).title = t.value; });
    cl.append(el('div', { class: 'list-item' + (isSel('chapter', ch.id) ? ' sel' : '') }, el('div', { class: 'head' },
      el('span', { class: 'time', onclick: () => seek(ch.t) }, fmtTime(ch.t)), t,
      el('button', { class: 'btn sm icon ghost', title: 'Eliminar', onclick: () => change(d => { d.chapters = d.chapters.filter(x => x.id !== ch.id); }) }, '×'))));
  }
  const ql = $('#quizList'); ql.innerHTML = '';
  for (const q of [...D().quizzes].sort((a, b) => a.t - b.t)) {
    const upd = fn => change(d => fn(d.quizzes.find(x => x.id === q.id)), { silent: true });
    const qt = el('textarea', { rows: 2, placeholder: 'Pregunta' }); qt.value = q.question; qt.onchange = () => upd(x => x.question = qt.value);
    const opts = el('div', { class: 'col', style: { gap: '4px' } });
    q.options.forEach((o, i) => {
      const r = el('input', { type: 'radio', name: 'q' + q.id, title: 'Respuesta correcta' }); r.checked = q.correct === i; r.onchange = () => upd(x => x.correct = i);
      const ot = el('input', { type: 'text', value: o, placeholder: 'Opción ' + (i + 1) }); ot.onchange = () => upd(x => x.options[i] = ot.value);
      opts.append(el('div', { class: 'row' }, r, ot, q.options.length > 2 ? el('button', { class: 'btn sm icon ghost', onclick: () => change(d => { const x = d.quizzes.find(y => y.id === q.id); x.options.splice(i, 1); if (x.correct >= x.options.length) x.correct = 0; }) }, '×') : null));
    });
    const ex = el('input', { type: 'text', value: q.explanation || '', placeholder: 'Retroalimentación (opcional)' }); ex.onchange = () => upd(x => x.explanation = ex.value);
    ql.append(el('div', { class: 'list-item' }, el('div', { class: 'head' }, el('span', { class: 'time', onclick: () => seek(q.t) }, fmtTime(q.t)), el('span', { class: 'grow small muted' }, 'Marque la respuesta correcta'),
      el('button', { class: 'btn sm icon ghost', title: 'Eliminar', onclick: () => change(d => { d.quizzes = d.quizzes.filter(x => x.id !== q.id); }) }, '×')),
      qt, opts, q.options.length < 6 ? el('button', { class: 'btn sm', style: { alignSelf: 'flex-start' }, onclick: () => change(d => d.quizzes.find(x => x.id === q.id).options.push('')) }, '+ Opción') : null, ex));
  }
  $('#teleText').value = D().teleprompter || '';
}
function addChapter() {
  const n = D().chapters.length + 1;
  const title = prompt('Título del capítulo:', n === 1 ? 'Introducción' : 'Capítulo ' + n);
  if (!title) return;
  change(d => d.chapters.push({ id: uid(), t: +S.t.toFixed(2), title }));
  openTab('marks');
}
$('#btnChapter').onclick = addChapter;
$('#btnQuiz').onclick = () => { change(d => d.quizzes.push({ id: uid(), t: +S.t.toFixed(2), question: '', options: ['', '', ''], correct: 0, explanation: '' })); openTab('marks'); };
$('#teleText').addEventListener('change', e => change(d => { d.teleprompter = e.target.value; }, { silent: true }));

/* ================================================================= Revisión (comentarios) */
async function loadComments() { try { S.comments = await API.get(`/api/projects/${PID}/comments`); renderComments(); renderTimeline(); } catch (_) { } }
function renderComments() {
  const box = $('#commentList'); box.innerHTML = '';
  if (!S.comments.length) box.append(el('p', { class: 'muted small' }, 'Sin comentarios.'));
  for (const c of S.comments) {
    const r = el('input', { type: 'checkbox', title: 'Resuelto' }); r.checked = !!c.resolved;
    r.onchange = async () => { await API.patch('/api/comments/' + c.id, { resolved: r.checked }); loadComments(); };
    box.append(el('div', { class: 'list-item' + (c.resolved ? ' done' : '') },
      el('div', { class: 'head' }, el('span', { class: 'time', onclick: () => seek(c.t) }, fmtTime(c.t)), el('b', { class: 'small grow' }, c.author || c.username),
        el('label', { class: 'chk small' }, r, 'Resuelto'),
        el('button', { class: 'btn sm icon ghost', onclick: async () => { if (confirm('¿Eliminar comentario?')) { await API.del('/api/comments/' + c.id); loadComments(); } } }, '×')),
      el('div', { class: 'small', style: { whiteSpace: 'pre-wrap' } }, c.text)));
  }
}
$('#btnComment').onclick = async () => {
  const text = $('#commentText').value.trim(); if (!text) return;
  await API.post(`/api/projects/${PID}/comments`, { t: S.t, text }); $('#commentText').value = ''; loadComments();
};

/* ================================================================= Exportaciones */
async function loadExports() { try { S.exports = await API.get(`/api/projects/${PID}/exports`); renderExports(); } catch (_) { } }
function renderExports() {
  const box = $('#exportList'); box.innerHTML = '';
  const running = S.jobs.filter(j => j.type === 'render' && (j.status === 'queued' || j.status === 'running'));
  for (const j of running) box.append(el('div', { class: 'list-item' }, el('b', { class: 'small' }, 'Renderizando ' + (j.params.format || '')), el('div', { class: 'progress' }, el('i', { style: { width: (j.progress * 100) + '%' } })), el('span', { class: 'small muted' }, j.message)));
  if (!S.exports.length && !running.length) box.append(el('p', { class: 'muted small' }, 'Aún no ha exportado. Use el botón «Exportar» (arriba a la derecha).'));
  const labels = { mp4: 'MP4', srt: 'SRT', vtt: 'VTT', srt_en: 'SRT inglés', vtt_en: 'VTT inglés', chapters: 'Capítulos', txt: 'Transcripción' };
  for (const e of S.exports) {
    const base = `/api/exports/${e.id}/file`;
    const pub = location.origin + '/v/' + e.token;
    const links = el('div', { class: 'links' }, ...e.files.filter(f => labels[f]).map(f => el('a', { class: 'btn sm', href: `${base}/${f}?download=1` }, '↓ ' + labels[f])),
      el('a', { class: 'btn sm', href: `/api/exports/${e.id}/scorm`, title: 'Paquete SCORM 1.2 para Moodle (con preguntas calificadas)' }, '↓ SCORM'));
    const pubChk = el('input', { type: 'checkbox' }); pubChk.checked = e.public;
    pubChk.onchange = async () => { await API.patch('/api/exports/' + e.id, { public: pubChk.checked }); loadExports(); };
    box.append(el('div', { class: 'export-card' },
      e.files.includes('poster') ? el('a', { href: `${base}/mp4`, target: '_blank', class: 'poster', style: { display: 'block', backgroundImage: `url(${base}/poster)` }, title: 'Ver video' }) : null,
      el('div', { class: 'body' },
        el('div', { class: 'row' }, el('b', { class: 'grow' }, e.title), el('span', { class: 'badge' }, e.format)),
        el('div', { class: 'small muted' }, `${fmtTime(e.meta.duration)} · ${e.meta.width}×${e.meta.height} · ${new Date(e.created_at * 1000).toLocaleString('es-PA')}`),
        links,
        el('label', { class: 'chk small' }, pubChk, 'Enlace público con reproductor interactivo'),
        e.public ? el('div', { class: 'row' }, el('input', { type: 'text', value: pub, readonly: true, class: 'mono small', onclick: ev => ev.target.select() }),
          el('button', { class: 'btn sm', onclick: () => { navigator.clipboard.writeText(pub); toast('Enlace copiado'); } }, 'Copiar'),
          el('a', { class: 'btn sm', href: pub, target: '_blank' }, 'Abrir')) : null,
        el('div', { class: 'row' }, el('button', { class: 'btn sm', onclick: () => showAnalytics(e) }, `Analítica (${e.views} vistas)`), el('div', { class: 'grow' }),
          canEdit() ? el('button', { class: 'btn sm danger', onclick: async () => { if (confirm('¿Eliminar esta exportación?')) { await API.del('/api/exports/' + e.id); loadExports(); } } }, 'Eliminar') : null))));
  }
}
async function showAnalytics(e) {
  const a = await API.get(`/api/exports/${e.id}/analytics`);
  $('#anTitle').textContent = 'Analítica · ' + e.title;
  const body = $('#anBody'); body.innerHTML = '';
  const max = Math.max(1, ...a.retention);
  body.append(el('div', { class: 'row' }, el('span', { class: 'badge ok' }, `${a.views} vistas`), el('span', { class: 'badge' }, `${a.completed} completas (≥90 %)`)),
    el('h4', {}, 'Retención por tramo de 5 s'),
    el('div', { class: 'ret-chart', title: 'Cantidad de espectadores que vieron cada tramo' }, ...a.retention.map((v, i) => el('i', { style: { height: (v / max * 100) + '%' }, title: `${fmtTime(i * 5)}: ${v}` }))),
    el('div', { class: 'row small muted' }, el('span', {}, '0:00'), el('div', { class: 'grow' }), el('span', {}, fmtTime(a.duration))));
  if (a.quizzes.length) body.append(el('h4', {}, 'Preguntas'));
  for (const q of a.quizzes) {
    const tot = Math.max(1, q.answers);
    body.append(el('div', { class: 'col', style: { gap: '4px' } }, el('b', { class: 'small' }, q.question || '(sin texto)'),
      el('span', { class: 'small muted' }, `${q.answers} respuestas · ${q.answers ? Math.round(q.right / q.answers * 100) : 0} % correctas`),
      ...q.options.map((o, i) => el('div', { class: 'qbar' + (i === q.correct ? ' ok' : '') }, el('i', { style: { width: (q.counts[i] / tot * 100) + '%' } }), el('span', {}, `${o || '—'} · ${q.counts[i]}`)))));
  }
  if (!e.public) body.append(el('p', { class: 'small muted' }, 'Active el enlace público para registrar vistas. En Moodle, las calificaciones del paquete SCORM quedan en el libro de calificaciones del curso.'));
  $('#dlgAnalytics').showModal();
}

$('#btnExport').onclick = async () => {
  await save();
  if (!D().clips.length) { toast('La línea de tiempo está vacía', true); return; }
  const f = $('#exportForm'); f.title.value = S.name; f.format.value = D().settings.format || '16:9';
  f.burn.checked = D().captions.burn; f.soft.checked = D().captions.soft;
  $('#dlgExport').showModal();
};
$('#dlgExport').addEventListener('close', async e => {
  if (e.target.returnValue !== 'ok') return;
  const f = $('#exportForm');
  const body = { title: f.title.value, format: f.format.value, reframe: f.reframe.value, crf: +f.crf.value, burn_captions: f.burn.checked, soft_captions: f.soft.checked };
  if (f.height.value) body.height = +f.height.value;
  try { await save(); await API.post(`/api/projects/${PID}/render`, body); toast('Render en cola. Puede seguir editando.'); openTab('exports'); pollJobs(); }
  catch (err) { toast(err.message, true); }
});

/* ================================================================= Compartir */
async function renderShare() {
  const list = await API.get(`/api/projects/${PID}/members`);
  const box = $('#shareList'); box.innerHTML = '';
  if (!list.length) box.append(el('p', { class: 'muted small' }, 'No compartido.'));
  for (const m of list) box.append(el('div', { class: 'row list-item', style: { flexDirection: 'row' } }, el('span', { class: 'grow' }, `${m.name || m.username} (${m.username})`), el('span', { class: 'badge' }, m.role),
    el('button', { class: 'btn sm ghost', onclick: async () => { await API.del(`/api/projects/${PID}/members/${m.id}`); renderShare(); } }, 'Quitar')));
}
$('#btnShare').onclick = () => { renderShare(); $('#dlgShare').showModal(); };
$('#shareAdd').onclick = async () => {
  try { await API.post(`/api/projects/${PID}/members`, { username: $('#shareUser').value.trim(), role: $('#shareRole').value }); $('#shareUser').value = ''; renderShare(); }
  catch (e) { toast(e.message, true); }
};

/* ================================================================= Trabajos en el servidor */
function showJobsBox() { $('#jobsBox').classList.remove('hidden'); }
async function pollJobs() {
  clearTimeout(S.jobsTimer);
  let r;
  try { r = await API.get(`/api/projects/${PID}/jobs`); } catch (_) { S.jobsTimer = setTimeout(pollJobs, 5000); return; }
  const prevActive = S.jobs.filter(j => j.status === 'queued' || j.status === 'running').map(j => j.id);
  S.jobs = r.jobs;
  let mediaChanged = false;
  for (const j of S.jobs) {
    if ((j.status === 'done' || j.status === 'error') && prevActive.includes(j.id) && !S.knownDone.has(j.id)) {
      S.knownDone.add(j.id);
      if (j.status === 'error') toast(`${({ ingest: 'Procesamiento', transcribe: 'Transcripción', render: 'Render' })[j.type]} falló: ${j.message}`, true, 8000);
      else if (j.type === 'ingest') mediaChanged = true;
      else if (j.type === 'transcribe') { await loadTranscript(j.media_id); computeWords(); renderTranscript(); renderMedia(); updateStage(); toast('Transcripción lista: ' + (j.filename || '')); }
      else if (j.type === 'render') { loadExports(); toast('¡Video exportado! Véalo en «Exportados».', false, 6000); }
    }
  }
  if (mediaChanged || Object.values(S.media).some(m => m.status === 'processing')) {
    const list = await API.get(`/api/projects/${PID}/media`);
    list.forEach(m => { const old = S.media[m.id]; S.media[m.id] = { ...m, _dur: old?._dur }; });
    for (const pa of [...S.pendingAdd]) {
      const m = S.media[pa.mid];
      if (m && m.status === 'ready') { S.pendingAdd.splice(S.pendingAdd.indexOf(pa), 1); if (pa.dur) m._dur = pa.dur; addMediaToTimeline(m, pa.at); }
      else if (m && m.status === 'error') S.pendingAdd.splice(S.pendingAdd.indexOf(pa), 1);
    }
  }
  renderMedia(); renderJobs();
  if (!$('[data-panel="exports"]').classList.contains('hidden')) renderExports();
  const active = S.jobs.some(j => j.status === 'queued' || j.status === 'running');
  S.jobsTimer = setTimeout(pollJobs, active ? 2000 : 15000);
}
function renderJobs() {
  const box = $('#jobsBox');
  const act = S.jobs.filter(j => j.status === 'queued' || j.status === 'running');
  box.querySelectorAll('.job.srv').forEach(n => n.remove());
  if (!act.length && !box.querySelector('.job')) { box.classList.add('hidden'); return; }
  showJobsBox();
  const names = { ingest: 'Procesando', transcribe: 'Transcribiendo', render: 'Exportando' };
  for (const j of act) box.append(el('div', { class: 'job srv' },
    el('div', { class: 't' }, el('span', { class: 'grow' }, `${names[j.type]} ${j.filename || j.params.format || ''}`), el('span', { class: 'muted' }, j.status === 'queued' ? 'en cola' : Math.round(j.progress * 100) + '%')),
    el('div', { class: 'progress' }, el('i', { style: { width: (j.progress * 100) + '%' } })),
    j.message && j.status === 'running' ? el('span', { class: 'muted small' }, j.message) : null));
}

/* ================================================================= Pestañas, transporte y teclado */
function openTab(name) {
  $$('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('.panel').forEach(p => p.classList.toggle('hidden', p.dataset.panel !== name));
  if (name === 'exports') renderExports();
  if (name === 'text') renderTranscript();
}
$$('.tabs button').forEach(b => b.onclick = () => openTab(b.dataset.tab));

function renderAll() {
  renderTimeline(); renderInspector(); syncLayers(); renderMedia(); renderAudioPanel(); renderMarks();
  if (!$('[data-panel="text"]').classList.contains('hidden')) renderTranscript();
  $('#btnUndo').disabled = !S.undo.length; $('#btnRedo').disabled = !S.redo.length;
}

$('#btnPlay').onclick = () => S.playing ? pause() : play();
$('#btnStart').onclick = () => seek(0);
$('#btnBack').onclick = () => seek(S.t - 5);
$('#btnFwd').onclick = () => seek(S.t + 5);
$('#btnSplit').onclick = () => splitAt(S.t);
$('#btnUndo').onclick = undo; $('#btnRedo').onclick = redo;
$('#btnHelp').onclick = () => $('#dlgHelp').showModal();
$('#showCaps').onchange = updateStage;
$('#rate').onchange = () => { vid.playbackRate = rate(); };
$('#projName').addEventListener('change', e => { S.name = e.target.value.trim() || S.name; document.title = S.name + ' — Editor'; S.dirty = true; save(); });

document.addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
  if (document.querySelector('dialog[open]')) return;
  const k = e.key.toLowerCase(), mod = e.ctrlKey || e.metaKey;
  if (mod && k === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (mod && k === 'y') { e.preventDefault(); redo(); return; }
  if (mod && k === 's') { e.preventDefault(); save(); return; }
  if (mod) return;
  const frame = 1 / FPS();
  switch (k) {
    case ' ': e.preventDefault(); S.playing ? pause() : play(); break;
    case 'j': seek(S.t - 5); break;
    case 'l': seek(S.t + 5); break;
    case 'k': pause(); break;
    case 'arrowleft': e.preventDefault(); if (S.playing) pause(); seek(S.t - (e.shiftKey ? 1 : frame)); break;
    case 'arrowright': e.preventDefault(); if (S.playing) pause(); seek(S.t + (e.shiftKey ? 1 : frame)); break;
    case 'home': seek(0); break;
    case 'end': seek(duration()); break;
    case 's': splitAt(S.t); break;
    case 'm': addChapter(); break;
    case 'delete': case 'backspace':
      e.preventDefault();
      if (S.txSel && document.activeElement === $('#transcript')) cutTxSelection(); else if (S.sel) deleteSelected(); else if (S.txSel) cutTxSelection();
      break;
    case 'escape': S.txSel = null; paintTxSel(); select(null); break;
  }
});

load().catch(e => { toast('No se pudo abrir el proyecto: ' + e.message, true, 10000); });
