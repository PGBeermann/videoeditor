/* Rasterización de capas gráficas a PNG en el navegador.
   Lo que el docente ve en la vista previa es exactamente el PNG que FFmpeg compone en el render.
   - Texto y rótulos: Canvas 2D
   - Ecuaciones y química: MathJax 3 (salida SVG autocontenida; mhchem para \ce{...})
   - Estructuras moleculares: RDKit.js (MinimalLib, BSD) a partir de SMILES */
'use strict';

const BRAND = { green: '#4A7729', red: '#A6192E', greenHi: '#7fb069' };

const LAYER_TYPES = {
  text:      { label: 'Texto',               defaults: { text: 'Escriba aquí', size: 5.5, color: '#ffffff', bg: '#000000', bgOpacity: 0.55, bold: true, align: 'left' }, w: 0.45 },
  lower:     { label: 'Rótulo (nombre)',     defaults: { title: 'Dr. Nombre Apellido', subtitle: 'Escuela de Química · UNACHI' }, w: 0.42 },
  latex:     { label: 'Ecuación LaTeX',      defaults: { tex: 'PV = nRT', color: '#ffffff', bg: '#000000', bgOpacity: 0.55 }, w: 0.32 },
  chem:      { label: 'Química (mhchem)',    defaults: { tex: '2H2 + O2 -> 2H2O', color: '#ffffff', bg: '#000000', bgOpacity: 0.55 }, w: 0.36 },
  molecule:  { label: 'Molécula (SMILES)',   defaults: { smiles: 'CCO', legend: 'Etanol', card: true, explicitH: false }, w: 0.26 },
  arrow:     { label: 'Flecha',              defaults: { color: '#ffd23f', angle: 0, thickness: 10 }, w: 0.14 },
  box:       { label: 'Recuadro / resaltado', defaults: { color: '#ffd23f', thickness: 8, shape: 'rect', fill: 0 }, w: 0.25 },
  image:     { label: 'Imagen',              defaults: { src: null, name: '' }, w: 0.3 },
};

function _canvas(w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(2, Math.round(w)); c.height = Math.max(2, Math.round(h));
  return c;
}

