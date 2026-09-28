'use strict';
// Acts II to V: the same route after the inking, from 1 July to 24 September, for new users.
// Raven keeps moving right: two short walks, a first take-off, then flight to the end.
// One real request runs through the second half, so every built-in agent shows what it does.
// Each important commit is a seam in the page; past it the page is another material.

// ---------- dates ----------
const DAILY = [["06-28",1],["06-29",7],["06-30",42],["07-01",14],["07-02",3],["07-03",6],["07-06",1],["07-07",4],["07-08",2],["07-09",1],["07-10",4],["07-12",2],["07-13",3],["07-16",3],["07-17",4],["07-19",2],["07-20",2],["07-21",5],["07-22",7],["07-23",6],["07-24",2],["07-26",1],["07-28",3],["07-29",3],["07-30",4],["07-31",4],["08-01",6],["08-03",2],["08-04",2],["08-05",3],["08-06",3],["08-10",7],["08-12",8],["08-13",3],["08-14",18],["08-15",1],["08-16",13],["08-17",24],["08-18",33],["08-19",16],["08-20",42],["08-21",30],["08-22",8],["08-23",42],["08-24",27],["08-25",52],["08-26",50],["08-27",44],["08-28",82],["08-29",50],["08-30",137],["08-31",51],["09-01",22],["09-02",42],["09-03",13],["09-04",7],["09-05",17],["09-06",22],["09-07",70],["09-08",46],["09-09",58],["09-10",64],["09-11",91],["09-12",2],["09-13",1],["09-14",1],["09-15",7],["09-16",38],["09-17",39],["09-18",17],["09-19",13],["09-20",31],["09-21",46],["09-22",57],["09-23",86],["09-24",6]];
const dayIndex = s => (Date.UTC(2026, +s.slice(0, 2) - 1, +s.slice(3, 5)) - Date.UTC(2026, 5, 28)) / 864e5;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dayLabel = s => `${MONTHS[+s.slice(0, 2) - 1]} ${+s.slice(3, 5)}`;
// Where each date sits on the branch. The spacing follows the story, the order follows the calendar.
const MARKS = [["06-28", 700], ["06-29", 1650], ["06-30", 2500], ["07-01", 3600], ["07-06", 4100], ["07-16", 7000], ["07-17", 7900], ["07-22", 14200], ["07-31", 15200],
  ["08-10", 15500], ["08-14", 16300], ["08-26", 17800], ["08-27", 19400], ["08-28", 21000], ["08-29", 24650], ["08-30", 26000], ["09-04", 27500], ["09-10", 29400],
  ["09-16", 31000], ["09-21", 32600], ["09-23", 34200], ["09-24", 35600]].map(([s, x]) => [dayIndex(s), x]);
function xOfDay(i) { for (let k = 1; k < MARKS.length; k++) if (i <= MARKS[k][0]) { const [a, xa] = MARKS[k - 1], [b, xb] = MARKS[k]; return lerp(xa, xb, (i - a) / (b - a)); } return MARKS[MARKS.length - 1][1]; }
const DAYS2 = (() => { let sum = 0; return DAILY.map(([s, n]) => (sum += n, {s, n, i: dayIndex(s), x: xOfDay(dayIndex(s)), total: sum})); })();
const SHOWN = new Set(['07-01', '07-06', '07-16', '07-17', '07-22', '08-10', '08-14', '08-26', '08-27', '08-28', '08-29', '08-30', '09-04', '09-10', '09-16', '09-21', '09-24']);

// ---------- seams ----------
const SEAM2 = 7700, SEAM3 = 15300, SEAM4 = 24500, SEAM5 = 27300;
const REGIONS = [['pencil', -1e9, SEAM], ['ink', SEAM, SEAM2], ['riso', SEAM2, SEAM3], ['screen', SEAM3, SEAM4], ['blue', SEAM4, SEAM5], ['mixed', SEAM5, 1e9]];
const seamAt = (x0, y) => x0 === SEAM ? seamX(y) : x0 + Math.sin(y / 85 + x0 * .01) * 12 + noise1(y / 55, x0 % 97) * 14;
const seamLine = (x0, v) => { const pts = [], y0 = v.y - H / 2 / v.z - 80, y1 = v.y + H / 2 / v.z + 80; for (let y = y0; y <= y1; y += 18) pts.push([seamAt(x0, y), y]); return pts; };

// ---------- timing: one start per stop on the route ----------
const ST = {hopA: T.end + .35, hopB: 35.2, launch: 38.1, fly: 38.4, evo: 44.0, july: 60.0, flock: 63.0, code: 69.0, oncall: 73.0,
  third: 77.0, blue: 86.0, design: 93.0, book: 98.0, remind: 103.0, harness: 107.0, finale: 113.0, end: 128};
ST.mem = ST.hopA + 3 * .45; ST.tools = ST.hopB + 3 * .45;
FILM_END = ST.end;
const HOPS2 = [];
for (let i = 0; i < 3; i++) HOPS2.push({t0: ST.hopA + i * .45, x0: 3340 + i * 150, x1: 3340 + (i + 1) * 150, d: .45, h: 50});
for (let i = 0; i < 3; i++) HOPS2.push({t0: ST.hopB + i * .45, x0: 3790 + i * 500 / 3, x1: 3790 + (i + 1) * 500 / 3, d: .45, h: 56});
const FLIGHT_X = [[ST.fly, 4290], [39.3, 4700], [ST.evo, 7750], [ST.flock, 15350], [ST.blue, 24550], [ST.design, 27350], [ST.finale, 35350], [ST.end, 37000]];
function flightX(t) {
  for (let k = 1; k < FLIGHT_X.length; k++) if (t < FLIGHT_X[k][0]) { const [a, xa] = FLIGHT_X[k - 1], [b, xb] = FLIGHT_X[k], u = (t - a) / (b - a); return lerp(xa, xb, k === 1 ? u * u : u); }
  return FLIGHT_X[FLIGHT_X.length - 1][1];
}
const SCROLL_X = 6990;
const bump = (t, a, b) => t < a || t > b ? 0 : Math.sin((t - a) / (b - a) * Math.PI);
function flightY(t) {
  const k = clamp((t - ST.fly) / .9, 0, 1);
  return lerp(topY(4290), 560, easeOut(k)) + 12 * Math.sin(t * TAU * 1.1) * k;
}
const ADULT_WALK = rvPose('adult', {young: .65, gx: .7});
const MEM = rvTrack([
  [0, ADULT_WALK], [.12, rvPose('adult', {young: .65, gx: -.7, gy: .2, tilt: -.1})], [.35, rvPose('adult', {young: .65, mood: 'happy', beakOpen: .3})],
  [.6, rvPose('adult', {young: .65, gx: .6, gy: -.8, eyeS: 1.12, pupil: .75, tuft: 1})], [.82, rvPose('adult', {young: .65, gx: .6, gy: -.8, squash: .9, lid: .35})],
  [.95, rvPose('adult', {young: .65, gx: .5, gy: -.9, eyeS: 1.15, pupil: .7, tuft: 1})], [1.3, rvPose('adult', {young: .65, mood: 'happy', tuft: 1, beakOpen: .35})],
  [1.77, ADULT_WALK],
]);
const TOOLS = rvTrack([
  [0, ADULT_WALK], [.2, rvPose('adult', {young: .65, gx: -.85, gy: .45, tilt: -.12})], [.55, rvPose('adult', {young: .65, gx: -.85, gy: .45, tilt: -.12, eyeS: 1.12, pupil: .75})],
  [.9, rvPose('adult', {young: .65, gx: -.4, gy: .3, lean: -.08})], [1.15, rvPose('adult', {young: .65, gx: .5, gy: .2, spark: 1, pupil: 1.2})],
  [1.4, rvPose('adult', {young: .62, mood: 'happy', beakOpen: .2})], [1.55, rvPose('adult', {young: .62, gx: .8, lid: .25, lidTilt: .4})],
]);
const beatPhase = t => t < ST.evo ? 2.6 * t : 2.6 * ST.evo + 2.1 * (t - ST.evo);
const flyPose = (t, o = {}) => rvPose('adult', {wingMode: 'spread', flap: -.35 + 1.05 * Math.cos(TAU * beatPhase(t)), tuck: 1, lean: .25, gx: .75, gy: .1,
  young: lerp(.62, 0, sm(ST.fly, 62, t, lin)), ...o});
let GRAB = 42.8;
function raven2(t) {
  const st = {x: 3340, y: topY(3340), rot: 0, pose: null, flying: false, air: 0};
  if (t < ST.hopA) { st.pose = rvBlend(FLEDGE(4.8), ADULT_WALK, sm(T.end, ST.hopA, t)); if (st.pose.wingMode === 'spread') st.pose.flap = lerp(-.18, .7, sm(T.end, ST.hopA, t)); return st; }
  const walk = (t0, t1) => { let r = null; for (const h of HOPS2) if (h.t0 >= t0 - 1e-6 && h.t0 < t1 && t >= h.t0) r = {h, u: Math.min(1, (t - h.t0) / h.d)};
    const [x, y] = hopRoot({...r.h, fromShell: false}, r.u); Object.assign(st, {x, y, pose: hopPose(ADULT_WALK, r.u), air: r.u > .2 && r.u < .82 ? 1 : 0}); return st; };
  if (t < ST.mem) return walk(ST.hopA, ST.mem);
  if (t < ST.hopB) { Object.assign(st, {x: 3790, y: topY(3790), pose: MEM(t - ST.mem)}); return st; }
  if (t < ST.tools) return walk(ST.hopB, ST.tools);
  if (t < ST.launch) { Object.assign(st, {x: 4290, y: topY(4290), pose: TOOLS(t - ST.tools)}); return st; }
  if (t < ST.fly) { const u = (t - ST.launch) / .3; Object.assign(st, {x: 4290, y: topY(4290), pose: rvPose('adult', {young: .62, squash: lerp(1, .84, u), lean: .1 + .15 * u, wing: .35 * u, gx: .8, gy: -.3})}); return st; }
  const k = clamp((t - ST.fly) / .9, 0, 1);
  // In the finale Raven flies on and, near the end, is shown looking back at the viewer.
  Object.assign(st, {x: flightX(t), y: flightY(t), rot: lerp(-.3, .24, easeOut(k)), flying: true,
    pose: flyPose(t, {tuck: lerp(.4, 1, k) - .7 * bump(t, GRAB - .35, GRAB + .35), gy: t < ST.evo ? lerp(.1, .6, bump(t, GRAB - .8, GRAB + .2)) : .1})});
  return st;
}
routeX = t => t < T.end ? chickState(t).x : raven2(t).x;
GRAB = firstTime(t => raven2(t).x >= SCROLL_X + 10, ST.fly, ST.evo);
function exposure2(t) { return raven2(t).flying || (t >= ST.hopA && t < ST.mem) || (t >= ST.hopB && t < ST.tools) ? QT1(t) : QT2(t); }
ZOOM.push([T.end + .1, 1.12], [ST.launch, 1.12], [ST.fly + 1.1, 1.0], [ST.finale, 1.0], [ST.finale + 2.6, .62], [ST.end, .62]);
CAMY.push([ST.launch, 592], [ST.fly + 1.1, 540], [ST.finale, 540], [ST.finale + 2.6, 470], [ST.end, 470]);
// One sweep of the route finds when it passes every date.
const dayCross = (() => { const out = DAYS2.map(() => ST.end); let k = 0; for (let t = 0; t <= ST.end && k < DAYS2.length; t += 1 / 48) { const x = routeX(t); while (k < DAYS2.length && x >= DAYS2[k].x - 60) out[k++] = t; } return out; })();

