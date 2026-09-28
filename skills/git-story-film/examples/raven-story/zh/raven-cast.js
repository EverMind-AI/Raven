'use strict';
// Raven, the narrator: a cartoon raven with big eyes, drawn as whole poses.
// Local space: origin on the ground between the feet, +x toward the beak, y down.
// Each drawing is rebuilt from an authored pose. Head and body are unioned into one
// continuous silhouette, so a new pose is a new outline rather than rotated parts.
// Raven traits kept on purpose: a heavy beak, shaggy throat hackles, a wedge tail,
// fingered primaries and a crown tuft that carries half of every expression.

const RC = {
  paper:'#f4efe4', pencil:'#3b342d', lead:'#7a7066', gold:'#c9962f', red:'#c4473a', pink:'#e7a9a0',
  ink:'#1d1b22', inkLine:'#121015', sheen:'#9aa2d6', white:'#fcfaf3', beakInk:'#45414d', beakHi:'#aaa5b8',
};
// Faces are SIL OFL and ship in fonts/ (scripts/fetch_fonts.sh), so every machine renders the same
// lettering. A face that fails to load stops the film from becoming ready, so list only what
// fonts/ contains. Each face claims a weight range, so asking for 600 never triggers synthetic bold.
const RV_FONT = '"LXGW WenKai",cursive';
for (const [family, file, desc] of [['LXGW WenKai', 'LXGWWenKai-Medium.ttf', {weight: '100 900'}]]) {
  const face = new FontFace(family, `url('fonts/${file}')`, desc);
  _photoLoads.push(face.load().then(f => document.fonts.add(f)).catch(e => { window.__error = 'font failed to load: ' + file; throw e; }));
}

const RV_BASE = {
  chick:{kind:'chick', bx:0, by:-66, brx:56, bry:54, lean:0, squash:1, hx:8, hy:-122, hr:50, tilt:0,
    gx:0, gy:0, lid:0, lidTilt:0, eyeS:1, pupil:1, mood:'open', spark:0, beakOpen:0, tuft:0,
    wingMode:'fold', wing:0, flap:0, tail:0, fa:-16, fb:18, lift:0, tuck:0, young:1},
  adult:{kind:'adult', bx:0, by:-104, brx:68, bry:80, lean:.1, squash:1, hx:30, hy:-198, hr:56, tilt:0,
    gx:0, gy:0, lid:0, lidTilt:0, eyeS:1, pupil:1, mood:'open', spark:0, beakOpen:0, tuft:0,
    wingMode:'fold', wing:0, flap:0, tail:0, fa:-20, fb:22, lift:0, tuck:0, young:0},
};
const RV_DISCRETE = new Set(['kind', 'mood', 'wingMode', 'body']);
const rvPose = (kind, o = {}) => ({...RV_BASE[kind], ...o});
function rvBlend(a, b, u) { const o = {}; for (const k in a) o[k] = RV_DISCRETE.has(k) ? (u < 1 ? a[k] : b[k]) : lerp(a[k], b[k] ?? a[k], u); return o; }

// A pose track: [[t, pose, ease?], ...]. Discrete fields switch on the key that sets them.
function rvTrack(keys) {
  return t => {
    if (t <= keys[0][0]) return keys[0][1];
    for (let i = 1; i < keys.length; i++) if (t < keys[i][0]) {
      const [t0, a] = keys[i - 1], [t1, b, e = easeIO] = keys[i];
      return rvBlend(a, b, e(clamp((t - t0) / (t1 - t0), 0, 1)));
    }
    return keys[keys.length - 1][1];
  };
}

const rv2 = {
  rot: (p, a) => [p[0] * Math.cos(a) - p[1] * Math.sin(a), p[0] * Math.sin(a) + p[1] * Math.cos(a)],
  add: (a, b) => [a[0] + b[0], a[1] + b[1]],
  sub: (a, b) => [a[0] - b[0], a[1] - b[1]],
  mul: (a, k) => [a[0] * k, a[1] * k],
  around: (p, o, a) => rv2.add(o, rv2.rot(rv2.sub(p, o), a)),
};

