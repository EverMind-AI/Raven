'use strict';
// The page Raven travels across: marks, lettering, the branch, beads, the nest and props.
// The branch is the timeline. It is drawn up to "today"; the future is a faint guide.
// Each commit is a shiny bead hanging from the branch, because ravens hoard shiny things.

const QT2 = t => Math.floor(t * 12 + 1e-6) / 12, QT1 = t => Math.floor(t * 24 + 1e-6) / 24;
const DAY = {
  '06-28': {x: 700, label: '6月28日', count: '第 1 次提交'},
  '06-29': {x: 1650, label: '6月29日', count: '累计 8 次提交'},
  '06-30': {x: 2500, label: '6月30日', count: '累计 50 次提交'},
};
const branchY = x => 800 + 12 * Math.sin(x / 620 + .7) + 5 * Math.sin(x / 190 + 1.3);
const branchT = x => clamp(22 - (x + 600) * .0022, 9, 22);
const topY = x => branchY(x) - branchT(x) / 2;
const CHICK_S = 1.35, FLEDGE_S = 1.1;
const hop = (a, b, u, h) => [lerp(a[0], b[0], u), lerp(a[1], b[1], u) - 4 * h * u * (1 - u)];
const lin = x => x;

// ---------- marks ----------
function pLine(c, pts, {w = 1.6, col = RC.pencil, al = 1, seed = 1, close = false, rough = .9} = {}) {
  if (pts.length < 2) return;
  c.save(); c.strokeStyle = col; c.lineCap = 'round'; c.lineJoin = 'round';
  for (let k = 0; k < 2; k++) { c.globalAlpha = al * (k ? .3 : .8); c.lineWidth = w * (k ? .55 : 1); wob(c, pts, rough + k * .9, seed + k * 13, close, {pressure: .55}); }
  c.restore();
}
function iLine(c, pts, {w = 2.6, col = RC.ink, al = 1, seed = 1, close = false, rough = .5} = {}) {
  if (pts.length < 2) return;
  c.save(); c.strokeStyle = col; c.lineCap = 'round'; c.lineJoin = 'round'; c.globalAlpha *= al; c.lineWidth = w; wob(c, pts, rough, seed, close, {pressure: .85}); c.restore();
}
// One call for both materials: pencil keeps its colour pencils, ink turns graphite black and bolder.
function mLine(c, look, pts, o = {}) {
  if (look !== 'ink') return pLine(c, pts, o);
  const keep = o.col === RC.red || o.col === RC.gold || o.col === RC.paper;
  iLine(c, pts, {...o, w: (o.w ?? 1.6) * 1.5, col: keep ? o.col : RC.ink});
}
function partial(pts, u) {
  if (u >= 1) return pts; if (u <= 0) return pts.slice(0, 1);
  const L = [0]; for (let i = 1; i < pts.length; i++) L.push(L[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  const d = L[L.length - 1] * u, out = [pts[0]];
  for (let i = 1; i < pts.length; i++) { if (L[i] <= d) out.push(pts[i]); else { const t = (d - L[i - 1]) / (L[i] - L[i - 1]); out.push([lerp(pts[i - 1][0], pts[i][0], t), lerp(pts[i - 1][1], pts[i][1], t)]); break; } }
  return out;
}
// Handwriting appears one character at a time, the newest one still fading in.
// The handwriting face has a hairline space, so a gap between Chinese and Latin text vanishes.
// Every space advances at least a third of the size; the same advance is used to lay out the line.
const charAdvance = (c, ch, size) => ch === ' ' ? Math.max(c.measureText(ch).width, size * .34) : c.measureText(ch).width;
const textWidth = (c, text, size) => [...text].reduce((w, ch) => w + charAdvance(c, ch, size), 0);
function writeOn(c, text, x, y, {size = 48, col = RC.pencil, p = 1, align = 'left', al = 1, rot = -.012} = {}) {
  if (p <= 0 || al <= 0) return;
  const chars = [...text], n = chars.length * clamp(p, 0, 1), full = Math.floor(n), frac = n - full;
  // Letters are placed one by one from the left edge, so alignment must not leak in from the caller.
  c.save(); c.font = `600 ${size}px ${RV_FONT}`; c.textBaseline = 'alphabetic'; c.textAlign = 'left';
  const w = textWidth(c, text, size), x0 = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x;
  c.translate(x0, y); c.rotate(rot);
  // Respect any fade the caller has set on the context.
  let xx = 0; const base = c.globalAlpha;
  for (let i = 0; i < chars.length && i <= full; i++) {
    const a = base * al * (i < full ? 1 : frac); if (a <= 0) break;
    c.fillStyle = col; c.globalAlpha = a * .88; c.fillText(chars[i], xx, 0);
    c.globalAlpha = a * .22; c.fillText(chars[i], xx + .7, .5);
    xx += charAdvance(c, chars[i], size);
  }
  c.restore();
}
function star(c, x, y, r, col, points = 4, fill = true) {
  if (r <= .4) return;
  c.save(); c.beginPath();
  for (let i = 0; i < points * 2; i++) { const a = i / (points * 2) * TAU - Math.PI / 2, rr = i % 2 ? r * (points === 4 ? .24 : .45) : r; i ? c.lineTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr) : c.moveTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr); }
  c.closePath(); if (fill) { c.fillStyle = col; c.fill(); } else { c.strokeStyle = col; c.lineWidth = 1.6; c.stroke(); } c.restore();
}
function arrow(c, look, from, to, {col, seed = 1, p = 1, bend = .22, al = 1} = {}) {
  if (p <= 0 || al <= 0) return;
  const mid = [(from[0] + to[0]) / 2 + (to[1] - from[1]) * bend, (from[1] + to[1]) / 2 - (to[0] - from[0]) * bend], pts = [];
  for (let i = 0; i <= 14; i++) { const t = i / 14; pts.push([(1 - t) ** 2 * from[0] + 2 * (1 - t) * t * mid[0] + t * t * to[0], (1 - t) ** 2 * from[1] + 2 * (1 - t) * t * mid[1] + t * t * to[1]]); }
  mLine(c, look, partial(pts, p), {w: 1.5, col, seed, al});
  if (p >= 1) { const a = Math.atan2(to[1] - pts[12][1], to[0] - pts[12][0]), h = 15;
    mLine(c, look, [[to[0] - Math.cos(a - .5) * h, to[1] - Math.sin(a - .5) * h], to, [to[0] - Math.cos(a + .5) * h, to[1] - Math.sin(a + .5) * h]], {w: 1.5, col, seed: seed + 1, al}); }
}
function note(c, look, lines, at, target, p, {col, al = 1, from} = {}) {
  if (p <= 0 || al <= 0) return;
  const color = col ?? (look === 'ink' ? RC.ink : RC.lead);
  lines.forEach((t, i) => writeOn(c, t, at[0], at[1] + i * 36, {size: 31, col: color, p: clamp(p * 1.6 - i * .35, 0, 1), al}));
  if (target) arrow(c, look, from ?? [at[0] - 14, at[1] - 10], target, {col: color, seed: (at[0] | 0) + 3, p: clamp(p * 1.5 - .4, 0, 1), al});
}

