// Delivery renderer: parallel Chrome workers pipe frames straight into ffmpeg,
// so a 4K film never lands on disk as thousands of PNGs. Each worker encodes one
// stretch of the timeline; the stretches are joined losslessly and the score is muxed in.
// The closing frame is also written once at the very start, so players and feeds show it as the cover;
// the score is delayed by that one frame to stay in sync. --no-cover leaves it out.
// Usage: node render-hq.mjs raven.html [--width 3840] [--fps 60] [--workers 4] [--frames N] [--out out]
import puppeteer from 'puppeteer-core';
import {spawn, execFileSync} from 'node:child_process';
import {mkdirSync, writeFileSync, rmSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const args = process.argv.slice(2), opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const cover = !args.includes('--no-cover'), file = args[0], width = +opt('--width', 3840), workers = +opt('--workers', 4), limit = opt('--frames'), out = path.resolve(opt('--out', 'out'));
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const url = pathToFileURL(path.resolve(file)); url.searchParams.set('bare', '1'); url.searchParams.set('w', String(width));
if (opt('--fps')) url.searchParams.set('fps', opt('--fps'));

async function openPage() {
  const browser = await puppeteer.launch({executablePath: CHROME, headless: true, protocolTimeout: 0});
  const page = await browser.newPage(), errors = []; page.on('pageerror', e => errors.push(String(e)));
  await page.goto(url.href, {waitUntil: 'load'}); await page.waitForFunction('window.__ready === true', {timeout: 120000});
  if (errors.length) throw new Error(errors.join('\n'));
  return {browser, page, errors};
}
const probe = await openPage();
const {N: all, fps, size} = await probe.page.evaluate(() => ({N: window.__NDRAW, fps: window.__fps, size: window.__size}));
const N = limit ? Math.min(all, +limit) : all, tag = `${path.basename(file, '.html')}-${size.h}p${fps}`, work = path.join(out, `.${tag}`);
mkdirSync(work, {recursive: true});
const wav = !limit && await probe.page.evaluate(() => window.__wav ? window.__wav() : null); await probe.browser.close();
if (wav) writeFileSync(path.join(work, 'score.wav'), Buffer.from(wav, 'base64'));
console.log(`${tag}: ${N} frames at ${fps} fps, ${size.w}x${size.h}, ${workers} workers`);

let done = 0; const t0 = Date.now();
async function worker(k) {
  const a = Math.floor(N * k / workers), z = Math.floor(N * (k + 1) / workers), seg = path.join(work, `seg${k}.mp4`);
  const ff = spawn('ffmpeg', ['-v', 'error', '-y', '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'png', '-i', '-',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '16', '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-r', String(fps), seg], {stdio: ['pipe', 'inherit', 'inherit']});
  const {browser, page, errors} = await openPage();
  if (k === 0 && cover) ff.stdin.write(Buffer.from((await page.evaluate(i => window.__frame(i), all - 1)).split(',')[1], 'base64'));
  for (let i = a; i < z; i++) {
    const data = await page.evaluate(i => window.__frame(i), i);
    if (errors.length) throw new Error(`frame ${i}: ${errors.join('\n')}`);
    if (!ff.stdin.write(Buffer.from(data.split(',')[1], 'base64'))) await new Promise(r => ff.stdin.once('drain', r));
    if (++done % 96 === 0 || done === N) { const s = (Date.now() - t0) / 1000; console.log(`${done}/${N}  ${(done / s).toFixed(2)} frames/s  eta ${Math.round((N - done) / (done / s) / 60)} min`); }
  }
  ff.stdin.end(); await new Promise((res, rej) => ff.on('close', c => c ? rej(new Error('ffmpeg exited ' + c)) : res())); await browser.close();
  return seg;
}
const segs = await Promise.all(Array.from({length: workers}, (_, k) => worker(k)));
writeFileSync(path.join(work, 'list.txt'), segs.map(s => `file '${s}'`).join('\n'));
const silent = path.join(out, `${tag}${limit ? '-test' : ''}-silent.mp4`), final = path.join(out, `${tag}.mp4`);
execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', path.join(work, 'list.txt'), '-c', 'copy', silent], {stdio: 'inherit'});
if (wav) { execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', silent, '-i', path.join(work, 'score.wav'), '-map', '0:v', '-map', '1:a', '-c:v', 'copy', ...(cover ? ['-af', `adelay=${(1000 / fps).toFixed(3)}:all=1`] : []), '-c:a', 'aac', '-b:a', '256k', '-t', String((N + (cover ? 1 : 0)) / fps), final], {stdio: 'inherit'}); rmSync(silent); }
rmSync(work, {recursive: true, force: true});
console.log(`Finished: ${wav ? final : silent} in ${((Date.now() - t0) / 60000).toFixed(1)} min`);