// The arc of `at` that lies outside `inside`, found by scanning then bisecting both edges,
// so the union outline slides smoothly as the head moves instead of snapping per sample.
function rvRun(at, inside, n = 180) {
  const out = k => !inside(at(k / n * TAU));
  let s = -1; for (let k = 0; k < n; k++) if (out(k) && !out(k - 1)) { s = k; break; }
  if (s < 0) return [0, TAU];
  let e = s; while (out(e + 1) && e - s < n) e++;
  const edge = (lo, hi) => { const a = inside(at(lo)); for (let i = 0; i < 26; i++) { const m = (lo + hi) / 2; if (inside(at(m)) === a) lo = m; else hi = m; } return (lo + hi) / 2; };
  return [edge((s - 1) / n * TAU, s / n * TAU), edge(e / n * TAU, (e + 1) / n * TAU)];
}
const rvSample = (at, [a0, a1], m) => Array.from({length: m}, (_, i) => at(a0 + (a1 - a0) * i / (m - 1)));

const RV_EYES = {
  adult:{near:[0, -16, 16, 20], far:[33, -18, 11.5, 18]},
  chick:{near:[-6, -6, 17, 20.5], far:[25, -8, 12.5, 19]},
};

function rvGeom(p) {
  const chick = p.kind === 'chick', sq = p.squash, rot = p.lean;
  const brx = p.brx / Math.sqrt(sq), bry = p.bry * sq, bc = [p.bx, p.by * sq];
  const hc = rv2.add(bc, rv2.rot([p.hx - p.bx, (p.hy - p.by) * lerp(1, sq, .75)], rot)), hr = p.hr;
  const hs = hr / RV_BASE[p.kind].hr, tilt = p.tilt;
  const bodyAt = a => rv2.add(bc, rv2.rot([Math.cos(a) * brx, Math.sin(a) * bry], rot));
  const inBody = q => { const d = rv2.rot(rv2.sub(q, bc), -rot); return (d[0] / brx) ** 2 + (d[1] / bry) ** 2 < 1; };
  const headAt = a => [hc[0] + Math.cos(a) * hr, hc[1] + Math.sin(a) * hr];
  const inHead = q => Math.hypot(q[0] - hc[0], q[1] - hc[1]) < hr;
  const head = rvSample(headAt, rvRun(headAt, inBody), 14), body = rvSample(bodyAt, rvRun(bodyAt, inHead), 16);
  const silhouette = [...head, ...body.slice(1, -1)];
  const bodyN = q => rv2.add(bc, rv2.rot([q[0] * brx, q[1] * bry], rot));
  const H = q => rv2.add(hc, rv2.rot(rv2.mul(q, hs), tilt));

  // Tail: a raven's wedge, longest in the middle. The chick only has a nub.
  const tb0 = bodyAt(Math.PI * .80), tb1 = bodyAt(Math.PI * 1.03), tm = rv2.mul(rv2.add(tb0, tb1), .5);
  const tdir = Math.atan2(tm[1] - bc[1], tm[0] - bc[0]) + .38 + p.tail, tl = chick ? 20 : 92 * (1 - .35 * p.young);
  const T = (u, v) => rv2.add(tm, rv2.rot([u, v], tdir));
  const tail = chick ? [T(-4, -13), T(tl * .6, -10), T(tl, 0), T(tl * .6, 10), T(-4, 13)]
    : [T(-6, -17), T(tl * .52, -25), T(tl, 0), T(tl * .52, 25), T(-6, 17)];
  const tailLines = chick ? [] : [[T(8, -4), T(tl * .55, -8), T(tl * .9, -2)], [T(8, 5), T(tl * .55, 9), T(tl * .88, 3)]];

  // Folded wing, authored in body units; raising it turns the drawing about the shoulder.
  let wing = null, wingLines = [], primaries = [], spread = null, farWing = null;
  if (p.wingMode === 'fold') {
    const W0 = chick
      ? [[-.06, -.42], [.22, -.20], [.20, .16], [-.04, .34], [-.40, .28], [-.60, .06], [-.46, -.24], [-.22, -.40]]
      : [[.05, -.62], [.38, -.34], [.44, .02], [.22, .40], [-.20, .56], [-.70, .52], [-1.30, .34], [-.95, .08], [-.55, -.30], [-.22, -.55]];
    // A flapping chick's wing leaves the belly and works from the back contour.
    const fk = chick ? clamp(p.flap / 1.1, 0, 1) : 0, shift = [-.40 * fk, .10 * fk];
    const shoulder = bodyN(rv2.add(W0[0], shift)), ang = p.wing + (chick ? p.flap : 0);
    const place = q => rv2.around(bodyN(rv2.add(q, shift)), shoulder, ang);
    wing = W0.map(place);
    if (chick) {
      wingLines = [[[-.36, .16], [-.16, .06], [.06, .10]], [[-.48, .02], [-.26, -.10], [-.04, -.08]]].map(l => l.map(place));
      // The far wing only shows once it is raised above the back line.
      if (p.flap > .12) { const root = bodyN([.46, -.22]);
        farWing = [[.46, -.22], [.70, -.16], [.90, 0], [.84, .14], [.64, .10], [.46, -.04]].map(q => rv2.around(bodyN(q), root, -p.flap * .9)); }
    } else {
      primaries = [0, 1, 2].map(i => [[-.50 + i * .05, .40 + i * .03], [-.86 + i * .04, .40 + i * .02], [-1.20 + i * .10, .33 + i * .045]].map(place));
      wingLines = [[[-.08, -.38], [.10, -.10], [.20, .22]], [[-.38, -.18], [-.18, .08], [-.08, .40]], [[-.66, .02], [-.50, .24], [-.42, .46]]].map(l => l.map(place));
    }
  } else {
    const WING = {lead:[[0, -12], [36, -24], [72, -28], [106, -25]], tips:[[152, -22], [148, -5], [139, 10], [125, 22], [107, 32]],
      notch:[[126, -13], [121, 1], [112, 14], [98, 24]], trail:[[86, 36], [63, 38], [41, 34], [19, 24], [0, 13]]};
    const outline = [...WING.lead]; for (let i = 0; i < 5; i++) { outline.push(WING.tips[i]); if (i < 4) outline.push(WING.notch[i]); } outline.push(...WING.trail);
    const make = (root, ang, k, fs) => {
      const P = q => rv2.add(root, rv2.rot([q[0] * k, q[1] * k * fs], ang));
      return {outline: outline.map(P), fingers: WING.notch.map((n, i) => [P([n[0] - 26, n[1] * .6]), P(n), P(WING.tips[i + 1])]),
        coverts: [[[18, -6], [44, 4], [70, 10]], [[24, 10], [50, 18], [76, 24]]].map(l => l.map(P))};
    };
    const k = 1.3 * (1 - .15 * p.young);
    spread = {
      // On the downstroke the far wing swings under the body instead of across the face.
      far: make(bodyN([.34, -.74]), -Math.PI / 2 + .62 + (p.flap < 0 ? -p.flap * 1.25 : -p.flap * .75), k * .86, .72),
      near: make(bodyN([.02, -.54]), -Math.PI / 2 - 1.02 + p.flap * .95, k, 1),
    };
  }

  // Legs keep their ground contacts; a tuck draws them up under the belly.
  const hipA = bodyAt(Math.PI * .60), hipB = bodyAt(Math.PI * .42), toe = chick ? .72 : 1;
  const legs = [[hipA, p.fa], [hipB, p.fb]].map(([hip, fx], i) => {
    const ground = [fx, -p.lift], tucked = rv2.add(hip, [-8 - i * 4, chick ? 6 : 10]);
    const F = [lerp(ground[0], tucked[0], p.tuck), lerp(ground[1], tucked[1], p.tuck)];
    const knee = rv2.add(rv2.mul(rv2.add(hip, F), .5), [-6 * (1 - p.tuck), 0]);
    const curl = p.tuck;
    const toes = [[14, 1], [10, 5], [-9, 2]].map(([x, y]) => [F, rv2.add(F, [lerp(x, x * .35, curl) * toe, lerp(y, y + 4, curl) * toe])]);
    return {line: [hip, knee, F], toes, foot: F};
  });

  // Face in head units, turned with the head.
  const young = p.young, bl = chick ? 1 : 1 - .22 * young;
  const bk = q => H([q[0] < 44 ? q[0] : 40 + (q[0] - 40) * bl, q[1]]);
  const up0 = chick ? [[34, 12], [46, 14], [60, 20], [54, 23]] : [[40, -4], [66, 0], [92, 10], [110, 22], [102, 25]];
  const lo0 = chick ? [[54, 23], [44, 25], [33, 23]] : [[102, 25], [82, 27], [60, 28], [40, 22]];
  const hingeU = up0[0], hingeL = lo0[lo0.length - 1];
  const beakUp = up0.map(q => bk(rv2.around(q, hingeU, -p.beakOpen * .12)));
  const beakLo = lo0.map(q => bk(rv2.around(q, hingeL, p.beakOpen * .5)));
  const seam = chick ? null : [bk([42, 9]), bk([70, 14]), bk([96, 21])];
  const nostril = chick ? null : [bk([50, 0]), bk([55, -2]), bk([60, 0])];
  const E = RV_EYES[chick ? 'chick' : 'adult'];
  const eye = ([x, y, rx, ry]) => ({c: H([x, y]), rx: rx * hs * p.eyeS, ry: ry * hs * p.eyeS, rot: tilt});
  const eyes = {near: eye(E.near), far: eye(E.far)};

  const crown = a => headAt(a);
  let tufts = [];
  if (!chick) tufts = [-2.06, -1.80, -1.54].map((a, i) => {
    const b = crown(a), len = (22 + i * 3) * (1 - .3 * young), dir = a - .55 * p.tuft - .15 + i * .05;
    return [rv2.add(b, [Math.cos(a) * -3, Math.sin(a) * -3]), rv2.add(b, [Math.cos(dir) * len * .55, Math.sin(dir) * len * .55]), rv2.add(b, [Math.cos(dir - .35) * len, Math.sin(dir - .35) * len])];
  });
  const wisps = (chick || young > .3) ? [-2.35, -2.0, -1.68, -1.38, -1.1].map((a, i) => {
    const b = crown(a), len = 12 + hash(i, 7) * 9, d = a + (p.tuft * .5) + (i - 2) * .12;
    return [rv2.add(b, [Math.cos(a) * -2, Math.sin(a) * -2]), rv2.add(b, [Math.cos(d) * len * .6, Math.sin(d) * len * .6]), rv2.add(b, [Math.cos(d + .6) * len, Math.sin(d + .6) * len])];
  }) : [];
  const hackles = chick ? [] : [[bk([34, 30]), H([22, 42]), H([30, 45]), H([16, 56]), H([24, 58]), H([8, 68])]];

  return {p, hc, hr, bc, brx, bry, rot, head, body, silhouette, tail, tailLines, wing, wingLines, primaries, spread, farWing,
    legs, beakUp, beakLo, seam, nostril, eyes, tufts, wisps, hackles, anchors: {
      crown: headAt(-Math.PI / 2 - .25), back: bodyAt(Math.PI * 1.04), chest: bodyAt(-.1), beak: beakUp[beakUp.length - 2]}};
}

