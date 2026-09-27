'use strict';
// An original score for Act I, synthesized with Web Audio on the film's own timeline.
// Pencil scratches and a sparse felt piano while the page is a sketch; a string pad
// arrives with the ink. Every cue is placed from the route (T, HOPS), so it cannot drift.
function ravenScore(ac, t0, destination) {
  // Makeup gain sits after the compressor, so quiet plucks come up without the thud clipping.
  const END = FILM_END, master = ac.createGain(), makeup = ac.createGain(); master.gain.value = 1.6;
  makeup.gain.setValueAtTime(1.45, t0); makeup.gain.setValueAtTime(1.45, t0 + END - 1.2); makeup.gain.linearRampToValueAtTime(0, t0 + END);
  const comp = ac.createDynamicsCompressor(); comp.threshold.value = -24; comp.knee.value = 12; comp.ratio.value = 3.5; comp.attack.value = .006; comp.release.value = .22;
  master.connect(comp); comp.connect(makeup); makeup.connect(destination);
  const verb = ac.createConvolver(), ir = ac.createBuffer(2, Math.floor(ac.sampleRate * 2.2), ac.sampleRate), r = rng(29);
  for (let ch = 0; ch < 2; ch++) { const d = ir.getChannelData(ch); for (let i = 0; i < d.length; i++) d[i] = (r() * 2 - 1) * Math.exp(-i / ac.sampleRate * 3.2); }
  verb.buffer = ir; const wet = ac.createGain(); wet.gain.value = .22; verb.connect(wet); wet.connect(master);
  const out = (node, send = .3, pan = 0) => { const p = ac.createStereoPanner(); p.pan.value = pan; node.connect(p); p.connect(master); const s = ac.createGain(); s.gain.value = send; p.connect(s); s.connect(verb); };
  const hz = n => 440 * 2 ** ((n - 69) / 12), at = t => t0 + t;
  const env = (g, t, a, v, d) => { g.gain.setValueAtTime(0, at(t)); g.gain.linearRampToValueAtTime(v, at(t) + a); g.gain.exponentialRampToValueAtTime(.0001, at(t) + d); };
  function piano(n, t, d = 2.4, v = .16, pan = 0) {
    const g = ac.createGain(), f = ac.createBiquadFilter(); f.type = 'lowpass'; f.frequency.value = 2600; g.connect(f); out(f, .55, pan);
    g.gain.setValueAtTime(0, at(t)); g.gain.linearRampToValueAtTime(v, at(t) + .006); g.gain.exponentialRampToValueAtTime(v * .25, at(t) + .3); g.gain.exponentialRampToValueAtTime(.0001, at(t) + d);
    for (const [k, a] of [[1, 1], [2.003, .28], [3.01, .09], [4.02, .04]]) { const o = ac.createOscillator(), m = ac.createGain(); o.frequency.value = hz(n) * k; m.gain.value = a; o.connect(m); m.connect(g); o.start(at(t)); o.stop(at(t) + d + .05); }
  }
  function bell(n, t, v = .07, pan = .3) {
    const g = ac.createGain(); out(g, .8, pan); env(g, t, .004, v, 2.6);
    for (const [k, a] of [[1, .7], [2.76, .2], [5.4, .08]]) { const o = ac.createOscillator(), m = ac.createGain(); o.frequency.value = hz(n) * k; m.gain.value = a; o.connect(m); m.connect(g); o.start(at(t)); o.stop(at(t) + 2.7); }
  }
  function chirp(t, n = 84, v = .05) {
    const o = ac.createOscillator(), g = ac.createGain(); o.frequency.setValueAtTime(hz(n), at(t)); o.frequency.exponentialRampToValueAtTime(hz(n + 8), at(t) + .06); o.frequency.exponentialRampToValueAtTime(hz(n + 3), at(t) + .15);
    env(g, t, .015, v, .2); o.connect(g); out(g, .3, .15); o.start(at(t)); o.stop(at(t) + .22);
  }
  function glide(t, d, f0, f1, v = .05, type = 'sine') {
    const o = ac.createOscillator(), g = ac.createGain(); o.type = type; o.frequency.setValueAtTime(f0, at(t)); o.frequency.exponentialRampToValueAtTime(f1, at(t) + d);
    g.gain.setValueAtTime(0, at(t)); g.gain.linearRampToValueAtTime(v, at(t) + .03); g.gain.setValueAtTime(v, at(t) + d * .7); g.gain.exponentialRampToValueAtTime(.0001, at(t) + d); o.connect(g); out(g, .25); o.start(at(t)); o.stop(at(t) + d + .02);
  }
  function tick(t, v = .1, f = 1500) { const o = ac.createOscillator(), g = ac.createGain(); o.frequency.setValueAtTime(f, at(t)); o.frequency.exponentialRampToValueAtTime(f * .6, at(t) + .05); env(g, t, .002, v, .07); o.connect(g); out(g, .2, -.1); o.start(at(t)); o.stop(at(t) + .08); }
  function thud(t, v = .3) { const o = ac.createOscillator(), g = ac.createGain(); o.frequency.setValueAtTime(95, at(t)); o.frequency.exponentialRampToValueAtTime(38, at(t) + .25); env(g, t, .004, v, .5); o.connect(g); out(g, .1); o.start(at(t)); o.stop(at(t) + .55); noise(t, .25, v * .5, 3, 400, 1); }
  // Band-limited noise with an optional tremolo: pencil on paper, flutter, rubbing.
  function noise(t, d, v, seed = 1, freq = 3000, q = .7, trem = 0) {
    const n = Math.ceil(ac.sampleRate * d), b = ac.createBuffer(1, n, ac.sampleRate), x = b.getChannelData(0), rr = rng(seed);
    for (let i = 0; i < n; i++) { const u = i / n, w = trem ? .55 + .45 * Math.sin(i / ac.sampleRate * TAU * trem + rr() * .2) : 1; x[i] = (rr() * 2 - 1) * Math.sin(Math.PI * u) ** .6 * w; }
    const s = ac.createBufferSource(), f = ac.createBiquadFilter(), g = ac.createGain(); s.buffer = b; f.type = 'bandpass'; f.frequency.value = freq; f.Q.value = q; g.gain.value = v; s.connect(f); f.connect(g); out(g, .15, -.2); s.start(at(t));
  }
  function pad(notes, t, d, v = .02) {
    notes.forEach((n, j) => { const g = ac.createGain(), f = ac.createBiquadFilter(); f.type = 'lowpass'; f.frequency.setValueAtTime(500, at(t)); f.frequency.linearRampToValueAtTime(1500, at(t) + d * .6); g.connect(f); out(f, .6, (j / (notes.length - 1) - .5) * .7);
      g.gain.setValueAtTime(0, at(t)); g.gain.linearRampToValueAtTime(v, at(t) + d * .35); g.gain.setValueAtTime(v, at(t) + d * .8); g.gain.exponentialRampToValueAtTime(.0001, at(t) + d + 1.4);
      for (const det of [-6, 5]) { const o = ac.createOscillator(); o.type = 'sawtooth'; o.frequency.value = hz(n); o.detune.value = det; o.connect(g); o.start(at(t)); o.stop(at(t) + d + 1.5); } });
  }
  // The page is drawn.
  noise(.1, 1.6, .022, 11, 4200, .8, 9); noise(1.3, 1.0, .018, 12, 3600, .8, 11); noise(1.9, .8, .02, 13, 3900, .8, 7);
  [[62, .25], [69, 1.0], [66, 1.8], [74, 2.6], [71, 3.3]].forEach(([n, t]) => piano(n, t, 2.6, .13, -.1));
  // Hatching: wobble, crack, pop, bonk, a first look, a first shiny thing.
  const h = T.hatch;
  [.5, .9, 1.3].forEach((t, i) => tick(h + t, .09, 900 + i * 180));
  for (let i = 0; i < 5; i++) noise(h + 1.6 + i * .08, .05, .12, 20 + i, 5200, 2);
  glide(h + 2.3, .16, 320, 950, .07); chirp(h + 2.36, 86, .06); glide(h + 2.35, .85, 700, 1300, .018);
  tick(h + 3.2, .16, 700); bell(81, h + 3.22, .04); piano(74, h + 3.7, 2, .1); tick(h + 4.3, .04, 2200);
  bell(86, h + 4.9, .06); bell(90, h + 5.5, .05); bell(93, h + 5.65, .04);
  chirp(h + 6.4, 84, .05); [69, 74, 78].forEach((n, i) => piano(n, h + 6.45 + i * .12, 2.2, .1, .15));
  // Every landing on the branch is a small step on the timeline.
  const steps = [74, 76, 78, 81, 78, 76];
  HOPS.forEach((hp, i) => { const land = hp.t0 + hp.d * .82; tick(land, .07, 1100 + (i % 3) * 120); if (i % 2 === 0) piano(steps[i % steps.length] - 12, land, 1.2, .05, .2); });
  bell(86, T29, .05); bell(88, T30, .05);
  // A note from a stranger, and a stamp.
  const sl = T.slip;
  noise(sl + .1, 1.1, .035, 31, 2500, .6, 6); tick(sl + 1.67, .12, 1300); thud(sl + 1.69, .12); chirp(sl + 2.0, 88, .045); noise(sl + 2.5, .35, .06, 32, 1500, .5);
  piano(66, sl + .4, 2, .1); piano(71, sl + 1.9, 2, .09);
  // The shiny cluster, the memory pack, the eraser, the fall.
  for (let i = 0; i < 12; i++) bell([74, 76, 78, 81, 83, 86][i % 6] + (i > 5 ? 12 : 0), T30 + .4 + i * .09, .025, .4);
  const f = T.fall;
  glide(f + .15, .14, 300, 800, .06); chirp(f + .45, 86, .045); piano(50, f + 1.3, 2.4, .12); piano(57, f + 1.3, 2.4, .08);
  noise(f + 1.62, .25, .08, 40, 1200, .5);
  for (let i = 0; i < 13; i++) noise(f + 1.7 + i * .085, .05, .05, 41 + i, 900, 1.2);
  noise(f + 2.25, .38, .05, 55, 2200, .9, 7);
  glide(f + 3.1, .45, 1300, 190, .06); thud(f + 3.55, .2);
  [86, 90, 93, 90, 86, 93].forEach((n, i) => bell(n, f + 4.8 + i * .2, .03, (i % 2 ? .4 : -.4)));
  for (let i = 0; i < 4; i++) tick(f + 5.5 + i * .09, .05, 1600 - i * 100);
  piano(62, f + 5.9, 2.4, .12); piano(69, f + 6.15, 2.4, .1);
  // Crossing into ink: a wet splash, the shake, and wings.
  noise(TCROSS - .05, .5, .06, 60, 700, .5); piano(50, TCROSS, 3, .1); pad([50, 57, 62, 66], TCROSS, 3.4, .014);
  const k = T.ink;
  for (let i = 0; i < 7; i++) noise(k + .7 + i * .045, .04, .06, 70 + i, 1800, 1.4);
  noise(k + .86, .3, .07, 80, 600, .4); chirp(k + 1.02, 80, .05);
  piano(66, k + 1.45, 2.2, .1); piano(74, k + 2.15, 2.4, .12); bell(86, k + 2.17, .05);
  [62, 66, 69, 74].forEach((n, i) => piano(n, k + 2.64 + i * .03, 2.8, .085, (i - 1.5) * .2)); pad([50, 57, 62, 66, 69, 74], k + 2.6, 1.8, .013); bell(86, k + 2.67, .07); bell(93, k + 2.9, .04);
  if (typeof flightCues === 'function') flightCues({piano, bell, chirp, glide, tick, thud, noise, pad});
}