// ---------- screen overlays ----------
function caption(c, look, lines, tau, fade = [Infinity, Infinity]) {
  resetT(c); const al = 1 - sm(fade[0], fade[1], tau, lin); let y = 150;
  for (const [text, t0, t1, size = 50] of lines) { writeOn(c, text, 118, y, {size, col: look === 'ink' ? RC.ink : RC.pencil, p: sm(t0, t1, tau, lin), al}); y += size * 1.5; }
}
function header(c, look, id, tau, t0 = -1, from = null) {
  resetT(c); const d = DAY[id], col = look === 'ink' ? RC.ink : RC.pencil, lead = look === 'ink' ? RC.ink : RC.lead;
  const gone = sm(t0, t0 + .18, tau, lin), k = sm(t0 + .18, t0 + .6, tau, lin);
  if (from && gone < 1) { const f = DAY[from]; writeOn(c, f.label, W - 118, 142, {size: 66, col, al: 1 - gone, align: 'right'}); writeOn(c, f.count, W - 118, 196, {size: 31, col: lead, al: 1 - gone, align: 'right'}); }
  writeOn(c, d.label, W - 118, 142, {size: 66, col, p: from ? k : sm(t0, t0 + .45, tau, lin), align: 'right'});
  writeOn(c, d.count, W - 118, 196, {size: 31, col: lead, p: sm(t0 + .3, t0 + .85, tau, lin), align: 'right'});
  mLine(c, look, partial([[W - 330, 214], [W - 220, 211], [W - 116, 216]], sm(t0 + .5, t0 + .9, tau, lin)), {w: 1.3, col: RC.gold, seed: 9});
}
function viewOf(cx, zoom) { const hw = W / 2 / zoom; return [cx - hw, cx + hw]; }
function camAt(tau, keys) {
  if (tau <= keys[0][0]) return keys[0].slice(1);
  for (let i = 1; i < keys.length; i++) if (tau < keys[i][0]) { const a = keys[i - 1], b = keys[i], u = easeIO(clamp((tau - a[0]) / (b[0] - a[0]), 0, 1)); return [lerp(a[1], b[1], u), lerp(a[2], b[2], u), lerp(a[3], b[3], u)]; }
  return keys[keys.length - 1].slice(1);
}
// ---------- the branch ----------
const TWIGS = [
  {x: 250, ang: -2.25, len: 190, t0: 9, t1: 2.5, end: 'leaf', seed: 3},
  {x: 1180, ang: .62, len: 130, t0: 7, t1: 2, end: 'leaf', seed: 5, under: true},
  {x: 1420, ang: -1.05, len: 160, t0: 8, t1: 2.5, end: 'bud', seed: 7},
  {x: 2150, ang: -2.05, len: 190, t0: 8, t1: 2.5, end: 'leaf', seed: 9},
];
const HIGH_TWIG = {x: 2600, ang: -.60, len: 440, t0: 12, t1: 3.5, end: 'leaf', seed: 11};
const INK_TWIGS = [
  {x: 4480, ang: .7, len: 130, t0: 7, t1: 2, end: 'leaf', seed: 15, under: true},
];
function twigPoint(t, u, side = 0) {
  const s = u * t.len, base = [t.x, t.under ? branchY(t.x) + branchT(t.x) / 2 - 2 : topY(t.x) + 3];
  const d = [Math.cos(t.ang), Math.sin(t.ang)], n = [-d[1], d[0]], sag = Math.sin(u * Math.PI) * t.len * .07, th = lerp(t.t0, t.t1, u) / 2;
  return [base[0] + d[0] * s + n[0] * (sag + side * th), base[1] + d[1] * s + n[1] * (sag + side * th)];
}
function leaf(c, look, x, y, ang, len, seed) {
  c.save(); c.translate(x, y); c.rotate(ang);
  const pts = [[0, 0], [len * .28, -len * .22], [len * .64, -len * .24], [len, 0], [len * .64, len * .2], [len * .28, len * .19]], path = curvePath(pts, true, 1.2);
  c.fillStyle = look === 'ink' ? RC.ink : RC.paper; c.fill(path);
  if (look !== 'ink') formHatch(c, path, [0, -len * .3, len, len * .6], {color: RC.lead, spacing: 4.5, length: 10, width: .6, opacity: .3, tone: (xx, yy) => yy > 0 ? .5 : .15, direction: () => .9, seed});
  mLine(c, look, pts, {w: 1.5, close: true, seed});
  mLine(c, look, [[2, 0], [len * .5, -2], [len * .92, 0]], {w: .9, al: .7, seed: seed + 1, col: look === 'ink' ? RC.paper : undefined});
  c.restore();
}
function drawTwig(c, look, t, p = 1) {
  if (p <= 0) return;
  const n = 16, top = [], bot = [];
  for (let i = 0; i <= n; i++) { const u = i / n * p; top.push(twigPoint(t, u, -1)); bot.push(twigPoint(t, u, 1)); }
  if (look === 'ink') { c.fillStyle = RC.ink; c.fill(polyPath([...top, ...bot.slice().reverse()])); }
  mLine(c, look, top, {w: 1.8, seed: t.seed}); mLine(c, look, bot, {w: 1.4, al: .8, seed: t.seed + 1});
  if (p >= 1) { const e = twigPoint(t, 1), a = t.ang + (t.under ? .3 : -.2);
    if (t.end === 'leaf') leaf(c, look, e[0], e[1], a, 62, t.seed + 5);
    else { const b = [[0, 0], [10, -7], [22, 0], [10, 7]]; c.save(); c.translate(e[0], e[1]); c.rotate(t.ang); c.fillStyle = look === 'ink' ? RC.ink : RC.paper; c.fill(curvePath(b, true)); mLine(c, look, b, {w: 1.4, close: true, seed: t.seed + 6}); c.restore(); } }
}
function drawBranch(c, look, view, {drawnTo = 4700, reveal = Infinity} = {}) {
  const x0 = view[0] - 80, x1 = view[1] + 80, end = Math.min(drawnTo, reveal), K = 400, ink = look === 'ink';
  // Fixed chunks keep every mark's seed attached to the paper while the camera travels.
  for (let k = Math.max(0, Math.floor((x0 + 700) / K)); k * K - 700 < Math.min(x1, end); k++) {
    const a = k * K - 700, b = Math.min(a + K + 26, end), top = [], bot = [];
    for (let x = a; ; x += 26) { const xx = Math.min(x, b); top.push([xx, branchY(xx) - branchT(xx) / 2]); bot.push([xx, branchY(xx) + branchT(xx) / 2]); if (xx >= b) break; }
    if (top.length < 2) continue;
    if (ink) { c.fillStyle = RC.ink; c.fill(polyPath([...top, ...bot.slice().reverse()])); }
    mLine(c, look, top, {w: 2.3, seed: 200 + k}); mLine(c, look, bot, {w: 1.8, al: .85, seed: 300 + k});
    for (let x = a + 8; x < b - 8; x += 13) {
      const h = hash(x, 5); if (h > (ink ? .26 : .34)) continue;
      const yt = branchY(x) - branchT(x) / 2, yb = branchY(x) + branchT(x) / 2, L = 6 + hash(x, 6) * 20, bend = (hash(x, 8) - .5) * 6;
      if (h < .1) mLine(c, look, [[x, lerp(yt, yb, .45)], [x + 18 + L, lerp(yt, yb, .5) + bend * .3]], {w: .7, al: .4, seed: x | 0, col: ink ? RC.paper : RC.lead});
      else mLine(c, look, [[x, yb - 3], [x + L * .5 + bend, lerp(yb, yt, .5)], [x + L, yt + 4]], {w: .6 + hash(x, 7) * .5, al: ink ? .5 : .42, seed: x | 0, col: ink ? RC.paper : RC.lead});
    }
    if (ink) mLine(c, look, top.map(([x, y]) => [x, y + 4]), {w: .8, al: .5, col: RC.paper, seed: 500 + k});
  }
  if (end < x1) { c.save(); c.setLineDash([12, 14]); c.strokeStyle = ink ? RC.ink : RC.lead; c.globalAlpha = .3; c.lineWidth = 1.2;
    c.beginPath(); for (let x = Math.max(end, x0); x <= x1; x += 30) { const y = branchY(x); x === Math.max(end, x0) ? c.moveTo(x, y) : c.lineTo(x, y); } c.stroke(); c.restore(); }
}
function drawDayMark(c, look, id, p = 1) {
  if (p <= 0) return; const x = DAY[id].x, y = branchY(x), t = branchT(x);
  mLine(c, look, partial([[x - 3, y - t / 2 - 9], [x + 3, y + t / 2 + 10]], clamp(p * 3, 0, 1)), {w: 2.2, seed: x, col: RC.red});
  writeOn(c, DAY[id].label, x, y + 64, {size: 42, col: look === 'ink' ? RC.ink : RC.pencil, p: clamp(p * 1.4 - .3, 0, 1), align: 'center'});
}