// ---------- companions: the four built-in agents and five invited ones ----------
const MATES = [
  {name: 'Research', body: '#2f6fd0', join: ST.flock + 2.0, ph: .3, keys: [[0, -330, -120]]},
  {name: 'Code', body: '#1f9e8f', join: ST.flock + 2.4, ph: .55, keys: [[0, -520, 110], [ST.code, -520, 110], [ST.code + .8, 230, 110], [ST.code + 3.3, 230, 110], [ST.code + 4.0, -520, 110]]},
  {name: 'Oncall', body: '#e0a030', join: ST.flock + 2.8, ph: .8, keys: [[0, -760, -30], [ST.oncall, -760, -30], [ST.oncall + .8, 240, -30], [ST.oncall + 3.3, 240, -30], [ST.oncall + 4.0, -760, -30]]},
  {name: 'Design', body: '#e0605a', join: ST.design + .3, ph: .1, keys: [[0, 330, -170], [ST.design + 1.0, 330, -170], [ST.design + 1.7, 240, 150], [ST.design + 4.3, 240, 150], [ST.design + 5.0, 330, -170]]},
];
const GUESTS = ['Claude Code', 'Codex', 'Kimi Code', 'OpenClaw', 'MiroThinker'].map((name, i) => ({name, guest: true, join: ST.third + .4 + i * .45, ph: .2 + i * .17, keys: [[0, -700 + i * 300, i % 2 ? 360 : 290]]}));
function keyedOff(keys, t) { let [, x, y] = keys[0]; for (let i = 1; i < keys.length; i++) { const [a, xa, ya] = keys[i - 1], [b, xb, yb] = keys[i]; if (t >= b) { x = xb; y = yb; } else if (t > a) { const u = easeIO((t - a) / (b - a)); x = lerp(xa, xb, u); y = lerp(ya, yb, u); break; } } return [x, y]; }
function mateState(m, t, rx, ry) {
  if (t < m.join) return null;
  const u = easeOut(clamp((t - m.join) / 1.8, 0, 1)), [ox, oy] = keyedOff(m.keys, t);
  return {x: rx + lerp(-1500, ox, u) + 16 * Math.sin(t * 1.3 + m.ph * 7), y: ry + oy * u + 14 * Math.sin(t * TAU * 1.05 + m.ph * 5), rot: .24,
    pose: rvPose('adult', {body: m.body, wingMode: 'spread', flap: -.35 + 1.05 * Math.cos(TAU * 2.1 * QT1(t) + m.ph * TAU), tuck: 1, lean: .25, gx: .75, gy: .05})};
}
// Accessories say who is who at a glance; invited agents wear a name tag.
function drawAccessory(c, m, G, place, look, t) {
  const col = look === 'blue' ? '#e8efff' : RC.inkLine, e = rvWorld(G.eyes.near.c, place), crown = rvWorld(G.anchors.crown, place), chest = rvWorld(G.anchors.chest, place);
  c.save(); c.strokeStyle = col; c.lineWidth = 2.2; c.fillStyle = col;
  if (m.guest) { const b = rvWorld(G.anchors.back, place); c.beginPath(); c.moveTo(b[0], b[1]); c.lineTo(b[0] - 18, b[1] + 26); c.stroke();
    c.font = `600 24px ${RV_FONT}`; const w = c.measureText(m.name).width + 20; c.fillStyle = look === 'blue' ? '#23396c' : RC.white; c.fillRect(b[0] - 18 - w / 2, b[1] + 26, w, 34); c.strokeRect(b[0] - 18 - w / 2, b[1] + 26, w, 34);
    writeOn(c, m.name, b[0] - 18, b[1] + 51, {size: 24, col, align: 'center', rot: 0}); }
  if (m.name === 'Research') { c.beginPath(); c.arc(e[0], e[1], G.eyes.near.rx * place.scale * 1.25, 0, TAU); c.stroke(); c.beginPath(); c.moveTo(e[0], e[1] + 14); c.quadraticCurveTo(e[0] - 10, e[1] + 40, e[0] - 24, e[1] + 44); c.stroke(); }
  if (m.name === 'Code') writeOn(c, '</>', chest[0] - 30, chest[1] + 12, {size: 26, col: look === 'blue' ? col : '#f6fbf4', rot: .2});
  if (m.name === 'Oncall') { c.save(); c.translate(crown[0], crown[1]); c.rotate(-.5); c.fillStyle = look === 'blue' ? '#23396c' : '#3b4a8a'; c.beginPath(); c.moveTo(-26, 6); c.quadraticCurveTo(0, -30, 34, -18); c.lineTo(20, 8); c.closePath(); c.fill(); c.stroke();
    c.fillStyle = '#fff4c4'; c.beginPath(); c.arc(36, -18, 7, 0, TAU); c.fill(); c.restore();
    const f = rvWorld(G.legs[0].foot, place), night = nightK(t);
    if (night > 0) { const g = c.createRadialGradient(f[0] - 4, f[1] + 34, 0, f[0] - 4, f[1] + 34, 160); g.addColorStop(0, `rgba(255,214,110,${.55 * night})`); g.addColorStop(1, 'rgba(255,214,110,0)'); c.fillStyle = g; c.beginPath(); c.arc(f[0] - 4, f[1] + 34, 160, 0, TAU); c.fill(); }
    c.beginPath(); c.moveTo(f[0], f[1]); c.lineTo(f[0] - 4, f[1] + 22); c.stroke(); c.fillStyle = 'rgba(255,214,110,.95)'; c.fillRect(f[0] - 14, f[1] + 22, 20, 24); c.strokeRect(f[0] - 14, f[1] + 22, 20, 24); }
  if (m.name === 'Design') { c.save(); c.translate(crown[0], crown[1] + 4); c.rotate(-.35); c.fillStyle = look === 'blue' ? '#23396c' : '#7a2e3a'; c.beginPath(); c.ellipse(0, -6, 34, 12, 0, 0, TAU); c.fill(); c.stroke(); c.beginPath(); c.moveTo(0, -18); c.lineTo(2, -28); c.stroke(); c.restore(); }
  c.restore();
}
const nightK = t => sm(ST.oncall + .2, ST.oncall + 1.0, t) * (1 - sm(ST.oncall + 2.8, ST.oncall + 3.6, t));

// ---------- props of Act II ----------
function drawMagnifier(c, x, y, rot, s = 1, col = RC.inkLine) {
  c.save(); c.translate(x, y); c.rotate(rot); c.scale(s, s); c.strokeStyle = col; c.lineWidth = 4; c.lineCap = 'round';
  c.beginPath(); c.moveTo(0, 0); c.lineTo(34, 0); c.stroke(); c.fillStyle = 'rgba(200,225,255,.5)'; c.beginPath(); c.arc(52, 0, 18, 0, TAU); c.fill(); c.lineWidth = 3.5; c.stroke();
  c.fillStyle = '#ffffff'; c.beginPath(); c.arc(46, -6, 4, 0, TAU); c.fill(); c.restore();
}
function drawPencilProp(c, x, y, rot, s = 1, chalk = false) {
  c.save(); c.translate(x, y); c.rotate(rot); c.scale(s, s);
  c.fillStyle = chalk ? '#23396c' : '#f0c24a'; c.fillRect(0, -7, 78, 14); c.fillStyle = chalk ? '#e8efff' : '#f3dcb5'; c.beginPath(); c.moveTo(78, -7); c.lineTo(100, 0); c.lineTo(78, 7); c.closePath(); c.fill();
  c.fillStyle = chalk ? '#e8efff' : RC.pencil; c.beginPath(); c.moveTo(94, -2); c.lineTo(100, 0); c.lineTo(94, 2); c.closePath(); c.fill();
  c.fillStyle = chalk ? '#e8efff' : RC.pink; c.fillRect(-10, -7, 10, 14);
  c.strokeStyle = chalk ? '#e8efff' : RC.inkLine; c.lineWidth = 1.6; c.strokeRect(-10, -7, 88, 14); c.restore();
}
function drawScroll(c, x, y, rot, s = 1) {
  c.save(); c.translate(x, y); c.rotate(rot); c.scale(s, s);
  c.fillStyle = RC.white; c.fillRect(-26, 0, 52, 64); iLine(c, [[-26, 0], [26, 0], [26, 64], [-26, 64], [-26, 0]], {w: 2, seed: 5, rough: .3});
  squiggleText(c, -18, 16, 36, 3, {color: RC.ink, seed: 3, lineH: 12, amp: 2.5, width: 1});
  c.fillStyle = RC.red; c.beginPath(); c.moveTo(16, 50); c.lineTo(40, 58); c.lineTo(16, 66); c.closePath(); c.fill();
  writeOn(c, 'source', 42, 66, {size: 20, col: RC.red, rot: 0}); c.restore();
}
const TOOL_ICONS = ['wrench', 'glass', 'scissors', 'hammer', 'brush', 'glass', 'ruler', 'key', 'wrench', 'brush', 'scissors', 'hammer'];
function toolIcon(c, kind, x, y, s = 1) {
  c.save(); c.translate(x, y); c.scale(s, s); c.strokeStyle = RC.inkLine; c.fillStyle = RC.inkLine; c.lineWidth = 3; c.lineCap = 'round';
  if (kind === 'wrench') { c.beginPath(); c.moveTo(-12, 12); c.lineTo(8, -8); c.stroke(); c.beginPath(); c.arc(11, -11, 7, .6, 5.2); c.stroke(); }
  if (kind === 'glass') { c.beginPath(); c.moveTo(-12, 12); c.lineTo(-3, 3); c.stroke(); c.beginPath(); c.arc(4, -4, 8, 0, TAU); c.stroke(); }
  if (kind === 'scissors') { c.beginPath(); c.moveTo(-10, -10); c.lineTo(10, 10); c.moveTo(10, -10); c.lineTo(-10, 10); c.stroke(); c.beginPath(); c.arc(-12, 12, 4, 0, TAU); c.arc(12, 12, 4, 0, TAU); c.stroke(); }
  if (kind === 'hammer') { c.beginPath(); c.moveTo(0, 14); c.lineTo(0, -6); c.stroke(); c.fillRect(-10, -12, 20, 7); }
  if (kind === 'brush') { c.beginPath(); c.moveTo(-10, 10); c.lineTo(6, -6); c.stroke(); c.beginPath(); c.ellipse(9, -9, 5, 3, -.8, 0, TAU); c.fill(); }
  if (kind === 'ruler') { c.strokeRect(-14, -5, 28, 10); for (let i = -10; i <= 10; i += 5) { c.beginPath(); c.moveTo(i, -5); c.lineTo(i, 0); c.stroke(); } }
  if (kind === 'key') { c.beginPath(); c.arc(-8, 0, 6, 0, TAU); c.moveTo(-2, 0); c.lineTo(14, 0); c.moveTo(10, 0); c.lineTo(10, 6); c.stroke(); }
  c.restore();
}
// The tool roll trails behind while walking, then rolls up toward the raven.
function drawToolRoll(c, t, rx) {
  const s = t - ST.hopB; if (s < 0 || t > ST.tools + 1.4) return;
  const right = Math.min(rx - 40, 4250), left = lerp(3790, right - 30, sm(ST.tools + .2, ST.tools + .9, t, easeIn));
  if (right - left < 8) return;
  const y = topY((left + right) / 2) - 2;
  c.fillStyle = RC.white; c.fillRect(left, y - 30, right - left, 30); iLine(c, [[left, y - 30], [right, y - 30]], {w: 2, seed: 41, rough: .3}); iLine(c, [[left, y], [right, y]], {w: 2, seed: 42, rough: .3});
  for (let i = 0, x = 3820; x < right - 20; i++, x += 38) if (x > left + 10) toolIcon(c, TOOL_ICONS[i % TOOL_ICONS.length], x, y - 15, .75);
  c.fillStyle = '#d9d2c0'; c.beginPath(); c.ellipse(left, y - 15, 9, 16, 0, 0, TAU); c.fill(); iLine(c, ellPts(left, y - 15, 9, 16, 0, 16), {w: 2, close: true, seed: 43, rough: .3});
}

