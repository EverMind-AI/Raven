'use strict';
// Optional Canvas materials. All boxes are REST-SPACE bounds: keep them fixed as
// tone changes; draw in local coordinates or deform the finished cached layer.
function formHatch(c, path, box, { seed = 1, color = PAL.ink, spacing = 9, length = 17, width = .8, opacity = .3, tone = () => .5, direction = () => .9 } = {}) {
  if (!(spacing > 0 && length > 0)) throw new Error('Hatching needs positive spacing and length');
  const [x, y, w, h] = box; c.save(); c.clip(path); c.strokeStyle = color; c.lineWidth = width; c.lineCap = 'round'; c.globalAlpha *= opacity; c.beginPath();
  // Grid ids, not RNG iteration order, determine whether each persistent stroke exists.
  for (let iy = Math.floor(y / spacing); iy <= Math.ceil((y + h) / spacing); iy++) for (let ix = Math.floor(x / spacing); ix <= Math.ceil((x + w) / spacing); ix++) {
    const id = Math.imul(ix, 73856093) ^ Math.imul(iy, 19349663), px = (ix + hash(id, seed) * .6) * spacing, py = (iy + hash(id, seed + 1) * .6) * spacing;
    if (hash(id, seed + 2) > clamp(tone(px, py), 0, 1)) continue;
    let xx = px, yy = py; c.moveTo(xx, yy); const len = length * (.65 + hash(id, seed + 3) * .5);
    for (let n = 0; n < 4; n++) { const a = direction(xx, yy); xx += Math.cos(a) * len / 4; yy += Math.sin(a) * len / 4; c.lineTo(xx, yy); }
  }
  c.stroke(); c.restore();
}
function graphite(c, path, box, { seed = 1, color = PAL.ink, tone = () => .4, direction = () => .6, softness = .5 } = {}) {
  formHatch(c, path, box, { seed, color, tone, direction, spacing: 4.5, length: 15, width: .65 + softness, opacity: .1 + softness * .08 });
  formHatch(c, path, box, { seed: seed + 9, color, tone: (x, y) => Math.max(0, tone(x, y) - .45), direction: (x, y) => direction(x, y) + .75, spacing: 6, length: 12, width: .55, opacity: .14 });
}
function screenFill(c, path, box, { color = PAL.fills[0], paper = PAL.paper, seed = 1, wear = .025 } = {}) {
  c.save(); c.fillStyle = color; c.fill(path); c.clip(path);
  const [x, y, w, h] = box, r = rng(seed); c.fillStyle = paper; c.globalAlpha *= .45;
  for (let i = 0, n = Math.ceil(w * h * Math.max(0, wear) / 12); i < n; i++) { const xx = x + r() * w, yy = y + r() * h, a = .2 + r() * .9; c.fillRect(xx, yy, a * 1.5, a); }
  c.restore();
}
function pigmentWash(c, path, box, { color = PAL.fills[0], seed = 1, opacity = .4, granulation = .18, edge = .15, blend = 'multiply' } = {}) {
  c.save(); c.globalCompositeOperation = blend; c.globalAlpha *= opacity; c.fillStyle = color; c.fill(path); c.clip(path);
  const [x, y, w, h] = box, r = rng(seed); c.globalAlpha *= granulation;
  for (let i = 0, n = Math.min(8000, Math.ceil(w * h / 25)); i < n; i++) { const xx = x + r() * w, yy = y + r() * h, radius = .5 + r() * 2.5; c.beginPath(); c.ellipse(xx, yy, radius * 1.3, radius, r() * TAU, 0, TAU); c.fill(); }
  c.restore();
  if (edge > 0) { c.save(); c.clip(path); c.globalCompositeOperation = blend; c.globalAlpha *= opacity * edge; c.strokeStyle = color; c.lineWidth = 3.5; c.stroke(path); c.restore(); }
}
