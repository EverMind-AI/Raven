/* Which agent marks the dark theme filters, and with which filter.
 *
 * Three kinds of file sit in `assets/agents/`, and `data-tone` is what carries
 * the distinction from the map in agent-mark.tsx to this stylesheet:
 *
 * - no tone: every shape carries its own fill. Renders the same in both themes
 *   and comes out as a photographic negative if inverted -- Claude's terracotta
 *   turns cyan. Filter nothing.
 * - `mono`: every shape is `currentColor`, which an <img> resolves against the
 *   SVG's own document rather than the page, so the file renders black and is
 *   invisible on a dark surface until the theme inverts the lot.
 * - `hybrid`: some shapes are and some are not, which is qoder alone. A plain
 *   invert lifts the currentColor half correctly AND takes the brand green with
 *   it (#2ADB5C -> #D524A3, magenta); the hue rotation puts the hue back.
 *
 * So neither filter is a theme preference, each is a per-file correction, and
 * applying the wrong one is silent. Pinned here because no DOM test can see it:
 * happy-dom applies no stylesheet, so `filter` is unobservable there. The
 * markup half -- that the tone reaches the element -- is pinned in
 * agent-mark.test.tsx, and the tone-matches-the-file half in
 * tests/test_ui_agent_marks.py; this is the half that says the stylesheet still
 * reads it, and reads it per tone.
 *
 * The provider marks are the same mechanism with the ratio reversed: those
 * files are all currentColor and one is not, so that block inverts by default
 * and exempts the exception. Agents are mostly colour, so this one filters
 * nothing by default. Neither may become a blanket rule.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')

function rules(pattern) {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, ' ')
  const found = []
  for (const match of stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = match[1].trim()
    if (pattern.test(selector)) found.push([selector, match[2]])
  }
  return found
}

const filtered = () => rules(/\.agent-mark\b/).filter(([, body]) => /filter:\s*invert/.test(body))

describe('the agent mark filters', () => {
  /* Both theme paths, because they are not one rule: an explicit choice stamps
     `data-theme`, and the default setting stamps nothing and leaves only the
     media query. A block written for one and not the other is a mark that is
     correct until the user touches the theme switch. Two tones x two paths. */
  it('filters both tones down both dark paths, and nothing else', () => {
    const all = filtered()
    expect(all.length).toBe(4)
    for (const [selector] of all) expect(selector).toMatch(/\[data-tone="(mono|hybrid)"\]/)
    const explicit = all.filter(([s]) => /\[data-theme="dark"\]/.test(s))
    const system = all.filter(([s]) => /:not\(\[data-theme="light"\]\)/.test(s))
    for (const path of [explicit, system]) {
      expect(path.length).toBe(2)
      expect(path.some(([s]) => /"mono"/.test(s))).toBe(true)
      expect(path.some(([s]) => /"hybrid"/.test(s))).toBe(true)
    }
  })

  /* The hybrid's rotation is the whole reason the tone is not just a boolean.
     Dropping it reads as a simplification and silently recolours a brand. */
  it('rotates the hue for the hybrid and only for the hybrid', () => {
    for (const [selector, body] of filtered()) {
      const rotates = /hue-rotate\(\s*180deg\s*\)/.test(body)
      expect(rotates).toBe(/"hybrid"/.test(selector))
    }
  })

  /* The guard that matters. A filter reaching every mark is the mistake this
     file exists to catch, and it reads as a one-word simplification. */
  it('leaves the marks that carry their own palette alone', () => {
    for (const [selector, body] of rules(/\.agent-mark\b/)) {
      if (/filter:\s*invert/.test(body)) expect(selector).toMatch(/\[data-tone=/)
    }
  })

  /* The tone filters handle a file that says nothing about the theme. Two files
     say plenty: miromind.svg, a favicon with its own prefers-color-scheme rule,
     and raven.svg, which carries one because a black bird on the dark surface
     is a silhouette and no tone filter can recolour a mark that is not one
     colour. An <img>'s SVG evaluates that rule against the embedding element's
     used color-scheme rather than against this page's data-theme. Both
     directions are needed, because the mismatch runs both ways -- a dark OS
     under the app's explicitly light default, and a light OS under a chosen
     dark theme -- and each looks like the mark simply having that colour.
     It must stay on the tile: on :root it would hand every native scrollbar
     and form control to the dark palette too, which no test here would see. */
  it('pins the embedded scheme to the chosen theme, on the tile', () => {
    const scoped = rules(/\.agent-mark\b/).filter(([, body]) => /color-scheme:/.test(body))
    expect(scoped.length).toBe(2)
    for (const [selector] of scoped) expect(selector).toMatch(/\.agent-mark img$/)
    expect(scoped.some(([s, b]) => /\[data-theme="dark"\]/.test(s) && /color-scheme:\s*dark/.test(b))).toBe(true)
    expect(scoped.some(([s, b]) => /\[data-theme="light"\]/.test(s) && /color-scheme:\s*light/.test(b))).toBe(true)
    expect(rules(/^:root$/).some(([, body]) => /color-scheme:/.test(body))).toBe(false)
  })

  /* One column for a brand mark and for the generic glyph, whatever each is
     made of. Without a fixed slot the roster starts its names at two different
     x positions depending on whether a row has a preset behind it. */
  it('keeps the slot one fixed size for both shapes', () => {
    const slot = rules(/^\.agent-mark$/)[0]?.[1]
    expect(slot).toBeTruthy()
    expect(/width:\s*\d/.test(slot)).toBe(true)
    expect(/flex:\s*none/.test(slot)).toBe(true)
  })

  /* A brand mark and a letter tile are alternatives in one slot, and a slot
     that sizes the tile has to size the mark to the same box. The roster size
     above is a default, not a geometry every list agrees on: the sub-agents
     card sizes its tile to 46px and the row that opens it to 28px, so a mark
     left at its default came out smaller in the header than in the row. The
     mismatch is silent -- nothing errors, the mark simply renders small -- and
     no DOM test can see it, because happy-dom applies no stylesheet.

     The expected box is read off the tile's own rule rather than restated, so
     a slot whose tile is resized takes its mark with it. The two slots are
     named because that is a fact about the markup rather than the stylesheet:
     these are the containers where an agent mark and a letter tile share one
     cell. An agent mark drawn in a container that sizes no tile (the desk's
     roster head, an instance row) keeps the default and belongs to neither. */
  const SHARED_SLOTS = ['pmdhead', 'surow']

  it('sizes an agent mark to the tile it stands in for, in every shared slot', () => {
    const boxOf = (body) => ({
      width: /(?:^|[;{\s])width:\s*([\d.]+)px/.exec(body)?.[1],
      height: /(?:^|[;{\s])height:\s*([\d.]+)px/.exec(body)?.[1],
      radius: /border-radius:\s*([\d.]+)px/.exec(body)?.[1],
    })
    for (const slot of SHARED_SLOTS) {
      const tile = rules(new RegExp(`^\\.${slot}\\s*>?\\s*\\.pmtile$`))
      const mark = rules(new RegExp(`^\\.${slot}\\s*>?\\s*\\.agent-mark$`))
      expect(tile.length, `.${slot} sizes a letter tile`).toBe(1)
      expect(mark.length, `.${slot} sizes an agent mark too`).toBe(1)
      const want = boxOf(tile[0][1])
      const got = boxOf(mark[0][1])
      /* Read, not assumed: a tile rule that stopped declaring a box would make
         every comparison below pass on a pair of undefineds. */
      expect(want.width, `.${slot} tile declares a width`).toBeTruthy()
      expect(want.radius, `.${slot} tile declares a radius`).toBeTruthy()
      expect(got.width, `.${slot} mark width`).toBe(want.width)
      expect(got.height, `.${slot} mark height`).toBe(want.height)
      expect(got.radius, `.${slot} mark radius`).toBe(want.radius)
    }
  })
})