// Materials for one drawing. A companion bird is the same drawing with its own body colour;
// chalk is the blueprint look, light line on navy.
function rvPal(look, p) {
  if (look === 'chalk') return {fill: '#1c2f5e', wing: '#23396c', far: '#20345f', beak: '#2b4178', line: '#e8efff', mat: 'pencil', sclera: '#e8efff', pupil: '#16254c', sheen: null, k: 2.1};
  if (look === 'ink') { const f = p.body ?? RC.ink; return {fill: f, wing: p.body ? shade(f, .14) : '#26232c', far: p.body ? shade(f, .22) : '#2a2731', beak: p.body ? shade(f, .45) : RC.beakInk,
    line: p.body ? shade(f, .6) : RC.inkLine, mat: 'ink', sclera: RC.white, pupil: RC.inkLine, sheen: p.body ? tint(f, .45) : RC.sheen, k: 1}; }
  return {fill: RC.paper, wing: RC.paper, far: RC.paper, beak: RC.paper, line: RC.pencil, mat: 'pencil', sclera: RC.paper, pupil: RC.pencil, sheen: null, k: 1.65};
}
// Stroke drawing for cels.js. Ids are semantic, so a held drawing holds its marks.
function rvStrokes(G, look, layer) {
  const ink = look !== 'pencil', s = [], add = (id, points, width, opacity = 1, extra = {}) => s.push({id, points, width, opacity, ...extra});
  const k = rvPal(look, G.p).k, gap = ink ? RC.paper : undefined, P = [[0, .12], [.15, .9], [.5, 1], [.85, .75], [1, .08]];
  const p = G.p;
  if (layer === 'back') {
    if (G.farWing) add('farwing/edge', G.farWing, 1.8 * k, .95, {close: true, pressure: [[0, .5], [.5, 1], [1, .5]]});
    if (G.spread) { const w = G.spread.far; add('far/edge', w.outline, 2.2 * k, .9, {close: true, pressure: P}); w.fingers.forEach((f, i) => add('far/finger/' + i, f, 1.1 * k, ink ? .8 : .6, {color: gap, pressure: P})); }
    for (const [i, L] of G.legs.entries()) { add('leg/' + i, L.line, 2.2 * k, .95, {corner: 1}); L.toes.forEach((t, j) => add('toe/' + i + '/' + j, t, 1.9 * k, .95)); }
    add('tail/edge', G.tail, 2 * k, .95, {corner: .9, pressure: P});
    G.tailLines.forEach((l, i) => add('tail/line/' + i, l, 1 * k, ink ? .7 : .55, {color: gap}));
  } else if (layer === 'body') {
    add('head', G.head, 2.1 * k, 1, {pressure: P});
    add('breast', G.body.slice(0, 9), 2.3 * k, 1, {pressure: P});
    add('back', G.body.slice(8), 1.9 * k, .95, {pressure: P});
    if (!ink) {
      add('back/search', G.body.slice(9, 15).map(([x, y], i) => [x + 2.5, y - 2 - i * .2]), .8, .28);
      add('head/search', G.head.slice(3, 10).map(([x, y]) => [x - 1.5, y + 2.2]), .8, .26);
    }
    if (G.wing) { add('wing/edge', G.wing, 1.9 * k, .95, {close: true, pressure: [[0, .5], [.5, 1], [1, .5]], color: look === 'ink' ? RC.inkLine : undefined}); G.wingLines.forEach((l, i) => add('wing/row/' + i, l, 1.1 * k, ink ? .75 : .5, {color: gap})); G.primaries.forEach((l, i) => add('wing/primary/' + i, l, 1.2 * k, ink ? .8 : .6, {color: gap})); }
    if (G.spread) { const w = G.spread.near; add('near/edge', w.outline, 2.3 * k, 1, {close: true, pressure: P}); w.fingers.forEach((f, i) => add('near/finger/' + i, f, 1.2 * k, ink ? .85 : .6, {color: gap})); w.coverts.forEach((l, i) => add('near/covert/' + i, l, 1 * k, ink ? .7 : .5, {color: gap})); }
    G.tufts.forEach((t, i) => add('tuft/' + i, t, 1.8 * k, 1, {pressure: [[0, .9], [1, .1]]}));
    G.wisps.forEach((t, i) => add('wisp/' + i, t, 1.1 * k, ink ? .9 : .75, {color: ink ? RC.sheen : undefined, pressure: [[0, .9], [1, .1]]}));
    G.hackles.forEach((h, i) => add('hackle/' + i, h, 1.3 * k, ink ? .85 : .7, {color: ink ? RC.sheen : undefined, corner: .6}));
    if (p.kind === 'chick' && !ink) for (let i = 0; i < 9; i++) {
      const u = (i + .5) / 9, a = -.35 + u * 1.55, q = rv2.add(G.bc, rv2.rot([Math.cos(a) * G.brx * .72, Math.sin(a) * G.bry * .72], G.rot));
      add('fluff/' + i, [[q[0] - 4, q[1] - 3], [q[0], q[1] + 2], [q[0] + 4, q[1] - 2]], .8, .35 + hash(i, 3) * .2);
    }
  } else {
    add('beak/up', G.beakUp, 2 * k, 1, {corner: .6});
    add('beak/lo', G.beakLo, 1.9 * k, 1, {corner: .6});
    if (G.seam) add('beak/seam', G.seam, 1 * k, .7, {color: ink ? RC.beakHi : undefined});
    if (G.nostril) add('beak/nostril', G.nostril, .9 * k, .75, {color: ink ? RC.beakHi : undefined});
    for (const side of ['near', 'far']) {
      const e = G.eyes[side], mood = p.mood, L = (x, y) => rv2.add(e.c, rv2.rot([x * e.rx, y * e.ry], e.rot));
      const light = ink ? RC.paper : undefined;
      if (mood === 'blink') add('eye/' + side, [L(-1, -.02), L(-.5, .26), L(0, .34), L(.5, .26), L(1, -.02)], 2 * k, 1, {color: light});
      else if (mood === 'happy') add('eye/' + side, [L(-1, .3), L(-.5, -.16), L(0, -.28), L(.5, -.16), L(1, .3)], 2.1 * k, 1, {color: light});
      else if (mood === 'squeeze') add('eye/' + side, side === 'near' ? [L(-.8, -.55), L(.55, 0), L(-.8, .55)] : [L(.8, -.55), L(-.55, 0), L(.8, .55)], 2 * k, 1, {color: light, corner: .5});
      else {
        add('eye/' + side, Array.from({length: 17}, (_, i) => L(Math.cos(i / 16 * TAU), Math.sin(i / 16 * TAU))), (side === 'near' ? 1.8 : 1.6) * k, 1, {close: false, pressure: [[0, .7], [.5, 1], [1, .7]], color: look === 'ink' ? RC.inkLine : undefined});
        if (p.lid > .02) { const y = -1 + p.lid * 2, t = p.lidTilt * (side === 'near' ? 1 : -1); add('lid/' + side, [L(-1.08, y - t * .45), L(0, y - .08), L(1.08, y + t * .45)], 1.8 * k, 1, {color: look === 'ink' ? RC.inkLine : undefined}); }
        if (mood === 'dizzy') { const pts = []; for (let i = 0; i <= 26; i++) { const a = i / 26 * TAU * 2.2, r = .12 + i / 26 * .62; pts.push(L(Math.cos(a) * r, Math.sin(a) * r)); } add('spiral/' + side, pts, 1.3 * k, 1, {color: look === 'ink' ? RC.inkLine : undefined}); }
      }
    }
  }
  return {strokes: s};
}

