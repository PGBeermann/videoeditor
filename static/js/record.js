/* Estudio de grabación: pantalla + cámara compuestas en un lienzo, anotaciones, pizarra,
   teleprompter y mezcla de audio. Se graba con MediaRecorder y se sube al proyecto. */
'use strict';
const PID = location.pathname.split('/').filter(Boolean).pop();
const $ = s => document.querySelector(s);
const cv = $('#cv'), ctx = cv.getContext('2d');
const R = {
  project: null, version: 0, screen: null, cam: null, mic: null,
  screenVid: document.createElement('video'), camVid: document.createElement('video'),
  strokes: [], drawing: null, pen: '#ffd23f',
  recorder: null, chunks: [], blob: null, startAt: 0, pausedMs: 0, pauseAt: 0,
  audioCtx: null, dest: null, micNode: null, sysNode: null, analyser: null,
  tele: { y: 0, playing: false, last: 0 },
};
[R.screenVid, R.camVid].forEach(v => { v.muted = true; v.playsInline = true; v.autoplay = true; });
$('#back').href = '/editor/' + PID;

const mode = () => document.querySelector('input[name=mode]:checked').value;

async function init() {
  const p = await API.get('/api/projects/' + PID);
  if (p.role === 'revisor') { toast('Los revisores no pueden grabar en este proyecto', true); }
  R.project = p; R.version = p.version;
  $('#projTitle').textContent = 'Grabar · ' + p.name;
  const s = p.data.settings;
  cv.width = s.width; cv.height = s.height;
  fitCanvas();
  $('#teleText').textContent = p.data.teleprompter || 'Escriba su guion en la pestaña «Lección» del editor o con «Editar guion».';
  if (p.data.teleprompter) $('#tele').classList.add('on');
  applyTele();
  await setupDevices();
  startTicker();
}

function fitCanvas() {
  const box = $('#stageBox');
  const aw = box.clientWidth - 28, ah = box.clientHeight - 28, r = cv.width / cv.height;
  let w = aw, h = aw / r; if (h > ah) { h = ah; w = ah * r; }
  cv.style.width = w + 'px'; cv.style.height = h + 'px';
}
new ResizeObserver(fitCanvas).observe($('#stageBox'));

/* ---------------------------------------------------------- Dispositivos */
async function setupDevices() {
  try { await openCamMic(); } catch (e) { toast('No se pudo acceder a la cámara/micrófono: ' + e.message, true, 8000); }
  const devs = await navigator.mediaDevices.enumerateDevices();
  const fill = (sel, kind, cur) => {
    sel.innerHTML = '';
    sel.append(el('option', { value: '' }, kind === 'videoinput' ? 'Sin cámara' : 'Sin micrófono'));
    devs.filter(d => d.kind === kind).forEach((d, i) => sel.append(el('option', { value: d.deviceId }, d.label || `${kind === 'videoinput' ? 'Cámara' : 'Micrófono'} ${i + 1}`)));
    sel.value = cur || (sel.options[1] ? sel.options[1].value : '');
  };
  fill($('#camSel'), 'videoinput', R.cam?.getVideoTracks()[0]?.getSettings().deviceId);
  fill($('#micSel'), 'audioinput', R.mic?.getAudioTracks()[0]?.getSettings().deviceId);
}

async function openCamMic() {
  const camId = $('#camSel').value, micId = $('#micSel').value;
  R.cam?.getTracks().forEach(t => t.stop()); R.mic?.getTracks().forEach(t => t.stop());
  R.cam = null; R.mic = null;
  const wantCam = mode() !== 'screen' && $('#camSel').value !== '' || $('#camSel').options.length === 0;
  if (wantCam) {
    R.cam = await navigator.mediaDevices.getUserMedia({ video: { deviceId: camId ? { exact: camId } : undefined, width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 30 } } });
    R.camVid.srcObject = R.cam; R.camVid.play().catch(() => { });
  }
  if ($('#micSel').value !== '' || $('#micSel').options.length === 0) {
    const ns = $('#nsChk').checked;
    R.mic = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: micId ? { exact: micId } : undefined, echoCancellation: ns, noiseSuppression: ns, autoGainControl: false, sampleRate: 48000, channelCount: 1 } });
  }
  rebuildAudio();
}

