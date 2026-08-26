// @vitest-environment happy-dom
/* The page has two kinds of caller for the same glyph -- one builds SVG by
 * hand, one renders it -- and a glyph that is built twice is a glyph that
 * drifts. These hold the pairs together at the attributes, which is where the
 * drift showed: the send arrow was 20px at stroke 1.8 in a sub-agent's
 * composer against 14px at 2.4 in the page's, the same path rendered as a
 * visibly different button.
 */

import { createRoot } from 'react-dom/client'
import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { CHEVRON_DOWN, Glyph, SendGlyph, ico } from './ico'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => { root.unmount() })
  document.body.innerHTML = ''
})

/* Every attribute both renderings carry, so a difference in any of them fails
   rather than only the ones a test happened to name. */
const shape = (svg: SVGSVGElement | Element): Record<string, string> => {
  const out: Record<string, string> = {}
  for (const attr of [...svg.attributes]) out[attr.name] = attr.value
  out['[path]'] = svg.querySelector('path')?.getAttribute('d') || ''
  return out
}

describe('the glyphs the page draws two ways', () => {
  it('renders a chevron identically whichever caller asks', () => {
    act(() => { root.render(<Glyph d={CHEVRON_DOWN} />) })
    const rendered = host.firstElementChild as Element
    const built = ico(CHEVRON_DOWN)

    expect(shape(rendered)).toEqual(shape(built))
  })

  it('carries the class through both, so a caller can style either', () => {
    act(() => { root.render(<Glyph d={CHEVRON_DOWN} cls="cv" />) })

    expect((host.firstElementChild as Element).getAttribute('class')).toBe('cv')
    expect(ico(CHEVRON_DOWN, 'cv').getAttribute('class')).toBe('cv')
  })

  it('gives a sub-agent composer the page composer send button, to the attribute', async () => {
    act(() => { root.render(<SendGlyph />) })
    const rendered = host.firstElementChild as Element

    /* The shipped string itself, not a copy of it here: a copy would agree
       with the test while the button drifted. */
    const { ICON_SEND } = await import('../features/composer/store')
    const built = document.createElement('div')
    built.innerHTML = ICON_SEND

    expect(shape(rendered)).toEqual(shape(built.firstElementChild as Element))
    /* Written out rather than compared against `SEND`, which both sides now
       read: shared, the equality above cannot notice the path changing, so the
       arrow the page ships is pinned here as a literal. */
    expect(shape(rendered)['[path]']).toBe('M5 12h13M12 5l7 7-7 7')
  })
})