// ---------- commits as beads ----------
const BEADS = {};
function beadsFor(id) {
  if (BEADS[id]) return BEADS[id];
  const out = [];
  if (id === '06-28') out.push({ax: 858, len: 52, r: 14, seed: 1});
  if (id === '06-29') for (let i = 0; i < 7; i++) out.push({ax: 1700 + i * 36 + hash(i, 5) * 10, len: 24 + hash(i, 6) * 46, r: 7.5 + hash(i, 7) * 2.2, seed: 10 + i});
  for (const b of out) b.ay = branchY(b.ax) + branchT(b.ax) / 2 - 1;
  if (id === '06-30') for (let i = 0; i < 42; i++) { const q = twigPoint(HIGH_TWIG, .2 + .76 * i / 41 + (hash(i, 11) - .5) * .015, 1); out.push({ax: q[0], ay: q[1] - 1, len: 14 + hash(i, 12) * 58, r: 6.5 + hash(i, 13) * 3, seed: 100 + i}); }
  for (const b of out) { b.x = b.ax + (hash(b.seed, 9) - .5) * 5; b.y = b.ay + b.len; }
  return BEADS[id] = out;
}
function drawBead(c, look, b, k, tau) {
  if (k <= 0) return;
  const s = clamp(easeOutBack(clamp(k, 0, 1)), 0, 1.25), y = b.ay + (b.y - b.ay) * Math.min(1, k * 1.4), r = b.r * s;
  mLine(c, look, [[b.ax, b.ay], [b.x, y - r]], {w: .8, al: .65, seed: b.seed, rough: .3});
  if (r < .5) return;
  const path = new Path2D(); path.arc(b.x, y, r, 0, TAU);
  if (look === 'ink') { c.fillStyle = '#d6a23a'; c.fill(path); iLine(c, ellPts(b.x, y, r, r, 0, 20), {w: 1.6, close: true, seed: b.seed, rough: .3}); }
  else { c.fillStyle = alpha(RC.gold, .22); c.fill(path);
    formHatch(c, path, [b.x - r, y - r, r * 2, r * 2], {color: RC.gold, spacing: 2.4, length: 6, width: .65, opacity: .75, tone: (xx, yy) => clamp(.45 + (xx - b.x + yy - y) / (r * 3), 0, .9), direction: () => 1, seed: b.seed});
    pLine(c, ellPts(b.x, y, r, r, 0, 20), {w: 1.25, close: true, seed: b.seed, rough: .3}); }
  c.fillStyle = look === 'ink' ? RC.white : '#fffbef'; c.beginPath(); c.arc(b.x - r * .36, y - r * .38, r * .26, 0, TAU); c.fill();
  const tw = (tau * .55 + hash(b.seed, 3)) % 1;
  if (tw < .09) star(c, b.x - r * .3, y - r * .45, r * 1.25 * Math.sin(tw / .09 * Math.PI), look === 'ink' ? RC.white : RC.gold);
}
function drawBeads(c, look, id, t0, tau, span = .6) {
  const list = beadsFor(id);
  list.forEach((b, i) => drawBead(c, look, b, sm(t0 + span * i / Math.max(1, list.length), t0 + span * i / Math.max(1, list.length) + .35, tau, lin), tau));
}