// ---------- the pages after the pencil ----------
const PAR = (v, k, x, y) => [v.x + (x - v.x) * k, y];
function beadsOnBranch(c, look, v, t) {
  const [a, b] = viewOf(v.x, v.z), today = routeX(t) + 650;
  for (const d of DAYS2) {
    if (d.i < 3 || d.x > today) continue; const w = Math.min(820, 26 + d.n * 8); if (d.x + w < a - 50 || d.x - w > b + 50) continue;
    for (let k = 0; k < d.n; k++) {
      const x = d.x + 30 + (k / Math.max(1, d.n - 1) - .5) * w + (hash(k, d.i) - .5) * 10; if (x < a - 20 || x > b + 20) continue;
      const ay = branchY(x) + branchT(x) / 2, y = ay + 14 + hash(k, d.i + 50) * 52, r = 5 + hash(k, d.i + 90) * 3;
      if (look === 'blue') { c.strokeStyle = 'rgba(232,239,255,.8)'; c.lineWidth = 1.2; c.beginPath(); c.moveTo(x, ay); c.lineTo(x, y - r); c.stroke(); c.beginPath(); c.arc(x, y, r, 0, TAU); c.stroke(); continue; }
      c.strokeStyle = look === 'screen' ? '#0a2540' : RC.inkLine; c.lineWidth = .9; c.globalAlpha = .6; c.beginPath(); c.moveTo(x, ay); c.lineTo(x, y - r); c.stroke(); c.globalAlpha = 1;
      if (look === 'riso') { c.fillStyle = 'rgba(255,72,176,.7)'; c.beginPath(); c.arc(x + 2, y + 1.5, r, 0, TAU); c.fill(); c.fillStyle = 'rgba(255,232,0,.9)'; c.beginPath(); c.arc(x - 1, y - 1, r, 0, TAU); c.fill(); }
      else { c.fillStyle = look === 'screen' ? '#f08a24' : '#d6a23a'; c.beginPath(); c.arc(x, y, r, 0, TAU); c.fill(); c.strokeStyle = look === 'screen' ? '#0a2540' : RC.inkLine; c.lineWidth = 1.3; c.stroke(); }
      c.fillStyle = '#fffbef'; c.beginPath(); c.arc(x - r * .35, y - r * .38, r * .28, 0, TAU); c.fill();
    }
  }
}
// A flat branch for printed looks; its points sit on a fixed grid, so nothing swims.
function flatBranch(c, look, v, t) {
  const [a, b] = viewOf(v.x, v.z), end = Math.min(b + 60, routeX(t) + 650), top = [], bot = [];
  for (let x = Math.floor((a - 60) / 26) * 26; x <= end; x += 26) { top.push([x, branchY(x) - branchT(x) / 2]); bot.push([x, branchY(x) + branchT(x) / 2]); }
  if (top.length > 1) {
    const path = polyPath([...top, ...bot.slice().reverse()]);
    if (look === 'riso') { c.save(); c.globalCompositeOperation = 'multiply'; c.translate(3, -2); c.fillStyle = 'rgba(0,120,191,.85)'; c.fill(path); c.translate(-6, 4); c.fillStyle = 'rgba(255,72,176,.6)'; c.fill(path); c.restore(); }
    else if (look === 'screen') { c.fillStyle = '#0a2540'; c.fill(path); }
    else if (look === 'blue') { c.strokeStyle = '#e8efff'; c.lineWidth = 2; c.beginPath(); top.forEach((p, i) => i ? c.lineTo(p[0], p[1]) : c.moveTo(p[0], p[1])); c.stroke(); c.beginPath(); bot.forEach((p, i) => i ? c.lineTo(p[0], p[1]) : c.moveTo(p[0], p[1])); c.stroke(); }
    else { c.fillStyle = RC.ink; c.fill(path); }
  }
  c.save(); c.setLineDash([12, 14]); c.strokeStyle = look === 'blue' ? '#e8efff' : RC.ink; c.globalAlpha = .3; c.lineWidth = 1.2; c.beginPath(); c.moveTo(end, branchY(end)); c.lineTo(b + 80, branchY(b + 80)); c.stroke(); c.restore();
}
function dayMarks(c, look, v, t) {
  const [a, b] = viewOf(v.x, v.z), today = routeX(t) + 650, col = look === 'blue' ? '#e8efff' : look === 'screen' ? '#0a2540' : look === 'riso' ? '#0078bf' : RC.ink;
  for (const d of DAYS2) {
    if (d.i < 3 || !SHOWN.has(d.s) || d.x > today || d.x < a - 100 || d.x > b + 100) continue;
    const y = branchY(d.x), th = branchT(d.x); c.strokeStyle = look === 'blue' ? '#ffd76a' : RC.red; c.lineWidth = 3; c.beginPath(); c.moveTo(d.x - 3, y - th / 2 - 9); c.lineTo(d.x + 3, y + th / 2 + 10); c.stroke();
    writeOn(c, dayLabel(d.s), d.x, y + 118, {size: 38, col, align: 'center'});
  }
}
// A halftone whose lattice belongs to the page: dots keep their places while the camera travels,
// and only the ones inside `box` are emitted.
function halftone(c, path, box, color, density, angle, seed, cell = 9) {
  const [bx, by, bw, bh] = box, ux = Math.cos(angle), uy = Math.sin(angle), corners = [[bx, by], [bx + bw, by], [bx, by + bh], [bx + bw, by + bh]];
  const pi = corners.map(([x, y]) => (x * ux + y * uy) / cell), pj = corners.map(([x, y]) => (-x * uy + y * ux) / cell);
  c.save(); c.clip(path); c.fillStyle = color; c.globalAlpha = .9; c.beginPath();
  const rad = cell * .62 * Math.sqrt(density);
  for (let i = Math.floor(Math.min(...pi)); i <= Math.ceil(Math.max(...pi)); i++) for (let j = Math.floor(Math.min(...pj)); j <= Math.ceil(Math.max(...pj)); j++) {
    const x = (i * ux - j * uy) * cell + (hash(i * 7919 + j, seed) - .5) * cell * .2, y = (i * uy + j * ux) * cell + (hash(j * 7919 + i, seed + 1) - .5) * cell * .2;
    if (x < bx || x > bx + bw || y < by || y > by + bh) continue; c.moveTo(x + rad, y); c.arc(x, y, rad, 0, TAU);
  }
  c.fill(); c.restore();
}
function scenery(c, look, v, t) {
  const [a, b] = viewOf(v.x, v.z);
  if (look === 'ink') {
    // An ink-wash range behind the long first flight.
    c.save(); c.translate(v.x * .45, 0);
    for (let layer = 0; layer < 2; layer++) { const pts = [[4200 * .55 - 400, 820]]; for (let x = 4200 * .55 - 400; x < SEAM2 * .55 + 600; x += 90) pts.push([x, 640 - layer * 70 + noise1(x / 260, 7 + layer) * 150]); pts.push([SEAM2 * .55 + 600, 820]);
      const p = curvePath(pts, true, .8); c.fillStyle = layer ? 'rgba(60,58,70,.12)' : 'rgba(60,58,70,.2)'; c.fill(p); iLine(c, pts.slice(1, -1), {w: 1.6, al: .45, seed: 90 + layer, rough: .8}); }
    c.restore();
    // The far tree that holds the answer.
    if (7050 > a - 400 && 7050 < b + 400) { const base = [7120, topY(7120)];
      c.fillStyle = RC.ink; c.fill(polyPath([[base[0] - 14, base[1]], [base[0] - 8, 470], [base[0] + 8, 470], [base[0] + 14, base[1]]]));
      for (let i = 0; i < 7; i++) c.fill(curvePath(blob(base[0] - 40 + hash(i, 3) * 110, 420 - hash(i, 4) * 90, 60 + hash(i, 5) * 30, 44, i + 3, {amp: .2}), true));
      iLine(c, [[base[0] - 6, 560], [7010, 540], [6960, 548]], {w: 5, seed: 71, rough: .3}); }
  }
  if (look === 'mixed') {
    const sx = look === 'riso' ? SEAM2 + 2600 : SEAM5 + 2200, p = PAR(v, .25, sx, 420), sun = new Path2D(); sun.arc(p[0], p[1], 140, 0, TAU);
    halftone(c, sun, [p[0] - 150, p[1] - 150, 300, 300], '#ff48b0', .45, .26, 11);
  }
  if (look === 'riso') {
    c.save(); c.translate(v.x * .4, 0);
    const pts = [[SEAM2 * .6 - 500, 830]]; for (let x = SEAM2 * .6 - 500; x < SEAM3 * .6 + 900; x += 120) pts.push([x, 610 + noise1(x / 300, 17) * 110]); pts.push([SEAM3 * .6 + 900, 830]);
    // Dots only where the camera can see them: a whole-range screen is hundreds of thousands of arcs.
    const hills = curvePath(pts, true, .8), lx = a - v.x * .4; halftone(c, hills, [lx - 60, 450, b - a + 120, 400], '#0078bf', .45, 1.31, 21); c.restore();
  }
  if (look === 'screen' || look === 'mixed') {
    const sx = look === 'screen' ? SEAM3 + 2200 : SEAM5 + 3600, p = PAR(v, .25, sx, 420), sun = new Path2D(); sun.arc(p[0], p[1], 120, 0, TAU);
    if (look === 'screen') { c.fillStyle = '#f08a24'; c.fill(sun); }
    c.save(); c.translate(v.x * .4, 0); const x0 = (look === 'screen' ? SEAM3 : SEAM5) * .6 - 600, x1 = x0 + 9000;
    const pts = [[x0, 830]]; for (let x = x0; x < x1; x += 140) pts.push([x, 640 + ((x / 140) % 2 ? -60 : 40) + noise1(x / 200, 23) * 50]); pts.push([x1, 830]);
    const hills = polyPath(pts); c.fillStyle = look === 'screen' ? '#2c6fa3' : 'rgba(44,111,163,.55)'; c.fill(hills);
    if (look === 'screen') { const lx = a - v.x * .4; halftone(c, hills, [lx - 60, 500, b - a + 120, 340], '#0a2540', .22, 0, 5, 7); } c.restore();
  }
  if (look === 'blue') {
    c.save(); c.strokeStyle = 'rgba(232,239,255,.12)'; c.lineWidth = 1; c.beginPath();
    for (let x = Math.floor(a / 60) * 60; x < b; x += 60) { c.moveTo(x, -200); c.lineTo(x, 1400); }
    for (let y = -180; y < 1400; y += 60) { c.moveTo(a, y); c.lineTo(b, y); } c.stroke(); c.restore();
  }
  if (look === 'mixed') {
    for (let i = 0; i < 9; i++) { const cx = SEAM5 + 300 + i * 620, p = PAR(v, .5, cx, 200 + hash(i, 3) * 120);
      pLine(c, [[p[0] - 90, p[1]], [p[0] - 60, p[1] - 30], [p[0] - 10, p[1] - 44], [p[0] + 40, p[1] - 26], [p[0] + 90, p[1]]], {w: 1.6, seed: i + 300, col: RC.lead}); }
  }
}
function seamDecor(c, look, v) {
  const x0 = {riso: SEAM2, screen: SEAM3, blue: SEAM4, mixed: SEAM5}[look]; if (!x0) return;
  const pts = seamLine(x0, v);
  if (look === 'riso') [['#ff48b0', -5], ['#0078bf', 3], ['#ffe800', 9]].forEach(([col, dx]) => { c.strokeStyle = col; c.globalAlpha = .8; c.lineWidth = 7; c.beginPath(); pts.forEach(([x, y], i) => i ? c.lineTo(x + dx, y) : c.moveTo(x + dx, y)); c.stroke(); c.globalAlpha = 1; });
  if (look === 'screen') { c.fillStyle = '#0a2540'; c.beginPath(); pts.forEach(([x, y], i) => { const xx = x + (i % 2 ? 10 : 0); i ? c.lineTo(xx, y) : c.moveTo(xx, y); }); for (let i = pts.length - 1; i >= 0; i--) c.lineTo(pts[i][0] + 18, pts[i][1]); c.closePath(); c.fill(); }
  if (look === 'blue') { c.save(); c.setLineDash([10, 8]); c.strokeStyle = '#ffd76a'; c.lineWidth = 2.5; c.beginPath(); pts.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y)); c.stroke(); c.restore(); }
  if (look === 'mixed') { iLine(c, pts, {w: 4, al: .8, seed: 5, rough: 1}); }
  const label = {riso: 'Self-evolution', screen: 'Multi-agent orchestration', blue: 'No-restart upgrades', mixed: 'The Harness of Harnesses'}[look];
  writeOn(c, label, x0 + 26, 480, {size: 30, col: look === 'blue' ? '#ffd76a' : look === 'riso' ? '#0078bf' : RC.ink, rot: 0});
}


const castLook = look => look === 'blue' ? 'chalk' : look === 'pencil' ? 'pencil' : 'ink';
function tinted(sprite, color) {
  const key = color; sprite.tints ??= {}; if (sprite.tints[key]) return sprite.tints[key];
  const cv = document.createElement('canvas'); cv.width = sprite.cv.width; cv.height = sprite.cv.height; const g = cv.getContext('2d');
  g.drawImage(sprite.cv, 0, 0); g.globalCompositeOperation = 'source-atop'; g.fillStyle = color; g.fillRect(0, 0, cv.width, cv.height);
  return sprite.tints[key] = cv;
}

