/* ══ composer ═════════════════════════════════════════════════════ */
/* Overridden by the live layer, which owns the attachment tray. A file on its
   own is a message -- "look at this" is what dropping it already said -- so an
   empty field with something attached must still be sendable. */
function hasAtts() { return false; }

function goState() {
  const b = $('#go');
  if (busy) { b.disabled = false; b.classList.add('halt'); b.innerHTML = ICON_STOP; b.setAttribute('aria-label', T('gui.stop')); }
  else {
    b.disabled = !$('#ta').value.trim() && !hasAtts();
    b.classList.remove('halt'); b.innerHTML = ICON_SEND; b.setAttribute('aria-label', T('gui.send'));
  }
}

function drawQ() {
  const box = $('#queued'); box.innerHTML = '';
  q.forEach((text, i) => {
    const row = mk('div', 'qrow');
    const v = mk('span', 'v', text);
    const ed = tipBtn(`<path d="${ACT_ICO.pen}"/>`, T('gui.q.edit'));
    const rm = tipBtn('<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>', T('gui.q.remove'));
    ed.onclick = () => {
      const inp = mk('input'); inp.value = text;
      v.replaceWith(inp); inp.focus(); inp.select();
      const done = (commit) => { if (commit && inp.value.trim()) q[i] = inp.value.trim(); drawQ(); };
      inp.onblur = () => done(true);
      inp.onkeydown = (e) => {
        if (composing(e)) return;
        if (e.key === 'Enter') { e.preventDefault(); done(true); }
        if (e.key === 'Escape') { inp.onblur = null; done(false); }
      };
    };
    rm.onclick = () => { q.splice(i, 1); drawQ(); };
    row.append(v, ed, rm);
    box.appendChild(row);
  });
}

/* ONE row for the whole life of a turn: it appears the moment the message is
   sent, rides the tail of the transcript under whatever is streaming, and gives
   way to the answer's own footer when the turn lands. One glyph, one clock -- the
   clock is what proves the stream is alive, since an animation on its own keeps
   dancing over a dead socket. A separate "starting" row on top of this one said
   the same thing twice. */
let liveT0 = 0, liveTick = null, liveRow = null;
/* The one indicator. Decorative to a screen reader -- whatever it sits beside
   carries the meaning in words, and three animated bars announced as anything
   would be noise on a row that repaints four times a second. */
function workGlyph() {
  const g = mk('span', 'wkg');
  g.setAttribute('aria-hidden', 'true');
  g.append(mk('i'), mk('i'), mk('i'));
  return g;
}

/* Its svg twin, for a node drawn inside a graph. Same three dots, same shared
   keyframes; only the element type differs. `y` is the baseline the bars used
   to stand on, so the dots are centred a glyph-height above it and the call
   sites keep the coordinates they already pass. */
function workGlyphSvg(x, y) {
  const ns = 'http://www.w3.org/2000/svg';
  const g = document.createElementNS(ns, 'g');
  g.setAttribute('class', 'workv');
  g.setAttribute('aria-hidden', 'true');
  [0, 4, 8].forEach((dx) => {
    const c = document.createElementNS(ns, 'circle');
    c.setAttribute('cx', String(x + dx + 1));
    c.setAttribute('cy', String(y - 4.5));
    c.setAttribute('r', '1.5');
    g.appendChild(c);
  });
  return g;
}

function turnLiveRow() {
  if (liveRow) return liveRow;
  liveRow = mk('div', 'turnlive');
  liveRow.append(workGlyph(), mk('span', 'lb'));
  liveRow.setAttribute('aria-live', 'off');
  return liveRow;
}
function drawTurnLive() {
  const row = turnLiveRow();
  if (!busy) {
    row.remove();
    if (liveTick) { clearInterval(liveTick); liveTick = null; }
    liveT0 = 0;
    return;
  }
  if (!liveT0) liveT0 = Date.now();
  const paint = () => {
    const stage = $('#stage');
    /* Only moved when something new landed after it: re-inserting a node
       restarts its CSS animation, so an unconditional append would make the
       glyph stutter four times a second. */
    if (stage && stage.lastElementChild !== row) stage.appendChild(row);
    /* The clock, and nothing else. It used to switch between three words
       (starting / writing / working) for a distinction the reader cannot act on:
       whether the stream has produced a token yet does not change what to do
       next, and the glyph already says the turn is alive. The word survives as
       the row's accessible name, where a reader who gets the row as text still
       needs it. */
    row.querySelector('.lb').textContent = dur(Date.now() - liveT0);
    row.setAttribute('aria-label', T('gui.live.busy', { t: dur(Date.now() - liveT0) }));
  };
  paint();
  if (!liveTick) liveTick = setInterval(paint, 250);
}

function drawMeter() {
  $('#meter').textContent = busy ? T('gui.meter.running')
    : use ? T('gui.meter.usage', { calls: use.calls, in: (use.in / 1000).toFixed(1), out: (use.out / 1000).toFixed(1) })
    : '';
  drawTurnLive();
}

const pickRun = (s) => /超时|timeout|登录|bug|修|fix|报错|定位|回调/.test(s) ? RUNS.fix : RUNS.gtm;

function send(text) {
  if (busy) { q.push(text); drawQ(); toast('已排队，本轮结束后发出'); return; }
  const p = $('#stage').querySelector('.pitch'); if (p) p.remove();
  ask(text);
  busy = true; use = null; tl = []; raw = [];
  drawMeter(); goState(); drawList();
  const run = pickRun(text);
  lastRun = run;
  const s = sess(cur);
  if (s && !s.run) {
    s.run = run.key; s.status = null;
    if (s.title === '新任务') { s.title = run.title; $('#title').textContent = plainTitle(s.title); }
    drawList();
  }
  replay(run, false);
}

function halt() {
  stop_(); busy = false;
  noteRow('已中断 · 上面的步骤保留', '', { quiet: true, host: $('#stage') });
  drawMeter(); goState(); drawList();
}

