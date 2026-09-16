/* The guard the pre-conversion analysis scripts share.
 *
 * Most of the scripts in this directory measure the layers as ONE
 * concatenated text, with the live half wrapped in a single IIFE -- the shape
 * src/legacy/ had before the module conversion. They are kept as the evidence
 * the conversion was planned from, not as tools for the tree as it stands, and
 * against the modules they would either crash or answer nonsense. So they say
 * which revision they apply to instead.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const BEFORE = 'the commit before "turn the concatenated page script into es modules"'

export function requireConcatEra(src) {
  const first = readFileSync(join(src, 'live', '010-boot-guard.js'), 'utf8')
  // The definitive marker of the module era, rather than the IIFE opener --
  // that part still carries a nested IIFE inside its install().
  if (!first.includes('export function install()')) return
  console.error(
    'This script reads the legacy layers as one concatenated script with the live\n'
    + 'half inside one IIFE. src/legacy/ is ES modules now, so run it against\n'
    + `${BEFORE},\nor use scripts/legacy-shape.test.mjs and scripts/legacy-undef.test.mjs, which\n`
    + 'read the module graph.',
  )
  process.exit(2)
}