// ---------- self-evolution: five gates, three candidates ----------
const GATES = ['Find what failed', 'Design a fix', 'Quick screen', 'Full retest', 'Beat the baseline'];
const GATE_T = [45.4, 47.9, 50.4, 52.9, 55.4];
const gateX = i => flightX(GATE_T[i]) + 260;
function drawGates(c, t) {
  GATES.forEach((g, i) => { const x = gateX(i), y0 = topY(x), top = 330, done = t > GATE_T[i] + .5, py = y0 + 70;
    // An arch the flock flies through; its name hangs under the branch.
    c.save(); c.strokeStyle = '#22366b'; c.lineWidth = 5; c.setLineDash([2, 12]); c.lineCap = 'round'; c.beginPath(); c.moveTo(x, y0); c.lineTo(x, top); c.quadraticCurveTo(x + 75, top - 60, x + 150, top); c.lineTo(x + 150, y0); c.stroke(); c.restore();
    iLine(c, [[x + 75, y0 + 10], [x + 75, py]], {w: 2, col: '#22366b', seed: i + 80, rough: .2});
    c.save(); c.globalCompositeOperation = 'multiply'; c.fillStyle = done ? 'rgba(255,72,176,.75)' : 'rgba(255,232,0,.9)'; c.fillRect(x - 40, py, 254, 66); c.fillStyle = 'rgba(0,120,191,.3)'; c.fillRect(x - 34, py + 5, 254, 66); c.restore();
    writeOn(c, `${i + 1}`, x - 22, py + 46, {size: 38, col: '#22366b', rot: 0}); writeOn(c, g, x + 14, py + 43, {size: g.length > 5 ? 23 : 27, col: '#22366b', rot: 0}); });
}
const PROOFS = [{id: 'A', col: '#ff48b0', off: [330, -90], score: 66}, {id: 'B', col: '#0078bf', off: [300, 160], score: 81}, {id: 'C', col: '#e8c400', off: [-330, 110], score: 41}];
function drawProofs(c, t, st) {
  if (t < 48.0 || t > 57.2) return;
  PROOFS.forEach((p, i) => {
    const out = easeOut(clamp((t - 48.0 - i * .15) / .8, 0, 1)); let [ox, oy] = [p.off[0] * out, p.off[1] * out], al = .85, drop = 0;
    const falls = {C: 50.6, A: 55.8}[p.id];
    if (p.id === 'B') { const m = sm(55.5, 56.3, t); ox *= 1 - m; oy *= 1 - m; al *= 1 - sm(56.0, 56.4, t, lin); }
    else if (t > falls) { drop = t - falls; al *= 1 - sm(falls + .8, falls + 1.4, t, lin); }
    if (al <= 0) return;
    const x = st.x + ox - drop * 120, y = st.y + oy + drop * drop * 500, pose = flyPose(QT1(t) + i * .13, {young: 0});
    if (drop > 0) { c.save(); c.globalAlpha = al; c.translate(x, y - 100); c.rotate(drop * 5); c.fillStyle = p.col; c.fill(curvePath(blob(0, 0, 44, 36, i + 9, {amp: .35}), true)); iLine(c, [[-26, -8], [0, 8], [22, -12]], {w: 2, seed: i + 50, col: '#ffffff'}); c.restore(); return; }
    const s = rvSprite(pose, 'ink'), tcv = tinted(s, p.col);
    c.save(); c.globalAlpha = al; c.globalCompositeOperation = 'multiply'; c.translate(x, y); c.rotate(.24); c.scale(FLEDGE_S * .7, FLEDGE_S * .7); c.drawImage(tcv, s.box[0], s.box[1], s.box[2], s.box[3]); c.restore();
    const tag = rvWorld([150, -40], {x, y, scale: FLEDGE_S * .7, rot: .24}), shown = (p.id === 'C' && t > 50.5) || (p.id !== 'C' && t > 53.0);
    c.save(); c.globalAlpha = al; if (shown) { c.fillStyle = 'rgba(255,255,255,.9)'; c.beginPath(); c.roundRect(tag[0] - 12, tag[1] - 40, 190, 54, 14); c.fill(); } writeOn(c, p.id, tag[0], tag[1], {size: 40, col: p.col, rot: 0});
    if (shown) writeOn(c, `${p.score} pts${p.id === 'B' ? ' ✓' : ''}`, tag[0] + 44, tag[1], {size: 32, col: p.id === 'B' ? '#0078bf' : RC.ink, p: sm(p.id === 'C' ? 50.5 : 53.0, (p.id === 'C' ? 50.5 : 53.0) + .4, t, lin), rot: 0});
    c.restore();
  });
  const me = rvWorld([30, 110], {x: st.x, y: st.y, scale: FLEDGE_S, rot: .24});
  if (t > 53.0 && t < 56.6) writeOn(c, 'Current me: 70 pts', me[0], me[1], {size: 30, col: RC.ink, p: sm(53.0, 53.5, t, lin), al: 1 - sm(56.2, 56.6, t, lin), rot: 0});
  if (t > 56.0 && t < 58.4) { const tag = rvWorld([-10, -170], {x: st.x, y: st.y, scale: FLEDGE_S, rot: .24}), k = clamp(easeOutBack(clamp((t - 56.0) / .35, 0, 1)), 0, 1.3);
    c.save(); c.translate(tag[0], tag[1]); c.rotate(-.2); c.scale(k, k); c.strokeStyle = '#0078bf'; c.lineWidth = 4; c.globalAlpha = 1 - sm(57.9, 58.4, t, lin); c.strokeRect(-56, -26, 112, 52); writeOn(c, 'Kept', 0, 14, {size: 36, col: '#0078bf', align: 'center', rot: 0}); c.restore(); }
}
// Gate 1: the weak spot is circled before anything is changed.
function drawDiagnosis(c, t, G, place) {
  const k = sm(45.5, 45.9, t, lin) * (1 - sm(56.0, 56.5, t, lin)); if (k <= 0) return;
  const w = rvWorld(rv2.add(G.bc, [-G.brx * .4, -G.bry * .3]), place);
  c.save(); c.globalAlpha = k; c.strokeStyle = RC.red; c.lineWidth = 4; c.beginPath(); c.ellipse(w[0], w[1], 90, 60, .3, 0, TAU * sm(45.5, 45.9, t, lin)); c.stroke(); c.restore();
  writeOn(c, 'weak spot', w[0] - 250, w[1] + 150, {size: 30, col: RC.red, p: sm(45.8, 46.4, t, lin), al: k, rot: 0});
}
function drawLoopIcon(c, t, st) {
  const k = sm(57.0, 57.5, t, lin) * (1 - sm(59.4, 59.9, t, lin)); if (k <= 0) return;
  const cx = st.x + 330, cy = st.y - 60; c.save(); c.globalAlpha = k; c.strokeStyle = '#0078bf'; c.lineWidth = 5; c.beginPath(); c.arc(cx, cy, 60, -.4, TAU - 1.0); c.stroke();
  const a = TAU - 1.0, e = [cx + Math.cos(a) * 60, cy + Math.sin(a) * 60]; c.fillStyle = '#0078bf'; c.beginPath(); c.moveTo(e[0] + 14, e[1] - 4); c.lineTo(e[0] - 6, e[1] - 16); c.lineTo(e[0] - 4, e[1] + 10); c.fill();
  writeOn(c, 'Next round', cx, cy + 8, {size: 21, col: '#0078bf', align: 'center', rot: 0}); c.restore();
}

