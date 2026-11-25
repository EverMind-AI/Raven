/* The page's quiet text stays readable on the grounds it is set on.
 *
 * Three greys carry the text a reader scans rather than reads -- the steps of a
 * turn (--chat-quiet), the rail's group labels and the composer's chips
 * (--rail-quiet), and the counts, timestamps and notes everywhere else
 * (--faint) -- plus the composer's placeholder (--composer-ph) and the size
 * line under a delivered file's card (--chat-file-meta). Each was once a single
 * #9d9587, about 2.9:1 on the light page: legible as a shape, not as words.
 * The digits on the amber count badges (--ink-on-badge) are held to the same
 * line; white on the light amber was 3.7:1. WCAG AA asks 4.5:1 of text this
 * size, and this holds each grey to it on every ground the table names, in all
 * four theme blocks: the light default, the dark one prefers-color-scheme
 * brings, and the two an explicit pick sets.
 *
 * The numbers are the tokens' own, not the pixels': a translucent ground is
 * composited over the block's --page, while the page itself lays the blue and
 * amber wash under the glass, which costs a few hundredths more. The values
 * were chosen from pixels sampled on the rendered page; this gate is what keeps
 * a later edit from walking them back.
 */

import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

/* Which grounds each grey is read on, by token. */
const GROUNDS = {
  '--chat-quiet': ['--paper', '--card-glass'],
  '--rail-quiet': ['--side-bg', '--composer-bg'],
  '--composer-ph': ['--composer-bg'],
  '--chat-file-meta': ['--ink'],
  '--faint': ['--page', '--paper', '--surface', '--card-glass'],
  '--ink-on-badge': ['--amber'],
}
const AA = 4.5

const BLOCKS = rules(/^:root(\[data-theme="(dark|light)"\])?$/)
  .filter(([, body]) => /--page:/.test(body))
  .map(([selector, body], i) => {
    const vars = new Map()
    for (const m of body.matchAll(/(--[\w-]+):\s*([^;]+);/g)) vars.set(m[1], m[2].trim())
    const name = selector === ':root' ? (i === 0 ? 'light (default)' : 'dark (prefers-color-scheme)') : selector
    return { name, vars }
  })

function rgba(value) {
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value)
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map((c) => c + c).join('') : hex[1]
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).concat(1)
  }
  const fn = /^rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\s*\)$/.exec(value)
  if (fn) return [+fn[1], +fn[2], +fn[3], fn[4] === undefined ? 1 : +fn[4]]
  return null
}

const resolve = (vars, name) => {
  let value = vars.get(name)
  for (let hop = 0; value && hop < 4; hop++) {
    const ref = /^var\((--[\w-]+)\)$/.exec(value)
    if (!ref) break
    value = vars.get(ref[1])
  }
  return value
}

const over = (top, base) => top.slice(0, 3).map((c, i) => c * top[3] + base[i] * (1 - top[3]))
const lum = (rgb) => {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }
  return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2])
}
const ratio = (a, b) => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05)

describe('the quiet text', () => {
  it('is what the count badges are written in', () => {
    for (const badge of ['.chrome-nav-badge', '.chrome-at-n']) {
      expect(decls(badge)?.get('color'), `${badge} should read --ink-on-badge, whose contrast this gate holds`).toBe('var(--ink-on-badge)')
    }
  })

  it('reads all four theme blocks', () => {
    expect(BLOCKS.map((b) => b.name)).toEqual([
      'light (default)', 'dark (prefers-color-scheme)', ':root[data-theme="dark"]', ':root[data-theme="light"]',
    ])
  })

  for (const block of BLOCKS) {
    for (const [grey, grounds] of Object.entries(GROUNDS)) {
      it(`${grey} holds ${AA}:1 on its grounds in ${block.name}`, () => {
        const page = rgba(resolve(block.vars, '--page'))
        const text = rgba(resolve(block.vars, grey) ?? '')
        expect(text, `${grey} is not a plain colour in ${block.name}; give it one or teach this gate to read it`).toBeTruthy()
        for (const ground of grounds) {
          const g = rgba(resolve(block.vars, ground) ?? '')
          expect(g, `${ground} is not a plain colour in ${block.name}`).toBeTruthy()
          const base = over(g, page)
          const r = ratio(over(text, base), base)
          expect(r, `${grey} on ${ground} in ${block.name} is ${r.toFixed(2)}:1; darken it on a light ground, lighten it on a dark one`)
            .toBeGreaterThanOrEqual(AA)
        }
      })
    }
  }
})
