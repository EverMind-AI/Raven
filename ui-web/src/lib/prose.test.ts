// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import * as confirmStore from '../state/confirm'
import * as pageStore from '../state/page'
import { resetSources, setSources } from '../state/sources'
import { md } from './prose'

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
  setTranslator((key) => key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
  setSources({ prose: source })
  return { asked }
}

afterEach(() => {
  resetTranslator()
  resetSources()
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

describe('prose renderer, mathematics', () => {
  /* A model asked for mathematics writes TeX. The renderer had no opinion
     about it, so a report on group theory arrived as its own source. */
  it('sets an inline formula as mathematics the browser can typeset', () => {
    wire()
    const out = md('the group $(G, \\cdot)$ is abelian')
    expect(out).toContain('<math')
    expect(out).toContain('</math>')
    /* MathML, not positioned glyphs: the browser sets it with its own fonts,
       which is why this ships no typeface. */
    expect(out).toContain('<mi>G</mi>')
    expect(out).not.toContain('$')
  })

  it('keeps a formula out of the markdown passes', () => {
    wire()
    /* Subscripts and products are underscores and stars, which the emphasis
       passes would otherwise eat -- `a_1 * b_2` losing its underscores, and
       the star pairing with the next bold in the line. */
    const out = md('take $a_1 * b_2$ here')
    expect(out).toContain('<msub>')
    expect(out).not.toContain('<em>')
    /* A code span still wins: a dollar in backticks is a dollar. */
    expect(md('the var `$PATH` is set')).toBe('<p>the var <code>$PATH</code> is set</p>')
  })

  it('leaves money alone, which is what a stray dollar usually is', () => {
    wire()
    /* The rule the markdown extensions settled on, and the reason for it: no
       space just inside either dollar, no digit just after the closing one,
       and an escaped dollar opens nothing. Without it this sentence set
       "5 and" as mathematics. */
    expect(md('it costs $5 and $10 total')).toBe('<p>it costs $5 and $10 total</p>')
    expect(md('from $5 to $10')).toBe('<p>from $5 to $10</p>')
    expect(md('a $ x $ b')).toBe('<p>a $ x $ b</p>')
    expect(md('costs \\$5 and \\$10 here')).not.toContain('<math')
  })

  it('gives the converter the source, not the escaped markup', () => {
    wire()
    /* `<`, `>` and `&` are ordinary characters in mathematics, and esc() has
       already turned them into entities by the time the TeX is read. */
    const out = md('when $a < b$ holds')
    expect(out).toContain('<math')
    expect(out).toContain('&lt;')
    expect(out).not.toContain('&amp;lt;')
  })

  it('draws display mathematics as a block of its own, in both spellings', () => {
    wire()
    /* The formula alone on its line between its own markers. */
    const split = md('before\n\n$$\n\\sum_{i=1}^{n} i\n$$\n\nafter')
    expect(split).toContain('<div class="mathblk">')
    expect(split).toContain('display="block"')
    expect(split).toContain('<p>before</p>')
    expect(split).toContain('<p>after</p>')

    /* And the whole thing on one line, which is what a report tends to be
       written in: reading only the spelling above set every one of them
       inline, with a stray marker standing on each side. */
    const one = md('before\n\n$$A \\cong \\mathbb{Z}/n\\mathbb{Z}$$\n\nafter')
    expect(one).toContain('<div class="mathblk">')
    expect(one).toContain('display="block"')
    expect(one).not.toContain('$')
    expect(one).toContain('<p>before</p>')
    expect(one).toContain('<p>after</p>')
  })

  it('leaves TeX it cannot read as the author wrote it', () => {
    wire()
    /* A formula rendered wrong is worse than a formula not rendered: the
       source is at least honest about being source. */
    const out = md('broken $\\frobnicate{x}$ here')
    expect(out).not.toContain('<math')
    expect(out).toContain('$')
    /* A lone opener is a dollar sign the author typed, not a block. */
    expect(md('$$\nnot closed')).not.toContain('mathblk')
  })
})
