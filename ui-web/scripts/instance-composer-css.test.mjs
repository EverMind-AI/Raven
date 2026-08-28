/* A sub-agent's composer is the page's own composer, one size down.
 *
 * The two share their markup's class names, so `.dock-in` gives both the glass,
 * the blur, the rim light and the border, and neither can drift from the other
 * on any of it. What `.instance-dock` is allowed to restate is the part that
 * genuinely differs inside a pane: how big the card is and how hard it casts.
 * Anything else appearing there is the two composers starting to diverge again,
 * which no DOM test can see -- the stylesheet is the only place it is written.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')

/* Every declaration under a selector that scopes into `.instance-dock`, as
   (selector, property), from the base rules and the pane's own container
   queries alike.
   Comments come out first. What sits between two rules is captured whole as
   the next one's "selector", prose included, so a comment that says
   `.instance-dock .go svg` -- this file's own explanation of the defect, were
   it ever copied into the stylesheet -- would read as a selector that is not
   there, and the `svg` guard below would fail on a sentence. */
function instanceDockDecls(source = css) {
  const out = []
  for (const rule of source.replace(/\/\*[\s\S]*?\*\//g, ' ').matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = rule[1].trim()
    if (!selector.includes('.instance-dock')) continue
    /* The dock's own placement is not part of the card. */
    if (/\.instance-dock(\.sasend)?$/.test(selector)) continue
    for (const decl of rule[2].split(';')) {
      const name = decl.split(':')[0]?.trim()
      if (name) out.push({ selector, name })
    }
  }
  return out
}

const instanceDockProps = () => new Set(instanceDockDecls().map((d) => d.name))

/* What "one size down and one step quieter" is allowed to say. Exactly what is
   there and nothing spare: a name left on this list after the rule that needed
   it went is a hole aimed at whatever that rule used to do. `cursor`, `opacity`
   and the two `stroke-*` caps sat here after the `.go svg` and `.go:disabled`
   overrides were deleted, which would have let both be pasted straight back. */
const ALLOWED = new Set([
  /* the card */
  'max-width', 'padding', 'border-radius', 'box-shadow',
  /* the writing line, and the send disc at the pane's narrow tier */
  'min-height', 'max-height', 'width', 'height',
  /* the one bar under it */
  'justify-content', 'margin-top',
  /* the failure line. The queued rows are NOT in here: `.instance-queue` and
     `.instance-qrow` are siblings of `.instance-dock` rather than descendants,
     so the collector never sees them. */
  'color', 'font-size', 'overflow', 'text-overflow', 'white-space',
])

describe('the sub-agent composer against the page composer', () => {
  it('restates only its size and its shadow, never the shared material', () => {
    const restated = [...instanceDockProps()].filter((name) => !ALLOWED.has(name)).sort()

    expect(restated).toEqual([])
    /* The guard is worth something only if the block is actually there. */
    expect(instanceDockProps().size).toBeGreaterThan(3)
  })

  it('leaves the send glyph to the component that draws it', () => {
    /* The other half, which a property list cannot reach: `width` and `height`
       have to stay allowed for the disc itself at the pane's narrow tier, so
       `.instance-dock .go svg { width: 20px; height: 20px }` -- the original
       defect, the arrow a size and a weight off the page's -- would pass on
       names alone. The glyph is the component's; the stylesheet has no business
       inside it. */
    const onGlyph = instanceDockDecls()
      .filter((decl) => /\bsvg\b/.test(decl.selector))
      .map((decl) => `${decl.selector} { ${decl.name} }`)

    expect(onGlyph).toEqual([])
  })

  it('reads rules, not the prose around them', () => {
    /* A stylesheet comment is not a rule. Both halves of the guard read the
       captured selector, so a comment mentioning either name would invent a
       declaration nobody wrote: the property list would report a spare name
       and the `svg` filter would report a rule, each on a sentence. */
    const stylesheet = [
      '/* .instance-dock .go svg is the arrow the component draws. */',
      '.somewhere-else { position: absolute }',
      '.instance-dock { padding: 10px 12px }',
    ].join('\n')

    expect(instanceDockDecls(stylesheet)).toEqual([])
  })

  it('takes the glass from the shared card rather than its own copy', () => {
    /* The four declarations that make the card a material. A sub-agent's
       composer must inherit every one of them: restated, they are what would
       drift. */
    const shared = css.match(/\n\.dock-in \{[^}]*\}/)
    expect(shared).toBeTruthy()
    for (const prop of ['backdrop-filter', 'background', 'border', '--rim']) {
      expect(shared[0]).toContain(prop)
    }
    const own = instanceDockProps()
    for (const prop of ['backdrop-filter', '-webkit-backdrop-filter', 'background', 'border', '--rim']) {
      expect(own.has(prop)).toBe(false)
    }
  })

  it('sits smaller at rest than the composer it copies', () => {
    const page = css.match(/\ntextarea \{[^}]*min-height: (\d+)px/)
    const pane = css.match(/\.instance-dock \.field textarea \{[^}]*min-height: (\d+)px/)
    expect(page).toBeTruthy()
    expect(pane).toBeTruthy()
    expect(Number(pane[1])).toBeLessThan(Number(page[1]))
  })
})