const RV_CELS = new Map();
function rvCel(G, look, layer, key) {
  const id = key + '|' + layer;
  if (RV_CELS.has(id)) return RV_CELS.get(id);
  // The family id seeds the marks, so consecutive drawings keep related pencil passes.
  const cel = compileCel(rvStrokes(G, look, layer), {id: 'raven/' + G.p.kind + '/' + look + '/' + layer});
  RV_CELS.set(id, cel); if (RV_CELS.size > 400) RV_CELS.delete(RV_CELS.keys().next().value);
  return cel;
}

function rvFillEye(g, G, look) {
  const ink = look !== 'pencil', p = G.p, P = rvPal(look, p);
  if (['blink', 'happy', 'squeeze'].includes(p.mood)) return;
  for (const side of ['near', 'far']) {
    const e = G.eyes[side];
    g.save(); g.translate(e.c[0], e.c[1]); g.rotate(e.rot);
    const sclera = new Path2D(); sclera.ellipse(0, 0, e.rx, e.ry, 0, 0, TAU);
    g.fillStyle = P.sclera; g.fill(sclera); g.clip(sclera);
    if (p.mood !== 'dizzy') {
      const rp = Math.min(e.rx, e.ry) * .66 * p.pupil, px = p.gx * e.rx * .42, py = p.gy * e.ry * .32;
      g.fillStyle = P.pupil; g.globalAlpha = ink ? 1 : .9;
      g.beginPath(); g.arc(px, py, rp, 0, TAU); g.fill(); g.globalAlpha = 1;
      g.fillStyle = look === 'chalk' ? '#ffffff' : P.sclera;
      if (p.spark > .05) {
        const s = rp * (.55 + .35 * p.spark), cx = px - rp * .28, cy = py - rp * .34;
        g.beginPath(); for (let i = 0; i < 8; i++) { const a = i / 8 * TAU - Math.PI / 2, r = i % 2 ? s * .28 : s; i ? g.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r) : g.moveTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r); } g.closePath(); g.fill();
      } else { g.beginPath(); g.arc(px - rp * .34, py - rp * .40, rp * .36, 0, TAU); g.fill(); }
      g.beginPath(); g.arc(px + rp * .34, py + rp * .34, rp * .15, 0, TAU); g.fill();
    }
    if (p.lid > .02) {
      const y = (-1 + p.lid * 2) * e.ry, t = p.lidTilt * (side === 'near' ? 1 : -1) * e.ry * .45;
      g.fillStyle = P.fill; g.beginPath(); g.moveTo(-e.rx * 1.2, y - t); g.lineTo(e.rx * 1.2, y + t); g.lineTo(e.rx * 1.2, -e.ry * 1.2); g.lineTo(-e.rx * 1.2, -e.ry * 1.2); g.closePath(); g.fill();
      if (look === 'pencil') { g.globalAlpha = .35; g.strokeStyle = RC.lead; g.lineWidth = .7; for (let i = -6; i < 8; i++) { g.beginPath(); g.moveTo(-e.rx + i * 4, y - t - 1); g.lineTo(-e.rx + i * 4 + 6, -e.ry); g.stroke(); } g.globalAlpha = 1; }
    }
    g.restore();
  }
}

