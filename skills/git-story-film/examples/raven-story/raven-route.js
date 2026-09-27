'use strict';
// Act I as one continuous shot. From the nest on, Raven only moves left to right; the camera
// follows it, never cuts and never turns back, and every date is a stop on the way.
// The page is pencil up to the v0.1.0 seam and ink after it, so walking forward is also
// what changes the material.

const SEAM = 3080, HOP_D = .38, INK_HOP = .42;
const T = {page: 0, hatch: 4, walk1: 11};
T.slip = T.walk1 + .3 + .6 + 5 * HOP_D;
T.walk2 = T.slip + 3.25;
T.fall = T.walk2 + 5 * HOP_D;
T.walk3 = T.fall + 6;
T.ink = T.walk3 + .3 + 4 * INK_HOP;
T.end = T.ink + 4.8;

// ---------- hopping ----------
const HOPS = [];
{
  const add = (t0, x0, x1, d, h, fromShell = false) => HOPS.push({t0, x0, x1, d, h, fromShell});
  add(T.walk1 + .3, EGG.x, 860, .6, 150, true);
  for (let i = 0; i < 5; i++) add(T.walk1 + .9 + i * HOP_D, 860 + i * 148, 860 + (i + 1) * 148, HOP_D, 46);
  for (let i = 0; i < 5; i++) add(T.walk2 + i * HOP_D, 1600 + i * 172, 1600 + (i + 1) * 172, HOP_D, 52);
  for (let i = 0; i < 4; i++) add(T.walk3 + .3 + i * INK_HOP, 2740 + i * 150, 2740 + (i + 1) * 150, INK_HOP, 56);
}
const shellFloor = () => eggCY() + 34;
function hopAt(t, t0, t1) {
  let last = null;
  for (const h of HOPS) { if (h.t0 < t0 - 1e-6 || h.t0 >= t1) continue; if (t >= h.t0 && t < h.t0 + h.d) return {h, u: (t - h.t0) / h.d}; if (h.t0 <= t) last = h; }
  return last ? {h: last, u: 1} : null;
}
function hopRoot(h, u) {
  const g0 = h.fromShell ? shellFloor() : topY(h.x0), g1 = topY(h.x1);
  if (u < .2) return [h.x0, g0];
  if (u >= .82) return [h.x1, g1];
  const v = (u - .2) / .62; return [lerp(h.x0, h.x1, v), lerp(g0, g1, v) - 4 * h.h * v * (1 - v)];
}
// One hop is a whole small performance: crouch, push, air, land, recover.
function hopPose(base, u) {
  const p = {...base};
  if (u < .2) { const a = u / .2; p.squash = lerp(1, .86, easeOut(a)); p.lean = base.lean + .12 * a; p.flap = .2 * a; }
  else if (u < .32) { p.squash = 1.12; p.lean = base.lean + .04; p.flap = .6; p.tuck = .25; }
  else if (u < .82) { const a = (u - .32) / .5; p.squash = lerp(1.06, 1, a); p.flap = a < .5 ? .95 : .35; p.tuck = .55; }
  else if (u < .92) { p.squash = .84; p.flap = .15; }
  else p.squash = lerp(.9, 1, (u - .92) / .08);
  return p;
}
const WALK = rvPose('chick', {gx: .7, gy: .05, tuft: .3});
const WALK_UP = rvPose('chick', {gx: .55, gy: -.7, spark: .8, pupil: 1.15, tuft: .5, tilt: -.06});

