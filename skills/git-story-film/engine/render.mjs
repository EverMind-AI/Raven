// Offline Canvas renderer. --grid N: overview; --strip START,COUNT: consecutive frames.
// --only 0,24: spot frames. --ar/--width override film defaults only when supplied.
// --look ink|pencil|riso|screen|doodle is passed through to films that support it.
import puppeteer from 'puppeteer-core';
import {execFileSync} from 'node:child_process';
import {existsSync, mkdirSync, mkdtempSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const args = process.argv.slice(2), flags = new Map();
let file;
for (let i = 0; i < args.length; i++) {
  if (args[i].startsWith('--')) {
    if (!['--out', '--ar', '--width', '--grid', '--strip', '--only', '--look'].includes(args[i])) throw new Error(`Unknown option: ${args[i]}`);
    if (!args[i + 1] || args[i + 1].startsWith('--')) throw new Error(`Missing value: ${args[i]}`);
    flags.set(args[i], args[++i]);
  } else if (!file) file = args[i]; else throw new Error(`Unexpected argument: ${args[i]}`);
}
if (!file) throw new Error('Usage: node render.mjs film.html [--grid 24 | --strip 48,12 | --only 0,24] [--out dir]');
const integer = (v, name, min = 1, max = 16384) => {
  const n = Number(v); if (!Number.isInteger(n) || n < min || n > max) throw new Error(`${name}: expected integer ${min}..${max}`); return n;
};
const only = flags.has('--only') ? flags.get('--only').split(',').map(v => integer(v, 'frame', 0, 1e7)) : null;
const grid = flags.has('--grid') ? integer(flags.get('--grid'), 'grid', 1, 240) : null;
const strip = flags.has('--strip') ? flags.get('--strip').split(',').map(Number) : null;
if (strip && (strip.length !== 2 || !Number.isInteger(strip[0]) || strip[0] < 0 || !Number.isInteger(strip[1]) || strip[1] < 1 || strip[1] > 240)) throw new Error('--strip expects START_FRAME,COUNT (count 1..240)');
if ([only, grid, strip].filter(Boolean).length > 1) throw new Error('Choose only one of --only, --grid, --strip');
const preview = !!(only || grid || strip), name = path.basename(file, '.html');
const out = path.resolve(flags.get('--out') || path.join(path.dirname(file), 'out'));
mkdirSync(out, {recursive: true});
// Every full render owns a new frame directory. Failed attempts retain their files for diagnosis.
const work = preview ? out : mkdtempSync(path.join(out, `.${name}-render-`));
const frames = path.join(work, `${name}-frames`); mkdirSync(frames, {recursive: true});
const url = pathToFileURL(path.resolve(file)); url.searchParams.set('bare', '1'); url.searchParams.set('frame', '0');
if (flags.has('--ar')) {
  const ar = flags.get('--ar'); if (!/^\d+(?:\.\d+)?[:x/]\d+(?:\.\d+)?$/.test(ar) || ar.split(/[:x/]/).some(v => Number(v) <= 0)) throw new Error('Invalid aspect ratio');
  url.searchParams.set('ar', ar);
}
if (flags.has('--width')) url.searchParams.set('w', integer(flags.get('--width'), 'width', 2));
if (flags.has('--look')) url.searchParams.set('look', flags.get('--look'));
function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  const mac = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'; if (existsSync(mac)) return mac;
  for (const bin of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']) {
    try { return execFileSync('which', [bin], {stdio: ['ignore', 'pipe', 'ignore']}).toString().trim(); } catch {}
  }
  throw new Error('No Chrome found. Set CHROME=/path/to/chrome');
}
const save = (f, data) => writeFileSync(f, Buffer.from(data.split(',')[1], 'base64'));
const ff = argv => execFileSync('ffmpeg', ['-v', 'error', '-y', ...argv], {stdio: 'inherit'});
const browser = await puppeteer.launch({executablePath: findChrome(), headless: true});
let N, fps, size, hasAudio = false;
try {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(url.href, {waitUntil: 'load'});
  await page.waitForFunction('window.__ready === true || window.__error', {timeout: 60000});
  const meta = await page.evaluate(() => ({N: window.__NDRAW, fps: window.__fps, size: window.__size, error: window.__error}));
  ({N, fps, size} = meta);
  if (meta.error || errors.length) throw new Error(meta.error || errors.join('\n'));
  if (!Number.isInteger(N) || N <= 0 || ![12, 24].includes(fps)) throw new Error('Invalid film frame count or fps');
  console.log(`${name}: ${N} frames, ${fps} fps, ${size.w}x${size.h}`);
  if (only?.some(i => i >= N) || strip && strip[0] + strip[1] > N) throw new Error(`Requested frames outside film (0..${N - 1})`);
  if (grid) save(path.join(out, `${name}-grid.jpg`), await page.evaluate(n => window.__grid(n), grid));
  if (strip) save(path.join(out, `${name}-strip-${strip[0]}.jpg`), await page.evaluate(([a, b]) => window.__strip(a, b), strip));
  const list = grid || strip ? [] : only || Array.from({length: N}, (_, i) => i);
  for (const [k, i] of list.entries()) {
    save(path.join(frames, `${String(i).padStart(4, '0')}.png`), await page.evaluate(i => window.__frame(i), i));
    if (errors.length) throw new Error(`Frame ${i}: ${errors.join('\n')}`);
    if (!preview && ((k + 1) % 120 === 0 || k === list.length - 1)) console.log(`Rendered ${k + 1}/${N}`);
  }
  if (!preview) {
    const wav = await page.evaluate(() => window.__wav ? window.__wav() : null);
    if (wav) { writeFileSync(path.join(work, `${name}-score.wav`), Buffer.from(wav, 'base64')); hasAudio = true; }
  }
  if (errors.length) throw new Error(errors.join('\n'));
} catch (e) {
  console.error(`Render failed; diagnostic files: ${work}`); throw e;
} finally { await browser.close(); }
if (!preview) {
  const duration = N / fps, outputFrames = N * 24 / fps, mp4 = path.join(work, `${name}.mp4`);
  ff(['-framerate', String(fps), '-i', path.join(frames, '%04d.png'), '-vf', 'fps=24', '-frames:v', String(outputFrames), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', mp4]);
  ff(['-i', mp4, '-vf', `fps=2,scale=240:-2,tile=6x${Math.max(1, Math.ceil(duration * 2 / 6))}`, '-frames:v', '1', path.join(work, `${name}-contact.jpg`)]);
  if (hasAudio) ff(['-i', mp4, '-i', path.join(work, `${name}-score.wav`), '-map', '0:v:0', '-map', '1:a:0', '-af', 'apad', '-t', String(duration), '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', path.join(work, `${name}-final.mp4`)]);
  writeFileSync(path.join(work, `${name}-render.json`), JSON.stringify({file: path.resolve(file), fps, frames: N, outputFps: 24, outputFrames, duration, size, look: flags.get('--look') || null, audio: hasAudio}, null, 2) + '\n');
  // Publish only completed outputs; remove stale companions only for this exact film.
  for (const suffix of ['-frames', '.mp4', '-contact.jpg', '-score.wav', '-final.mp4', '-render.json']) {
    const dest = path.join(out, name + suffix), src = path.join(work, name + suffix);
    if (suffix === '-frames') rmSync(dest, {recursive: true, force: true});
    if (existsSync(src)) renameSync(src, dest); else rmSync(dest, {force: true});
  }
  rmSync(work, {recursive: true, force: true});
  console.log(`Finished: ${path.join(out, name + (hasAudio ? '-final.mp4' : '.mp4'))}`);
} else console.log(`Preview: ${out}`);
