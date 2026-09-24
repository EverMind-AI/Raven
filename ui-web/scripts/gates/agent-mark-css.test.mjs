/* What the dark theme does to an agent mark: nothing to the mark, and only the
 * plate under it changes.
 *
 * Three kinds of file sit in `assets/agents/`, and `data-tone` carries the
 * distinction from the map in AgentMark.tsx: no tone (own fills), `mono`
 * (every shape `currentColor`, so the file draws black) and `hybrid` (qoder:
 * brand green beside one currentColor tone). The dark theme used to correct
 * each kind with a filter -- invert the mono files, invert and hue-rotate the
 * hybrid -- and each correction was a guess about the file that went wrong
 * silently: hermesagent.svg is an illustration and came out as its negative,
 * and qoder's green came back as a different green (#2ADB5C -> #008203).
 *
 * So every mark sits on a light plate in both themes, as the app icon does,
 * and the plate is what the dark theme dims. Pinned here because no DOM test
 * can see it: happy-dom applies no stylesheet, so `filter`, `background` and
 * `color-scheme` are unobservable there. The markup half -- that the tone
 * reaches the element -- is pinned in AgentMark.test.tsx, and the
 * tone-matches-the-file half in tests/test_ui_agent_marks.py.
 *
 * The provider marks are a different mechanism (all currentColor, inverted by
 * default with one exemption) and are not read here.
 */

import { describe, expect, it } from 'vitest'

import { rules } from './css.mjs'

describe('the agent mark plate', () => {
  /* A filter reaching a mark is the mistake this file exists to catch, and it
     reads as a one-line fix for one mark. */
  it('filters no mark, in either theme path', () => {
    for (const [selector, body] of rules(/\.agent-mark\b/)) {
      expect(body, selector).not.toMatch(/filter:/)
    }
  })

  /* The plate is a token of its own, not the theme's surface: `--surface` goes
     dark with the theme, and a brand mark on it needs the recolouring this file
     forbids. Both dark paths -- an explicit choice stamps `data-theme`, the
     default setting stamps nothing and leaves only the media query -- and the
     explicit light path, which has to win back the light plate on a dark OS. */
  it('draws every tile on the plate token, which each theme block declares', () => {
    const tile = rules(/^\.agent-mark$/)[0]?.[1]
    expect(tile).toBeTruthy()
    expect(tile).toMatch(/background:\s*var\(--mark-plate\)/)
    expect(tile).toMatch(/border:[^;]*var\(--mark-plate-line\)/)
    const declares = (selector) => rules(selector).some(([, body]) => /--mark-plate:\s*#/.test(body))
    expect(declares(/^:root$/)).toBe(true)
    expect(declares(/^:root\[data-theme="dark"\]$/)).toBe(true)
    expect(declares(/^:root\[data-theme="light"\]$/)).toBe(true)
  })

  /* raven.svg and miromind.svg answer prefers-color-scheme from inside the
     file, which an <img>'s SVG evaluates against the embedding element's used
     color-scheme rather than this page's data-theme. On a light plate each has
     to draw its light self under either theme and either OS, so the scheme is
     pinned light on the tile, unconditionally. It must stay on the tile: on
     :root it would hand every native scrollbar and form control to one palette
     too, which no test here would see. */
  it('pins the embedded scheme light, on the tile, in both themes', () => {
    const scoped = rules(/\.agent-mark\b/).filter(([, body]) => /color-scheme:/.test(body))
    expect(scoped.length).toBe(1)
    expect(scoped[0][0]).toBe('.agent-mark img')
    expect(scoped[0][1]).toMatch(/color-scheme:\s*light/)
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

  /* A slot that overrides the roster size has to declare a whole box. That
     size is a default, not a geometry every list agrees on: the sub-agents card
     draws its mark at 46px and the row that opens it at 28px, so a mark left at
     its default came out smaller in the header than in the row. The mismatch is
     silent -- nothing errors, the mark simply renders small -- and no DOM test
     can see it, because happy-dom applies no stylesheet.

     These two slots used to size a letter tile as well, and the box was read
     off the tile's rule rather than restated here. The tile is gone, so the
     mark's own rule is the box, and what is left to hold is that each slot
     declares all three of width, height and radius: a rule that drops one falls
     back to the default for that property alone, which is the same silent
     half-size. An agent mark in a container that overrides nothing (the desk's
     roster head, an instance row) keeps the default and is not read here.

     The slots are derived from the stylesheet rather than listed, so a
     container added later is held to the same rule without this file being
     edited -- a hand-written list is a claim about a set that nothing checks. */
  const SIZED_SLOTS = rules(/^\.[\w-]+\s*>\s*\.agent-mark$/)

  it('gives an agent mark a whole box in every slot that resizes it', () => {
    const boxOf = (body) => ({
      width: /(?:^|[;{\s])width:\s*([\d.]+)px/.exec(body)?.[1],
      height: /(?:^|[;{\s])height:\s*([\d.]+)px/.exec(body)?.[1],
      radius: /border-radius:\s*([\d.]+)px/.exec(body)?.[1],
    })
    /* Read, not assumed: a stylesheet that stopped sizing the mark anywhere
       would make the loop below pass over an empty set. */
    expect(SIZED_SLOTS.length, 'some container sizes an agent mark').toBeGreaterThan(0)
    for (const [slot, body] of SIZED_SLOTS) {
      const got = boxOf(body)
      expect(got.width, `${slot} declares a width`).toBeTruthy()
      expect(got.height, `${slot} declares a height`).toBeTruthy()
      expect(got.radius, `${slot} declares a radius`).toBeTruthy()
      expect(got.height, `${slot} is square`).toBe(got.width)
    }
  })
})
