// @vitest-environment happy-dom
/* The listing highlighter: which fences it colours, in whose colours, and that
 * what it hands back is still the source, character for character. */

import { describe, expect, it } from 'vitest'

import { highlight } from './highlight'
import { md } from './prose'

const text = (html: string): string => {
  const box = document.createElement('div')
  box.innerHTML = `<pre>${html}</pre>`
  return (box.firstChild as HTMLElement).textContent || ''
}

describe('highlight', () => {
  it("colours a stylesheet in the design's roles", () => {
    const html = highlight(':root {\n  --font-sans: "Manrope";\n}\nbody { font-family: var(--font-sans); }', 'css') as string
    expect(html).toBeTruthy()
    expect(html).toContain('var(--code-keyword)')
    expect(html).toContain('var(--code-property)')
    expect(html).toContain('var(--code-string)')
  })

  it('names roles and never values', () => {
    const html = highlight('const a: string = "x" // note', 'ts') as string
    const colours = [...html.matchAll(/color:([^;"]+)/g)].map((m) => m[1])
    expect(colours.length).toBeGreaterThan(0)
    expect(colours.every((c) => /^var\(--code-[a-z]+\)$/.test(c as string))).toBe(true)
  })

  it('keeps the source exactly, markup and all', () => {
    const src = '<div class="a">&amp; <b>x</b></div>\n\tif (a < b && c > d) {}'
    const html = highlight(src, 'html') as string
    expect(html).not.toContain('<div')
    expect(text(html)).toBe(src)
  })

  it('answers by alias, and a neighbour for the grammars it does not carry', () => {
    expect(highlight('echo hi', 'bash')).toBeTruthy()
    expect(highlight('x = 1', 'py')).toBeTruthy()
    expect(highlight('int main() { return 0; }', 'cpp')).toBeTruthy()
    expect(highlight('const a = <b />', 'jsx')).toBeTruthy()
  })

  it('leaves a language it cannot read to the caller', () => {
    expect(highlight('whatever', 'klingon')).toBeNull()
  })
})

describe('a fenced listing in prose', () => {
  it('is coloured when the fence names its language', () => {
    const html = md('```python\ndef f():\n    return "x"\n```')
    expect(html).toContain('<div class="cblk lang">')
    expect(html).toContain('var(--code-')
  })

  it('stays plain text without one, or with one the highlighter lacks', () => {
    expect(md('```\nls -la <dir>\n```')).toContain('<pre>ls -la &lt;dir&gt;</pre>')
    expect(md('```klingon\nQapla <x>\n```')).toContain('<pre>Qapla &lt;x&gt;</pre>')
  })
})