// ---------- the request that runs through the second half ----------
const TASKS = [['Research', 'Research'], ['Code', 'Code'], ['Monitor', 'Oncall'], ['Deck', 'Design']];
function drawRequest(c, t, st) {
  const s = t - ST.flock; if (s < .2 || s > 6.2) return;
  const k = easeOut(clamp((s - .2) / .7, 0, 1)), al = 1 - sm(5.6, 6.2, s, lin);
  const x = st.x + 330, y = st.y - 290 + (1 - k) * -260;
  c.save(); c.globalAlpha = al; c.translate(x, y); c.rotate(-.04 + (1 - k) * .4);
  c.fillStyle = RC.white; c.fillRect(-20, -20, 560, 250); c.strokeStyle = '#0a2540'; c.lineWidth = 2.5; c.strokeRect(-20, -20, 560, 250);
  writeOn(c, 'Compare three rivals, scrape their data,', 0, 26, {size: 28, col: '#0a2540', p: sm(.9, 1.6, s, lin), rot: 0});
  writeOn(c, 'monitor it overnight, then make a deck.', 0, 64, {size: 28, col: '#0a2540', p: sm(1.6, 2.2, s, lin), rot: 0});
  TASKS.forEach(([label], i) => { const p = sm(2.4 + i * .35, 2.7 + i * .35, s, lin); if (p <= 0) return; const bx = i * 132, by = 120;
    c.globalAlpha = al * p; c.fillStyle = ['#2f6fd0', '#1f9e8f', '#e0a030', '#e0605a'][i]; c.fillRect(bx, by, 112, 64); writeOn(c, label, bx + 56, by + 42, {size: label.length > 3 ? 20 : 24, col: '#ffffff', align: 'center', rot: 0});
    if (i < 3) { c.strokeStyle = '#0a2540'; c.beginPath(); c.moveTo(bx + 114, by + 32); c.lineTo(bx + 130, by + 32); c.stroke(); } });
  c.restore();
}
function drawCodePage(c, t, st, mates) {
  const s = t - ST.code, o = mates.find(q => q.m.name === 'Code'); if (!o || s < .6 || s > 4.0) return;
  const k = clamp(easeOutBack(clamp((s - .6) / .4, 0, 1)), 0, 1.2), al = 1 - sm(3.4, 4.0, s, lin), x = o.s.x + 120, y = o.s.y - 120;
  c.save(); c.globalAlpha = al; c.translate(x, y); c.rotate(.08); c.scale(k, k); c.fillStyle = '#10261f'; c.fillRect(0, 0, 220, 150); c.strokeStyle = '#0a2540'; c.lineWidth = 2; c.strokeRect(0, 0, 220, 150);
  ['#8fe3c8', '#f6fbf4', '#f0c24a', '#f6fbf4', '#8fe3c8'].forEach((col, i) => { c.fillStyle = col; c.fillRect(16 + (i % 2) * 18, 18 + i * 22, 110 + hash(i, 4) * 70, 8); }); c.restore();
  if (s > 1.4) { const p = clamp(easeOutBack(clamp((s - 1.4) / .35, 0, 1)), 0, 1.3); c.save(); c.globalAlpha = al; c.translate(x + 170, y + 150); c.rotate(-.15); c.scale(p, p); c.strokeStyle = '#1f9e8f'; c.lineWidth = 4; c.fillStyle = RC.white;
    c.fillRect(-120, -26, 240, 52); c.strokeRect(-120, -26, 240, 52); writeOn(c, '12 tests passed ✓', 0, 12, {size: 28, col: '#1f9e8f', align: 'center', rot: 0}); c.restore(); }
}
function drawNight(c, t, v) {
  const k = nightK(t), dawn = sm(ST.oncall + 2.8, ST.oncall + 3.3, t) * (1 - sm(ST.oncall + 3.3, ST.oncall + 4.2, t)); if (k <= 0 && dawn <= 0) return;
  c.save(); resetT(c);
  if (k > 0) { c.fillStyle = `rgba(14,22,48,${.78 * k})`; c.fillRect(0, 0, W, H); c.fillStyle = `rgba(255,255,255,${.8 * k})`; for (let i = 0; i < 60; i++) c.fillRect(hash(i, 3) * W, hash(i, 4) * H * .6, 2.4, 2.4);
    c.fillStyle = `rgba(250,240,200,${k})`; c.beginPath(); c.arc(W * .72, 330, 52, 0, TAU); c.fill(); c.fillStyle = `rgba(14,22,48,${.78 * k})`; c.beginPath(); c.arc(W * .72 + 22, 316, 46, 0, TAU); c.fill(); }
  if (dawn > 0) { const g = c.createLinearGradient(0, H, 0, 0); g.addColorStop(0, `rgba(255,170,90,${.35 * dawn})`); g.addColorStop(1, 'rgba(255,170,90,0)'); c.fillStyle = g; c.fillRect(0, 0, W, H); }
  c.restore(); cam(c, v.x, v.y, v.z);
}
function drawSlides(c, t, st, mates) {
  const s = t - ST.design, o = mates.find(q => q.m.name === 'Design'); if (!o || s < 1.8 || s > 5.2) return;
  const al = 1 - sm(4.6, 5.2, s, lin);
  [0, 1, 2].forEach(i => { const k = easeOut(clamp((s - 1.8 - i * .3) / .5, 0, 1)); if (k <= 0) return;
    const x = o.s.x + 120 + i * 150 * k, y = o.s.y - 170 + i * 20; c.save(); c.globalAlpha = al; c.translate(x, y); c.rotate(-.12 + i * .1);
    c.fillStyle = RC.white; c.fillRect(0, 0, 200, 124); c.strokeStyle = '#7a2e3a'; c.lineWidth = 2.5; c.strokeRect(0, 0, 200, 124); c.fillStyle = '#e0605a'; c.fillRect(14, 14, 110, 12);
    if (i === 0) { c.fillStyle = '#2f6fd0'; [40, 64, 30].forEach((h, j) => c.fillRect(24 + j * 34, 110 - h, 22, h)); }
    if (i === 1) { c.fillStyle = '#e0a030'; c.beginPath(); c.moveTo(70, 72); c.arc(70, 72, 34, -1.2, 2.6); c.fill(); }
    if (i === 2) { c.fillStyle = '#1f9e8f'; for (let j = 0; j < 3; j++) c.fillRect(20, 40 + j * 22, 150 - j * 30, 10); }
    c.restore(); });
  if (s > 3.2) { const p = clamp(easeOutBack(clamp((s - 3.2) / .35, 0, 1)), 0, 1.3); c.save(); c.globalAlpha = al; c.translate(o.s.x + 330, o.s.y + 10); c.rotate(-.12); c.scale(p, p);
    c.strokeStyle = '#e0605a'; c.lineWidth = 4; c.strokeRect(-100, -26, 200, 52); writeOn(c, 'All done ✓', 0, 12, {size: 30, col: '#e0605a', align: 'center', rot: 0}); c.restore(); }
}
function drawPlaybook(c, t, st) {
  const s = t - ST.book; if (s < .2 || s > 5.2) return;
  const k = clamp(easeOutBack(clamp((s - .2) / .5, 0, 1)), 0, 1.2), al = 1 - sm(4.6, 5.2, s, lin), x = st.x + 250, y = st.y - 290;
  c.save(); c.globalAlpha = al; c.translate(x, y); c.rotate(-.05); c.scale(k, k);
  c.fillStyle = '#8a4b2a'; c.fillRect(-8, -8, 476, 236); c.fillStyle = RC.white; c.fillRect(0, 0, 226, 220); c.fillRect(234, 0, 226, 220);
  writeOn(c, 'Playbook', 20, 40, {size: 30, col: '#8a4b2a', rot: 0}); writeOn(c, 'Competitor scan', 20, 78, {size: 26, col: RC.ink, rot: 0});
  TASKS.forEach(([label], i) => { c.fillStyle = ['#2f6fd0', '#1f9e8f', '#e0a030', '#e0605a'][i]; const bx = 250 + (i % 2) * 104, by = 24 + Math.floor(i / 2) * 96; c.fillRect(bx, by, 90, 56); writeOn(c, label, bx + 45, by + 36, {size: label.length > 3 ? 16 : 20, col: '#ffffff', align: 'center', rot: 0}); });
  if (s > 1.8) { const p = clamp(easeOutBack(clamp((s - 1.8) / .35, 0, 1)), 0, 1.3); c.save(); c.translate(113, 160); c.scale(p, p); c.fillStyle = RC.red; c.beginPath(); c.roundRect(-86, -26, 172, 52, 26); c.fill(); writeOn(c, 'Run again ↻', 0, 10, {size: 26, col: '#ffffff', align: 'center', rot: 0}); c.restore(); }
  c.restore();
}
function drawRemind(c, t, st) {
  const s = t - ST.remind; if (s < .2 || s > 4.2) return;
  const k = clamp(easeOutBack(clamp((s - .2) / .4, 0, 1)), 0, 1.2), al = 1 - sm(3.6, 4.2, s, lin), ring = s < 2 ? Math.sin(s * 60) * .12 : 0;
  c.save(); c.globalAlpha = al; c.translate(st.x + 300, st.y - 200); c.rotate(ring); c.scale(k, k);
  c.fillStyle = RC.red; c.beginPath(); c.arc(0, 0, 58, 0, TAU); c.fill(); c.fillStyle = RC.white; c.beginPath(); c.arc(0, 0, 46, 0, TAU); c.fill();
  c.strokeStyle = RC.ink; c.lineWidth = 5; c.beginPath(); c.moveTo(0, 0); c.lineTo(0, -30); c.moveTo(0, 0); c.lineTo(22, 8); c.stroke();
  c.fillStyle = RC.red; [-1, 1].forEach(d => { c.beginPath(); c.arc(d * 44, -48, 18, 0, TAU); c.fill(); }); c.restore();
  [['Slack', '#4a154b'], ['Telegram', '#2aa3df'], ['Web', '#0a2540']].forEach(([n, col], i) => { const x = st.x + 420 + i * 230, y0 = topY(x), p = sm(.8 + i * .3, 1.2 + i * .3, s, lin); if (p <= 0) return;
    c.save(); c.globalAlpha = al * p; iLine(c, [[x, y0], [x, y0 - 150]], {w: 4, col: RC.ink, seed: i + 5, rough: .3}); c.fillStyle = col; c.beginPath(); c.roundRect(x - 70, y0 - 214, 140, 64, 14); c.fill();
    writeOn(c, n, x, y0 - 170, {size: 26, col: '#ffffff', align: 'center', rot: 0}); c.restore(); });
}
function drawRoutes(c, look, t, st, mates) {
  if (t > ST.finale) return;
  const col = look === 'blue' ? '#ffd76a' : '#0a2540', pos = n => { const o = mates.find(q => q.m.name === n); return o ? [o.s.x - 40, o.s.y - 70] : null; };
  const me = [st.x - 20, st.y - 90], F = ST.flock;
  const edges = [[me, pos('Research'), F + 3.6], [pos('Research'), pos('Code'), F + 4.1], [pos('Code'), pos('Oncall'), F + 4.6], [pos('Oncall'), pos('Design'), ST.design + 1.0]];
  for (const [a, b, t0] of edges) { if (!a || !b) continue; const p = sm(t0, t0 + .6, t, lin); if (p <= 0) continue;
    const pts = [a, [(a[0] + b[0]) / 2, Math.min(a[1], b[1]) - 60], b]; c.save(); c.setLineDash([10, 9]); c.strokeStyle = col; c.lineWidth = 2.4; c.globalAlpha = look === 'mixed' ? .45 : .8;
    const pp = partial(smoothPts(pts, false, 6, 3), p); c.beginPath(); pp.forEach((q, i) => i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])); c.stroke(); c.restore(); }
  // Each built-in agent wears the task it was given.
  TASKS.forEach(([label, n], i) => { const p = pos(n); if (!p) return; const k = sm(F + 3.8 + i * .3, F + 4.3 + i * .3, t, lin) * (1 - sm(ST.harness - .5, ST.harness, t, lin)); if (k <= 0) return;
    c.save(); c.globalAlpha = k; c.font = `600 24px ${RV_FONT}`; const w = c.measureText(label).width + 26, bx = p[0] + 30, by = p[1] + 40;
    c.fillStyle = look === 'blue' ? '#23396c' : ['#2f6fd0', '#1f9e8f', '#c08a18', '#d0524c'][i]; c.beginPath(); c.roundRect(bx - w / 2, by - 24, w, 36, 18); c.fill();
    writeOn(c, label, bx, by + 2, {size: 24, col: '#ffffff', align: 'center', rot: 0}); c.restore(); });
  if (look !== 'screen') return;
  // Invited agents speak the shared protocol; finished work gets a tick.
  mates.filter(q => q.m.guest).forEach(({s}, i) => {
    const t0 = ST.third + 3.2 + i * .35, k = sm(t0, t0 + .25, t, lin) * (1 - sm(t0 + 1.6, t0 + 1.9, t, lin));
    if (k > 0 && i % 2 === 0) { c.save(); c.globalAlpha = k; c.fillStyle = '#ffffff'; c.strokeStyle = '#0a2540'; c.lineWidth = 2.5; const bx = s.x + 10, by = s.y - 170; c.beginPath(); c.roundRect(bx - 60, by - 30, 120, 52, 16); c.fill(); c.stroke(); writeOn(c, 'ACP ~', bx, by + 6, {size: 24, col: '#0a2540', align: 'center', rot: 0}); c.restore(); }
    const t1 = ST.third + 5.6 + i * .3, p = clamp(easeOutBack(clamp((t - t1) / .3, 0, 1)), 0, 1.3); if (p <= 0 || t > ST.blue) return;
    c.save(); c.translate(s.x + 60, s.y - 120); c.scale(p, p); c.fillStyle = '#f08a24'; c.beginPath(); c.arc(0, 0, 22, 0, TAU); c.fill(); c.strokeStyle = '#ffffff'; c.lineWidth = 5; c.beginPath(); c.moveTo(-9, 1); c.lineTo(-2, 8); c.lineTo(11, -8); c.stroke(); c.restore();
  });
}
function drawSkeleton(c, t, G, place) {
  const b0 = ST.blue; if (t < b0 + 1 || t > b0 + 5) return;
  const P = q => rvWorld(q, place), spine = [G.hc, G.bc, rv2.add(G.bc, [-G.brx * .9, G.bry * .3])].map(P), ribs = [-.5, -.1, .3].map(k => [rv2.add(G.bc, [k * G.brx, -G.bry * .5]), rv2.add(G.bc, [k * G.brx + 10, G.bry * .55])].map(P));
  const bones = (dx, dy, col, dash, al) => { c.save(); c.globalAlpha = al; c.strokeStyle = col; c.lineWidth = 3; c.setLineDash(dash); c.translate(dx, dy);
    c.beginPath(); spine.forEach((q, i) => i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])); c.stroke(); ribs.forEach(r => { c.beginPath(); c.moveTo(...r[0]); c.lineTo(...r[1]); c.stroke(); }); c.restore(); };
  const inN = sm(b0 + 1, b0 + 1.5, t, lin), leave = sm(b0 + 3, b0 + 4.2, t), newIn = sm(b0 + 2, b0 + 3, t);
  bones(-leave * 320, leave * leave * 380, '#e8efff', [8, 7], inN * (1 - sm(b0 + 3.8, b0 + 4.3, t, lin)));
  bones((1 - newIn) * 420, -(1 - newIn) * 160, '#ffd76a', [], newIn * (1 - sm(b0 + 4.6, b0 + 5, t, lin)));
  const lab = P([G.bc[0], G.bc[1] + G.bry + 60]);
  writeOn(c, 'old skeleton', lab[0] - 70 - leave * 320, lab[1] + leave * leave * 380, {size: 30, col: '#e8efff', al: inN * (1 - sm(b0 + 3.8, b0 + 4.3, t, lin)), rot: 0});
  writeOn(c, 'new skeleton', lab[0] + 10 + (1 - newIn) * 420, lab[1] - (1 - newIn) * 160, {size: 30, col: '#ffd76a', al: newIn * (1 - sm(b0 + 4.6, b0 + 5, t, lin)), rot: 0});
}
function drawShower(c, t, v) {
  const s = t - (ST.blue + 4.4); if (s < 0 || s > 1.6) return;
  const [a, b] = viewOf(v.x, v.z);
  for (let i = 0; i < 137; i++) { const x = a + hash(i, 7) * (b - a), d = .15 + hash(i, 8) * .5, u = clamp((s - hash(i, 9) * .6) / d, 0, 1); if (u <= 0) continue;
    const y = lerp(v.y - H / 2 / v.z - 40, branchY(x) + 30 + hash(i, 10) * 40, u * u); c.fillStyle = '#ffd76a'; c.beginPath(); c.arc(x, y, 6, 0, TAU); c.fill(); }
  writeOn(c, '+137', v.x + 200, v.y - 200, {size: 90, col: '#ffd76a', p: sm(0, .3, s, lin), al: 1 - sm(1.2, 1.6, s, lin), rot: -.05});
}
const TOOL_OF = {Research: 'glass', Code: 'key', Oncall: 'hammer', Design: 'brush'};
function drawReins(c, t, st, place, mates) {
  const me = rvWorld([90, -140], place), H0 = ST.harness;
  mates.forEach(({m, s}, i) => {
    const target = [s.x + 40, s.y - 100], p = sm(H0 + .2 + i * .15, H0 + .8 + i * .15, t, lin); if (p <= 0) return;
    const pts = smoothPts([me, [(me[0] + target[0]) / 2, Math.max(me[1], target[1]) + 60], target], false, 6, 3);
    c.save(); c.strokeStyle = '#8a4b2a'; c.lineWidth = m.guest ? 2 : 3; c.globalAlpha = m.guest ? .7 : 1; c.beginPath(); partial(pts, p).forEach((q, j) => j ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])); c.stroke(); c.restore();
    if (m.guest) return;
    // Each bird's own harness: a strap round the chest and a line to its tool.
    const k = sm(H0 + 1.8 + i * .25, H0 + 2.4 + i * .25, t, lin); if (k <= 0) return;
    c.save(); c.globalAlpha = k; c.strokeStyle = '#8a4b2a'; c.lineWidth = 3; c.beginPath(); c.ellipse(s.x + 10, s.y - 60, 46, 30, .24, 0, TAU); c.stroke();
    const tool = [s.x - 150, s.y + 40 + 10 * Math.sin(t * 3 + i)]; c.beginPath(); c.moveTo(s.x - 30, s.y - 40); c.quadraticCurveTo(s.x - 100, s.y + 30, tool[0] + 20, tool[1]); c.stroke();
    c.fillStyle = RC.white; c.beginPath(); c.arc(tool[0], tool[1], 24, 0, TAU); c.fill(); c.stroke(); toolIcon(c, TOOL_OF[m.name], tool[0], tool[1], 1); c.restore();
  });
}