function _rgba(hex, a) {
  const n = parseInt(hex.replace('#', ''), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function _roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

function _wrap(ctx, text, maxW) {
  const out = [];
  for (const para of String(text).split('\n')) {
    let line = '';
    for (const word of para.split(/\s+/)) {
      const test = line ? line + ' ' + word : word;
      if (ctx.measureText(test).width > maxW && line) { out.push(line); line = word; }
      else line = test;
    }
    out.push(line);
  }
  return out;
}

function renderText(p, W, H) {
  const fs = Math.round(p.size / 100 * H);
  const pad = Math.round(fs * 0.45);
  const font = `${p.bold ? 700 : 500} ${fs}px system-ui, "Segoe UI", Roboto, Arial, sans-serif`;
  const probe = _canvas(10, 10).getContext('2d'); probe.font = font;
  const lines = _wrap(probe, p.text || ' ', W - pad * 2);
  const lh = Math.round(fs * 1.22);
  const h = lines.length * lh + pad * 2 - (lh - fs) * 0.6;
  const c = _canvas(W, h), ctx = c.getContext('2d');
  if (p.bgOpacity > 0) { ctx.fillStyle = _rgba(p.bg, p.bgOpacity); _roundRect(ctx, 0, 0, W, h, fs * 0.3); ctx.fill(); }
  ctx.font = font; ctx.fillStyle = p.color; ctx.textBaseline = 'top';
  ctx.textAlign = p.align === 'center' ? 'center' : p.align === 'right' ? 'right' : 'left';
  const x = p.align === 'center' ? W / 2 : p.align === 'right' ? W - pad : pad;
  lines.forEach((ln, i) => ctx.fillText(ln, x, pad + i * lh));
  return c;
}

function renderLower(p, W, H) {
  const fs1 = Math.round(H * 0.042), fs2 = Math.round(H * 0.028);
  const pad = Math.round(H * 0.022), stripe = Math.round(H * 0.012);
  const h = pad * 2 + fs1 + fs2 + Math.round(H * 0.012);
  const c = _canvas(W, h), ctx = c.getContext('2d');
  ctx.fillStyle = BRAND.green; ctx.fillRect(0, 0, W, h);
  ctx.fillStyle = BRAND.red; ctx.fillRect(0, 0, stripe, h);
  ctx.fillStyle = 'rgba(0,0,0,.18)'; ctx.fillRect(stripe, h - Math.round(fs2 + pad * 1.2), W, Math.round(fs2 + pad * 1.2));
  ctx.textBaseline = 'top'; ctx.fillStyle = '#fff';
  ctx.font = `700 ${fs1}px system-ui, "Segoe UI", Roboto, Arial, sans-serif`;
  ctx.fillText(p.title || '', stripe + pad, pad, W - stripe - pad * 2);
  ctx.font = `500 ${fs2}px system-ui, "Segoe UI", Roboto, Arial, sans-serif`;
  ctx.fillStyle = 'rgba(255,255,255,.92)';
  ctx.fillText(p.subtitle || '', stripe + pad, h - fs2 - Math.round(pad * 0.6), W - stripe - pad * 2);
  return c;
}

let _mjReady = null;
function mathjaxReady() {
  if (_mjReady) return _mjReady;
  _mjReady = new Promise((resolve, reject) => {
    window.MathJax = {
      loader: { load: [] },
      tex: { packages: { '[+]': ['mhchem', 'cancel', 'color'] } },
      svg: { fontCache: 'none' },
      options: { enableMenu: false },
      startup: { typeset: false, ready: () => { MathJax.startup.defaultReady(); MathJax.startup.promise.then(resolve); } },
    };
    const s = document.createElement('script');
    s.src = '/static/vendor/mathjax/tex-svg-full.js';
    s.onerror = () => reject(new Error('No se pudo cargar MathJax'));
    document.head.appendChild(s);
  });
  return _mjReady;
}

function _svgToImage(svgText) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('SVG no válido'));
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText);
  });
}

async function renderTex(p, W, H, chem) {
  await mathjaxReady();
  const src = chem ? `\\ce{${p.tex}}` : p.tex;
  const node = await MathJax.tex2svgPromise(src, { display: true });
  const svg = node.querySelector('svg');
  if (!svg) throw new Error('No se pudo interpretar la expresión');
  const err = svg.querySelector('[data-mjx-error]');
  if (err) throw new Error('LaTeX: ' + err.getAttribute('data-mjx-error'));
  const vb = svg.getAttribute('viewBox').split(/\s+/).map(Number);
  const pad = Math.round(H * 0.022);
  const innerW = W - pad * 2;
  const innerH = innerW * vb[3] / vb[2];
  svg.setAttribute('width', innerW); svg.setAttribute('height', innerH);
  svg.setAttribute('color', p.color); svg.style.color = p.color;
  svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  const img = await _svgToImage(new XMLSerializer().serializeToString(svg));
  const c = _canvas(W, innerH + pad * 2), ctx = c.getContext('2d');
  if (p.bgOpacity > 0) { ctx.fillStyle = _rgba(p.bg, p.bgOpacity); _roundRect(ctx, 0, 0, c.width, c.height, pad * 0.8); ctx.fill(); }
  ctx.drawImage(img, pad, pad, innerW, innerH);
  return c;
}

let _rdkit = null;
function rdkitReady() {
  if (_rdkit) return _rdkit;
  _rdkit = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/static/vendor/rdkit/RDKit_minimal.js';
    s.onload = () => window.initRDKitModule({ locateFile: () => '/static/vendor/rdkit/RDKit_minimal.wasm' })
      .then(resolve, reject);
    s.onerror = () => reject(new Error('No se pudo cargar RDKit'));
    document.head.appendChild(s);
  });
  return _rdkit;
}