// ---------- nest, egg, shell ----------
const NEST = {x: 700, w: 150, h: 56};
const nestY = () => topY(700) - 6;
function drawNest(c, look, p = 1, front = false) {
  if (p <= 0) return; const cx = NEST.x, cy = nestY(), n = front ? 8 : 28;
  for (let i = 0; i < n; i++) {
    const k = front ? i + 100 : i; if (hash(k, 1) > p * 1.1) continue;
    const y = front ? -NEST.h * .5 + hash(k, 4) * 16 : -NEST.h * .62 + hash(k, 4) * NEST.h, r = NEST.w * (.62 + hash(k, 3) * .5);
    const lean = (hash(k, 7) - .5) * 30, sag = 16 + hash(k, 8) * 18;
    const pts = [[-r, y - 10 + lean * .3], [-r * .5, y + sag * .7], [0, y + sag], [r * .55, y + sag * .55], [r + hash(k, 9) * 30, y - 12 - lean * .3]].map(([x, yy]) => [cx + x, cy + yy]);
    mLine(c, look, pts, {w: .7 + hash(k, 5) * 1.1, al: .28 + hash(k, 6) * .4, seed: k + 20});
  }
  // A few stray twigs poking out of the rim.
  if (!front) for (let i = 0; i < 6; i++) { if (hash(i, 61) > p) continue; const sx = i % 2 ? 1 : -1, x0 = cx + sx * NEST.w * (.7 + hash(i, 62) * .3), y0 = cy - NEST.h * (.3 + hash(i, 63) * .3);
    mLine(c, look, [[x0, y0], [x0 + sx * (18 + hash(i, 64) * 26), y0 - 8 - hash(i, 65) * 18]], {w: 1, al: .6, seed: i + 700}); }
}
const EGG = {x: 700, rx: 60, ry: 78};
const eggCY = () => nestY() - 52;
const eggPt = (cx, cy, a) => { const s = Math.sin(a); return [cx + Math.cos(a) * EGG.rx * (1 + .1 * s), cy + s * EGG.ry]; };
const eggOutline = (cx, cy, n = 44) => Array.from({length: n}, (_, i) => eggPt(cx, cy, i / n * TAU));
const crackPts = (cx, cy) => Array.from({length: 11}, (_, i) => [cx - EGG.rx * 1.02 + i / 10 * EGG.rx * 2.04, cy - EGG.ry * .12 + (i === 0 || i === 10 ? 0 : i % 2 ? -11 : 9)]);
const lowerArc = (cx, cy) => Array.from({length: 22}, (_, i) => eggPt(cx, cy, -.12 + (Math.PI + .24) * i / 21));
const upperArc = (cx, cy) => Array.from({length: 18}, (_, i) => eggPt(cx, cy, Math.PI + .12 + (Math.PI - .24) * i / 17));
// Raven eggs are speckled; the blotches belong to the shell, so they travel with it.
const SPECKS = Array.from({length: 16}, (_, i) => ({a: hash(i, 71) * TAU, r: .2 + hash(i, 72) * .65, s: 2.2 + hash(i, 73) * 3.4, seed: i}));
function specks(c, cx, cy, keep) {
  c.save(); c.fillStyle = RC.lead;
  for (const s of SPECKS) { const x = cx + Math.cos(s.a) * s.r * EGG.rx, y = cy + Math.sin(s.a) * s.r * EGG.ry; if (!keep(y)) continue; c.globalAlpha = .45; c.fill(curvePath(blob(x, y, s.s, s.s * .7, s.seed, {amp: .3}), true)); }
  c.restore();
}
function drawEgg(c, look, {rot = 0, crack = 0, squash = 1, p = 1} = {}) {
  if (p <= 0) return; const cx = EGG.x, cy = eggCY(), by = cy + EGG.ry;
  c.save(); c.translate(cx, by); c.rotate(rot); c.scale(1 / Math.sqrt(squash), squash); c.translate(-cx, -by);
  const pts = eggOutline(cx, cy), path = curvePath(pts, true, 1.3);
  c.fillStyle = RC.paper; c.fill(path);
  const k = sm(.6, 1, p, lin);
  if (k > 0) { formHatch(c, path, [cx - 72, cy - 90, 144, 180], {color: RC.lead, spacing: 5, length: 12, width: .7, opacity: .32 * k, seed: 61, tone: (x, y) => clamp((x - cx) / EGG.rx * .35 + (y - cy) / EGG.ry * .3 + .12, 0, .55), direction: () => 1.2});
    c.save(); c.globalAlpha *= k; specks(c, cx, cy, () => true); c.restore(); }
  pLine(c, partial([...pts, pts[0]], p), {w: 2.3, seed: 60});
  if (crack > 0) pLine(c, partial(crackPts(cx, cy), crack), {w: 1.9, seed: 62, col: RC.pencil});
  c.restore();
}
function drawLowerShell(c) {
  const cx = EGG.x, cy = eggCY(), pts = [...crackPts(cx, cy), ...lowerArc(cx, cy)], path = polyPath(pts);
  c.fillStyle = RC.paper; c.fill(path);
  formHatch(c, path, [cx - 72, cy - 30, 144, 120], {color: RC.lead, spacing: 5, length: 12, width: .7, opacity: .32, seed: 61, tone: (x, y) => clamp((x - cx) / EGG.rx * .35 + (y - cy) / EGG.ry * .3 + .12, 0, .55), direction: () => 1.2});
  specks(c, cx, cy, y => y > cy - EGG.ry * .05);
  pLine(c, lowerArc(cx, cy), {w: 2.3, seed: 63}); pLine(c, crackPts(cx, cy), {w: 1.8, seed: 62});
}
// The top shell in its own space: origin at the middle of the crack, dome upward.
const TOP_SHELL = (() => { const cy = 0 + EGG.ry * .12; return [...upperArc(0, cy), ...crackPts(0, cy).slice().reverse()]; })();
function drawTopShell(c, look, x, y, rot, s = 1) {
  c.save(); c.translate(x, y); c.rotate(rot); c.scale(s, s);
  const path = polyPath(TOP_SHELL);
  c.fillStyle = look === 'ink' ? RC.white : RC.paper; c.fill(path);
  if (look !== 'ink') { formHatch(c, path, [-70, -90, 140, 100], {color: RC.lead, spacing: 5, length: 12, width: .7, opacity: .3, seed: 64, tone: (xx) => clamp(.2 + xx / 200, 0, .5), direction: () => 1.2}); specks(c, 0, EGG.ry * .12, yy => yy < 0); }
  else { c.save(); c.clip(path); c.fillStyle = RC.ink; for (const sp of SPECKS) { const xx = Math.cos(sp.a) * sp.r * EGG.rx, yy = EGG.ry * .12 + Math.sin(sp.a) * sp.r * EGG.ry; if (yy < 0) c.fill(curvePath(blob(xx, yy, sp.s, sp.s * .7, sp.seed, {amp: .3}), true)); } c.restore(); }
  mLine(c, look, upperArc(0, EGG.ry * .12), {w: 2.2, seed: 65}); mLine(c, look, crackPts(0, EGG.ry * .12), {w: 1.7, seed: 66});
  c.restore();
}
// Where the hat sits on a given drawing: 60% up the head, turned with it.
function hatSpot(G, place) {
  const q = [G.hc[0], G.hc[1] - G.hr * .6], w = rvWorld(q, place);
  return {x: w[0], y: w[1], rot: (place.rot ?? 0) + G.p.tilt * (place.flip ?? 1)};
}