async function pickScreen() {
  try {
    R.screen?.getTracks().forEach(t => t.stop());
    R.screen = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: 30 }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: $('#sysChk').checked, selfBrowserSurface: 'exclude', surfaceSwitching: 'include' });
    R.screenVid.srcObject = R.screen; R.screenVid.play().catch(() => { });
    R.screen.getVideoTracks()[0].addEventListener('ended', () => { R.screen = null; toast('Se dejó de compartir la pantalla'); });
    rebuildAudio();
  } catch (e) { if (e.name !== 'NotAllowedError') toast(e.message, true); }
}

function rebuildAudio() {
  if (!R.audioCtx) { R.audioCtx = new AudioContext({ sampleRate: 48000 }); R.dest = R.audioCtx.createMediaStreamDestination(); R.analyser = R.audioCtx.createAnalyser(); R.analyser.fftSize = 1024; }
  try { R.micNode?.disconnect(); R.sysNode?.disconnect(); } catch (_) { }
  R.micNode = R.sysNode = null;
  if (R.mic && R.mic.getAudioTracks().length) {
    R.micNode = R.audioCtx.createMediaStreamSource(R.mic);
    R.micNode.connect(R.dest); R.micNode.connect(R.analyser);
  }
  if (R.screen && R.screen.getAudioTracks().length && $('#sysChk').checked) {
    R.sysNode = R.audioCtx.createMediaStreamSource(new MediaStream(R.screen.getAudioTracks()));
    const g = R.audioCtx.createGain(); g.gain.value = 0.8; R.sysNode.connect(g); g.connect(R.dest);
  }
}

/* ---------------------------------------------------------- Composición (temporizador en Worker: no se frena en segundo plano) */
function startTicker() {
  const src = 'let t=setInterval(()=>postMessage(0),1000/30);onmessage=e=>{if(e.data==="stop")clearInterval(t)}';
  const w = new Worker(URL.createObjectURL(new Blob([src], { type: 'text/javascript' })));
  w.onmessage = () => { draw(); meter(); teleStep(); };
}

