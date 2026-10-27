// Where the conversation surface's type reaches inside markdown, read off the
// stylesheet. happy-dom applies no stylesheet, so the component tests cannot
// see a size at all -- and the bug this pins is a cascade one: the shared
// .prose rules read the conversation tokens directly, so the workspace, the
// knowledge base, a task's answer, a skill's text in settings and a
// delegate's delivered text all stepped up with the reply while their own
// body text stayed put.
import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

const LOCAL = ['--md-h2', '--md-h3', '--md-code', '--md-dense', '--md-cell-code']
const REPLY = '.msg.ai .answer > .prose, .msg.ai .step > .say.prose'

describe('the markdown type scale', () => {
  it('gives every markdown body its own sizes, not ones inherited from around it', () => {
    /* Declared on .prose itself rather than once on :root: the delivered body
       sits inside a reply's card, so a value set on the reply would flow into
       it unless each body resets its own. */
    const prose = decls('.prose')
    for (const name of LOCAL) expect(prose.get(name), name).toMatch(/^\d+(\.\d+)?px$/)
  })

  it('steps up only the reply and its narration, by name', () => {
    /* Not `.msg.ai .prose`: the delivered body is inside .msg.ai too. */
    const reply = decls(REPLY)
    expect(reply).not.toBeNull()
    for (const name of LOCAL) expect(reply.has(name), name).toBe(true)
    const readers = rules(/\.prose/).filter(([, body]) => body.includes('var(--fs-'))
    expect(readers.map(([selector]) => selector)).toEqual([REPLY])
  })

  it('sizes each part from the body it sits in', () => {
    expect(decls('.prose h2').get('font-size')).toBe('var(--md-h2)')
    expect(decls('.prose h3').get('font-size')).toBe('var(--md-h3)')
    expect(decls('.prose code').get('font-size')).toBe('var(--md-code)')
    expect(decls('.prose pre').get('font-size')).toBe('var(--md-dense)')
    expect(decls('.prose table').get('font-size')).toBe('var(--md-dense)')
    expect(decls('.prose td code').get('font-size')).toBe('var(--md-cell-code)')
    /* The language strip is drawn by the markdown renderer, so it sits in a
       body; the fallback is the body default for anywhere it does not. */
    expect(decls('.cblang').get('font-size')).toMatch(/^var\(--md-dense, \d+(\.\d+)?px\)$/)
  })

  it('leaves code in a heading or a header cell at the size around it', () => {
    expect(decls('.prose h2 code, .prose h3 code').get('font-size')).toBe('inherit')
    expect(decls('.prose th code').get('font-size')).toBe('inherit')
  })
})