// ---------- characters in a given material ----------
function drawCast(c, look, t, st) {
  const cl = castLook(look), place = {x: st.x, y: st.y, scale: FLEDGE_S, rot: st.rot};
  if (cl === 'pencil') return;
  const mates = [...MATES, ...GUESTS].map(m => ({m, s: mateState(m, t, st.x, st.y)})).filter(o => o.s);
  if (look === 'riso') { drawGates(c, t); drawProofs(c, t, st); }
  if (look === 'mixed' && t > ST.harness) { c.save(); c.globalAlpha = 1 - sm(ST.finale + .2, ST.finale + 1.0, t, lin); drawReins(c, t, st, place, mates); c.restore(); }
  for (const {m, s} of mates) {
    const mp = {x: s.x, y: s.y, scale: FLEDGE_S * (m.guest ? .55 : .72), rot: s.rot};
    const pose = m.guest ? {...s.pose, body: undefined, gx: .6} : cl === 'chalk' ? {...s.pose, body: undefined} : s.pose, lk = m.guest && cl !== 'chalk' ? 'pencil' : cl;
    const G = drawRaven(c, pose, {...mp, look: lk}); drawAccessory(c, m, G, mp, look, t);
  }
  const G = drawRaven(c, st.pose, {...place, look: cl}), beak = rvWorld(G.anchors.beak, place), foot = rvWorld(G.legs[0].foot, place);
  if (t > ST.mem + .12 && t < ST.fly + .6) { const back = rvWorld(G.anchors.back, place), k = clamp(easeOutBack(clamp((t - ST.mem - .12) / .3, 0, 1)), 0, 1.2);
    c.save(); c.globalAlpha = 1 - sm(ST.fly, ST.fly + .6, t, lin); drawPack(c, 'ink', back[0] - 8, back[1] + 6, place.rot - .18, 1.1 * k); c.restore(); }
  if (t > ST.tools + 1.35 && t < ST.evo + .3) drawMagnifier(c, beak[0] - 10, beak[1] + 4, place.rot + .15, .9);
  if (t >= ST.evo + .3 && t < 58.5) { c.save(); c.globalAlpha = 1 - sm(58.0, 58.5, t, lin); drawPencilProp(c, beak[0] - 34, beak[1] + 2, place.rot + .1, .8); c.restore(); }
  if (t >= GRAB && t < ST.evo + .3) drawScroll(c, foot[0], foot[1] + 4, .15, .9);
  if (t >= ST.evo + .3 && t < ST.evo + 1.8) { const u = t - ST.evo - .3; drawScroll(c, foot[0] - u * 260, foot[1] + 4 + u * u * 700, .15 + u * 3, .9); }
  if (look === 'riso') { drawDiagnosis(c, t, G, place); drawLoopIcon(c, t, st); }
  if (cl === 'chalk') drawSkeleton(c, t, G, place);
  if (look === 'screen' || look === 'mixed' || look === 'blue') drawRoutes(c, look, t, st, mates);
  if (look === 'screen') { drawRequest(c, t, st); drawCodePage(c, t, st, mates); }
  if (look === 'mixed') { drawSlides(c, t, st, mates); drawPlaybook(c, t, st); drawRemind(c, t, st); }
  return G;
}

// ---------- notes pinned to the page ----------
function pageNotes(c, look, t) {
  const col = look === 'blue' ? '#e8efff' : look === 'riso' ? '#0078bf' : look === 'screen' ? '#0a2540' : RC.ink;
  const N = [
    ['ink', ['#71 · EverOS long-term memory'], [3560, 985], ST.mem + .2], ['ink', ['#93 · SkillForge, skills on demand'], [4010, 985], ST.tools + .3],
    ['ink', ['#136 · deep research, with citations'], [6800, 985], GRAB - .3],
    ['riso', ['#140 · Evolver · AppWorld benchmark'], [8300, 1050], ST.evo + 1.0], ['riso', ['#170 · import memory from other AI tools'], [14300, 985], ST.july + .4],
    ['screen', ['subagent DAG · task graph'], [16100, 985], ST.flock + 3.4], ['screen', ['raven-code'], [17700, 985], ST.code + .6], ['screen', ['raven-oncall'], [19300, 985], ST.oncall + .6],
    ['screen', ['13 third-party agent presets · ACP / CLI / API'], [20900, 985], ST.third + 1.0],
    ['blue', ['generation model · hot swap between turns'], [24800, 985], ST.blue + 1.5], ['blue', ['Aug 30 · 137 commits'], [25900, 985], ST.blue + 4.4],
    ['mixed', ['raven-design'], [27600, 985], ST.design + .8], ['mixed', ['Playbook'], [29400, 985], ST.book + .6], ['mixed', ['reminders · chat channels'], [31200, 985], ST.remind + .6],
    ['mixed', ['#558 generated harness'], [32700, 985], ST.harness + .6],
  ];
  for (const [lk, lines, at, t0] of N) if (lk === look) note(c, 'ink', lines, at, null, sm(t0, t0 + .8, t, lin), {col});
}

// ---------- one region of the page ----------
function drawRegion(c, look, t, v, st) {
  if (look === 'pencil') return drawPage(c, 'pencil', t, v, {visible: false, x: 0, y: 0, rot: 0, dx: 0, inShell: false, air: 0, pose: rvPose('adult')});
  if (look === 'blue') { resetT(c); c.fillStyle = '#1c2f5e'; c.fillRect(0, 0, W, H); grain(c, rectPath(0, 0, W, H), [0, 0, W, H], 600, '#ffffff', .05, 7, 1.4); }
  else paper(c, look === 'riso' ? '#f3ede1' : RC.paper, null, 87);
  cam(c, v.x, v.y, v.z);
  // What Act I never drew fades in after the hand-over instead of popping.
  const fresh = look === 'ink' ? sm(T.end, T.end + .8, t, lin) : 1;
  c.save(); c.globalAlpha = fresh; scenery(c, look, v, t); c.restore();
  if (look === 'ink') {
    drawBranch(c, 'ink', viewOf(v.x, v.z), {drawnTo: Math.max(3580, routeX(t) + 650)}); INK_TWIGS.forEach(tw => drawTwig(c, 'ink', tw)); drawSeam(c, 'ink', v);
    drawTopShell(c, 'ink', 3240, topY(3240) - 21, Math.PI + .25, .92); drawSplash(c, Math.max(t, T.ink + 2));
    note(c, 'ink', ['v0.1.0 public preview · #36', 'one-line install · #8'], [3140, 915], null, 1);
    drawToolRoll(c, t, routeX(t));
    if (t > ST.mem + .45 && t < ST.mem + 1.5) { const s = t - ST.mem, x = 3790 - 40, y = topY(3790) - 150, bounce = s > .82 ? (s - .82) : 0;
      c.save(); c.translate(x + 200 * (1 - sm(.45, .8, s)) + bounce * 500, y - 200 * (1 - sm(.45, .8, s)) - bounce * 600 + bounce * bounce * 200); c.rotate(-.4 + bounce * 6); c.scale(.72, .72); drawEraser(c, 0, 0, 0); c.restore();
      if (s > .82 && s < 1.1) writeOn(c, 'Boing!', x + 60, y - 90, {size: 44, col: RC.red, rot: -.1}); }
    if (t < GRAB) drawScroll(c, SCROLL_X - 30, 548, .1, .9);
  } else flatBranch(c, look, v, t);
  seamDecor(c, look, v);
  c.save(); c.globalAlpha = fresh; dayMarks(c, look, v, t); beadsOnBranch(c, look, v, t); c.restore();
  pageNotes(c, look, t);
  if (look === 'riso') julyCards(c, t, v);
  if (look === 'screen') drawNight(c, t, v);
  if (look === 'blue') drawShower(c, t, v);
  drawCast(c, look, t, st);
}
function julyCards(c, t, v) {
  [[14450, 'Jul 21', 'Model per task'], [14950, 'Jul 22', 'Import memory']].forEach(([x, d, txt], i) => { const y0 = topY(x); iLine(c, [[x, y0], [x, y0 - 230]], {w: 4, col: '#0078bf', seed: i + 60, rough: .3});
    c.save(); c.translate(x, y0 - 300); c.rotate((hash(i, 3) - .5) * .1); c.globalCompositeOperation = 'multiply';
    c.fillStyle = 'rgba(255,72,176,.55)'; c.fillRect(-116, -66, 232, 132); c.fillStyle = 'rgba(0,120,191,.35)'; c.fillRect(-110, -62, 232, 132); c.globalCompositeOperation = 'source-over';
    writeOn(c, d, -96, -24, {size: 28, col: '#0078bf', rot: 0}); writeOn(c, txt, -96, 22, {size: 32, col: RC.ink, rot: 0}); c.restore(); });
}

// ---------- the feature card, top right ----------
const CARDS = [
  [2.3, 'Raven', 'the agent that runs your agents'], [T29, 'Safety', 'untrusted input gets flagged'], [T30, 'Long-term memory', 'first try, rolled back'], [TCROSS, 'Open source', 'installs with one command'],
  [ST.mem + .2, 'Long-term memory', 'remembers you and your work'], [ST.tools, 'Skills on demand', '110,000+ skills, loaded when needed'], [ST.fly, 'Deep research', 'Raven-Research, answers with sources'],
  [ST.evo, 'Self-evolution', 'every version is scored; only winners stay'], [ST.july, 'Import memory', 'bring your memory along'], [ST.flock, 'Multi-agent orchestration', 'one request becomes a task graph'],
  [ST.code, 'Raven-Code', 'turns requests into tested, working code'], [ST.oncall, 'Raven-Oncall', 'takes the night shift, reports at dawn'], [ST.third, 'Third-party agents', 'Claude Code, Codex, Kimi Code...'],
  [ST.blue, 'No-restart upgrades', 'change settings on the fly'], [ST.design, 'Raven-Design', 'decks, charts, brand assets'], [ST.book, 'Playbook', 'save a flow, rerun it anytime'],
  [ST.remind, 'Reminders', 'nudges you on time, even in Slack'], [ST.harness, 'The Harness of Harnesses', 'one agent to run them all'],
];
function dateLine(t) {
  if (t < T.end) { const id = t < T29 ? '06-28' : t < T30 ? '06-29' : '06-30'; return `${DAY[id].label} · ${DAY[id].count}`; }
  let k = 2; for (let i = 0; i < DAYS2.length; i++) if (dayCross[i] <= t) k = i;
  return `${dayLabel(DAYS2[k].s)} · ${DAYS2[k].total.toLocaleString('en-US')} commits so far`;
}
function featureCard(c, look, t) {
  if (t < CARDS[0][0]) return;
  const hide = 1 - sm(ST.finale + .2, ST.finale + .8, t, lin); if (hide <= 0) return;
  let k = 0; for (let i = 0; i < CARDS.length; i++) if (CARDS[i][0] <= t) k = i;
  const [t0, title, sub] = CARDS[k], prev = k > 0 ? CARDS[k - 1] : null, gone = sm(t0, t0 + .15, t, lin), p = sm(t0 + .1, t0 + .6, t, lin);
  const col = look === 'blue' || nightK(t) > .5 ? '#e8efff' : look === 'ink' ? RC.ink : RC.pencil, lead = look === 'blue' || nightK(t) > .5 ? '#c9d6f5' : RC.lead;
  resetT(c); c.save(); c.globalAlpha = hide;
  if (prev && gone < 1) { writeOn(c, prev[1], W - 118, 128, {size: 58, col, al: 1 - gone, align: 'right'}); writeOn(c, prev[2], W - 118, 176, {size: 30, col, al: 1 - gone, align: 'right'}); }
  writeOn(c, title, W - 118, 128, {size: title.length > 12 ? 46 : 58, col, p, align: 'right'});
  writeOn(c, sub, W - 118, 176, {size: 30, col, p: sm(t0 + .35, t0 + .9, t, lin), align: 'right'});
  writeOn(c, dateLine(t), W - 118, 222, {size: 23, col: lead, align: 'right', rot: 0});
  c.strokeStyle = RC.gold; c.lineWidth = 1.5; c.beginPath(); c.moveTo(W - 430, 236); c.lineTo(W - 116, 238); c.stroke(); c.restore();
}

