// The dependency-inversion ratchet. The live layer historically worked by
// REASSIGNING bindings the demo shell declares (`SESS = []`, redefining
// drawList, ...). Every such shared mutable global is one strand of the
// demo<->live coupling the DataSource seam is unwinding, so the count may
// only go DOWN. CI fails when it rises; lowering it means also lowering
// CEILING here in the same change, which is the point: the number is the
// debt, in the diff, every time.
//
//   node ui/scripts/count-shared-globals.mjs         # report + assert
//   node ui/scripts/count-shared-globals.mjs --list  # name every strand
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const CEILING = 24

const src = join(fileURLToPath(new URL('..', import.meta.url)), 'src')
const read = (dir) =>
  readdirSync(join(src, dir))
    .filter((f) => f.endsWith('.js'))
    .sort()
    .map((f) => readFileSync(join(src, dir, f), 'utf8'))
    .join('\n')

const demo = read('demo')
const live = read('live')

// Top-level demo declarations (the demo shell is all top-level statements).
const declared = new Set()
for (const m of demo.matchAll(/^(?:const|let|var)\s+([A-Za-z_$][\w$]*)|^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)) {
  declared.add(m[1] ?? m[2])
}

// Live-side writes to those names: plain reassignment at statement start.
const shared = new Set()
for (const m of live.matchAll(/^\s*([A-Za-z_$][\w$]*)\s*=[^=]/gm)) {
  if (declared.has(m[1])) shared.add(m[1])
}

const names = [...shared].sort()
if (process.argv.includes('--list')) for (const n of names) console.log(n)
console.log(`shared mutable globals (demo-declared, live-assigned): ${names.length} (ceiling ${CEILING})`)
if (names.length > CEILING) {
  console.error('count-shared-globals: the coupling GREW. New code must go through the DataSource seam, not a demo global.')
  process.exit(1)
}