async function renderMolecule(p, W, H) {
  const RDKit = await rdkitReady();
  let mol = RDKit.get_mol(String(p.smiles || '').trim());
  if (!mol || !mol.is_valid()) { if (mol) mol.delete(); throw new Error('SMILES no válido'); }
  if (p.explicitH) {
    const molblock = mol.add_hs();
    mol.delete();
    mol = RDKit.get_mol(molblock);
  }
  return renderMoleculeFrom(mol, p, W, H);
}

async function renderMoleculeFrom(mol, p, W, H) {
  const legendH = p.legend ? Math.round(H * 0.05) : 0;
  const size = W;
  const opts = { width: size, height: Math.round(size * 0.75), bondLineWidth: Math.max(2, W / 220), fixedBondLength: Math.round(W * 0.14),
                 addStereoAnnotation: true, padding: 0.08, minFontSize: Math.round(W / 28), maxFontSize: Math.round(W / 14) };
  if (!p.card) { opts.backgroundColour = [0, 0, 0, 0]; opts.clearBackground = false; opts.atomColourPalette = { 6: [1, 1, 1] }; opts.symbolColour = [1, 1, 1]; }
  let svgText;
  try { svgText = mol.get_svg_with_highlights(JSON.stringify(opts)); }
  finally { mol.delete(); }
  if (!p.card) svgText = svgText.replace(/<rect[^>]*style=['"][^'"]*fill:#FFFFFF[^'"]*['"][^>]*><\/rect>/i, '')
    .replace(/stroke:#000000/g, 'stroke:#FFFFFF').replace(/fill:#000000/g, 'fill:#FFFFFF');
  const img = await _svgToImage(svgText);
  const c = _canvas(W, opts.height + legendH), ctx = c.getContext('2d');
  if (p.card) { ctx.fillStyle = '#ffffff'; _roundRect(ctx, 0, 0, c.width, c.height, Math.round(W * 0.04)); ctx.fill(); }
  ctx.drawImage(img, 0, 0, W, opts.height);
  if (legendH) {
    ctx.fillStyle = p.card ? '#1a1d21' : '#ffffff';
    ctx.font = `600 ${Math.round(legendH * 0.62)}px system-ui, "Segoe UI", Roboto, Arial, sans-serif`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(p.legend, W / 2, opts.height + legendH / 2 - 4, W * 0.92);
  }
  return c;
}

function renderArrow(p, W) {
  const c = _canvas(W, W), ctx = c.getContext('2d');
  const t = Math.max(2, p.thickness * W / 200);
  ctx.translate(W / 2, W / 2); ctx.rotate((p.angle || 0) * Math.PI / 180);
  const L = W * 0.44, head = Math.max(t * 2.6, W * 0.16);
  ctx.strokeStyle = p.color; ctx.fillStyle = p.color; ctx.lineWidth = t; ctx.lineCap = 'round';
  ctx.shadowColor = 'rgba(0,0,0,.45)'; ctx.shadowBlur = t;
  ctx.beginPath(); ctx.moveTo(-L, 0); ctx.lineTo(L - head * 0.8, 0); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(L, 0); ctx.lineTo(L - head, -head * 0.6); ctx.lineTo(L - head, head * 0.6); ctx.closePath(); ctx.fill();
  return c;
}

function renderBox(p, W, aspect) {
  const h = Math.round(W / (aspect || 1.6));
  const c = _canvas(W, h), ctx = c.getContext('2d');
  const t = Math.max(2, p.thickness * W / 300);
  ctx.strokeStyle = p.color; ctx.lineWidth = t;
  ctx.shadowColor = 'rgba(0,0,0,.4)'; ctx.shadowBlur = t;
  if (p.fill > 0) ctx.fillStyle = _rgba(p.color, p.fill);
  if (p.shape === 'ellipse') {
    ctx.beginPath(); ctx.ellipse(W / 2, h / 2, W / 2 - t, h / 2 - t, 0, 0, Math.PI * 2);
  } else { _roundRect(ctx, t, t, W - t * 2, h - t * 2, t * 1.5); }
  if (p.fill > 0) ctx.fill();
  ctx.stroke();
  return c;
}

async function renderImage(p, W) {
  if (!p.src) throw new Error('Seleccione una imagen');
  const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error('Imagen no válida')); i.src = p.src; });
  const h = Math.round(W * img.naturalHeight / img.naturalWidth);
  const c = _canvas(W, h); c.getContext('2d').drawImage(img, 0, 0, W, h);
  return c;
}