function drawContain(v, x, y, w, h) {
  const vw = v.videoWidth, vh = v.videoHeight; if (!vw) return;
  const s = Math.min(w / vw, h / vh), dw = vw * s, dh = vh * s;
  ctx.drawImage(v, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
}
function drawCover(v, x, y, w, h) {
  const vw = v.videoWidth, vh = v.videoHeight; if (!vw) return;
  const s = Math.max(w / vw, h / vh), sw = w / s, sh = h / s;
  ctx.drawImage(v, (vw - sw) / 2, (vh - sh) / 2, sw, sh, x, y, w, h);
}

function draw() {
  const W = cv.width, H = cv.height, m = mode();
  ctx.fillStyle = m === 'board' ? '#fbfbf8' : '#000'; ctx.fillRect(0, 0, W, H);
  if (m === 'board') {           // cuadrícula tenue de pizarra
    ctx.strokeStyle = 'rgba(0,0,0,.05)'; ctx.lineWidth = 1;
    for (let x = 0; x < W; x += 60) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    for (let y = 0; y < H; y += 60) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  }
  if ((m === 'screen' || m === 'screencam') && R.screen) drawContain(R.screenVid, 0, 0, W, H);
  if ((m === 'screen' || m === 'screencam') && !R.screen) {
    ctx.fillStyle = '#8b949c'; ctx.font = `600 ${Math.round(H * 0.035)}px system-ui`; ctx.textAlign = 'center';
    ctx.fillText('Pulse «Elegir pantalla o ventana…»', W / 2, H / 2);
  }
  if (m === 'cam' && R.cam) drawCover(R.camVid, 0, 0, W, H);
  // anotaciones
  const now = performance.now(), fade = $('#fadeChk').checked;
  R.strokes = R.strokes.filter(s => !fade || !s.done || now - s.done < 5000);
  for (const s of R.strokes) {
    const a = fade && s.done ? Math.max(0, 1 - Math.max(0, now - s.done - 4000) / 1000) : 1;
    ctx.globalAlpha = a; ctx.strokeStyle = s.color; ctx.lineWidth = s.width * W / 1920; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.beginPath(); s.pts.forEach((p, i) => i ? ctx.lineTo(p[0] * W, p[1] * H) : ctx.moveTo(p[0] * W, p[1] * H)); ctx.stroke();
  }
  ctx.globalAlpha = 1;
  // cámara en la esquina
  if ((m === 'screencam' || m === 'board') && R.cam) {
    const size = +$('#pipSize').value * Math.min(W, H * 16 / 9), shape = $('#pipShape').value, pos = $('#pipPos').value;
    const w = size, h = shape === 'circle' ? size : size * 9 / 16, mg = Math.round(W * 0.025);
    const x = pos.includes('l') ? mg : W - w - mg, y = pos.includes('t') ? mg : H - h - mg;
    ctx.save(); ctx.beginPath();
    if (shape === 'circle') ctx.arc(x + w / 2, y + h / 2, w / 2, 0, Math.PI * 2);
    else { const r = w * 0.04; ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); }
    ctx.closePath(); ctx.shadowColor = 'rgba(0,0,0,.5)'; ctx.shadowBlur = 18; ctx.fillStyle = '#000'; ctx.fill(); ctx.shadowBlur = 0; ctx.clip();
    drawCover(R.camVid, x, y, w, h); ctx.restore();
    ctx.beginPath(); ctx.lineWidth = Math.max(3, W / 400); ctx.strokeStyle = '#4A7729';
    if (shape === 'circle') ctx.arc(x + w / 2, y + h / 2, w / 2, 0, Math.PI * 2); else ctx.rect(x, y, w, h);
    ctx.stroke();
  }
}

function meter() {
  if (!R.analyser || !R.micNode) { $('#meter').style.width = '0'; return; }
  const buf = new Float32Array(R.analyser.fftSize); R.analyser.getFloatTimeDomainData(buf);
  let peak = 0; for (const v of buf) peak = Math.max(peak, Math.abs(v));
  const db = 20 * Math.log10(peak || 1e-6);
  $('#meter').style.width = clamp((db + 60) / 60 * 100, 0, 100) + '%';
}

/* ---------------------------------------------------------- Anotaciones */
const PENS = ['#ffd23f', '#ff4d6d', '#4cc9f0', '#80ed99', '#ffffff', '#111111'];
PENS.forEach((c, i) => { const p = el('div', { class: 'pen' + (i ? '' : ' on'), style: { background: c }, title: c }); p.onclick = () => { R.pen = c; document.querySelectorAll('.pen').forEach(x => x.classList.remove('on')); p.classList.add('on'); }; $('#pens').append(p); });
function cvPoint(e) { const r = cv.getBoundingClientRect(); return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]; }
cv.addEventListener('pointerdown', e => { cv.setPointerCapture(e.pointerId); R.drawing = { pts: [cvPoint(e)], color: R.pen, width: 7, done: 0 }; R.strokes.push(R.drawing); });
cv.addEventListener('pointermove', e => { if (R.drawing) R.drawing.pts.push(cvPoint(e)); });
cv.addEventListener('pointerup', () => { if (R.drawing) { R.drawing.done = performance.now(); R.drawing = null; } });
$('#btnClear').onclick = () => { R.strokes = []; };

