// Every state the session row's tail slot can hold must have a visible mark,
// read straight off the stylesheet. jsdom does no layout and computes no custom
// properties, so the component test can see `data-sig` and the ARIA label and
// nothing about whether the reader is shown anything at all: `ask` shipped with
// geometry and no colour, which renders a transparent 6px dot over a timestamp
// the same selector hides.
//
// The set is DERIVED from the component rather than listed here. Listing it
// would repeat the omission for the next state added -- the whole defect was a
// state reaching the tail slot with nobody having written its mark.
import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')
const page = readFileSync(new URL('../src/features/rail/RailPage.tsx', import.meta.url), 'utf8')

/* The `tail` line is the producer: whatever it admits is what can reach the
   slot. Anchored on `const tail =` so an unrelated comparison elsewhere in the
   file cannot widen the set. */
const sigs = () => {
  const line = page.split('\n').find((l) => l.includes('const tail ='))
  if (!line) throw new Error('no `const tail =` in RailPage.tsx')
  const found = [...line.matchAll(/live === '([a-z]+)'/g)].map((m) => m[1])
  if (!found.length) throw new Error(`no states in: ${line.trim()}`)
  return found
}

const rule = (sig) => {
  const at = css.indexOf(`.sess .w[data-sig="${sig}"] i`)
  if (at < 0) return null
  return css.slice(at, css.indexOf('}', at))
}

describe('the session row tail marks', () => {
  it('admits at least the four states the rail draws', () => {
    /* A guard on the derivation itself: a regex that matched nothing would make
       every assertion below vacuous. */
    expect(sigs()).toEqual(expect.arrayContaining(['run', 'done', 'err', 'ask']))
  })

  it('gives every one of them a mark that paints', () => {
    for (const sig of sigs()) {
      const body = rule(sig)
      expect(body, `no .sess .w[data-sig="${sig}"] i rule`).toBeTruthy()
      /* Geometry is shared by the bare `[data-sig] i` rule, so what a state has
         to bring is something that puts ink on those 6 pixels. A fill or a ring
         both count; neither being present is the bug. */
      expect(body, `${sig} has no fill and no ring`).toMatch(/background:|box-shadow:/)
    }
  })

  it('tells the four apart by more than a hue', () => {
    /* `run` a bare disc, `done` a bare ring, `err` a bare disc in another
       colour, `ask` a disc inside a halo. The pair that would be confusable at a
       glance is run/err, and those two are already separated by the column they
       report in -- see the comment above the `err` rule. */
    expect(rule('run')).toMatch(/background:/)
    expect(rule('run')).not.toMatch(/box-shadow:/)
    expect(rule('done')).toMatch(/box-shadow:/)
    expect(rule('done')).not.toMatch(/background:/)
    expect(rule('ask')).toMatch(/background:/)
    expect(rule('ask')).toMatch(/box-shadow:/)
  })

  it('paints ask from a token every theme defines', () => {
    /* An incompletely defined token is the same bug wearing a rule: the mark
       would be transparent in whichever theme forgot it. Four blocks carry the
       palette -- the light root, the dark media query, and one per explicit
       data-theme. */
    const token = (rule('ask').match(/var\(--([a-z-]+)\)/) || [])[1]
    expect(token).toBeTruthy()
    expect(css.match(new RegExp(`--${token}:`, 'g')) || []).toHaveLength(4)
  })
})