// One whole drawing: fills and material belong to the pose, then the three stroke layers.
function rvRender(g, p, look, key) {
  const G = rvGeom(p), P = rvPal(look, p), ink = look !== 'pencil', chick = p.kind === 'chick';
  const sil = curvePath(G.silhouette, true, 1.4), tail = curvePath(G.tail, true, .9);
  const box = [G.bc[0] - G.brx - 70, G.hc[1] - G.hr - 40, G.brx * 2 + 170, G.bc[1] + G.bry - G.hc[1] + G.hr + 60];
  const lineCol = P.line, mat = P.mat;
  if (G.spread) { const f = curvePath(G.spread.far.outline, true, .8); g.fillStyle = P.far; g.fill(f); if (!ink) formHatch(g, f, [-320, -480, 640, 520], {color: RC.lead, spacing: 6, length: 13, width: .7, opacity: .3, tone: () => .38, direction: () => 1.1, seed: 11}); }
  g.fillStyle = P.fill; g.fill(tail);
  if (G.farWing) { g.fillStyle = P.wing; g.fill(curvePath(G.farWing, true, 1.1)); }
  drawCel(g, rvCel(G, look, 'back', key), {material: mat, color: lineCol});
  g.fillStyle = P.fill; g.fill(sil);
  if (P.sheen) {
    // Gloss: pale strokes only on the lit crown and shoulder, so the black mass stays black.
    formHatch(g, sil, box, {color: P.sheen, spacing: 7, length: 15, width: .9, opacity: .42, seed: 23,
      tone: (x, y) => clamp(.62 - (y - G.hc[1] + G.hr) / (G.hr * 2.6) - (x - G.bc[0]) / (G.brx * 4), 0, .55), direction: (x, y) => -.55 + (x - G.hc[0]) / 260});
  } else if (!ink) {
    formHatch(g, sil, box, {color: RC.lead, spacing: 5.5, length: 13, width: .75, opacity: .34, seed: 31,
      tone: (x, y) => clamp((y - G.bc[1]) / G.bry * .55 + (x - G.bc[0]) / G.brx * .12 + .08, 0, .62), direction: () => 1.05});
    if (chick) { const cheek = new Path2D(); const c0 = rv2.add(G.eyes.near.c, [G.eyes.near.rx * .6, G.eyes.near.ry * 1.25]); cheek.ellipse(c0[0], c0[1], 12, 7, 0, 0, TAU);
      formHatch(g, cheek, [c0[0] - 14, c0[1] - 9, 28, 18], {color: RC.red, spacing: 3.2, length: 7, width: .7, opacity: .45, tone: () => .8, direction: () => 1.2, seed: 5}); }
  }
  if (G.wing) {
    const w = curvePath(G.wing, true, 1.1); g.fillStyle = P.wing; g.fill(w);
    if (!ink) formHatch(g, w, box, {color: RC.lead, spacing: 5, length: 12, width: .7, opacity: .3, seed: 41, tone: (x, y) => clamp(.2 + (y - G.bc[1]) / G.bry * .35, 0, .5), direction: () => .35});
  }
  if (G.spread) { const n = curvePath(G.spread.near.outline, true, .8); g.fillStyle = P.wing; g.fill(n); if (!ink) formHatch(g, n, [-320, -480, 640, 520], {color: RC.lead, spacing: 6, length: 13, width: .7, opacity: .22, tone: () => .3, direction: () => .9, seed: 12}); }
  drawCel(g, rvCel(G, look, 'body', key), {material: mat, color: lineCol});
  const beak = polyPath([...G.beakUp, ...G.beakLo.slice(1, -1)]);
  g.fillStyle = P.beak; g.fill(beak);
  if (!ink) formHatch(g, beak, [G.hc[0], G.hc[1] - 20, 140, 80], {color: RC.lead, spacing: 4, length: 9, width: .6, opacity: .3, tone: () => .4, direction: () => .2, seed: 7});
  rvFillEye(g, G, look);
  drawCel(g, rvCel(G, look, 'face', key), {material: mat, color: lineCol});
  return G;
}

