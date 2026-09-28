// Page-level checks the preview renderer does not do. Run from the film folder.
//   node frames.mjs film.html load                    load the page, report errors, frame count, fps
//   node frames.mjs film.html shots 12.5,40,96 [--w 1920] [--param k=v] [--out out/shots]
//                                                      PNGs at chosen *seconds* (not frame indices)
//   node frames.mjs film.html score [--out out/score.wav]
//                                                      render the synthesized score, print peak/RMS per second
// Needs puppeteer-core in node_modules and Google Chrome (or CHROME=/path).
import puppeteer from 'puppeteer-core';
import {pathToFileURL} from 'node:url';
import {mkdirSync, writeFileSync} from 'node:fs';
import path from 'node:path';

const [file, cmd, list] = process.argv.slice(2), args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
if (!file || !cmd) { console.error('usage: node frames.mjs film.html load|shots|score ...'); process.exit(2); }
const url = pathToFileURL(path.resolve(file)); url.searchParams.set('bare', '1'); url.searchParams.set('w', opt('--w', '1920'));
for (let i = 0; i < args.length; i++) if (args[i] === '--param') { const [k, v] = args[i + 1].split('='); url.searchParams.set(k, v); }
const b = await puppeteer.launch({executablePath: process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, protocolTimeout: 0});
const p = await b.newPage(), errors = [];
p.on('pageerror', e => errors.push(String(e.stack || e).split('\n').slice(0, 3).join(' | ')));
p.on('console', m => m.type() === 'error' && errors.push(m.text()));
try {
  await p.goto(url.href);
  await p.waitForFunction('window.__ready === true || window.__error', {timeout: 120000}).catch(() => {});
  const meta = await p.evaluate(() => ({ready: window.__ready, error: window.__error, frames: window.__NDRAW, fps: window.__fps, size: window.__size}));
  if (!meta.ready || errors.length) { console.log('NOT READY', meta.error || '', '\n' + errors.join('\n')); process.exitCode = 1; }
  else if (cmd === 'load') console.log(`ok: ${meta.frames} frames at ${meta.fps} fps = ${(meta.frames / meta.fps).toFixed(2)} s, ${meta.size.w}x${meta.size.h}`);
  else if (cmd === 'shots') {
    const out = opt('--out', 'out/shots'); mkdirSync(out, {recursive: true});
    for (const s of list.split(',').map(Number)) {
      const i = Math.min(meta.frames - 1, Math.round(s * meta.fps));
      writeFileSync(path.join(out, `${s}s.png`), Buffer.from((await p.evaluate(i => window.__frame(i), i)).split(',')[1], 'base64'));
    }
    console.log(`wrote ${list.split(',').length} shots to ${out}` + (errors.length ? '\nERRORS\n' + errors.join('\n') : ''));
  } else if (cmd === 'score') {
    const b64 = await p.evaluate(() => window.__wav ? window.__wav() : null);
    if (!b64) { console.log('film has no score'); }
    else {
      const buf = Buffer.from(b64, 'base64'), out = opt('--out', 'out/score.wav'); mkdirSync(path.dirname(out), {recursive: true}); writeFileSync(out, buf);
      const n = (buf.length - 44) / 4, sr = 48000, rows = []; let peak = 0;
      for (let s = 0; s < Math.ceil(n / sr); s++) { let pk = 0, sq = 0, c = 0; for (let i = s * sr; i < Math.min(n, (s + 1) * sr); i++) { const v = Math.abs(buf.readInt16LE(44 + i * 4) / 32768); pk = Math.max(pk, v); sq += v * v; c++; } peak = Math.max(peak, pk); rows.push(`${s}s ${pk.toFixed(2)}/${Math.sqrt(sq / c).toFixed(3)}`); }
      console.log(`score ${rows.length} s, max peak ${peak.toFixed(2)} (keep under ~0.8; quiet sections should still reach ~0.05 RMS)\n` + rows.join('  '));
    }
  }
} finally { await b.close(); }
