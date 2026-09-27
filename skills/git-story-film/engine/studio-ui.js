'use strict';
// The studio: the page a film opens in for review. Load it after core.js and before the film's own script.
// defineFilm() calls buildPlayer(), and this declaration replaces core.js's basic bar with
//   - the film, sized so the transport and the track stay on screen;
//   - a scrub track as wide as the film: acts as bands, shots as numbered cells, drag anywhere to seek;
//   - playback from the current frame, with the score in sync (rendered once, then started at the offset);
//   - the storyboard underneath: one frame per shot drawn live from the film; click one to jump there.
// Shots come from shots.js (python3 scripts/storyboard.py shots.json --js shots.js) or ?shots=<path>.
// Without it every timeline scene counts as a shot; shots past the film's end show as not built yet.
// Keys: space play/pause · ←/→ one frame · shift+←/→ one second · [ ] previous/next shot · Home/End.
// Renders load the film with ?bare, which never builds a player, so none of this runs there.
function buildPlayer() {
  const N = FILM.NDRAW, DUR = FILM.DUR, fps = FPS_DRAW, qs = new URLSearchParams(location.search);
  const TINT = ['#e7cf8f', '#b9d3a8', '#a9c8e0', '#e2aaa2', '#c9b6df', '#eab784', '#9fd0c9', '#d6c3a0'];
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[ch]);
  const tc = (t, tenths = true) => { const d = Math.floor(Math.max(0, t) * 10 + 1e-6), m = Math.floor(d / 600), s = (d - m * 600) / 10;
    return `${m}:${tenths ? s.toFixed(1).padStart(4, '0') : String(Math.floor(s)).padStart(2, '0')}`; };
  const frameAt = t => Math.max(0, Math.min(N - 1, Math.floor(t * fps + 1e-6)));
  const place = (e, a, b) => { e.style.left = `${Math.max(0, a) / DUR * 100}%`; e.style.width = `${Math.max(0, Math.min(b, DUR) - Math.max(0, a)) / DUR * 100}%`; };
  const nn = n => String(n).padStart(2, '0');

  const css = el('style'); css.textContent = `
body.studio{display:block;margin:0;min-height:100vh;background:#16151b;color:#e8e4dc;font:13px/1.45 -apple-system,system-ui,"PingFang SC",sans-serif}
#studio{max-width:1440px;margin:0 auto;padding:12px 16px 56px;font-variant-numeric:tabular-nums}
#studio .st-flip{display:inline-grid;justify-items:center}#studio .st-flip>span{grid-area:1/1}#studio .st-flip>span:not(.on){visibility:hidden}
#studio .st-stage{margin:0 auto;width:min(100%,calc((100vh - 170px) * var(--ar)))}
#studio canvas#c{display:block;width:100%;height:auto;max-width:none;max-height:none;background:#000;border-radius:4px;box-shadow:0 0 0 1px #ffffff1a}
#studio .st-bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin:10px 0 8px}
#studio button{font:inherit;color:#f1ece2;background:#ffffff14;border:1px solid #ffffff2e;border-radius:5px;padding:5px 11px;cursor:pointer}
#studio button:hover{background:#ffffff26}#studio button:disabled{opacity:.45;cursor:default}
#studio button[aria-pressed="true"]{background:#c9962f;border-color:#c9962f;color:#16151b}
#studio .st-tc{font:600 15px ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;min-width:13.5ch}
#studio .st-now{flex:1 1 240px;min-width:0;color:#c9c2b6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#studio .st-info{flex:1 0 100%;min-width:0;font:11px ui-monospace,Menlo,monospace;color:#8f897e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#studio .st-track{position:relative;height:64px;border-radius:5px;background:#ffffff0a;touch-action:none;user-select:none;-webkit-user-select:none}
#studio .st-band{position:absolute;top:0;height:20px;box-sizing:border-box;padding:1px 6px 0;border-top:4px solid var(--c);border-left:1px solid #16151b;font-size:11px;line-height:15px;color:#d6cfc2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#studio .st-cell{position:absolute;top:22px;bottom:3px;box-sizing:border-box;border-left:1px solid #16151b;border-radius:3px;background:var(--c);opacity:.5;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:11px;font-weight:700;color:#16151b}
#studio .st-track.flat .st-cell{top:3px}#studio .st-cell.on{opacity:1}
#studio input.st-scrub{position:absolute;inset:0;width:100%;height:100%;margin:0;padding:0;background:transparent;-webkit-appearance:none;appearance:none;cursor:pointer}
#studio input.st-scrub::-webkit-slider-runnable-track{height:100%;background:transparent;border:0}
#studio input.st-scrub::-webkit-slider-thumb{-webkit-appearance:none;width:3px;height:64px;background:#ff5a4f;box-shadow:0 0 0 1px #000a}
#studio input.st-scrub::-moz-range-track{height:100%;background:transparent;border:0}
#studio input.st-scrub::-moz-range-progress{background:transparent}
#studio input.st-scrub::-moz-range-thumb{width:3px;height:64px;border:0;border-radius:0;background:#ff5a4f}
#studio input.st-scrub:focus-visible{outline:2px solid #c9962f;outline-offset:3px}
#studio .st-hover{position:absolute;bottom:calc(100% + 4px);transform:translateX(-50%);padding:2px 7px;border-radius:3px;background:#000d;color:#fff;font:11px ui-monospace,Menlo,monospace;white-space:nowrap;pointer-events:none;z-index:2}
#studio .st-ticks{position:relative;height:16px;margin-top:3px}
#studio .st-ticks span{position:absolute;transform:translateX(-50%);font:10px ui-monospace,Menlo,monospace;color:#8f897e}
#studio .st-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;margin:24px 0 10px}
#studio .st-head h2{margin:0;font-size:15px;font-weight:600;color:#d6cfc2}#studio .st-head span{color:#8f897e;font-size:12px}
#studio .st-board{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
#studio .st-shot{display:flex;flex-direction:column;justify-content:flex-start;width:100%;text-align:left;padding:6px 6px 10px;border-radius:6px;border:0;background:#ffffff0a;color:inherit;outline:2px solid transparent;outline-offset:0}
#studio .st-shot:hover{background:#ffffff16}#studio .st-shot.on{outline-color:#ff5a4f}#studio .st-shot:focus-visible{outline-color:#c9962f}
#studio .st-shot canvas,#studio .st-todo{display:block;width:100%;height:auto;aspect-ratio:var(--ar);border-radius:4px;background:#000}
#studio .st-todo{display:grid;place-items:center;background:repeating-linear-gradient(135deg,#ffffff0a 0 8px,transparent 8px 16px);color:#8f897e;font-size:12px}
#studio .st-h{display:flex;gap:8px;align-items:baseline;margin:6px 2px 2px}#studio .st-h b{font-size:15px}
#studio .st-h span{color:#ff8a80;font:12px ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
#studio .st-act{margin:0 2px;color:#8f897e;font-size:11px}#studio .st-card{margin:2px;color:#e2b85a;font-weight:600;font-size:12px}
#studio .st-line{margin:3px 2px 0;color:#c9c2b6;font-size:12px}`;
  document.head.append(css); document.body.classList.add('studio');

  const root = el('div'), stage = el('div', 'st-stage'); root.id = 'studio'; root.style.setProperty('--ar', (OUT_W / OUT_H).toFixed(5));
  document.body.prepend(root); stage.append(cv);
  const bar = el('div', 'st-bar'), bPlay = el('button'), bSnd = el('button'), bPrev = el('button', '', '‹ shot'), bNext = el('button', '', 'shot ›');
  const time = el('span', 'st-tc'), now = el('span', 'st-now'), info = el('span', 'st-info');
  [bPlay, bSnd, bPrev, bNext].forEach(b => { b.type = 'button'; }); bPlay.title = 'space'; bPrev.title = 'previous shot  ['; bNext.title = 'next shot  ]';
  // A toggle stacks every label it can show in one grid cell and reveals one, so switching never changes its width.
  const flip = (b, labels) => { const box = el('span', 'st-flip'), spans = {}; for (const [k, t] of Object.entries(labels)) box.append(spans[k] = el('span', '', t)); b.append(box);
    return k => { for (const [j, s] of Object.entries(spans)) s.classList.toggle('on', j === k); }; };
  const setPlay = flip(bPlay, { play: '▶ play', pause: '❚❚ pause' }), setSnd = flip(bSnd, { off: '♪ sound off', wait: '♪ loading…', on: '♪ sound on', none: '♪ no score' });
  setPlay('play'); setSnd(FILM.score ? 'off' : 'none'); bSnd.disabled = !FILM.score; bSnd.setAttribute('aria-pressed', 'false');
  bar.append(bPlay, bSnd, bPrev, bNext, time, now, info);
  const track = el('div', 'st-track'), scrub = el('input', 'st-scrub'), hover = el('div', 'st-hover'), ticks = el('div', 'st-ticks');
  Object.assign(scrub, { type: 'range', min: 0, max: N - 1, step: 1, value: 0 }); scrub.setAttribute('aria-label', 'position in the film'); hover.hidden = true;
  track.append(scrub, hover);
  const head = el('div', 'st-head'), hint = el('span'), board = el('div', 'st-board'); head.append(el('h2', '', 'storyboard'), hint);
  root.append(stage, bar, track, ticks, head, board);
  ui = { scrub, info, msg: info };   // core.js's show() keeps the playhead and the readout current

  const st = { playing: false, sound: false, ac: null, buf: null, src: null, t0: 0, off: 0, shots: [], on: null, jobs: [] };
  const shotAt = t => { let s = null; for (const x of st.shots) { if (x.missing || x.start > t + 1e-6) break; s = x; } return s; };
  const label = () => setPlay(st.playing ? 'pause' : 'play');
  function seek(i) { if (st.playing) pause(); cur = -1; show(Math.max(0, Math.min(N - 1, Math.round(i)))); }

  // The picture never waits for the sound. The score renders once off the main thread (after the storyboard
  // frames, or as soon as sound is switched on); when it is ready the audio joins at the picture's position
  // and becomes the clock, so a slow frame is dropped rather than drifting.
  const clock = () => st.src ? st.ac.currentTime : performance.now() / 1000;
  const position = () => st.off + Math.max(0, clock() - st.t0);
  const ensureScore = () => st.bufP || (st.bufP = (() => { const sr = 48000, oac = new OfflineAudioContext(2, Math.ceil(sr * DUR), sr); FILM.score(oac, 0, oac.destination); return oac.startRendering(); })()
    .then(b => { st.buf = b; syncSound(); return b; }, e => { console.error(e); st.bufP = null; st.sound = false; syncSound(); throw e; }));
  function play() {
    if (st.playing) return;
    st.playing = true; st.off = (cur < 0 || cur >= N - 1 ? 0 : cur) / fps; st.t0 = performance.now() / 1000;
    label(); requestAnimationFrame(tick); if (st.sound) attachSound();
  }
  async function attachSound() {
    try { st.ac = st.ac || new AudioContext(); await st.ac.resume(); await ensureScore(); } catch (e) { return; }
    if (!st.playing || !st.sound || st.src) return;
    const t = Math.min(DUR, position() + .05);   // audio starts 50 ms from now, at the frame the picture will be on
    st.src = st.ac.createBufferSource(); st.src.buffer = st.buf; st.src.connect(st.ac.destination);
    st.off = t; st.t0 = st.ac.currentTime + .05; st.src.start(st.t0, t);
  }
  function detachSound() { const t = position(); stopSound(); st.off = t; st.t0 = performance.now() / 1000; }
  function stopSound() { if (!st.src) return; try { st.src.stop(); } catch (e) {} st.src.disconnect(); st.src = null; }
  function tick() {
    if (!st.playing) return;
    const t = position();
    if (t >= DUR) { pause(); cur = -1; show(N - 1); return; }
    show(frameAt(t)); requestAnimationFrame(tick);
  }
  function pause() { st.playing = false; stopSound(); label(); }
  function syncSound() {
    bSnd.setAttribute('aria-pressed', String(st.sound));
    if (FILM.score) setSnd(!st.sound ? 'off' : st.buf ? 'on' : 'wait');
  }
  function jump(d) {
    const live = st.shots.filter(s => !s.missing); if (!live.length) return;
    const t = Math.max(0, cur) / fps, s = shotAt(t); let k = s ? live.indexOf(s) : -1;
    if (d < 0 && s && t - s.start > .5) k += 1;   // back to the start of this shot first, like a media player
    seek(frameAt(live[Math.max(0, Math.min(live.length - 1, k + d))].start));
  }
  bPlay.onclick = () => (st.playing ? pause() : play());
  bSnd.onclick = () => {
    st.sound = !st.sound; syncSound();
    if (!st.sound) { if (st.playing) detachSound(); return; }
    ensureScore().catch(() => {}); if (st.playing) attachSound();
  };
  bPrev.onclick = () => jump(-1); bNext.onclick = () => jump(1);
  scrub.addEventListener('input', () => seek(+scrub.value));
  track.addEventListener('pointermove', e => {
    const r = track.getBoundingClientRect(), f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), s = shotAt(f * DUR);
    hover.hidden = false; hover.textContent = tc(f * DUR) + (s ? `  ·  ${nn(s.n)}${s.card ? ' ' + s.card : ''}` : '');
    const w = hover.offsetWidth; hover.style.left = `${Math.max(w / 2, Math.min(r.width - w / 2, f * r.width))}px`;
  });
  track.addEventListener('pointerleave', () => { hover.hidden = true; });
  addEventListener('keydown', e => {
    const t = e.target; if (e.metaKey || e.ctrlKey || e.altKey || (t.closest && t.closest('textarea,select,input:not(.st-scrub),[contenteditable]'))) return;
    const k = e.key, at = Math.max(0, cur);
    if (k === ' ') { e.preventDefault(); st.playing ? pause() : play(); }
    else if (k === 'ArrowLeft' || k === 'ArrowRight') { if (t === scrub && !e.shiftKey) return; e.preventDefault(); seek(at + (k === 'ArrowRight' ? 1 : -1) * (e.shiftKey ? fps : 1)); }
    else if (k === '[' || k === ']') { e.preventDefault(); jump(k === ']' ? 1 : -1); }
    else if (k === 'Home' || k === 'End') { e.preventDefault(); seek(k === 'Home' ? 0 : N - 1); }
  });
  addEventListener('keyup', e => { if (e.key === ' ' && e.target.closest && e.target.closest('button')) e.preventDefault(); });   // space already toggled play; don't also click the focused button

  // Readout and highlight follow whatever frame core.js last drew (playback, scrubbing, keys).
  let seen = -2;
  (function watch() { if (cur !== seen) { seen = cur; const t = Math.max(0, cur) / fps; time.textContent = `${tc(t)} / ${tc(DUR, false)}`; mark(shotAt(t)); } requestAnimationFrame(watch); })();
  function mark(s) {
    if (s === st.on) return;
    if (st.on) { st.on.cell?.classList.remove('on'); st.on.el?.classList.remove('on'); }
    st.on = s; now.textContent = s ? [nn(s.n), s.act, s.card, s.line].filter(Boolean).join('  ·  ') : '';
    if (s) { s.cell?.classList.add('on'); s.el?.classList.add('on'); }
  }

  function useShots(data) {
    const given = data && Array.isArray(data.shots) && data.shots.length ? data.shots : null; let acc = 0;
    const list = given ? given.map((s, k) => ({ n: s.n || k + 1, start: +s.start || 0, end: s.end == null ? null : +s.end, key: s.key == null ? null : +s.key, act: s.act || '', card: s.card || '', lines: s.lines || {} }))
      : FILM.timeline.map((s, k) => { const x = { n: k + 1, start: acc, end: acc + s.dur, key: null, act: '', card: s.name || '', lines: {} }; acc += s.dur; return x; });
    list.sort((a, b) => a.start - b.start);
    list.forEach((s, k) => { if (s.end == null || s.end <= s.start) s.end = k + 1 < list.length ? list[k + 1].start : DUR; s.missing = s.start >= DUR - 1e-6; s.line = Object.values(s.lines)[0] || ''; });
    st.shots = list; track.classList.toggle('flat', !list.some(s => s.act));
    drawTrack(); drawBoard(); seen = -2;
  }
  function drawTrack() {
    track.querySelectorAll('.st-band,.st-cell').forEach(e => e.remove());
    let g = -1, band = null;
    for (const s of st.shots.filter(x => !x.missing)) {
      if (!band || band.act !== s.act) { g++; band = { act: s.act, start: s.start, el: s.act ? el('div', 'st-band', s.act) : null }; if (band.el) { band.el.style.setProperty('--c', TINT[g % TINT.length]); track.insertBefore(band.el, scrub); } }
      if (band.el) place(band.el, band.start, s.end);
      const c = el('div', 'st-cell', String(s.n)); place(c, s.start, s.end); c.style.setProperty('--c', TINT[g % TINT.length]); s.cell = c; track.insertBefore(c, scrub);
    }
    ticks.textContent = ''; const step = DUR <= 180 ? 10 : DUR <= 600 ? 30 : 60;
    for (let t = step; t < DUR - 1e-6; t += step) { const x = el('span', '', tc(t, false)); x.style.left = `${t / DUR * 100}%`; ticks.append(x); }
  }
  function drawBoard() {
    board.textContent = ''; st.jobs = [];
    const tw = 400, th = Math.round(tw * OUT_H / OUT_W);
    for (const s of st.shots) {
      const b = el('button', 'st-shot'); let pic; b.type = 'button';
      if (s.missing) { pic = el('div', 'st-todo', 'not in the film yet'); b.disabled = true; }
      else { pic = el('canvas'); pic.width = tw; pic.height = th; st.jobs.push({ c: pic, frame: frameAt(s.key != null ? s.key : (s.start + Math.min(s.end, DUR)) / 2) }); b.onclick = () => seek(frameAt(s.start)); }
      const meta = el('div');
      meta.innerHTML = `<div class="st-h"><b>${nn(s.n)}</b><span>${tc(s.start, false)}–${tc(s.end, false)}</span></div>` + (s.act ? `<div class="st-act">${esc(s.act)}</div>` : '') +
        (s.card ? `<div class="st-card">${esc(s.card)}</div>` : '') + Object.values(s.lines).map(v => `<p class="st-line">${esc(v).split(' / ').join('<br>')}</p>`).join('');
      b.append(pic, meta); s.el = b; board.append(b);
    }
    const built = st.shots.filter(s => !s.missing).length;
    hint.textContent = `${st.shots.length} shots${built < st.shots.length ? ` · ${built} in the film so far` : ''} · each frame drawn live from the film`;
    setTimeout(nextThumb, 0);
  }
  // One thumbnail per task, drawn on the film's own canvas and restored in the same task, so nothing flickers.
  function nextThumb() {
    if (window.__error) return;
    if (!window.__ready || st.playing) { setTimeout(nextThumb, st.playing ? 400 : 100); return; }
    const job = st.jobs.shift();
    if (!job) { if (FILM.score) ensureScore().catch(() => {}); return; }   // storyboard done: get the sound ready too
    const keep = cur; cur = -1; show(job.frame); job.c.getContext('2d').drawImage(cv, 0, 0, job.c.width, job.c.height); cur = -1; show(keep < 0 ? 0 : keep);
    setTimeout(nextThumb, 0);
  }

  if (window.SHOTS) useShots(window.SHOTS);
  else { const s = document.createElement('script'); s.src = qs.get('shots') || 'shots.js'; s.onload = () => useShots(window.SHOTS); s.onerror = () => useShots(null); document.head.append(s); }
}