// ---------- props ----------
function drawSlip(c, look, x, y, rot, tag = 0) {
  c.save(); c.translate(x, y); c.rotate(rot);
  const pts = [[-64, -42], [62, -46], [66, 38], [-60, 42]], path = polyPath(pts);
  c.fillStyle = RC.paper; c.fill(path); c.save(); c.globalAlpha = .08; c.fillStyle = RC.pencil; c.translate(4, 5); c.fill(path); c.restore();
  pLine(c, [...pts, pts[0]], {w: 1.6, seed: 71, rough: .5});
  squiggleText(c, -48, -24, 64, 3, {color: RC.lead, seed: 4, lineH: 14, amp: 3, width: 1});
  // A sly face scribbled by a stranger: narrow eyes and a crooked grin.
  pLine(c, [[26, 0], [38, -6], [48, 0]], {w: 1.4, seed: 72}); pLine(c, [[26, 14], [36, 20], [48, 12], [52, 16]], {w: 1.3, seed: 73});
  c.fillStyle = RC.pencil; c.beginPath(); c.arc(37, -2, 2.6, 0, TAU); c.fill();
  if (tag > 0) {
    const s = clamp(easeOutBack(clamp(tag, 0, 1)), 0, 1.3);
    c.save(); c.translate(-18, 20); c.rotate(-.16); c.scale(s, s);
    const tp = [[-46, -18], [40, -18], [54, 0], [40, 18], [-46, 18]], p2 = polyPath(tp);
    c.fillStyle = '#fbeae4'; c.fill(p2); pLine(c, [...tp, tp[0]], {w: 1.8, col: RC.red, seed: 74, rough: .4});
    c.strokeStyle = RC.red; c.lineWidth = 1.4; c.beginPath(); c.arc(38, 0, 4, 0, TAU); c.stroke();
    writeOn(c, '不可信', -4, 9, {size: 25, col: RC.red, align: 'center', rot: 0});
    c.restore();
  }
  c.restore();
}
function drawPack(c, look, x, y, rot, s, al = 1) {
  if (al <= 0) return;
  c.save(); c.globalAlpha *= al; c.translate(x, y); c.rotate(rot); c.scale(s, s);
  const body = [[-30, -24], [26, -26], [30, 24], [-28, 26]], path = curvePath(body, true, .9);
  c.fillStyle = RC.paper; c.fill(path);
  formHatch(c, path, [-32, -28, 64, 56], {color: RC.lead, spacing: 5, length: 11, width: .7, opacity: .35, seed: 81, tone: (xx, yy) => clamp(.25 + yy / 60, 0, .6), direction: () => 1.1});
  pLine(c, body, {w: 1.8, close: true, seed: 82});
  pLine(c, [[-28, -10], [0, -4], [28, -12]], {w: 1.3, seed: 83});
  pLine(c, [[-10, -26], [-6, -38], [6, -38], [10, -26]], {w: 1.4, seed: 84});
  writeOn(c, '记忆', 0, 16, {size: 20, col: RC.pencil, align: 'center', rot: 0});
  c.restore();
}
function drawEraser(c, x, y, rot) {
  c.save(); c.translate(x, y); c.rotate(rot);
  const body = [[-58, -22], [52, -24], [56, 22], [-54, 24]], path = polyPath(body);
  c.fillStyle = RC.pink; c.fill(path);
  formHatch(c, path, [-60, -26, 120, 52], {color: RC.red, spacing: 4, length: 10, width: .6, opacity: .35, seed: 91, tone: () => .5, direction: () => .9});
  const band = polyPath([[-14, -23], [22, -24], [24, 23], [-12, 24]]); c.fillStyle = '#d8dde6'; c.fill(band);
  hatch(c, band, [-16, -26, 44, 52], {angle: .3, gap: 4, len: 8, color: RC.lead, alpha: .5, width: .7, seed: 92});
  pLine(c, [...body, body[0]], {w: 1.7, seed: 93, rough: .4}); pLine(c, [[-14, -23], [-12, 24]], {w: 1.2, seed: 94}); pLine(c, [[22, -24], [24, 23]], {w: 1.2, seed: 95});
  c.restore();
}
function drawCrumbs(c, x, y, k) {
  if (k <= 0 || k >= 1) return;
  for (let i = 0; i < 9; i++) { const xx = x + (hash(i, 31) - .5) * 60, yy = y + k * (40 + hash(i, 32) * 120) + (hash(i, 33) - .5) * 20;
    pLine(c, [[xx - 3, yy], [xx, yy - 2], [xx + 3, yy + 1]], {w: 1.4, col: RC.red, al: (1 - k) * .8, seed: i + 50, rough: .3}); }
}
function drawDizzy(c, look, x, y, tau, al = 1) {
  if (al <= 0) return;
  for (let i = 0; i < 3; i++) { const a = QT2(tau) * 5 + i * TAU / 3, sx = x + Math.cos(a) * 70, sy = y + Math.sin(a) * 20;
    c.save(); c.globalAlpha *= al; star(c, sx, sy, 11, alpha(RC.gold, .7), 5, true); star(c, sx, sy, 11, look === 'ink' ? RC.ink : RC.pencil, 5, false); c.restore(); }
}
function drawDust(c, look, x, y, k) {
  if (k <= 0 || k >= 1) return;
  for (let i = 0; i < 7; i++) { const px = x + (i - 3) * 36 + (hash(i, 41) - .5) * 20 + (i - 3) * k * 30, py = y - k * (40 + hash(i, 42) * 60), r = (16 + hash(i, 43) * 16) * (.5 + k);
    mLine(c, look, ellPts(px, py, r, r * .8, 0, 18), {w: 1.4, al: (1 - k) * .8, close: true, seed: i + 60}); }
}
function drawSpeed(c, look, x, y, k) {
  if (k <= 0) return;
  for (let i = 0; i < 7; i++) { const px = x + (i - 3) * 22 + (hash(i, 51) - .5) * 10, L = 80 + hash(i, 52) * 120;
    mLine(c, look, [[px, y - 270 - L], [px + 1, y - 270]], {w: 1.1, al: .55 * k, seed: i + 70}); }
}