/* ---------------------------------------------------------- Teleprompter */
function applyTele() {
  const t = $('#teleText');
  t.style.fontSize = $('#teleSize').value + 'px';
  t.style.transform = `translateY(${-R.tele.y}px)` + ($('#teleMirror').checked ? ' scaleX(-1)' : '');
}
function teleStep() {
  const now = performance.now();
  if (R.tele.playing) {
    const dt = (now - R.tele.last) / 1000;
    const max = $('#teleText').scrollHeight - $('#tele').clientHeight * 0.34;
    R.tele.y = Math.min(max > 0 ? max : 0, R.tele.y + dt * +$('#teleSpeed').value);
    applyTele();
  }
  R.tele.last = now;
}
function teleStart(on) { R.tele.playing = on; R.tele.last = performance.now(); $('#teleToggle').textContent = on ? '❚❚ Pausar' : '▶ Desplazar'; }
$('#btnTele').onclick = () => $('#tele').classList.toggle('on');
$('#teleToggle').onclick = () => teleStart(!R.tele.playing);
$('#teleReset').onclick = () => { R.tele.y = 0; applyTele(); };
$('#teleSize').oninput = applyTele; $('#teleMirror').onchange = applyTele;
$('#tele').addEventListener('wheel', e => { e.preventDefault(); R.tele.y = Math.max(0, R.tele.y + e.deltaY * 0.5); applyTele(); }, { passive: false });
$('#teleEdit').onclick = async () => {
  const a = $('#teleArea');
  if (a.classList.contains('hidden')) { a.value = R.project.data.teleprompter || ''; a.classList.remove('hidden'); $('#teleEdit').textContent = 'Guardar guion'; return; }
  try {
    const fresh = await API.get('/api/projects/' + PID);
    fresh.data.teleprompter = a.value;
    const r = await API.put('/api/projects/' + PID, { data: fresh.data, version: fresh.version });
    R.project.data.teleprompter = a.value; R.version = r.version;
    $('#teleText').textContent = a.value; $('#tele').classList.add('on');
    a.classList.add('hidden'); $('#teleEdit').textContent = 'Editar guion'; toast('Guion guardado');
  } catch (e) { toast(e.message, true); }
};