// ---------- the performances at each stop ----------
const HATCH = rvTrack([
  [2.30, rvPose('chick', {mood: 'squeeze', squash: 1.14, flap: .7, beakOpen: .7})],
  [2.60, rvPose('chick', {mood: 'blink', squash: .95, flap: .2, beakOpen: .3}), easeOut],
  [2.95, rvPose('chick', {mood: 'blink', squash: 1, flap: 0, beakOpen: 0})],
  [3.20, rvPose('chick', {mood: 'squeeze', squash: 1})],
  [3.30, rvPose('chick', {mood: 'squeeze', squash: .86}), easeOut],
  [3.55, rvPose('chick', {mood: 'blink', squash: 1.0}), easeOut],
  [3.70, rvPose('chick', {mood: 'open', eyeS: 1.14, pupil: .7, beakOpen: .35})],
  [4.20, rvPose('chick', {mood: 'open', eyeS: 1.08, pupil: .8, beakOpen: .1})],
  [4.30, rvPose('chick', {mood: 'blink', eyeS: 1.08, pupil: .8})],
  [4.42, rvPose('chick', {mood: 'open', eyeS: 1.03, pupil: .9})],
  [4.80, rvPose('chick', {gx: -.9, gy: .1, tilt: -.12, lean: -.04, pupil: .95})],
  [5.00, rvPose('chick', {gx: -.9, gy: .1, tilt: -.12, lean: -.04, pupil: .95})],
  [5.35, rvPose('chick', {gx: .95, gy: .45, tilt: .14, lean: .05})],
  [5.50, rvPose('chick', {gx: .95, gy: .5, tilt: .16, lean: .05, spark: 1, pupil: 1.25, eyeS: 1.06})],
  [5.95, rvPose('chick', {gx: .9, gy: .55, tilt: .2, lean: .14, spark: 1, pupil: 1.25, eyeS: 1.06, beakOpen: .2})],
  [6.30, rvPose('chick', {gx: 0, gy: 0, tilt: .04, spark: .6, pupil: 1.15})],
  [6.40, rvPose('chick', {mood: 'happy', tilt: .08, beakOpen: .45})],
  [7.00, rvPose('chick', {mood: 'happy', tilt: .1, beakOpen: .35})],
]);
const SLIP_CHICK = rvTrack([
  [0, WALK],
  [.3, rvPose('chick', {gx: .7, gy: -.7, tilt: .1, eyeS: 1.06, tuft: .5})],
  [.8, rvPose('chick', {gx: .8, gy: -.1, tilt: .1})],
  [1.2, rvPose('chick', {gx: .8, gy: .5, tilt: .12, lean: .08})],
  [1.55, rvPose('chick', {gx: .8, gy: .5, tilt: .16, lean: .2, lid: .42, lidTilt: .55})],
  [1.67, rvPose('chick', {gx: .8, gy: .6, tilt: .32, lean: .32, lid: .3, lidTilt: .5, beakOpen: .3, hx: 22})],
  [1.85, rvPose('chick', {gx: .6, gy: .4, tilt: .1, lean: .1, lid: .2, lidTilt: .3})],
  [2.0, rvPose('chick', {mood: 'happy', tilt: .06, beakOpen: .3})],
  [2.4, rvPose('chick', {mood: 'happy', tilt: .06, beakOpen: .2})],
  [2.5, rvPose('chick', {gx: .5, gy: .3, lean: .18, fb: 44})],
  [2.7, rvPose('chick', {gx: .8, gy: .3, lean: .05, fb: 18})],
  [3.25, WALK],
]);
function slipAt(s) {
  const rest = [1736, topY(1736) - 42];
  if (s < .1) return null;
  if (s < 1.2) { const u = (s - .1) / 1.1; return {x: lerp(2010, rest[0], easeOut(u)) + Math.sin(u * Math.PI * 3) * 46 * (1 - u), y: lerp(240, rest[1], u), rot: Math.sin(u * Math.PI * 3) * .55 * (1 - u) + .06 * u}; }
  if (s < 2.52) return {x: rest[0], y: rest[1], rot: .06};
  const u = (s - 2.52) / .85; return {x: rest[0] + 420 * u, y: rest[1] + 760 * u * u, rot: .06 + u * 2.2};
}
const FALL_CHICK = rvTrack([
  [0, WALK],
  [.3, rvPose('chick', {gx: -.7, gy: .2, tilt: -.1})],
  [.45, rvPose('chick', {mood: 'happy', tilt: -.05, beakOpen: .3})],
  [.75, rvPose('chick', {mood: 'happy', beakOpen: .2})],
  [.9, rvPose('chick', {gx: .5, gy: -.9, tilt: -.1, spark: 1, pupil: 1.2, tuft: .4})],
  [1.2, rvPose('chick', {gx: .5, gy: -.9, tilt: -.1, spark: 1, pupil: 1.2})],
  [1.3, rvPose('chick', {gx: .6, gy: -.7, lid: .35, lidTilt: .5, tilt: -.05})],
  [1.55, rvPose('chick', {gx: .6, gy: -.7, lid: .35, lidTilt: .5, squash: .84, lean: .22, flap: .35})],
  [1.68, rvPose('chick', {gx: .6, gy: -.8, lid: .3, lidTilt: .5, squash: 1.14, lean: .1, flap: 1.1, tuck: .5, beakOpen: .3}), easeOut],
  [2.3, rvPose('chick', {gx: .6, gy: -.8, lid: .3, lidTilt: .5, squash: 1.02, tuck: .8, beakOpen: .35, flap: 1.1})],
  [2.75, rvPose('chick', {gx: -.2, gy: .2, eyeS: 1.16, pupil: .68, tuck: .8, beakOpen: .1, flap: .4})],
  [3.1, rvPose('chick', {gx: -.2, gy: .2, eyeS: 1.16, pupil: .68, tuck: .8, beakOpen: .1, flap: .4})],
  [3.17, rvPose('chick', {gx: 0, gy: -.6, eyeS: 1.16, pupil: .66, squash: 1.12, flap: 1.1, tuck: .3, beakOpen: .75}), easeOut],
  [3.95, rvPose('chick', {mood: 'dizzy', squash: 1.05, flap: .8, tuck: .6, tilt: -.1})],
  [4.65, rvPose('chick', {mood: 'dizzy', squash: 1.0, flap: .5, tuck: .4})],
  [4.73, rvPose('chick', {mood: 'dizzy', squash: .84, flap: .1, tuck: 0}), easeOut],
  [4.98, rvPose('chick', {mood: 'dizzy', squash: 1.0, tilt: -.12})],
  [5.5, rvPose('chick', {mood: 'dizzy', tilt: .1})],
  [5.85, rvPose('chick', {mood: 'open', gx: .6, gy: .05, lid: .3, lidTilt: .55, tuft: .6})],
  [6.0, rvPose('chick', {gx: .6, gy: .05, lid: .3, lidTilt: .55, tuft: .6})],
]);
function fallPose(s) {
  const p = {...FALL_CHICK(s)};
  if (s > 1.7 && s < 2.85) { const f = Math.floor(s * 24 / 2) % 2; p.flap = f ? 1.15 : .15; p.squash *= f ? 1.03 : .98; }
  if (s > 3.17 && s < 3.95) p.flap = Math.floor(s * 24 / 2) % 2 ? 1.2 : .9;
  if (s > 3.95 && s < 4.65) p.flap = Math.floor(s * 24 / 3) % 2 ? .9 : .3;
  if (s > 4.98 && s < 5.5) p.tilt = .13 * Math.sin(s * 9);
  if (s >= 5.5 && s < 5.85) { const f = Math.floor(s * 24 / 2) % 2; p.tilt = f ? .17 : -.17; p.gx = f ? .5 : -.5; }
  return p;
}
function fallRoot(s) {
  const st = [2460, topY(2460)], a1 = [2560, 640], a2 = [2640, 590], land = [2740, topY(2740)];
  if (s < 1.62) return {p: st, rot: 0};
  if (s < 1.85) { const u = (s - 1.62) / .23; return {p: hop(st, a1, easeOut(u), 30), rot: .05 * u}; }
  if (s < 2.85) { const u = (s - 1.85) / 1.0; return {p: [lerp(a1[0], a2[0], u), lerp(a1[1], a2[1], easeOut(u)) + Math.sin(QT1(s) * 38) * 5], rot: .05}; }
  if (s < 3.1) return {p: a2, rot: .05};
  if (s < 3.95) { const u = clamp((s - 3.1) / .42, 0, 1); return {p: [a2[0] + 25 * u, a2[1] + 830 * u * u], rot: .05 - .5 * u}; }
  if (s < 4.65) { const u = (s - 3.95) / .7; return {p: hop([2700, 1420], land, u, 470), rot: -.3 * (1 - u)}; }
  return {p: land, rot: 0};
}
const INK_CHICK = rvTrack([
  [0, WALK],
  [.18, rvPose('chick', {gx: -.3, gy: .8, eyeS: 1.15, pupil: .7, tuft: 1, tilt: -.08})],
  [.55, rvPose('chick', {gx: -.3, gy: .8, eyeS: 1.15, pupil: .7, tuft: 1, tilt: -.08})],
  [.62, rvPose('chick', {mood: 'blink', eyeS: 1.1, tuft: .8})],
  [.7, rvPose('chick', {gx: 0, gy: 0, eyeS: 1.1, pupil: .75, tuft: .8})],
]);
const FLEDGE = rvTrack([
  [1.0, rvPose('adult', {young: .65, mood: 'squeeze', squash: .9})],
  [1.15, rvPose('adult', {young: .65, mood: 'blink'})],
  [1.3, rvPose('adult', {young: .65})],
  [1.45, rvPose('adult', {young: .65, gx: -.55, gy: .75, eyeS: 1.15, pupil: .7, wing: .35, tilt: -.1, tuft: 1})],
  [1.95, rvPose('adult', {young: .65, gx: -.55, gy: .75, eyeS: 1.15, pupil: .7, wing: .35, tilt: -.1, tuft: 1})],
  [2.15, rvPose('adult', {young: .65, gx: 0, gy: -.1, spark: 1, pupil: 1.2, eyeS: 1.05, wing: .1, tuft: .8, beakOpen: .3})],
  [2.45, rvPose('adult', {young: .65, squash: .88, lean: .14, spark: .6, pupil: 1.1, wing: .2})],
  [2.62, rvPose('adult', {young: .65, wingMode: 'spread', flap: .35, squash: 1.06, gy: -.5, beakOpen: .55, tuft: 1})],
  [2.9, rvPose('adult', {young: .65, wingMode: 'spread', flap: -.25, gy: -.4, beakOpen: .5, tuft: 1}), easeOut],
  [4.8, rvPose('adult', {young: .65, wingMode: 'spread', flap: -.18, gy: -.3, beakOpen: .3, tuft: .9})],
]);
// Wet ink makes the chick shake like a dog; the fledgling steps out of the splash.
function inkPose(s) {
  if (s < 1.0) { const p = {...INK_CHICK(s)}; if (s > .7) { const f = Math.floor(s * 24) % 2; p.tilt = f ? .22 : -.22; p.gx = f ? .6 : -.6; p.squash = f ? 1.04 : .97; } return p; }
  const p = {...FLEDGE(s)}; if (s > 2.9) p.flap += .06 * Math.sin(s * 6); return p;
}

