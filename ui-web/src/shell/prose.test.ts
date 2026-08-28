// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import { md } from './prose'

import type { Shell } from './bridge'
import type { ProseSource } from './prose'

/* The renderer reads two things and nothing else: whether a string is a path
   that opens, and what a link's local target is. The fakes here are
   deliberately blunt (anything under src/ is a file, anything under out/ is a
   target) so a test says what the RENDERER did with the answer rather than
   re-testing the demo's path heuristics. T returns its key. */
function wire(over: Partial<ProseSource> = {}): { asked: string[] } {
  const asked: string[] = []
  const source: ProseSource = {
    pathOf: (s) => {
      asked.push(s)
      return s.startsWith('src/') ? s.replace(/:\d+$/, '') : null
    },
    linkTargetOf: (u) => (u.startsWith('out/') ? { p: u.replace(/\/$/, ''), dir: u.endsWith('/') } : null),
    ...over,
  }
  const shell: Shell = {
    T: (key) => key,
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  window.DS = { prose: source }
  return { asked }
}

afterEach(() => {
  delete window.RavenShell
  delete window.DS
})

describe('prose renderer, typed values', () => {
  it('chips a path inside backticks and leaves a plain code span alone', () => {
    const { asked } = wire()
    expect(md('`src/main.tsx` and `plain` here')).toBe(
      '<p><code class="pth" data-p="src/main.tsx" role="link" tabindex="0">src/main.tsx</code>'
      + ' and <code>plain</code> here</p>',
    )
    /* Only backticked strings are ever offered: guessing at bare words would
       turn ordinary prose into fake links. */
    expect(asked).toEqual(['src/main.tsx', 'plain'])
  })

  it('never offers a bare word in a sentence to the resolver', () => {
    const { asked } = wire()
    md('the file src/main.tsx is over there')
    expect(asked).toEqual([])
  })

  it('hands a local link the author wrote to a chip, with their words on it', () => {
    wire()
    const out = md('[open the folder](out/deliverables/)')
    expect(out).toContain('class="artf" data-p="out/deliverables" data-d="1"')
    expect(out).toContain('<span class="tx">open the folder</span>')
    /* The path rides as fine print so the label still says which folder. */
    expect(out).toContain('<span class="pn">deliverables</span>')
  })

  it('drops a link whose target resolves to nothing, keeping the words', () => {
    wire()
    expect(md('[nope](not-a-path) stays')).toBe('<p>nope stays</p>')
  })

  it('escapes before building attributes, so text cannot break out of one', () => {
    wire()
    expect(md('a "quote" <b>tag</b> & amp')).toBe('<p>a &quot;quote&quot; &lt;b&gt;tag&lt;/b&gt; &amp; amp</p>')
  })

  it('holds a code span aside so its stars do not pair with the next bold', () => {
    wire()
    expect(md('`*.tsx` then **bold** end')).toBe('<p><code>*.tsx</code> then <strong>bold</strong> end</p>')
  })

  it('keeps the bare-url pass out of an href it just built', () => {
    wire()
    expect(md('[x](https://example.com/a) done')).toBe(
      '<p><a href="https://example.com/a" target="_blank" rel="noreferrer noopener">x</a> done</p>',
    )
  })
})

describe('prose renderer, urls', () => {
  it('keeps a paren the url opened and drops one it did not', () => {
    wire()
    const out = md('see (https://example.com/x) and https://en.wikipedia.org/wiki/Foo_(bar).')
    expect(out).toContain('>https://example.com/x</a>)')
    expect(out).toContain('>https://en.wikipedia.org/wiki/Foo_(bar)</a>.')
  })

  it('does not swallow a trailing stop, or cut a query string at its amp', () => {
    wire()
    expect(md('at https://example.com/x. done')).toContain('>https://example.com/x</a>. done')
    expect(md('q https://example.com/?a=1&b=2 done')).toContain('href="https://example.com/?a=1&amp;b=2"')
  })
})

describe('prose renderer, blocks', () => {
  it('gives a fence with a language name a header, and one without a bare card', () => {
    wire()
    const lang = md('```bash\nnpm test\n```')
    expect(lang).toContain('<div class="cblk lang">')
    expect(lang).toContain('<span class="cblang">bash</span>')
    expect(lang).toContain('<pre>npm test</pre>')
    const bare = md('```\nnpm test\n```')
    expect(bare.startsWith('<div class="cblk"><pre>npm test</pre>')).toBe(true)
    expect(bare).not.toContain('cblang')
    /* The copy button is the whole point of the card. */
    expect(bare).toContain('<button class="cbcp" type="button" title="gui.code.copy"')
  })

  it('renders a fence holding one value as that value, not as a card', () => {
    wire()
    expect(md('```\nhttps://example.com/x\n```')).toBe(
      '<p><a href="https://example.com/x" target="_blank" rel="noreferrer noopener">https://example.com/x</a></p>',
    )
    expect(md('```\nsrc/main.tsx\n```')).toBe(
      '<p><code class="pth" data-p="src/main.tsx" role="link" tabindex="0">src/main.tsx</code></p>',
    )
    /* Two words is a listing again, however short. */
    expect(md('```\nnpm test\n```')).toContain('class="cblk"')
  })

  it('closes a fence at the first bare run of its own marker, and nests when told', () => {
    wire()
    /* CommonMark's rule, and the reason the info-string guard exists: a run of
       the same marker closes the block, so the inner ``` here ends the outer
       one -- while the inner ```bash, carrying an info string, does not. */
    const three = md('```markdown\nintro\n```bash\ninner\n```\n```')
    expect(three).toContain('<pre>intro\n```bash\ninner</pre>')
    /* The two ways to actually nest, both intact: more marks on the outer
       fence, or a different marker. */
    expect(md('````markdown\nintro\n```bash\ninner\n```\n````'))
      .toContain('<pre>intro\n```bash\ninner\n```</pre>')
    expect(md('~~~markdown\nintro\n```bash\ninner\n```\n~~~'))
      .toContain('<pre>intro\n```bash\ninner\n```</pre>')
  })

  it('dedents an indented fence by its own indent and no further', () => {
    wire()
    expect(md('  ```js\n  body\n    deeper\n  ```')).toContain('<pre>body\n  deeper</pre>')
  })

  it('keeps an escaped pipe inside its cell', () => {
    wire()
    expect(md('| re | note |\n|---|---|\n| `a\\|b` | alt |')).toBe(
      '<div class="tw"><table><thead><tr><th>re</th><th>note</th></tr></thead>'
      + '<tbody><tr><td><code>a|b</code></td><td>alt</td></tr></tbody></table></div>',
    )
  })

  it('consumes a table header whose delimiter row has not streamed in yet', () => {
    wire()
    /* The scanner must never decline every branch and spin on the same line. */
    expect(md('| a | b |')).toBe('<p>| a | b |</p>')
  })

  it('draws a rule as a gap, and collapses a run of them into one', () => {
    wire()
    expect(md('a\n---\n***\nb')).toBe('<p>a</p><div class="brk"></div><p>b</p>')
  })

  it('caps heading depth at two visual levels', () => {
    wire()
    expect(md('# one\n## two\n### three\n###### six')).toBe(
      '<h2>one</h2><h2>two</h2><h3>three</h3><h3>six</h3>',
    )
  })
})

describe('prose renderer, lists', () => {
  it('rebuilds nesting from the indent', () => {
    wire()
    expect(md('- top\n  - kid\n- back')).toBe('<ul><li>top<ul><li>kid</li></ul></li><li>back</li></ul>')
  })

  it('starts a new list where numbers turn into bullets at the same depth', () => {
    wire()
    expect(md('- a\n1. b')).toBe('<ul><li>a</li></ul><ol><li>b</li></ol>')
  })

  it('carries the first number as the start attribute', () => {
    wire()
    expect(md('3. three\n4. four')).toBe('<ol start="3"><li>three</li><li>four</li></ol>')
  })

  it('marks a checklist as states, with nothing to toggle', () => {
    wire()
    expect(md('- [ ] no\n- [x] yes')).toBe('<ul><li class="tk">no</li><li class="tk on">yes</li></ul>')
  })

  it('reads a blank line between items as spacing, not the end of the list', () => {
    wire()
    expect(md('- a\n\n- b')).toBe('<ul><li>a</li><li>b</li></ul>')
    /* A blank followed by something that is not an item does close it. */
    expect(md('- a\n\nprose')).toBe('<ul><li>a</li></ul><p>prose</p>')
  })
})

describe('prose renderer, line flow', () => {
  it('flows a wrapped line back together, with a space only where Latin meets', () => {
    wire()
    expect(md('one line\ntwo line')).toBe('<p>one line two line</p>')
    expect(md('中文一行\n中文二行')).toBe('<p>中文一行中文二行</p>')
    expect(md('中文结尾\nlatin')).toBe('<p>中文结尾 latin</p>')
  })

  it('honours the breaks the author actually asked for', () => {
    wire()
    expect(md('kept  \nnext')).toBe('<p>kept<br>next</p>')
    expect(md('kept\\\nnext')).toBe('<p>kept<br>next</p>')
    /* A hand-indented line is layout, and a bolded lead-in is its own line. */
    expect(md('intro\n    a command')).toBe('<p>intro<br>    a command</p>')
    expect(md('**one** x\n**two** y')).toBe('<p><strong>one</strong> x<br><strong>two</strong> y</p>')
  })
})