/* ---------------------------------------------------------- Grabación */
function pickMime() {
  const cands = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4;codecs=avc1,mp4a', 'video/mp4'];
  return cands.find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
}
function setState(st) {
  const show = (id, v) => $(id).classList.toggle('hidden', !v);
  show('#btnRec', st === 'idle'); show('#btnPause', st === 'rec' || st === 'paused'); show('#btnStop', st === 'rec' || st === 'paused');
  show('#recDot', st === 'rec'); show('#btnUse', st === 'review'); show('#btnDiscard', st === 'review'); show('#btnSaveLocal', st === 'review');
  show('#cv', st !== 'review'); show('#review', st === 'review');
  $('#btnPause').textContent = st === 'paused' ? '▶ Reanudar' : '❚❚ Pausar';
  document.querySelectorAll('.side input, .side select, #btnScreen').forEach(n => { if (!n.closest('.pens') && !['teleSpeed', 'teleSize', 'teleMirror', 'fadeChk'].includes(n.id)) n.disabled = st === 'rec' || st === 'paused'; });
  R.state = st;
}
async function countdown() {
  const c = $('#count'); c.classList.remove('hidden');
  for (let i = 3; i > 0; i--) { c.textContent = i; await new Promise(r => setTimeout(r, 800)); }
  c.classList.add('hidden');
}
$('#btnRec').onclick = async () => {
  const m = mode();
  if ((m === 'screen' || m === 'screencam') && !R.screen) { await pickScreen(); if (!R.screen) return; }
  if (R.audioCtx.state === 'suspended') await R.audioCtx.resume();
  await countdown();
  const stream = cv.captureStream(30);
  R.dest.stream.getAudioTracks().forEach(t => stream.addTrack(t));
  const mime = pickMime();
  R.recorder = new MediaRecorder(stream, { mimeType: mime || undefined, videoBitsPerSecond: +$('#quality').value, audioBitsPerSecond: 160000 });
  R.chunks = [];
  R.recorder.ondataavailable = e => e.data.size && R.chunks.push(e.data);
  R.recorder.onstop = () => {
    R.blob = new Blob(R.chunks, { type: R.recorder.mimeType || 'video/webm' });
    const url = URL.createObjectURL(R.blob);
    $('#review').src = url;
    const ext = (R.blob.type.includes('mp4') ? 'mp4' : 'webm');
    $('#btnSaveLocal').href = url; $('#btnSaveLocal').download = `grabacion.${ext}`;
    setState('review');
  };
  R.recorder.start(1000);
  R.startAt = performance.now(); R.pausedMs = 0;
  setState('rec');
  if (R.project.data.teleprompter && $('#tele').classList.contains('on')) teleStart(true);
  tickTimer();
};
$('#btnPause').onclick = () => {
  if (R.state === 'rec') { R.recorder.pause(); R.pauseAt = performance.now(); setState('paused'); teleStart(false); }
  else { R.recorder.resume(); R.pausedMs += performance.now() - R.pauseAt; setState('rec'); }
};
$('#btnStop').onclick = () => { teleStart(false); R.recorder.stop(); };
function tickTimer() {
  if (R.state !== 'rec' && R.state !== 'paused') return;
  const ms = (R.state === 'paused' ? R.pauseAt : performance.now()) - R.startAt - R.pausedMs;
  $('#timer').textContent = fmtTime(ms / 1000);
  setTimeout(tickTimer, 250);
}
$('#btnDiscard').onclick = () => { if (!confirm('¿Descartar esta toma?')) return; R.blob = null; $('#review').removeAttribute('src'); $('#timer').textContent = '0:00'; setState('idle'); };
$('#btnUse').onclick = async () => {
  if (!R.blob) return;
  const d = new Date(), pad = n => String(n).padStart(2, '0');
  const name = `grabacion-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.${R.blob.type.includes('mp4') ? 'mp4' : 'webm'}`;
  $('#btnUse').disabled = true; $('#upProg').classList.remove('hidden');
  try {
    const r = await API.uploadFile(PID, R.blob, name, { autoTranscribe: true }, p => $('#upProg i').style.width = (p * 100) + '%');
    location.href = `/editor/${PID}?add=${r.id}`;
  } catch (e) { toast('Error al subir: ' + e.message + '. Use «Guardar copia» para no perder la toma.', true, 10000); $('#btnUse').disabled = false; }
};

/* ---------------------------------------------------------- Controles */
document.querySelectorAll('input[name=mode]').forEach(r => r.onchange = () => openCamMic().catch(e => toast(e.message, true)));
$('#btnScreen').onclick = pickScreen;
$('#camSel').onchange = () => openCamMic().catch(e => toast(e.message, true));
$('#micSel').onchange = () => openCamMic().catch(e => toast(e.message, true));
$('#nsChk').onchange = () => openCamMic().catch(e => toast(e.message, true));
$('#sysChk').onchange = rebuildAudio;
document.addEventListener('keydown', e => {
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
  if (e.key === 'c' || e.key === 'C') R.strokes = [];
  if (e.key === 'ArrowUp' && R.tele) { R.tele.y = Math.max(0, R.tele.y - 40); applyTele(); }
  if (e.key === 'ArrowDown' && R.tele) { R.tele.y += 40; applyTele(); }
});
window.addEventListener('beforeunload', e => { if (R.state === 'rec' || R.state === 'paused' || (R.state === 'review' && R.blob)) { e.preventDefault(); e.returnValue = ''; } });
setState('idle');
init().catch(e => toast(e.message, true, 10000));