// ---------- where the raven is ----------
function chickState(t) {
  const st = {visible: true, x: EGG.x, y: shellFloor(), rot: 0, dx: 0, inShell: false, air: 0, pose: null};
  if (t < T.hatch + 2.3) { st.visible = false; return st; }
  if (t < T.walk1) { st.inShell = true; st.pose = HATCH(t - T.hatch); return st; }
  if (t < T.walk1 + .3) { st.inShell = true; st.pose = rvBlend(HATCH(7), WALK, sm(T.walk1, T.walk1 + .3, t)); return st; }
  const hopping = (t0, t1, pick) => { const r = hopAt(t, t0, t1); const [x, y] = hopRoot(r.h, r.u); Object.assign(st, {x, y, pose: hopPose(pick(r.h), r.u), inShell: r.h.fromShell && r.u < .3, air: r.u > .2 && r.u < .82 ? Math.sin((r.u - .2) / .62 * Math.PI) : 0}); return st; };
  if (t < T.slip) return hopping(T.walk1 + .3, T.slip, () => WALK);
  if (t < T.walk2) { Object.assign(st, {x: 1600, y: topY(1600), pose: SLIP_CHICK(t - T.slip)}); return st; }
  if (t < T.fall) return hopping(T.walk2, T.fall, () => WALK);
  if (t < T.walk3) { const s = t - T.fall, r = fallRoot(s); Object.assign(st, {x: r.p[0], y: r.p[1], rot: r.rot, pose: fallPose(s)}); return st; }
  if (t < T.walk3 + .3) { Object.assign(st, {x: 2740, y: topY(2740), pose: rvBlend(fallPose(6), WALK_UP, sm(T.walk3, T.walk3 + .3, t))}); return st; }
  if (t < T.ink) return hopping(T.walk3 + .3, T.ink, h => h.x0 < 2980 ? WALK_UP : WALK);
  const s = t - T.ink, p = inkPose(s);
  Object.assign(st, {x: 3340, y: topY(3340), pose: p, dx: s > .7 && s < 1.0 ? (Math.floor(s * 24) % 2 ? 7 : -7) : 0});
  return st;
}
// Drawings are exposed per action: travel and falls on ones, thinking on twos.
function exposure(t) {
  const on1 = [[T.walk1, T.slip], [T.walk2, T.fall], [T.walk3, T.ink], [T.hatch + 2.28, T.hatch + 2.72], [T.hatch + 3.18, T.hatch + 3.42],
    [T.slip + 1.63, T.slip + 1.85], [T.fall + 1.6, T.fall + 3.6], [T.fall + 3.9, T.fall + 4.85], [T.fall + 5.5, T.fall + 5.85], [T.ink + .68, T.ink + 1.2], [T.ink + 2.55, T.ink + 2.95]];
  return on1.some(([a, b]) => t >= a && t < b) ? QT1(t) : QT2(t);
}
// Placement runs on every output frame; the drawing is exposed on ones or twos. Taking both
// from the exposed time would make the character stutter against a camera that moves every frame.
function placedAt(state, t, te) { const drawn = state(te), at = state(t); return {...drawn, x: at.x, y: at.y, rot: at.rot, air: at.air}; }
const firstTime = (test, from, to) => { for (let t = from; t <= to; t += 1 / 240) if (test(t)) return t; return to; };
const T29 = firstTime(t => chickState(t).x >= 1530, T.walk1, T.slip);
const T30 = firstTime(t => chickState(t).x >= 2380, T.walk2, T.fall);
const TCROSS = firstTime(t => chickState(t).x >= SEAM, T.walk3, T.ink);

