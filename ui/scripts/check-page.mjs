// Assembly gate for the served page. Zero dependencies by design: it must be
// runnable in CI straight after `python3 ui/build.py`, before any npm install.
//
//   node ui/scripts/check-page.mjs
//
// Checks, in order:
//   1. dist/index.html exists and carries exactly one <style> and exactly two
//      inline <script> blocks -- the island bundle, then the page script;
//   2. no assembly marker survived into the artifact (a leftover marker means
//      build.py replaced the wrong thing or the source lost one);
//   3. every script payload parses as a whole -- the live parts are fragments
//      of one IIFE, so only the assembled artifact is checkable, and this is
//      the earliest point a mis-ordered or truncated part would surface.
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dist = join(fileURLToPath(new URL('..', import.meta.url)), 'dist', 'index.html')
const html = readFileSync(dist, 'utf8')

const fail = (msg) => { console.error(`check-page: ${msg}`); process.exit(1) }

const count = (re) => (html.match(re) ?? []).length
if (count(/<style>/g) !== 1 || count(/<\/style>/g) !== 1) fail('expected exactly one <style> block')
// Two, not one, since the first feature left the concatenated script: the
// island bundle rides its own tag ahead of the page script. Still a fixed
// count -- an unexpected third block is as wrong as zero -- and still all
// inline (no src= anywhere) so the one-file contract holds. Tags are
// matched at line start only: the island bundle legitimately carries the
// string `<script>` inside a template literal, and that is content.
if (count(/^<script>$/gm) !== 2 || count(/^<\/script>$/gm) !== 2) fail('expected exactly two <script> blocks')
if (/<script\s+[^>]*src\s*=/.test(html)) fail('external script reference breaks the one-file contract')

for (const marker of ['/*__STYLE__*/', '/*__MODERN__*/', '/*__DEMO__*/', '/*__I18N__*/']) {
  if (html.includes(marker)) fail(`assembly marker ${marker} survived into dist/index.html`)
}

const scripts = [...html.matchAll(/^<script>\n([\s\S]*?)\n<\/script>$/gm)].map((m) => m[1])
if (scripts.length !== 2) fail(`extracted ${scripts.length} script payloads, expected 2`)
const dir = mkdtempSync(join(tmpdir(), 'raven-page-'))
try {
  scripts.forEach((script, i) => {
    // `node --check` validates JS; these two validate the HTML embedding: a
    // raw closer would end the block early, and `<!--` flips the parser into
    // escaped script-data where the real closer stops closing.
    if (/<\/script/.test(script)) fail(`script ${i} contains a raw </script closer`)
    if (script.includes('<!--')) fail(`script ${i} contains <!--, which breaks inline script parsing`)
    const file = join(dir, `page-script-${i}.js`)
    writeFileSync(file, script)
    const res = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' })
    if (res.status !== 0) fail(`assembled script ${i} does not parse:\n${res.stderr}`)
  })
} finally {
  rmSync(dir, { recursive: true, force: true })
}

console.log(`check-page: OK (${html.length.toLocaleString('en-US')} bytes, ${scripts.length} scripts parse)`)