const RV_SPRITES = new Map();
const RV_BOX = {chick: [-190, -270, 380, 310], adult: [-290, -440, 540, 480]};
function rvKey(p) { return Object.keys(p).sort().map(k => typeof p[k] === 'number' ? k + ':' + p[k].toFixed(3) : k + ':' + p[k]).join(','); }
function rvSprite(p, look) {
  const key = rvKey(p), id = look + '|' + S + '|' + key;
  if (RV_SPRITES.has(id)) { const v = RV_SPRITES.get(id); RV_SPRITES.delete(id); RV_SPRITES.set(id, v); return v; }
  const box = RV_BOX[p.kind], dpi = 2 * S, cv = document.createElement('canvas');
  cv.width = Math.ceil(box[2] * dpi); cv.height = Math.ceil(box[3] * dpi);
  const g = cv.getContext('2d'); g.setTransform(dpi, 0, 0, dpi, -box[0] * dpi, -box[1] * dpi);
  const G = rvRender(g, p, look, key), v = {cv, box, G};
  RV_SPRITES.set(id, v); if (RV_SPRITES.size > 36) RV_SPRITES.delete(RV_SPRITES.keys().next().value);
  return v;
}
// Places a drawing in the caller's space. Returns the geometry for props that attach to it.
function drawRaven(c, p, {x = 0, y = 0, scale = 1, rot = 0, flip = 1, look = 'pencil', alpha: al = 1} = {}) {
  const s = rvSprite(p, look);
  c.save(); c.translate(x, y); c.rotate(rot); c.scale(scale * flip, scale); c.globalAlpha *= al;
  c.drawImage(s.cv, s.box[0], s.box[1], s.box[2], s.box[3]); c.restore();
  return s.G;
}
// A point of the drawing (local) carried into the caller's space with the same placement.
function rvWorld(q, {x = 0, y = 0, scale = 1, rot = 0, flip = 1} = {}) { return rv2.add([x, y], rv2.rot([q[0] * scale * flip, q[1] * scale], rot)); }
