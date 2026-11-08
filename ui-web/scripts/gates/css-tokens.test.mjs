// Every custom property a stylesheet reads has to be one some stylesheet
// writes.
//
//   var(--accent) where no --accent exists is not an error anywhere: the
//   declaration is simply dropped, so the rule paints nothing and the page
//   looks as though the rule were never written. That is how a translucent
//   highlight over a document page shipped invisible -- the colour gate passed
//   it, because that gate reads only src/styles/page.css and only asks whether
//   a literal went through a token, never whether the token exists.
//
// A reference with a fallback -- var(--x, #fff) -- is deliberate and allowed:
// the author said what happens when it is not set.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const root = join(fileURLToPath(new URL('../..', import.meta.url)), 'src')

/* Properties set from script rather than from a stylesheet, with who sets
   them. A pin here is a claim that something assigns it at runtime, which is
   why each names the file that does. */
const PINNED = {
  // features/desk/DeskSurface.tsx writes these on the surface as it is dragged.
  '--desk-col': 'features/desk/DeskSurface.tsx',
  '--desk-left-row': 'features/desk/DeskSurface.tsx',
  '--desk-right-row': 'features/desk/DeskSurface.tsx',
  // page.css:3306 -- a progress fill, set per element as it moves.
  '--f': 'the element that carries the bar',
  // features/settings/styles.css:287 -- predates this gate and paints nothing;
  // left as found rather than guessed at, because which grey it wanted is the
  // author's to say.
  '--surface-2': 'unset today: a sticky group header that paints no background',
}

function sheets(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out.push(...sheets(path))
    else if (name.endsWith('.css')) out.push(path)
  }
  return out
}

describe('the stylesheets', () => {
  it('read no custom property that nothing defines', () => {
    const files = sheets(root)
    const defined = new Set()
    for (const path of files) {
      for (const hit of readFileSync(path, 'utf8').matchAll(/(--[a-z0-9-]+)\s*:/g)) defined.add(hit[1])
    }

    const loose = []
    for (const path of files) {
      const css = readFileSync(path, 'utf8')
      /* Only references with no fallback: the closing paren right after the
         name is what says nobody wrote what to do without it. */
      for (const hit of css.matchAll(/var\(\s*(--[a-z0-9-]+)\s*\)/g)) {
        const name = hit[1]
        if (defined.has(name) || name in PINNED) continue
        const line = css.slice(0, hit.index).split('\n').length
        loose.push(`${relative(root, path)}:${line} ${name}`)
      }
    }

    expect(loose, 'define the property, use the one that exists, or give var() a fallback').toEqual([])
  })

  it('pins nothing that a stylesheet has since defined', () => {
    const defined = new Set()
    for (const path of sheets(root)) {
      for (const hit of readFileSync(path, 'utf8').matchAll(/(--[a-z0-9-]+)\s*:/g)) defined.add(hit[1])
    }

    expect(Object.keys(PINNED).filter((name) => defined.has(name)), 'take these off PINNED').toEqual([])
  })
})