// ---------- the camera: it follows, it never goes back ----------
function keyed(keys, t) {
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) if (t < keys[i][0]) { const [a, va] = keys[i - 1], [b, vb] = keys[i]; return lerp(va, vb, easeIO(clamp((t - a) / (b - a), 0, 1))); }
  return keys[keys.length - 1][1];
}
const ZOOM = [[0, 1.0], [4, 1.06], [5.2, 1.45], [T.walk1 - .2, 1.45], [T.walk1 + .9, 1.3], [T.slip + .2, 1.34], [T.walk2, 1.3], [T.fall, 1.24], [T.walk3, 1.24], [T.ink, 1.26], [T.end, 1.12]];
const CAMY = [[0, 640], [4, 640], [5.2, 606], [T.walk1 + .9, 604], [T.fall + 3.1, 604], [T.fall + 3.5, 640], [T.fall + 4.2, 640], [T.fall + 4.8, 604], [T.end, 592]];
// The later acts extend the route; the camera follows whichever route is installed here.
let FILM_END = T.end, routeX = t => chickState(t).x;
// A moving average of a path that never moves left cannot move left either.
function viewAt(t) {
  let sx = 0; const n = 16, span = 1.4;
  for (let k = 0; k < n; k++) sx += routeX(clamp(t + (k / (n - 1) - .5) * span, 0, FILM_END));
  const k = sm(T.fall + 3.55, T.fall + 3.9, t, lin), shake = k > 0 && k < 1 ? Math.sin(t * 70) * 9 * (1 - k) : 0;
  return {x: sx / n + 150, y: keyed(CAMY, t) + shake, z: keyed(ZOOM, t)};
}
const toScreen = (v, [x, y]) => [(x - v.x) * v.z + W / 2, (y - v.y) * v.z + H / 2];