// ---------- captions after the inking ----------
function overlays2(c, t) {
  const light = (t > ST.blue && t < ST.design) || nightK(t) > .5, col = light ? '#e8efff' : RC.ink;
  const cap = (lines, fade) => { resetT(c); const al = 1 - sm(fade[0], fade[1], t, lin); let y = 150;
    for (const [text, t0, t1, size = 50] of lines) { writeOn(c, text, 118, y, {size, col, p: sm(t0, t1, t, lin), al}); y += size * 1.5; } };
  cap([['That same day, they opened the nest.', -2, -1, 52], ['Now I\'m in ink. No erasing me.', -2, -1, 48]], [T.end + .3, T.end + .7]);
  cap([['Memory\'s back. This time it sticks.', ST.mem + .3, ST.mem + 1.1, 52]], [ST.hopB + .5, ST.hopB + .8]);
  cap([["I don't lug every tool around,", ST.tools + .1, ST.tools + .7, 52], ['just the one the job needs.', ST.tools + .8, ST.tools + 1.4, 48]], [ST.fly + .6, ST.fly + .9]);
  cap([['I\'ll fly a long way for an answer,', 39.3, 40.1, 52], ['and bring back the sources.', 40.2, 40.9, 48]], [ST.evo + .2, ST.evo + .6]);
  cap([['I can swap out my own feathers.', ST.evo + .6, ST.evo + 1.4, 52], ['Every new version must clear five gates,', ST.evo + 1.6, ST.evo + 2.5, 48], ['and only stays if it beats me.', ST.evo + 2.7, ST.evo + 3.6, 48]], [ST.evo + 12.6, ST.evo + 13.0]);
  cap([['Round by round, I get better.', ST.evo + 13.2, ST.evo + 14.2, 52]], [ST.july - .3, ST.july]);
  cap([['Coming from another AI tool?', ST.july + .2, ST.july + 1.1, 48], ['Bring your memories with you.', ST.july + 1.2, ST.july + 1.7, 48]], [ST.flock - .3, ST.flock]);
  cap([['You just say what you need.', ST.flock + .6, ST.flock + 1.4, 52], ['I break it down for the right birds.', ST.flock + 3.4, ST.flock + 4.8, 48]], [ST.code - .3, ST.code]);
  cap([['Coding goes to this one,', ST.code + .3, ST.code + 1.0, 52], ['and it\'s tested before it ships.', ST.code + 1.2, ST.code + 1.8, 48]], [ST.oncall - .3, ST.oncall]);
  cap([['Need someone to watch it all night?', ST.oncall + .3, ST.oncall + 1.1, 52], ['This one\'s on it. Go get some sleep.', ST.oncall + 1.3, ST.oncall + 1.9, 48]], [ST.third - .3, ST.third]);
  cap([['Bring in other agents, too:', ST.third + .5, ST.third + 1.4, 50], ['Claude Code, Codex, Kimi Code...', ST.third + 1.6, ST.third + 2.8, 46], ['all in the same task graph.', ST.third + 3.4, ST.third + 4.4, 46]], [ST.blue - .3, ST.blue]);
  cap([['Our busiest day: 137 commits.', ST.blue + .5, ST.blue + 1.4, 50], ['I swapped my skeleton mid-flight,', ST.blue + 1.8, ST.blue + 3.0, 46], ['so settings change with no restart.', ST.blue + 3.4, ST.blue + 4.4, 46]], [ST.design - .3, ST.design]);
  cap([['And for the finale:', ST.design + .5, ST.design + 1.2, 52], ['a polished deck. Job done.', ST.design + 1.6, ST.design + 2.8, 48]], [ST.book - .3, ST.book]);
  cap([['A flow that works becomes a Playbook.', ST.book + .4, ST.book + 1.5, 50], ['Next time, just say the word.', ST.book + 1.8, ST.book + 2.8, 48]], [ST.remind - .3, ST.remind]);
  cap([["I'll nudge you when it's time,", ST.remind + .3, ST.remind + 1.1, 52], ['and you can find me in Slack.', ST.remind + 1.3, ST.remind + 2.2, 48]], [ST.harness - .3, ST.harness]);
  cap([['My own flock or someone else\'s,', ST.harness + .3, ST.harness + 1.3, 48], ['I hold all the reins.', ST.harness + 1.5, ST.harness + 2.2, 48]], [ST.harness + 3.0, ST.harness + 3.3]);
  cap([['That\'s why they call me', ST.harness + 3.4, ST.harness + 3.9, 48], ['The Harness of Harnesses', ST.harness + 4.0, ST.harness + 5.0, 56]], [ST.finale - .2, ST.finale + .2]);
}
// The ending, in three parts on one clock:
//   the whole route in miniature -> Raven signs "Made By Raven" -> the name card with the repo link.
const END = {recapOut: ST.finale + 1.8, made: ST.finale + 2.0, card: ST.finale + 8.4};
const REPO = 'https://github.com/evermind-ai/raven';
function routeRecap(c, t) {
  const k = sm(ST.finale + .3, ST.finale + 1.5, t, lin), out = sm(END.recapOut, END.recapOut + .3, t, lin); if (k <= 0 || out >= 1) return;
  resetT(c); const x0 = 120, x1 = W - 120, y = H - 150, xAt = i => lerp(x0, x1, i / 88);
  c.save(); c.globalAlpha = 1 - out; c.fillStyle = 'rgba(244,239,228,.92)'; c.fillRect(0, y - 150, W, 260);
  const bands = [['#8b8278', 0, 2], [RC.ink, 2, 19], ['#ff48b0', 19, 43], ['#2c6fa3', 43, 62], ['#1c2f5e', 62, 68], ['#d6a23a', 68, 88]];
  bands.forEach(([col, a, b]) => { const xa = xAt(a), xb = Math.min(xAt(b), lerp(x0, x1, k)); if (xb > xa) { c.fillStyle = col; c.fillRect(xa, y - 5, xb - xa, 10); } });
  for (const d of DAYS2) { const x = xAt(d.i); if (x > lerp(x0, x1, k)) continue; for (let j = 0; j < d.n; j++) { const yy = y + 12 + (j % 34) * 3.2, xx = x + Math.floor(j / 34) * 4; c.fillStyle = '#d6a23a'; c.fillRect(xx - 1.4, yy, 2.8, 2.4); } }
  [['06-28', 'Jun 28'], ['07-17', 'Self-evolution'], ['08-10', 'Multi-agent'], ['08-30', '137'], ['09-24', 'Sep 24 · 1,686 commits']].forEach(([s, lab], i) => { const x = xAt(dayIndex(s)); if (x <= lerp(x0, x1, k)) writeOn(c, lab, x, y - 22, {size: 26, col: RC.ink, align: i === 4 ? 'right' : 'center', rot: 0}); });
  c.restore();
}
function blankPage(c, al) { if (al <= 0) return; c.save(); resetT(c); c.globalAlpha = al; c.fillStyle = RC.paper; c.fillRect(0, 0, W, H); grain(c, rectPath(0, 0, W, H), [0, 0, W, H], 1200, shade(RC.paper, .5), .06, 5, 1.6); c.restore(); }
// Raven flies in with its pencil and signs the film in two lines. The pencil tip rides the
// last written letter; between the lines Raven flies back to the left margin.
const SIGN = [{text: 'Made by Raven,', y: 500, t0: 1.1, t1: 2.5}, {text: 'made for you.', y: 660, t0: 2.9, t1: 4.1}];
function madeBy(c, t) {
  const s = t - END.made; if (s < 0 || t > END.card + .6) return;
  blankPage(c, sm(0, .4, s, lin));
  // The signature leaves before the card arrives, so the two never overlap.
  c.save(); c.globalAlpha *= 1 - sm(END.card - END.made - .9, END.card - END.made - .5, s, lin);
  const size = 104; c.save(); c.font = `600 ${size}px ${RV_FONT}`;
  const lines = SIGN.map(L => { const w = textWidth(c, L.text, size), x0 = W / 2 - w / 2, p = sm(L.t0, L.t1, s, lin), n = Math.floor([...L.text].length * p);
    return {...L, w, x0, p, tip: x0 + textWidth(c, [...L.text].slice(0, n).join(''), size) + (p < 1 ? 20 : 0)}; }); c.restore();
  lines.forEach((L, i) => writeOn(c, L.text, W / 2, L.y, {size, col: RC.ink, align: 'center', p: L.p, rot: -.02 + i * .01}));
  const L2 = lines[1], under = sm(4.1, 4.45, s, lin);
  if (under > 0) iLine(c, partial([[L2.x0 - 10, L2.y + 34], [W / 2, L2.y + 42], [L2.x0 + L2.w + 10, L2.y + 30]], under), {w: 6, col: RC.red, seed: 4, rough: 1.2});
  const land = [L2.x0 + L2.w + 130, L2.y + 30], q = QT1(s), writing = (L, i) => i ? [L.tip - 60, L.y + 280 + 8 * Math.sin(s * 9)] : [L.tip - 70, L.y - 180 + 8 * Math.sin(s * 9)];
  let x, yy, pose, rot = 0, up = false; const pen = flyPose(q, {young: 0, gx: .6, gy: .9, tuck: 1}), penUp = flyPose(q, {young: 0, gx: .7, gy: -.9, tuck: 1});
  if (s < 1.1) { const u = easeOut(clamp(s / 1.0, 0, 1)); x = lerp(-200, lines[0].x0 - 80, u); yy = lerp(220, lines[0].y - 180, u); pose = flyPose(q, {young: 0, gx: .8, gy: .4}); rot = .2; }
  else if (s < 2.5) { [x, yy] = writing(lines[0], 0); pose = pen; rot = .45; }
  // Between the lines Raven swoops round the right end of the first line and down under the second.
  else if (s < 2.9) { const u = easeIO((s - 2.5) / .4), a = writing({...lines[0], tip: lines[0].x0 + lines[0].w}, 0), b = writing({...L2, tip: L2.x0}, 1);
    x = lerp(a[0], b[0], u) + Math.sin(u * Math.PI) * 260; yy = lerp(a[1], b[1], u); pose = flyPose(q, {young: 0, gx: -.7, gy: .6}); rot = lerp(.3, -.1, u); up = u > .6; }
  else if (s < 4.1) { [x, yy] = writing(L2, 1); pose = penUp; rot = -.12; up = true; }
  else if (s < 4.45) { x = L2.x0 + L2.w - 40 + (s - 4.1) * 300; yy = L2.y + 280; pose = penUp; rot = -.12; up = true; }
  else if (s < 4.85) { const u = (s - 4.45) / .4, p0 = [L2.x0 + L2.w + 60, L2.y + 280]; [x, yy] = hop(p0, land, u, 120); pose = u < .8 ? flyPose(q, {young: 0, gy: .5}) : rvPose('adult', {squash: .86, gx: -.3}); rot = .2 * (1 - u); }
  else { [x, yy] = land; const k2 = s - 4.85; pose = k2 < .3 ? rvPose('adult', {squash: lerp(.86, 1, k2 / .3), gx: -.4}) : rvPose('adult', {mood: k2 > .5 && k2 < .62 ? 'blink' : k2 > .8 ? 'happy' : 'open', gx: -.6, gy: .2, tuft: 1, tilt: -.08}); }
  const place = {x, y: yy, scale: 1.0, rot}, G = drawRaven(c, pose, {...place, look: 'ink'});
  if (s > 1.0 && s < 4.6) { const b = rvWorld(G.anchors.beak, place); if (up) drawPencilProp(c, b[0] - 10, b[1] - 6, -1.2, .9); else drawPencilProp(c, b[0] - 30, b[1] + 4, .9, .9); }
  if (s > 5.2) star(c, x + 120, yy - 250, 18 * clamp((s - 5.2) / .2, 0, 1) * (1 - clamp((s - 5.6) / .3, 0, 1)), RC.gold, 4);
  c.restore();
}
// A pencil-drawn search box: a wobbly outline that draws itself, the GitHub mark, the link typed in.
function roundedPts(x, y, w, h, r, n = 12) { const pts = [], cs = [[x + w - r, y + r, -Math.PI / 2], [x + w - r, y + h - r, 0], [x + r, y + h - r, Math.PI / 2], [x + r, y + r, Math.PI]];
  for (const [cx, cy, a0] of cs) for (let i = 0; i <= n; i++) { const a = a0 + i / n * Math.PI / 2; pts.push([cx + Math.cos(a) * r, cy + Math.sin(a) * r]); } pts.push(pts[0]); return pts; }
