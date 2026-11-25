// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import * as confirmStore from '../state/confirm'
import * as pageStore from '../state/page'
import { resetSources, setSources } from '../state/sources'
import { md, mdBlocks, mdDocument } from './prose'

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
  /* What a listing holds, read as text: a fence that names its language is
     coloured (lib/highlight.ts), so its markup is spans around the source. */
  const preText = (html: string): string => {
    const box = document.createElement('div')
    box.innerHTML = html
    return box.querySelector('pre')?.textContent ?? ''
  }

  it('gives a fence with a language name a header, and one without a bare card', () => {
    wire()
    const lang = md('```bash\nnpm test\n```')
    expect(lang).toContain('<div class="cblk lang">')
    expect(lang).toContain('<span class="cblang">bash</span>')
    expect(preText(lang)).toBe('npm test')
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
    expect(preText(three)).toBe('intro\n```bash\ninner')
    /* The two ways to actually nest, both intact: more marks on the outer
       fence, or a different marker. */
    expect(preText(md('````markdown\nintro\n```bash\ninner\n```\n````')))
      .toBe('intro\n```bash\ninner\n```')
    expect(preText(md('~~~markdown\nintro\n```bash\ninner\n```\n~~~')))
      .toBe('intro\n```bash\ninner\n```')
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

  it('reaches a displayed formula written under a list item', () => {
    wire()
    /* The list scanner takes the indented lines under an item as more of its
       text and hands them to the inline pass one at a time, where a bare pair
       of markers matches nothing -- so the item showed the delimiters and the
       source. A formula standing alone under an item is a block belonging to
       the item, and leaves the list the way an indented fence already does. */
    const out = md('- derivation\n  $$\n  x^2 + y^2\n  $$')
    expect(out).toContain('<div class="mathblk">')
    expect(out).toContain('display="block"')
    expect(out).not.toContain('$')
    expect(out).toContain('<li>derivation</li>')

    /* Both spellings, so where it is written does not decide what it is. */
    const one = md('- derivation\n  $$x^2 + y^2$$')
    expect(one).toContain('<div class="mathblk">')
    expect(one).not.toContain('$')

    /* A pair inside the item's own sentence is not that shape: it stays in the
       item, and it is set inline, or it takes a line of its own and breaks the
       sentence in three. Where it was written says which it is. */
    const inside = md('- when $$x$$ holds we win')
    expect(inside).not.toContain('mathblk')
    expect(inside).toContain('<li>when <math')
    expect(inside).toContain('holds we win</li>')
    expect(inside).not.toContain('display="block"')
  })

  it('leaves the list scanner alone otherwise', () => {
    wire()
    /* The break above must fire on a formula line and nothing else. */
    expect(md('- one\n  more of one')).toBe('<ul><li>one more of one</li></ul>')
    expect(md('- one\n  - two')).toContain('<li>one<ul><li>two</li></ul></li>')
    expect(md('- code\n  ```js\n  let a = 1\n  ```')).toContain('cblk')
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

describe('prose renderer, one block at a time', () => {
  it('hands back what md() writes, one string per top-level block', () => {
    wire()
    const src = [
      '## Head', '', 'one paragraph', 'still the same one', '', '```ts', 'const a = 1', '```', '',
      '- a', '- b', '', '| x | y |', '|---|---|', '| 1 | 2 |', '', '$$', 'x^2', '$$', '', '> quoted', '',
      '---', '', 'last',
    ].join('\n')
    const blocks = mdBlocks(src)
    expect(blocks.join('')).toBe(md(src))
    const box = document.createElement('template')
    box.innerHTML = md(src)
    expect(blocks).toHaveLength(box.content.children.length)
    expect(blocks.map((b) => {
      const one = document.createElement('template')
      one.innerHTML = b
      return one.content.children.length
    })).toEqual(blocks.map(() => 1))
  })

  it('has nothing to hand back for an empty reply', () => {
    wire()
    expect(mdBlocks('')).toEqual([])
    expect(mdBlocks('\n\n')).toEqual([])
  })
})

describe('prose renderer, a markdown file as a document', () => {
  it('keeps the written heading levels and gives each one an address', () => {
    wire()
    const doc = mdDocument('# Guide\n\ntext\n\n## Install\n\n### From source\n\n## Usage')
    expect(doc.headings).toEqual([
      { id: 'mdoc-guide', label: 'Guide', level: 1 },
      { id: 'mdoc-install', label: 'Install', level: 2 },
      { id: 'mdoc-from-source', label: 'From source', level: 3 },
      { id: 'mdoc-usage', label: 'Usage', level: 2 },
    ])
    expect(doc.html).toContain('<h3 id="mdoc-from-source" data-mh="From source" tabindex="-1">')
    /* The answer renderer still caps at two visual levels: this is a
       document's behaviour, not the chat body's. */
    expect(md('### From source')).toBe('<h3>From source</h3>')
  })

  it('makes duplicate labels distinct addresses', () => {
    wire()
    const doc = mdDocument('# Notes\n\n## Notes\n\n## Notes')
    expect(doc.headings.map((h) => h.id)).toEqual(['mdoc-notes', 'mdoc-notes-1', 'mdoc-notes-2'])
  })

  it('wraps the title and its lead paragraph, then groups the body by top-level heading', () => {
    wire()
    const html = mdDocument('# Title\n\nlead\n\n## One\n\nfirst\n\n## Two\n\nsecond').html
    expect(html.startsWith('<header class="workspace-md-intro"><h1 id="mdoc-title"')).toBe(true)
    expect(html).toContain('<p>lead</p></header>')
    expect(html).toContain('<section class="workspace-md-section"><h2 id="mdoc-one"')
    expect(html).toContain('<div class="workspace-md-paper"><p>first</p></div></section>')
    expect(html).toContain('<div class="workspace-md-paper"><p>second</p></div></section>')
  })

  it('numbers footnotes by their first reference and links each one back', () => {
    wire()
    const doc = mdDocument('one[^b] two[^a] again[^b]\n\n[^a]: the a note\n[^b]: the b note')
    expect(doc.html).toContain('<a class="workspace-md-ref" id="mdoc-fnref-1"'
      + ' href="#mdoc-fn-1" role="doc-noteref">1</a>')
    expect(doc.html).toContain('<a class="workspace-md-ref" id="mdoc-fnref-2"'
      + ' href="#mdoc-fn-2" role="doc-noteref">2</a>')
    /* The repeated reference keeps the number and carries no second id: the
       backlink returns to the first one. */
    const refs = doc.html.match(/<a class="workspace-md-ref"[^>]*>/g) ?? []
    expect(refs).toHaveLength(3)
    expect(refs.filter((ref) => ref.includes('id="mdoc-fnref-1"'))).toHaveLength(1)
    expect(doc.html).toContain('<li id="mdoc-fn-1">the b note '
      + '<a class="workspace-md-back" href="#mdoc-fnref-1" role="doc-backlink">↩</a></li>')
    expect(doc.html).toContain('<li id="mdoc-fn-2">the a note ')
    expect(doc.headings.at(-1)).toMatchObject({ label: 'Footnotes', level: 2 })
    /* The definition lines are notes, not paragraphs where they were written. */
    expect(doc.html).not.toContain('[^a]:')
  })

  it('leaves a reference with no definition as the text it is', () => {
    wire()
    expect(mdDocument('see[^gone]').html).toContain('<p>see[^gone]</p>')
  })

  it('leaves a definition inside a code fence to the listing', () => {
    wire()
    const doc = mdDocument('see[^a]\n\n```md\n[^a]: not a note\n```\n\n[^a]: the note')
    expect(doc.html).toContain('<li id="mdoc-fn-1">the note ')
    /* The sample in the fence is still the sample, not a second note. */
    expect(doc.html).toContain('not a note')
    expect(doc.html.match(/role="doc-backlink"/g)).toHaveLength(1)
  })

  it('resolves a local anchor to the id the heading was given', () => {
    wire()
    const html = mdDocument('[jump](#Usage) and [same](#mdoc-usage)\n\n## Usage').html
    expect(html).toContain('<a href="#mdoc-usage">jump</a>')
    expect(html).toContain('<a href="#mdoc-usage">same</a>')
  })

  it('leaves an anchor no heading answers as the author wrote it', () => {
    wire()
    expect(mdDocument('[jump](#nowhere)').html).toContain('<a href="#nowhere">jump</a>')
  })
})