// ---------- the page, drawn in one material ----------
function drawnTo(t) {
  if (t < T29) return 1820;
  if (t < T30) return lerp(1820, 2380, sm(T29, T29 + .6, t));
  return lerp(2380, SEAM + 40, sm(T30, T30 + .7, t));
}
function drawChick(c, look, t, st) {
  if (!st.visible) return null;
  const place = {x: st.x + st.dx, y: st.y, scale: st.pose.kind === 'adult' ? FLEDGE_S : CHICK_S, rot: st.rot};
  return {G: drawRaven(c, st.pose, {...place, look}), place};
}
function drawHat(c, look, t, st, drawn) {
  const s = t - T.ink;
  if (t < T.hatch + 2.3) return;
  if (t < T.hatch + 3.2) {
    const u = clamp((t - T.hatch - 2.3) / .9, 0, 1), start = [EGG.x, eggCY() - EGG.ry * .12];
    const land = hatSpot(rvGeom(HATCH(3.2)), {x: EGG.x, y: shellFloor(), scale: CHICK_S});
    const q = hop(start, [land.x, land.y], u, 250); drawTopShell(c, look, q[0], q[1], -u * TAU * 1.25 + land.rot * u, .92); return;
  }
  const rest = [3240, topY(3240) - 21];
  if (s >= 2.62) { const u = clamp((s - 2.62) / .7, 0, 1), from = hatSpot(rvGeom(FLEDGE(2.6)), {x: 3340, y: topY(3340), scale: FLEDGE_S}), q = hop([from.x, from.y], rest, u, 130);
    drawTopShell(c, look, q[0], q[1], lerp(from.rot, Math.PI + .25, u), .92); return; }
  if (!drawn) return;
  const h = hatSpot(drawn.G, drawn.place), f = t - T.fall;
  const lift = (f > 3.1 && f < 4.73 ? 26 * sm(3.1, 3.25, f) : 0) + st.air * 7, b = t < T.hatch + 3.45 ? Math.sin((t - T.hatch - 3.2) / .25 * Math.PI) * 8 : 0;
  drawTopShell(c, look, h.x, h.y - lift - b, h.rot + (lift > 10 ? .25 : 0), .92);
}
function drawSplash(c, t) {
  const s = t - T.ink; if (s < .72) return;
  const o = [3340, topY(3340) - 95];
  for (let i = 0; i < 16; i++) {
    const a = -Math.PI * (.08 + .84 * hash(i, 81)), sp = 320 + hash(i, 82) * 420, life = .18 + hash(i, 83) * .3, tt = Math.min(s - .72, life);
    const x = o[0] + Math.cos(a) * sp * tt, y = o[1] + Math.sin(a) * sp * tt + 1400 * tt * tt, r = 3 + hash(i, 84) * 5;
    c.fillStyle = RC.ink; c.fill(curvePath(blob(x, y, r * (tt >= life ? 1.4 : 1), r * (tt >= life ? .9 : 1), i + 5, {amp: .35}), true));
    if (tt >= life && hash(i, 85) > .4) { c.beginPath(); c.arc(x + r * 2.2, y - r * .6, r * .35, 0, TAU); c.fill(); }
  }
  if (s > .86 && s < 1.14) { const k = Math.sin((s - .86) / .28 * Math.PI);
    for (let i = 0; i < 8; i++) { const a = i / 8 * TAU, x = o[0] + Math.cos(a) * 46 * k, y = o[1] + Math.sin(a) * 40 * k;
      c.fillStyle = RC.ink; c.fill(curvePath(blob(x, y, 52 * k, 44 * k, i + 30, {amp: .2}), true)); }
    c.fillStyle = RC.paper; c.globalAlpha = .9 * k; c.beginPath(); c.arc(o[0] - 20, o[1] - 24, 9 * k, 0, TAU); c.arc(o[0] + 26, o[1] + 8, 6 * k, 0, TAU); c.fill(); c.globalAlpha = 1; }
}
function drawPage(c, look, t, v, st) {
  paper(c, RC.paper, null, 87);
  cam(c, v.x, v.y, v.z);
  const span = viewOf(v.x, v.z), ink = look === 'ink';
  if (ink) {
    drawBranch(c, 'ink', span, {drawnTo: 3580}); INK_TWIGS.forEach(tw => drawTwig(c, 'ink', tw)); drawSeam(c, 'ink', v);
    const drawn = drawChick(c, 'ink', t, st); drawHat(c, 'ink', t, st, drawn); drawSplash(c, t);
    note(c, 'ink', ['v0.1.0 public preview · #36', 'one-line install · #8'], [3140, 915], null, sm(T.ink + 2.9, T.ink + 3.8, t, lin));
    return;
  }
  drawBranch(c, 'pencil', span, {drawnTo: drawnTo(t), reveal: t < 4 ? lerp(-170, 1880, sm(.1, 1.7, t, easeOut)) : Infinity});
  drawTwig(c, 'pencil', TWIGS[0], sm(.9, 1.4, t)); drawTwig(c, 'pencil', TWIGS[1], sm(1.2, 1.7, t)); drawTwig(c, 'pencil', TWIGS[2], sm(1.45, 1.95, t));
  drawTwig(c, 'pencil', TWIGS[3], sm(T29 + .3, T29 + .8, t)); drawTwig(c, 'pencil', HIGH_TWIG, sm(T30 + .2, T30 + .9, t));
  drawSeam(c, 'pencil', v);
  drawDayMark(c, 'pencil', '06-28', sm(.9, 1.7, t, lin)); drawDayMark(c, 'pencil', '06-29', sm(T29, T29 + .8, t, lin)); drawDayMark(c, 'pencil', '06-30', sm(T30, T30 + .8, t, lin));
  drawBead(c, 'pencil', beadsFor('06-28')[0], sm(T.hatch + 4.9, T.hatch + 5.35, t, lin), t);
  drawBeads(c, 'pencil', '06-29', T29 + .2, t, .7); drawBeads(c, 'pencil', '06-30', T30 + .4, t, 1.1);
  drawNest(c, 'pencil', sm(1.3, 2.3, t, lin));
  const te = exposure(t);
  if (t < T.hatch + 2.3) {
    const h = te - T.hatch, wobble = h > .4 && h < 1.6 ? .075 * Math.sin((h - .4) / 1.2 * TAU * 3) * Math.sin((h - .4) / 1.2 * Math.PI) : 0;
    drawEgg(c, 'pencil', {p: sm(1.9, 2.8, t, lin), rot: wobble, crack: sm(T.hatch + 1.6, T.hatch + 2.0, t, lin), squash: h > 2.0 ? .97 : 1});
  } else if (!st.inShell) drawLowerShell(c);
  if (!st.inShell) drawNest(c, 'pencil', sm(2.2, 2.7, t, lin), true);
  const slip = t >= T.slip && t < T.walk2 + 1 ? slipAt(t - T.slip) : null;
  const f = t - T.fall, fallNow = f >= 0 && t < T.walk3;
  let back = null;
  if (fallNow) { const G0 = rvGeom(st.pose); back = rvWorld(G0.anchors.back, {x: st.x, y: st.y, scale: CHICK_S, rot: st.rot});
    drawPack(c, 'pencil', back[0] - 6, back[1] + 4, st.rot - .18 + st.pose.lean, lerp(1.25, 1.18, sm(.15, .3, f)), sm(.15, .25, f, lin) * (1 - sm(2.45, 2.85, f, lin))); }
  if (st.visible && st.pose.kind === 'chick') { const drawn = drawChick(c, 'pencil', t, st); if (st.inShell) { drawLowerShell(c); drawNest(c, 'pencil', 1, true); } drawHat(c, 'pencil', t, st, drawn);
    if (fallNow && f > 4.75 && f < 5.85) { const h = hatSpot(drawn.G, drawn.place); drawDizzy(c, 'pencil', h.x, h.y - 70, t, 1 - sm(5.6, 5.85, f, lin)); } }
  else drawHat(c, 'pencil', t, st, null);
  if (slip) drawSlip(c, 'pencil', slip.x, slip.y, slip.rot, sm(1.69, 1.89, te - T.slip, lin));
  if (fallNow) {
    if (f > 2.2 && f < 2.9) { const u = sm(2.2, 2.35, f, lin), out = sm(2.72, 2.9, f, lin), rub = Math.sin(QT1(f) * 44) * 26;
      c.save(); c.translate(lerp(back[0] + 260, back[0] - 14 + rub * .7, u) + out * 300, lerp(back[1] - 230, back[1] + 2, u) - out * 260); c.scale(.72, .72); drawEraser(c, 0, 0, -.35 + .15 * u); c.restore(); }
    drawCrumbs(c, back[0], back[1], sm(2.4, 3.3, f, lin));
    drawSpeed(c, 'pencil', st.x, st.y, f > 3.2 && f < 3.6 ? 1 : 0);
    drawDust(c, 'pencil', 2672, v.y + H / 2 / v.z, sm(3.55, 4.15, f, lin));
  }
  note(c, 'pencil', ['raven init', 'dc0fb282'], [880, 470], [EGG.x + 58, eggCY() - 44], sm(2.8, 3.6, t, lin), {al: 1 - sm(T.hatch + 2.0, T.hatch + 2.4, t, lin)});
  note(c, 'pencil', ['first commit'], [912, 930], [beadsFor('06-28')[0].x + 8, beadsFor('06-28')[0].y + 18], sm(T.hatch + 5.4, T.hatch + 6.1, t, lin), {from: [906, 910]});
  note(c, 'pencil', ['f2eb8197', 'label untrusted content'], [1860, 480], null, sm(T.slip + 1.9, T.slip + 2.6, t, lin));
  if (slip && t < T.slip + 2.52) arrow(c, 'pencil', [1850, 470], [slip.x + 26, slip.y - 50], {col: RC.lead, seed: 1863, p: sm(T.slip + 2.1, T.slip + 2.5, t, lin)});
  const merged = sm(T.fall + .2, T.fall + .8, t, lin), reverted = sm(T.fall + 2.5, T.fall + 3.1, t, lin);
  if (merged > 0) {
    if (reverted <= 0) note(c, 'pencil', ['#9  memory · merged'], [2150, 460], back ? [back[0] - 34, back[1] - 18] : null, merged, {from: [2382, 452]});
    else { writeOn(c, '#9  memory · merged', 2150, 460, {size: 31, col: RC.lead}); pLine(c, partial([[2142, 449], [2360, 445]], reverted * 2), {w: 2, col: RC.red, seed: 12});
      writeOn(c, 'Revert #9 · b922ade9', 2150, 502, {size: 31, col: RC.red, p: clamp(reverted * 1.6 - .5, 0, 1)}); }
  }
}
// The seam where the page turns to ink: a wet vertical brush edge at v0.1.0.
const seamX = y => SEAM + Math.sin(y / 85 + 1) * 12 + noise1(y / 55, 91) * 14;
function seamPoints(v) { const pts = [], y0 = v.y - H / 2 / v.z - 80, y1 = v.y + H / 2 / v.z + 80; for (let y = y0; y <= y1; y += 18) pts.push([seamX(y), y]); return pts; }
// It belongs to the page, so each material draws its own half of it before any character.
function drawSeam(c, look, v) {
  if (Math.abs(toScreen(v, [SEAM, v.y])[0] - W / 2) > W / 2 + 120) return;
  const pts = seamPoints(v); iLine(c, pts, {w: 5, al: .9, seed: 17, rough: 1.2});
  if (look !== 'ink') { pts.forEach(([x, y]) => { const k = Math.round(y / 18); for (let j = 0; j < 2; j++) { const id = k * 2 + j, L = 8 + hash(id, 5) * 30, dy = (hash(id, 6) - .5) * 14;
    iLine(c, [[x - 1, y + dy], [x - L, y + dy + (hash(id, 7) - .5) * 6]], {w: .8 + hash(id, 8) * 1.8, al: .7, seed: id, rough: .3}); } }); return; }
  for (let i = 0; i < 5; i++) { const y = 300 + hash(i, 21) * 700, L = 20 + hash(i, 22) * 60, x = seamX(y) + 2; iLine(c, [[x, y], [x + 1, y + L]], {w: 2.2, al: .85, seed: i + 30, rough: .2}); c.fillStyle = RC.ink; c.beginPath(); c.arc(x + 1, y + L + 2, 3.2, 0, TAU); c.fill(); }
  writeOn(c, 'v0.1.0 · open source', SEAM + 26, 480, {size: 30, col: RC.ink, rot: 0});
}
const LB = layer();
function renderInto(L, fn) { const g = L.getContext('2d'); g.save(); g.setTransform(1, 0, 0, 1, 0, 0); g.clearRect(0, 0, L.width, L.height); fn(g); g.restore(); return L; }
function overlays(c, t) {
  const look = t >= TCROSS ? 'ink' : 'pencil';
  caption(c, look, [["Hi, I'm Raven. Let me show you how I hatched.", 1.0, 2.2, 50], ['I started out as a single line of code.', 2.3, 3.5, 44]], t, [T.hatch + .5, T.hatch + 1.0]);
  caption(c, look, [['Ravens love shiny things.', T.hatch + 5.5, T.hatch + 6.2, 52], ['Each commit is one more for the pile.', T.hatch + 6.25, T.hatch + 6.9, 46]], t, [T.walk1 + 1.1, T.walk1 + 1.4]);
  caption(c, look, [['This branch is my timeline.', T.walk1 + 1.5, T.walk1 + 2.2, 52], ['I only move forward.', T.walk1 + 2.3, T.walk1 + 2.7, 46]], t, [T.slip + .25, T.slip + .5]);
  caption(c, look, [['Day two, lesson one:', T.slip + .55, T.slip + 1.3, 48], ['flag anything a stranger slips me.', T.slip + 1.35, T.slip + 2.4, 48]], t, [T.walk2 + 1.0, T.walk2 + 1.3]);
  caption(c, look, [['Day three: my first fall.', T.fall + 4.3, T.fall + 5.0, 52], ['My brand-new memory got rolled back.', T.fall + 5.1, T.fall + 6.0, 48]], t, [T.ink + .6, T.ink + .9]);
  caption(c, look, [['That same day, they opened the nest.', T.ink + 1.3, T.ink + 2.0, 52], ['Now I\'m in ink. No erasing me.', T.ink + 2.2, T.ink + 3.4, 48]], t);
  featureCard(c, look, t);
}
// Every frame of Act I comes from here, so a stop never resets the route.
function journey(c, t) {
  const v = viewAt(t), st = placedAt(chickState, t, exposure(t)), seamSx = toScreen(v, [SEAM, v.y])[0];
  if (seamSx > W + 80) drawPage(c, 'pencil', t, v, st);
  else {
    drawPage(c, 'pencil', t, v, st);
    renderInto(LB, g => drawPage(g, 'ink', t, v, st));
    resetT(c); c.save(); const clip = [...seamPoints(v).map(p => toScreen(v, p)), [W + 20, H + 20], [W + 20, -20]];
    c.clip(polyPath(clip)); c.drawImage(LB, 0, 0, W, H); c.restore();
  }
  resetT(c); overlays(c, t);
}