/* Devuelve {blob, w, h} con el PNG final en píxeles del lienzo del proyecto (W0×H0). */
async function rasterizeLayer(ov, W0, H0) {
  const W = Math.max(8, Math.round(ov.w * W0));
  const p = ov.params || {};
  let c;
  switch (ov.type) {
    case 'text': c = renderText(p, W, H0); break;
    case 'lower': c = renderLower(p, W, H0); break;
    case 'latex': c = await renderTex(p, W, H0, false); break;
    case 'chem': c = await renderTex(p, W, H0, true); break;
    case 'molecule': c = await renderMolecule(p, W, H0); break;
    case 'arrow': c = renderArrow(p, W); break;
    case 'box': c = renderBox(p, W, p.aspect || 1.6); break;
    case 'image': c = await renderImage(p, W); break;
    default: throw new Error('Tipo de capa desconocido');
  }
  const blob = await new Promise(r => c.toBlob(r, 'image/png'));
  return { blob, w: c.width, h: c.height };
}

/* Tarjeta de título a pantalla completa (se agrega como clip de imagen a la secuencia). */
function renderTitleCard(p, W, H) {
  const c = _canvas(W, H), ctx = c.getContext('2d');
  const g = ctx.createLinearGradient(0, 0, W, H);
  g.addColorStop(0, '#2f4f1a'); g.addColorStop(1, BRAND.green);
  ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = BRAND.red; ctx.fillRect(0, H - Math.round(H * 0.035), W, Math.round(H * 0.035));
  ctx.fillStyle = 'rgba(255,255,255,.06)';
  for (let i = 0; i < 6; i++) { ctx.beginPath(); ctx.arc(W * 0.86, H * 0.28, H * (0.12 + i * 0.09), 0, Math.PI * 2); ctx.lineWidth = 2; ctx.strokeStyle = 'rgba(255,255,255,.07)'; ctx.stroke(); }
  const vertical = H > W;
  const margin = Math.round(W * (vertical ? 0.08 : 0.07));
  const fsT = Math.round((vertical ? W * 0.085 : H * 0.085));
  ctx.textBaseline = 'top'; ctx.fillStyle = '#fff';
  ctx.font = `600 ${Math.round(fsT * 0.36)}px system-ui, "Segoe UI", Roboto, Arial, sans-serif`;
  ctx.fillStyle = 'rgba(255,255,255,.8)';
  ctx.fillText((p.institution || '').toUpperCase(), margin, Math.round(H * (vertical ? 0.3 : 0.22)), W - margin * 2);
  ctx.font = `750 ${fsT}px system-ui, "Segoe UI", Roboto, Arial, sans-serif`;
  ctx.fillStyle = '#fff';
  const lines = _wrap(ctx, p.title || 'Título de la lección', W - margin * 2);
  let y = Math.round(H * (vertical ? 0.36 : 0.32));
  for (const ln of lines.slice(0, 4)) { ctx.fillText(ln, margin, y); y += Math.round(fsT * 1.12); }
  ctx.font = `500 ${Math.round(fsT * 0.45)}px system-ui, "Segoe UI", Roboto, Arial, sans-serif`;
  ctx.fillStyle = 'rgba(255,255,255,.92)';
  if (p.subtitle) { y += Math.round(fsT * 0.2); for (const ln of _wrap(ctx, p.subtitle, W - margin * 2).slice(0, 3)) { ctx.fillText(ln, margin, y); y += Math.round(fsT * 0.58); } }
  if (p.author) {
    ctx.font = `600 ${Math.round(fsT * 0.4)}px system-ui, "Segoe UI", Roboto, Arial, sans-serif`;
    ctx.fillStyle = '#fff';
    ctx.fillText(p.author, margin, H - Math.round(H * 0.035) - Math.round(fsT * 1.1), W - margin * 2);
  }
  return new Promise(r => c.toBlob(r, 'image/png'));
}
