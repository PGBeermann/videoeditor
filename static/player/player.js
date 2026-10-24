/* Reproductor interactivo: capítulos, subtítulos, transcripción y preguntas incrustadas.
   Modos: "public" (enlace web, registra analítica) y "scorm" (paquete SCORM 1.2 para Moodle). */
(function () {
  'use strict';
  function h(tag, attrs, kids) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      if (attrs[k] == null || attrs[k] === false) continue;
      if (k === 'class') e.className = attrs[k];
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c != null) e.append(c.nodeType ? c : document.createTextNode(c)); });
    return e;
  }
  function fmt(s) { s = Math.max(0, s || 0); var m = Math.floor(s / 60), x = Math.floor(s % 60); return m + ':' + (x < 10 ? '0' : '') + x; }

  /* ----- Adaptador SCORM 1.2 (busca el objeto API en ventanas padre, según ADL) ----- */
  function findAPI(win) {
    var tries = 0;
    while (win && tries < 10) { if (win.API) return win.API; if (win.parent === win) break; win = win.parent; tries++; }
    return null;
  }
  function Scorm() {
    this.api = findAPI(window) || (window.opener ? findAPI(window.opener) : null);
    this.ok = false;
    if (this.api) { this.ok = String(this.api.LMSInitialize('')) === 'true'; }
    if (this.ok && this.get('cmi.core.lesson_status') === 'not attempted') this.set('cmi.core.lesson_status', 'incomplete');
  }
  Scorm.prototype.get = function (k) { try { return this.ok ? this.api.LMSGetValue(k) : ''; } catch (e) { return ''; } };
  Scorm.prototype.set = function (k, v) { try { if (this.ok) this.api.LMSSetValue(k, String(v)); } catch (e) { /* */ } };
  Scorm.prototype.commit = function () { try { if (this.ok) this.api.LMSCommit(''); } catch (e) { /* */ } };
  Scorm.prototype.finish = function () { try { if (this.ok) { this.api.LMSCommit(''); this.api.LMSFinish(''); this.ok = false; } } catch (e) { /* */ } };

  function mount(root, cfg) {
    root.classList.add('evp');
    var video = h('video', { controls: '', playsinline: '', preload: 'metadata', poster: cfg.poster || null });
    video.src = cfg.video;
    (cfg.tracks || []).forEach(function (t, i) {
      video.append(h('track', { kind: 'subtitles', src: t.src, srclang: t.lang, label: t.label, default: i === 0 ? '' : null }));
    });
    var overlay = h('div', { class: 'evp-quiz hidden' });
    var stage = h('div', { class: 'evp-stage' }, [video, overlay]);
    var chapters = (cfg.chapters || []).slice().sort(function (a, b) { return a.t - b.t; });
    var quizzes = (cfg.quizzes || []).map(function (q, i) { return Object.assign({ idx: i }, q); }).sort(function (a, b) { return a.t - b.t; });
    var answered = {}, score = 0;
    var chList = h('ol', { class: 'evp-ch' });
    chapters.forEach(function (c) {
      chList.append(h('li', { onclick: function () { video.currentTime = c.t + 0.01; video.play(); } }, [h('span', { class: 't' }, [fmt(c.t)]), h('span', {}, [c.title])]));
    });
    var speed = h('select', { class: 'evp-speed', title: 'Velocidad', 'aria-label': 'Velocidad de reproducción' });
    [0.75, 1, 1.25, 1.5, 1.75, 2].forEach(function (r) { var o = h('option', { value: r }, [r + '×']); if (r === 1) o.selected = true; speed.append(o); });
    speed.onchange = function () { video.playbackRate = +speed.value; };
    var progress = h('span', { class: 'evp-progress' });
    var side = h('aside', { class: 'evp-side' }, [
      h('h1', {}, [cfg.title]),
      h('div', { class: 'evp-tools' }, [speed, cfg.transcript ? h('a', { href: cfg.transcript, target: '_blank', class: 'evp-btn' }, ['Transcripción']) : null, progress]),
      chapters.length ? h('h2', {}, ['Capítulos']) : null, chapters.length ? chList : null,
      quizzes.length ? h('p', { class: 'evp-note' }, [quizzes.length + ' pregunta' + (quizzes.length > 1 ? 's' : '') + ' durante el video.']) : null,
    ]);
    root.append(stage, side);

    function updateProgress() {
      if (!quizzes.length) return;
      var n = Object.keys(answered).length;
      progress.textContent = 'Preguntas: ' + n + '/' + quizzes.length + (n ? ' · ' + Math.round(score / quizzes.length * 100) + ' %' : '');
    }
    updateProgress();

    /* Capítulo activo */
    video.addEventListener('timeupdate', function () {
      var t = video.currentTime, cur = -1;
      chapters.forEach(function (c, i) { if (t >= c.t) cur = i; });
      Array.prototype.forEach.call(chList.children, function (li, i) { li.classList.toggle('on', i === cur); });
      // ¿Llegó a una pregunta?
      for (var i = 0; i < quizzes.length; i++) {
        var q = quizzes[i];
        if (!answered[q.idx] && t >= q.t && t < q.t + 1.5 && !video.seeking) { ask(q); break; }
      }
    });

    function ask(q) {
      video.pause();
      if (document.fullscreenElement) document.exitFullscreen().catch(function () { });
      overlay.innerHTML = '';
      var opts = h('div', { class: 'evp-opts' });
      var fb = h('div', { class: 'evp-fb' });
      var cont = h('button', { class: 'evp-btn primary hidden', onclick: function () { overlay.classList.add('hidden'); video.play(); } }, ['Continuar ▶']);
      q.options.forEach(function (o, i) {
        if (!String(o).trim()) return;
        opts.append(h('button', { class: 'evp-opt', 'data-i': i, onclick: function () {
          if (answered[q.idx] !== undefined) return;
          var ok = i === +q.correct;
          answered[q.idx] = i; if (ok) score++;
          Array.prototype.forEach.call(opts.children, function (b) { b.disabled = true; });
          this.classList.add(ok ? 'ok' : 'bad');
          if (!ok) { var c = opts.querySelector('[data-i="' + (+q.correct) + '"]'); if (c) c.classList.add('ok'); }
          fb.textContent = (ok ? '¡Correcto! ' : 'No es correcto. ') + (q.explanation || '');
          fb.className = 'evp-fb ' + (ok ? 'ok' : 'bad');
          cont.classList.remove('hidden');
          report(q, i, ok); updateProgress();
        } }, [o]));
      });
      overlay.append(h('div', { class: 'evp-card' }, [h('div', { class: 'evp-qh' }, ['Pregunta']), h('p', { class: 'evp-q' }, [q.question || '']), opts, fb, cont]));
      overlay.classList.remove('hidden');
    }

    /* Analítica / SCORM */
    var scorm = cfg.mode === 'scorm' ? new Scorm() : null;
    var viewId = null, lastBeat = -10;
    function report(q, choice, ok) {
      if (cfg.mode === 'public' && cfg.api) {
        fetch(cfg.api + '/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ view_id: viewId, quiz_idx: q.idx, choice: choice }) }).catch(function () { });
      }
      if (scorm && scorm.ok) {
        var n = Object.keys(answered).length - 1;
        scorm.set('cmi.interactions.' + n + '.id', 'Q' + (q.idx + 1));
        scorm.set('cmi.interactions.' + n + '.type', 'choice');
        scorm.set('cmi.interactions.' + n + '.student_response', String(choice));
        scorm.set('cmi.interactions.' + n + '.correct_responses.0.pattern', String(q.correct));
        scorm.set('cmi.interactions.' + n + '.result', ok ? 'correct' : 'wrong');
        scorm.set('cmi.core.score.min', 0); scorm.set('cmi.core.score.max', 100);
        scorm.set('cmi.core.score.raw', Math.round(score / quizzes.length * 100));
        scorm.commit();
      }
    }
    if (scorm && scorm.ok) {
      var loc = parseFloat(scorm.get('cmi.core.lesson_location'));
      if (loc > 5) video.addEventListener('loadedmetadata', function () { if (loc < video.duration - 5 && confirm('¿Continuar desde ' + fmt(loc) + '?')) video.currentTime = loc; }, { once: true });
    }
    video.addEventListener('timeupdate', function () {
      var t = video.currentTime;
      if (cfg.mode === 'public' && cfg.api && !video.paused && Math.abs(t - lastBeat) >= 5) {
        lastBeat = t;
        fetch(cfg.api + '/view', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ view_id: viewId, t: t }) })
          .then(function (r) { return r.json(); }).then(function (d) { viewId = d.view_id; }).catch(function () { });
      }
      if (scorm && scorm.ok && Math.abs(t - lastBeat) >= 10) { lastBeat = t; scorm.set('cmi.core.lesson_location', t.toFixed(1)); scorm.commit(); }
    });
    video.addEventListener('ended', function () {
      if (!scorm || !scorm.ok) return;
      var allAnswered = Object.keys(answered).length >= quizzes.length;
      if (quizzes.length) {
        var pct = Math.round(score / quizzes.length * 100);
        scorm.set('cmi.core.score.raw', pct);
        scorm.set('cmi.core.lesson_status', allAnswered ? (pct >= 70 ? 'passed' : 'failed') : 'incomplete');
      } else scorm.set('cmi.core.lesson_status', 'completed');
      scorm.commit();
    });
    window.addEventListener('beforeunload', function () { if (scorm) { scorm.set('cmi.core.lesson_location', video.currentTime.toFixed(1)); scorm.finish(); } });
    document.addEventListener('keydown', function (e) {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
      if (e.key === ' ' && document.activeElement !== video) { e.preventDefault(); video.paused ? video.play() : video.pause(); }
    });
  }
  window.EVPlayer = { mount: mount };
})();
