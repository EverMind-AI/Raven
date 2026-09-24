/* Every logo the provider maps name must actually be in the bundle.
 *
 * `ProviderMark.tsx` resolves a name to `assets/providers/<icon>.svg` without
 * being able to say whether that file exists -- the request decides, and a miss
 * degrades to an initial rather than to a broken image. That degradation is
 * deliberate, and it is also what makes a typo invisible: the row looks exactly
 * like a vendor nobody has drawn yet. So the two halves are compared here,
 * where the filesystem can answer.
 *
 * Both directions matter. A map value with no file is a logo that silently
 * never draws; a file no map names is dead weight in the wheel, and a vendor
 * whose asset was added while its line was forgotten reads as the first case
 * from the UI and the second from the tree.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src')
const MARK = readFileSync(join(SRC, 'components', 'ProviderMark.tsx'), 'utf8')

const body = (name, open, close) => {
  const start = MARK.indexOf(name)
  if (start < 0) throw new Error(`${name} is gone from ProviderMark.tsx`)
  const from = MARK.indexOf(open, start)
  const to = MARK.indexOf(`\n${close}`, from)
  return MARK.slice(from, to)
}

/* Values only. The keys are a provider slug, a vendor namespace and a regexp
   respectively, and none of those names a file. */
const mapValues = (name) => [...body(name, '{', '}').matchAll(/:\s*'([a-z0-9-]+)'/g)].map((m) => m[1])
const patternValues = () => [...body('VENDOR_BY_NAME', '[', ']').matchAll(/,\s*'([a-z0-9-]+)'\]/g)].map((m) => m[1])

const named = new Set([...mapValues('const ICONS'), ...mapValues('const VENDOR_ICONS'), ...patternValues()])
/* A vendor's dark drawing is named by the pair set rather than by an icon map,
   so it is folded in here under the one spelling the component builds. */
const pairBlock = MARK.match(/const DARK_PAIRED = new Set\(\[([\s\S]*?)\]\)/)
if (!pairBlock) throw new Error('DARK_PAIRED is gone from ProviderMark.tsx')
const paired = [...pairBlock[1].matchAll(/'([a-z0-9-]+)'/g)].map((m) => `${m[1]}-dark`)
paired.forEach((name) => named.add(name))
const onDisk = new Set(readdirSync(join(SRC, 'assets', 'providers')).map((f) => f.replace(/\.svg$/, '')))

describe('provider logo assets', () => {
  it('ships a file for every logo the maps name', () => {
    expect([...named].filter((icon) => !onDisk.has(icon)).sort()).toEqual([])
  })

  it('names every file it ships', () => {
    expect([...onDisk].filter((icon) => !named.has(icon)).sort()).toEqual([])
  })

  it('reads a plausible number of them, so a broken parse cannot pass empty', () => {
    /* Both assertions above are satisfied by two empty sets, which is what a
       renamed map or a changed brace style would produce. */
    expect(named.size).toBeGreaterThan(30)
    expect(onDisk.size).toBeGreaterThan(30)
  })
})