// The official GitHub mark (primer/octicons mark-github-24), drawn unmodified in solid ink.
const GITHUB_MARK = new Path2D('M10.226 17.284c-2.965-.36-5.054-2.493-5.054-5.256 0-1.123.404-2.336 1.078-3.144-.292-.741-.247-2.314.09-2.965.898-.112 2.111.36 2.83 1.01.853-.269 1.752-.404 2.853-.404 1.1 0 1.999.135 2.807.382.696-.629 1.932-1.1 2.83-.988.315.606.36 2.179.067 2.942.72.854 1.101 2 1.101 3.167 0 2.763-2.089 4.852-5.098 5.234.763.494 1.28 1.572 1.28 2.807v2.336c0 .674.561 1.056 1.235.786 4.066-1.55 7.255-5.615 7.255-10.646C23.5 6.188 18.334 1 11.978 1 5.62 1 .5 6.188.5 12.545c0 4.986 3.167 9.12 7.435 10.669.606.225 1.19-.18 1.19-.786V20.63a2.9 2.9 0 0 1-1.078.224c-1.483 0-2.359-.808-2.987-2.313-.247-.607-.517-.966-1.034-1.033-.27-.023-.359-.135-.359-.27 0-.27.45-.471.898-.471.652 0 1.213.404 1.797 1.235.45.651.921.943 1.483.943.561 0 .92-.202 1.437-.719.382-.381.674-.718.944-.943');
function githubMark(c, x, y, r, k) {
  if (k <= 0) return; c.save(); c.translate(x, y); c.scale(k * r / 11.5, k * r / 11.5); c.translate(-12, -12);
  c.fillStyle = RC.ink; c.fill(GITHUB_MARK); c.restore();
}
function searchBar(c, t, s) {
  const x = 470, y = 718, w = W - 940, h = 92, drawP = sm(1.5, 2.1, s, lin); if (drawP <= 0) return;
  const outline = roundedPts(x, y, w, h, 44), fillA = sm(1.9, 2.2, s, lin);
  c.save(); c.globalAlpha = fillA; c.fillStyle = RC.white; c.beginPath(); c.roundRect(x, y, w, h, 44); c.fill(); c.restore();
  iLine(c, partial(outline, drawP), {w: 3.4, seed: 21, rough: 1.4});
  if (drawP > .6) iLine(c, partial(outline.map(([px, py]) => [px + 2, py + 2.5]), (drawP - .6) / .4), {w: 1.2, al: .4, seed: 22, rough: 1.8});
  githubMark(c, x + 62, y + h / 2, 29, clamp(easeOutBack(clamp((s - 2.0) / .35, 0, 1)), 0, 1.25));
  const typed = clamp((s - 2.3) / 1.3, 0, 1), n = Math.round(REPO.length * typed), size = 40;
  c.save(); c.font = `600 ${size}px ${RV_FONT}`; c.fillStyle = RC.ink; c.textBaseline = 'middle'; c.textAlign = 'left'; c.fillText(REPO.slice(0, n), x + 118, y + h / 2 + 2);
  const cx = x + 118 + c.measureText(REPO.slice(0, n)).width + 6;
  if (s > 2.2 && (typed < 1 || Math.floor(s * 2.2) % 2 === 0)) { c.fillRect(cx, y + 24, 3, h - 48); }
  c.restore();
  // The search button on the right: a hand-drawn magnifier that gets pressed once the link is in.
  const bx = x + w - 60, by = y + h / 2, press = s > 3.8 && s < 4.05 ? .86 : 1, bk = sm(2.0, 2.3, s, lin);
  if (bk > 0) { c.save(); c.globalAlpha = bk; c.translate(bx, by); c.scale(press, press); c.fillStyle = RC.ink; c.beginPath(); c.arc(0, 0, 34, 0, TAU); c.fill();
    c.strokeStyle = RC.paper; c.lineWidth = 4.5; c.lineCap = 'round'; c.beginPath(); c.arc(-4, -4, 12, 0, TAU); c.stroke(); c.beginPath(); c.moveTo(5, 5); c.lineTo(15, 15); c.stroke(); c.restore(); }
  if (s > 3.8) { const k = clamp((s - 3.8) / .5, 0, 1); for (let i = 0; i < 6; i++) { const a = i / 6 * TAU + .3, r0 = 44 + k * 26; c.save(); c.globalAlpha = 1 - k; iLine(c, [[bx + Math.cos(a) * r0, by + Math.sin(a) * r0], [bx + Math.cos(a) * (r0 + 16), by + Math.sin(a) * (r0 + 16)]], {w: 3, col: RC.gold, seed: i, rough: .2}); c.restore(); } }
}
function closingCard(c, t) {
  const card = sm(END.card - .5, END.card, t, lin); if (card <= 0) return;
  blankPage(c, card);
  const s = t - END.card, textTop = 330, edge = textTop + 48, rise = easeOutBack(clamp((s - .2) / .6, 0, 1));
  if (s > .2) {
    // Only the head clears the top of the letters; the body stays hidden behind the name.
    const blink = (s > 1.6 && s < 1.75) || (s > 2.1 && s < 2.22), look = s < .9 ? .2 : s < 1.3 ? -.85 : s < 1.6 ? .85 : s > 2.3 ? .3 : .1;
    const pose = rvPose('adult', {mood: blink ? 'blink' : 'open', gx: look, gy: s > 2.3 ? .95 : .45, tilt: look * .08 - .04, tuft: 1, eyeS: 1.12, pupil: 1.1});
    c.save(); c.beginPath(); c.rect(0, 0, W, edge); c.clip(); drawRaven(c, pose, {x: W / 2 - 70, y: textTop + lerp(480, 292, rise), scale: 1.25, look: 'ink'}); c.restore();
  }
  c.save(); c.globalAlpha = card;
  c.fillStyle = RC.ink; c.font = `600 132px ${RV_FONT}`; c.textAlign = 'center'; c.fillText('Raven', W / 2, textTop + 110); c.textAlign = 'left';
  writeOn(c, 'the harness of harnesses', W / 2, textTop + 196, {size: 56, col: RC.ink, align: 'center', p: sm(.3, .9, s, lin), rot: 0});
  writeOn(c, 'the agent that runs all your agents', W / 2, textTop + 262, {size: 34, col: RC.lead, align: 'center', p: sm(.9, 1.4, s, lin), rot: 0});
  searchBar(c, t, s);
  c.restore();
}
function finale(c, t) { routeRecap(c, t); madeBy(c, t); closingCard(c, t); }

// ---------- every frame after the inking ----------
const LC = layer();
function journey2(c, t) {
  const v = viewAt(t), st = placedAt(raven2, t, exposure2(t)), [a, b] = viewOf(v.x, v.z);
  const vis = REGIONS.filter(([, x0, x1]) => x1 > a - 60 && x0 < b + 60);
  vis.forEach(([look, x0], i) => {
    if (i === 0) return drawRegion(c, look, t, v, st);
    const L = i === 1 ? LB : LC; renderInto(L, g => drawRegion(g, look, t, v, st));
    resetT(c); c.save(); c.clip(polyPath([...seamLine(x0, v).map(p => toScreen(v, p)), [W + 20, H + 20], [W + 20, -20]])); c.drawImage(L, 0, 0, W, H); c.restore();
  });
  const lookNow = REGIONS.find(([, x0, x1]) => v.x >= x0 && v.x < x1)[0];
  resetT(c); overlays2(c, t); featureCard(c, lookNow, t); finale(c, t);
}

// ---------- sound for the second half, placed from the same route ----------
function flightCues({piano, bell, chirp, glide, tick, thud, noise, pad}) {
  HOPS2.forEach((h, i) => tick(h.t0 + h.d * .82, .07, 1000 + (i % 3) * 120));
  glide(ST.mem + .15, .14, 300, 800, .06); tick(ST.mem + .82, .14, 800); glide(ST.mem + .85, .5, 500, 1500, .04); chirp(ST.mem + 1.3, 84, .05);
  noise(ST.hopB, 1.35, .03, 101, 1400, .6, 5); noise(ST.tools + .2, .7, .04, 102, 900, .6); glide(ST.tools + 1.1, .15, 400, 900, .05); chirp(ST.tools + 1.4, 86, .04);
  noise(ST.fly - .05, .6, .08, 103, 700, .4); [62, 66, 69].forEach((n, i) => piano(n, ST.fly + i * .15, 2.2, .1));
  for (let t = ST.fly + .3; t < ST.end - 7; t += 1 / 2.3) noise(t, .12, .012, Math.round(t * 10), 500, .7);
  piano(74, GRAB, 1.8, .1); bell(86, GRAB + .02, .05);
  // Evolution: the press keeps time; every gate rings, the winner gets the brightest bell.
  for (let t = ST.evo + .4; t < 58; t += .5) tick(t, .05, 420);
  GATE_T.forEach((t, i) => bell(79 + i * 2, t + .5, .05)); thud(50.6, .1); glide(50.7, .7, 600, 300, .03); bell(88, 53.1, .05); thud(56.0, .14); bell(93, 56.05, .07); glide(55.9, .8, 600, 250, .03);
  [60.4, 61.3].forEach((t, i) => bell([76, 79][i], t, .05));
  // Orchestration: the request lands, the tasks appear, each built-in agent arrives with a note.
  glide(ST.flock + .3, .5, 900, 500, .03); [0, 1, 2, 3].forEach(i => piano(69 + i * 2, ST.flock + 2.4 + i * .35, 1.2, .08));
  [[ST.flock + 2.0, 69], [ST.flock + 2.4, 71], [ST.flock + 2.8, 74]].forEach(([t, n]) => chirp(t + .1, n + 12, .04));
  tick(ST.code + 1.4, .12, 1400); bell(86, ST.code + 1.45, .06);
  pad([45, 52, 57], ST.oncall + .2, 3.0, .016); bell(81, ST.oncall + 1.0, .03); [74, 78, 81].forEach((n, i) => piano(n, ST.oncall + 3.1 + i * .2, 1.8, .08)); chirp(ST.oncall + 3.6, 86, .05);
  GUESTS.forEach((g, i) => { piano(62 + i * 3, g.join + .6, 1.3, .07); chirp(g.join + .7, 84 + i, .03); });
  for (let i = 0; i < 5; i++) { chirp(ST.third + 3.2 + i * .35, 88, .03); tick(ST.third + 5.6 + i * .3, .09, 1400); }
  pad([38, 45, 50], ST.blue + .4, 6.4, .02); thud(ST.blue + 3.0, .18); glide(ST.blue + 3.1, .8, 400, 120, .03);
  for (let i = 0; i < 40; i++) bell(86 + (i % 5) * 2, ST.blue + 4.4 + i * .03, .012, (i % 2 ? .5 : -.5));
  chirp(ST.design + .4, 82, .05); [0, 1, 2].forEach(i => glide(ST.design + 1.8 + i * .3, .2, 500, 900, .03)); tick(ST.design + 3.2, .12, 1200); bell(90, ST.design + 3.25, .06);
  glide(ST.book + .2, .3, 300, 700, .04); tick(ST.book + 1.8, .1, 1300); bell(88, ST.book + 1.85, .05);
  for (let i = 0; i < 16; i++) tick(ST.remind + .2 + i * .1, .05, 2400);
  [[ST.design, [50, 57, 62, 66]], [ST.book, [47, 54, 59, 62]], [ST.remind, [43, 50, 55, 59]], [ST.harness, [45, 52, 57, 61]], [ST.harness + 3, [50, 57, 62, 66]]].forEach(([t, ch]) => pad(ch, t, 2.8, .012));
  [62, 66, 69, 74].forEach((n, i) => piano(n, ST.harness + .3 + i * .15, 1.4, .08));
  [62, 66, 69, 74, 78].forEach((n, i) => piano(n, ST.finale + .1 + i * .06, 3.5, .08)); pad([50, 57, 62, 66, 69], ST.finale, 7, .015);
  // Made By Raven: wings in, pencil on paper, a red underline, a happy chirp.
  const M = END.made, K = END.card;
  glide(M + .2, .8, 500, 1100, .03); noise(M + 1.1, 1.4, .03, 150, 3800, .8, 11); glide(M + 2.5, .4, 900, 600, .02); noise(M + 2.9, 1.2, .03, 152, 3800, .8, 11); noise(M + 4.1, .4, .05, 151, 2400, .7); tick(M + 4.85, .08, 900); chirp(M + 5.3, 86, .06); bell(90, M + 5.2, .05);
  // The card: the head pops up, the box draws itself, each letter of the link is a key press.
  pad([50, 57, 62, 66, 69], K - .3, 6.2, .014); glide(K + .2, .4, 300, 700, .04); chirp(K + .6, 84, .05); tick(K + 1.65, .05, 2200); tick(K + 2.15, .05, 2200);
  glide(K + 1.5, .6, 400, 800, .025); tick(K + 2.05, .1, 1300);
  for (let i = 0; i < REPO.length; i++) tick(K + 2.3 + 1.3 * i / REPO.length, .03, 2600 + (i % 3) * 200);
  thud(K + 3.8, .08); bell(86, K + 3.85, .07); bell(93, K + 4.1, .05); [62, 66, 69, 74].forEach((n, i) => piano(n, K + 3.9 + i * .08, 2.4, .07));
}
