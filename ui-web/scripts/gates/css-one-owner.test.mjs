/* A class is defined by one stylesheet, not two.
 *
 * components/ModelPicker.tsx shipped naming `.mpick` -- the class the composer's
 * own picker has owned in styles/page.css for as long as it has existed, where
 * it carries `position: fixed` and a portal's placement. The new component,
 * which its own header says renders in the flow under the row that opened it,
 * silently became a floating layer: 720x560 anchored under a pill, 197px of it
 * below the fold, with the composer's positioning code addressing it.
 *
 * check-class-namespace answers the neighbouring question -- which DOMAIN may
 * name a class -- by reading features/<domain>/manifest.ts, so a file under
 * components/ is page vocabulary to it and a collision with page.css is not
 * something it looks for. This gate is the other half, and it reads the
 * stylesheets rather than the markup: if two sheets both define a rule for the
 * same class, one of them is overriding the other by accident.
 *
 * A sheet CLAIMS a class when it styles it unscoped: the first class of the
 * selector's first compound, which is the element the rule is about.
 * `.mpick {...}` and `.mpick .find {...}` both claim `mpick`; `.mpk-model.opt`
 * claims `mpk-model`, since what follows on a compound is a modifier. What a
 * sheet reaches INSIDE its own container it does not claim -- the settings
 * sheet's `.settings-ppcard .mpk-search` tunes the component where it embeds
 * it, which is the normal way one sheet adjusts another's component, and the
 * component's own generic descendants (`.mpk-prov .c`) are already scoped.
 * Two sheets claiming one name is the fault: one is overriding the other by
 * accident.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { relPath, root } from './paths.mjs'

const repo = root(new URL('../../', import.meta.url))
const src = join(repo, 'src')

function sheets() {
  const found = [join(src, 'styles', 'page.css')]
  const features = join(src, 'features')
  for (const entry of readdirSync(features, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const sheet = join(features, entry.name, 'styles.css')
    try {
      readFileSync(sheet)
      found.push(sheet)
    } catch {
      /* most domains have no stylesheet of their own */
    }
  }
  const components = join(src, 'components')
  for (const entry of readdirSync(components, { withFileTypes: true })) {
    if (entry.isFile() && entry.name.endsWith('.css')) found.push(join(components, entry.name))
  }
  return found
}

/** Every class this stylesheet claims. */
function claimed(css) {
  const out = new Set()
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, ' ')
  for (const match of stripped.matchAll(/([^{}]+)\{[^{}]*\}/g)) {
    const head = match[1].trim()
    if (head.startsWith('@')) continue
    for (const one of head.split(',')) {
      const first = one.trim().split(/\s+|>|\+|~/).filter(Boolean)[0]
      if (!first) continue
      const cls = /\.([a-zA-Z0-9_-]+)/.exec(first)
      if (cls) out.add(cls[1])
    }
  }
  return out
}

describe('every class', () => {
  it('is defined by one stylesheet only', () => {
    const owners = new Map()
    for (const sheet of sheets()) {
      const name = relPath(repo, sheet)
      for (const cls of claimed(readFileSync(sheet, 'utf8'))) {
        owners.set(cls, [...(owners.get(cls) ?? []), name])
      }
    }
    const shared = [...owners]
      .filter(([, where]) => where.length > 1)
      .map(([cls, where]) => `.${cls}: ${where.join(' + ')}`)
      .sort()
    expect(shared).toEqual([])
  })
})
