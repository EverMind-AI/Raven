// @vitest-environment happy-dom
/* The More group as the page root renders it.
 *
 * What the rows are and what they open is state/navfly.test.ts's; the shape of
 * the group is the region golden's and both boot goldens'. What is here is what
 * neither can see: that a row's glyph really is drawn for the page it names,
 * and that the two values on these elements which React renders but does not
 * own -- the fold's `data-open` and each row's `aria-current` -- survive a
 * re-render, because React diffs against the props it rendered last rather than
 * against the document.
 */
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import * as lang from '../state/lang'
import * as navfly from '../state/navfly'
import { mountPageRoot } from '../test/pageRoot'

/* The source with its block comments dropped: the modules below name the
   attribute in their own prose, and what is asked here is who renders it. */
const code = (rel: string): string =>
  (readFileSync(`src/${rel}`, 'utf8') as string).replace(/\/\*[\s\S]*?\*\//g, '')

const fly = (): HTMLElement => document.getElementById('moreFly') as HTMLElement
const rows = (): HTMLElement[] => [...fly().querySelectorAll<HTMLElement>('.navi')]
const marks = (): Array<string | null> => rows().map((b) => b.getAttribute('aria-current'))
const names = (): Array<string | null> => rows().map((b) => b.querySelector('.nm')?.textContent ?? null)

let unmount = (): void => {}

beforeEach(() => {
  navfly._resetForTests()
  document.body.innerHTML = ''
  unmount = mountPageRoot()
})

afterEach(() => {
  unmount()
  unmount = () => {}
  document.body.innerHTML = ''
})

describe('the more flyout', () => {
  it('hands the group over empty, and draws one row per module once asked', () => {
    expect(fly().childNodes).toHaveLength(0)
    navfly.draw()
    expect(rows()).toHaveLength(navfly.MORE_ROWS.length)
  })

  /* Per row, not once: a glyph table missing an entry for a page renders that
     row with an empty box, which a count of the rows cannot see. */
  it('draws the glyph its page is named by, inside one box', () => {
    navfly.draw()
    rows().forEach((b, i) => {
      const page = navfly.MORE_ROWS[i]?.page
      expect(b.children, page).toHaveLength(2)
      const svg = b.children[0] as SVGElement
      expect(svg.tagName.toLowerCase(), page).toBe('svg')
      expect(svg.getAttribute('viewBox'), page).toBe('0 0 24 24')
      expect(svg.children.length, page).toBeGreaterThan(0)
    })
  })

  /* An attribute React renders as a constant leaves no trace: it diffs against
     the props it rendered last, so adding aria-current="false" here beside the
     mark the store writes leaves every case in the suite green (measured). The
     count that matters is therefore over the source, the way the new-task row's
     one handler is counted in Rail.test.tsx. */
  it('renders no mark of its own: the rows aria-current has one writer', () => {
    expect(code('chrome/MoreFly.tsx')).not.toMatch(/aria-current/)
    expect(code('state/navfly.ts')).toMatch(/aria-current/)
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. */
describe('the more flyout once a language is applied', () => {
  it('renders the applied words, and leaves the mark and the fold alone', () => {
    document.getElementById('connectionsPage')!.dataset.open = 'true'
    navfly.draw()
    /* The state the rows are in before the flip: the middle page is the one
       that is up, and the group has been unfolded by its own writer. */
    expect(marks()).toEqual(['false', 'true', 'false'])
    fly().dataset.open = 'true'
    const served = names()
    expect(served.every((n) => !!n && !n.startsWith('gui.'))).toBe(true)

    lang.set('zh')

    expect(names()).not.toEqual(served)
    expect(marks()).toEqual(['false', 'true', 'false'])
    expect(fly().dataset.open).toBe('true')
  })
})
