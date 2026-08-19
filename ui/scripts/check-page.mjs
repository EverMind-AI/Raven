// Assembly gate for the served page. Zero dependencies by design: it must be
// runnable in CI straight after `python3 ui/build.py`, before any npm install.
//
//   node ui/scripts/check-page.mjs
//
// Checks, in order:
//   1. dist/index.html exists and carries exactly one <style> and one <script>;
//   2. no assembly marker survived into the artifact (a leftover marker means
//      build.py replaced the wrong thing or the source lost one);
//   3. the script payload parses as a whole -- the live parts are fragments of
//      one IIFE, so only the assembled artifact is checkable, and this is the
//      earliest point a mis-ordered or truncated part would surface.
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
if (count(/<script>/g) !== 1 || count(/<\/script>/g) !== 1) fail('expected exactly one <script> block')

for (const marker of ['/*__STYLE__*/', '/*__DEMO__*/', '/*__I18N__*/']) {
  if (html.includes(marker)) fail(`assembly marker ${marker} survived into dist/index.html`)
}

const script = html.slice(html.indexOf('<script>') + '<script>'.length, html.indexOf('</script>'))
const dir = mkdtempSync(join(tmpdir(), 'raven-page-'))
try {
  const file = join(dir, 'page-script.js')
  writeFileSync(file, script)
  const res = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' })
  if (res.status !== 0) fail(`assembled script does not parse:\n${res.stderr}`)
} finally {
  rmSync(dir, { recursive: true, force: true })
}

console.log(`check-page: OK (${html.length.toLocaleString('en-US')} bytes, script parses)`)
