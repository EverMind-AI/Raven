/* The prose renderer: an answer's markdown becomes the HTML the transcript,
 * the file viewer and the replay all paint. A pure function in the bundle
 * rather than a component -- it has no state, no DOM and no lifecycle, and
 * every caller hands it a string and puts the result somewhere itself.
 *
 * Three seams, all through DS.prose: whether a string is a path that can
 * actually be opened, what a markdown link's local target resolves to, and
 * what opening one does. Everything else here is layout. This file only says
 * which strings earn a chip; the click those chips carry belongs to
 * shell/chips.ts, which reads the same source.
 */

import { ds, t } from './bridge'

export interface ProseTarget {
  p: string
  dir: boolean
}

export interface ProseSource {
  /* A workspace-relative path, or null when the string is not one that can be
     opened. A path that merely looks like one must answer null: a dead link is
     worse than no link. */
  pathOf(s: string): string | null
  /* What a markdown link's local target resolves to, or null. */
  linkTargetOf(u: string): ProseTarget | null
  /* What clicking the resulting chip does. Takes the resolved target rather
     than the raw string: the two resolvers above already decided what the
     string was, and re-deciding it at click time is how the two halves drift
     apart. Optional because a source that renders prose nobody can click --
     the transcript's own tests do -- has nothing to say here. */
  open?(target: ProseTarget): void
}

const source = (): ProseSource => ds<ProseSource>('prose')

const esc = (s: unknown): string =>
  String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string)

/* The shell's copy glyph, carried here rather than reached for: the button
   below is built as a string, and the constant is markup, not behaviour. */
const ICON_CP = '<svg class="ic" width="13" height="13" viewBox="0 0 24 24" fill="none"'
  + ' stroke="currentColor" stroke-width="1.7" aria-hidden="true">'
  + '<rect x="9" y="9" width="11" height="11" rx="2.4"/>'
  + '<path d="M15 5.5A1.5 1.5 0 0 0 13.5 4H6a2 2 0 0 0-2 2v7.5A1.5 1.5 0 0 0 5.5 15"/></svg>'
  + '<svg class="ck" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
  + ' stroke-width="2.1" stroke-linecap="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7"/></svg>'

/* The deliverable chip: what a final answer's "open the folder / read the
   handoff / here is the zip" renders as. A real element with an icon and the
   author's own words, not a mono path chip -- the reader is being handed a
   thing, and the thing's name matters more than where it sits. The path rides
   as fine print so "open the delivery folder" still says which folder. */
function artfChip(rel: string, label: string, isDir: boolean): string {
  const name = rel.split('/').filter(Boolean).pop() || rel
  const text = (label || '').trim() || name
  const d = isDir
    ? 'M3.5 7.5a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z'
    : 'M7 3.5h7L19 8.5v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2ZM13.5 4v5h5'
  return `<span class="artf" data-p="${rel}"${isDir ? ' data-d="1"' : ''} role="link" tabindex="0">`
    + `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`
    + `<span class="tx">${text}</span>`
    + (text === name ? '' : `<span class="pn">${name}</span>`)
    + '</span>'
}

/* A list item at any indent. Depth comes from the indent, so a sub-list reads
   as a sub-list instead of landing in the prose as a literal "- ..." line. */
const LI_RE = /^([ \t]*)([-*+]|\d{1,3}[.)])[ \t]+(.*)$/
const TASK_RE = /^\[([ xX])\][ \t]+/

/* Where a newline in the source becomes a newline on screen. A single newline
   inside a paragraph is the model's own wrapping, not a break the reader asked
   for -- honouring it left ragged half-lines everywhere ("GitHub repo:" on one
   line, the url on the next). So lines flow back together, and only these earn
   a break: a blank line (a new paragraph), an explicit hard break (two trailing
   spaces or a backslash), and any block of its own -- a heading, a list, a
   quote, a table, a code card, a rule.
   The joint takes a space only between two ASCII words; between CJK characters
   a space would open a visible hole. */
const BREAK = '<div class="brk"></div>'
const HARD_BR = /(?:[ \t]{2,}|\\)$/
const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/
/* The two shapes that keep their own line because a run of them IS the layout:
   an enumerated step, and a bolded lead-in. Glued together they read as one
   run-on sentence. */
const KEEP_LINE = /^(?:\*\*|第[一二三四五六七八九十百千两0-9]+[步条点章节项个]?[、.,:：)）]?)/

type Inline = (s: string) => string

function flow(lines: string[], inl: Inline): string {
  return lines.reduce((out, raw, k) => {
    const text = raw.replace(/[ \t]+$/, '').replace(/\\$/, '')
    if (!k) return inl(text)
    const prev = lines[k - 1]!.trimEnd()
    /* A line the author indented by hand is layout -- a command, a snippet the
       model wrote without a fence -- so it keeps its own line. */
    if (HARD_BR.test(lines[k - 1]!) || KEEP_LINE.test(text) || /^(?: {4,}|\t)/.test(raw)) {
      return `${out}<br>${inl(text)}`
    }
    /* Only two CJK characters meeting need no space; anywhere Latin is involved
       the joint would otherwise weld a word to the next one. */
    const gap = CJK.test(prev.slice(-1)) && CJK.test(text.slice(0, 1)) ? '' : ' '
    return out + gap + inl(text)
  }, '')
}

interface Item {
  ind: number
  ord: boolean
  num: number
  task: boolean | null
  text: string[]
}

/* Items carry their own indent, so nesting is rebuilt here rather than by
   recursive parsing: each item swallows the deeper items that follow it. */
function listHtml(items: Item[], inl: Inline): string {
  if (!items.length) return ''
  const base = items[0]!.ind
  const ord = items[0]!.ord
  const tag = ord ? 'ol' : 'ul'
  let out = `<${tag}${ord && items[0]!.num !== 1 ? ` start="${items[0]!.num}"` : ''}>`
  let k = 0
  while (k < items.length) {
    const it = items[k]!
    /* Numbers turning into bullets at the same depth is a new list to a
       reader, so it is one here too. */
    if (it.ind <= base && it.ord !== ord) return `${out}</${tag}>${listHtml(items.slice(k), inl)}`
    k += 1
    const kids: Item[] = []
    while (k < items.length && items[k]!.ind > it.ind) kids.push(items[k++]!)
    const cls = it.task == null ? '' : ` class="tk${it.task ? ' on' : ''}"`
    out += `<li${cls}>${flow(it.text, inl)}${listHtml(kids, inl)}</li>`
  }
  return `${out}</${tag}>`
}

export function md(src: string): string {
  /* Only http(s) is turned into a link, and the scheme is matched literally --
     an answer that mentions a URL should be openable, but nothing in the text
     gets to choose the scheme. The match is limited to the ASCII characters a
     URL may contain, so CJK prose running straight up against the URL with no
     space ends the match; a trailing ASCII stop is not swallowed either. */
  const link = (s: string): string => s.replace(/https?:\/\/[\w\-.~:/?#[\]@!$&'()*+,;=%]+/g, (u) => {
    /* esc() has already run, so a quote or an angle bracket AROUND the url is
       now an entity whose every character is legal inside one. Cut there --
       but not at the '&' of a real query string, which is '&amp;'. */
    const stop = u.replace(/&(?:quot|gt|lt|#\d+);?[\s\S]*$/, '')
    const rest = u.slice(stop.length)
    /* A closing paren belongs to the url when the url opened one:
       .../Foo_(bar) is one link, "(see https://a.com/x)" is not. */
    const paired = (stop.match(/\(/g) || []).length >= (stop.match(/\)/g) || []).length
    const tail = (new RegExp(paired ? '[.,;:!?\'"\\]}]+$' : '[.,;:!?\'")\\]}]+$').exec(stop) || [''])[0]!
    const url = tail ? stop.slice(0, -tail.length) : stop
    return `<a href="${url}" target="_blank" rel="noreferrer noopener">${url}</a>${tail}${rest}`
  })
  /* Backticks are the author's own signal that this is a value, not prose, so
     the path test only ever runs inside them -- guessing at bare words in a
     sentence would turn ordinary text into fake links. */
  const codeSpan = (s: string): string => {
    const rel = source().pathOf(s)
    if (!rel) return `<code>${s}</code>`
    return `<code class="pth" data-p="${rel}" role="link" tabindex="0">${s}</code>`
  }
  /* esc() FIRST, always: every attribute built below (href, data-p) relies on
     a literal quote already being an entity by the time it gets there.
     Then code spans and links are set aside as placeholders before emphasis and
     autolinking run -- otherwise a glob in backticks pairs its own stars with
     the next bold in the line, and the bare-url pass reaches into an href it
     just built. Everything comes back at the end. */
  const inl: Inline = (s) => {
    const held: string[] = []
    const keep = (html: string): string => `\u0001${held.push(html) - 1}\u0001`
    let t_ = esc(s).replace(/`([^`]+)`/g, (_, c: string) => keep(codeSpan(c)))
    /* [text](target) is the form a model writes far more often than a bare url.
       http(s) becomes a link, a workspace path becomes the usual chip, and any
       other target is dropped -- a dead link is worse than plain text. */
    t_ = t_.replace(/!?\[([^\]\n]*)\]\(([^\s)]+)\)/g, (_, tx: string, u: string) => {
      if (/^https?:\/\//.test(u)) {
        return keep(`<a href="${u}" target="_blank" rel="noreferrer noopener">${tx || u}</a>`)
      }
      /* A local target is a deliverable being handed over, so it renders as
         one: the author's words on a chip that opens the thing. codeSpan(u)
         here used to throw the label away and show the raw path instead. */
      const hit = source().linkTargetOf(u)
      return hit ? keep(artfChip(hit.p, tx, hit.dir)) : (tx || u)
    })
    t_ = t_.replace(/~~([^~]+)~~/g, '<del>$1</del>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^\w*])\*([^\s*][^*]*)\*(?![\w*])/g, '$1<em>$2</em>')
    return link(t_).replace(/\u0001(\d+)\u0001/g, (_, k: string) => held[+k]!)
  }
  const L = src.split('\n')
  const o: string[] = []
  let i = 0
  while (i < L.length) {
    const l = L[i]!
    /* Indentation is allowed on the fence: a fenced block inside a list item is
       something models write constantly, and anchoring at column 0 left it in
       the prose as literal backtick lines. The body is dedented by the fence's
       own indent, never past its content. */
    const fen = /^([ \t]*)(`{3,}|~{3,})/.exec(l)
    if (fen) {
      const pad = fen[1]!.length
      const mark = fen[2]![0]!
      const bars = fen[2]!.length
      const flat = (s: string): string => (s.slice(0, pad).trim() ? s : s.slice(pad))
      /* What closes this block, and nothing else: the same marker, at least as
         many of it, carrying no info string, and not indented deeper than the
         opener. A model demonstrating markdown nests a ```bash inside a
         ```markdown -- taking that inner fence as the closer spills the rest of
         the answer into the prose with a stray ``` in it. */
      const closes = (s: string): boolean => {
        const m = new RegExp(`^([ \\t]*)(\\${mark}{${bars},})[ \\t]*$`).exec(s)
        return !!m && m[1]!.replace(/\t/g, '    ').length <= pad + 3
      }
      /* Only an info string that looks like a language name earns a header --
         anything else would put junk in the label. */
      const info = l.trim().slice(bars).trim().split(/\s+/)[0]!
      const lang = /^[\w+#.-]{1,16}$/.test(info) ? info : ''
      const lines: string[] = []
      i++
      while (i < L.length && !closes(L[i]!)) lines.push(flat(L[i++]!))
      i++
      /* A command is there to be run somewhere else, so it gets a one-click
         copy rather than asking the reader to sweep-select it by hand. The
         button carries no payload: the click reads the block's text, which
         is the escaped source already on screen. */
      /* A fence holding nothing but one URL or one path is a value the model
         wrapped out of habit, not a listing: a card with a copy button around a
         single link is the wrong shape for it. Render it as the value it is. */
      const only = lines.length === 1 ? lines[0]!.trim() : ''
      if (/^https?:\/\/\S+$/.test(only) || (only && !/\s/.test(only) && source().pathOf(only))) {
        o.push(`<p>${inl(/^https?:/.test(only) ? only : '`' + only + '`')}</p>`)
        continue
      }
      const cp = `<button class="cbcp" type="button" title="${esc(t('gui.code.copy'))}"`
        + ` aria-label="${esc(t('gui.code.copy'))}">${ICON_CP}</button>`
      const pre = `<pre>${esc(lines.join('\n'))}</pre>`
      o.push(lang
        ? `<div class="cblk lang"><div class="cbhd"><span class="cblang">${esc(lang)}</span>${cp}</div>${pre}</div>`
        : `<div class="cblk">${pre}${cp}</div>`)
      continue
    }
    if (/^\|/.test(l) && /^\|[\s:|-]+\|/.test(L[i + 1] || '')) {
      /* An escaped pipe stays inside its cell: splitting on the raw character
         tears the row, and a torn row is a wrong table (a regex alternation or
         a shell pipeline in a cell is enough to trigger it). */
      const cut = (r: string): string[] => r.replace(/\\\|/g, '\u0001').replace(/^\||\|$/g, '')
        .split('|').map((c) => c.trim().replace(/\u0001/g, '|'))
      const h = cut(l)
      i += 2
      const b: string[][] = []
      while (i < L.length && /^\|/.test(L[i]!)) b.push(cut(L[i++]!))
      o.push(`<div class="tw"><table><thead><tr>${h.map((c) => `<th>${inl(c)}</th>`).join('')}</tr></thead><tbody>${
        b.map((r) => `<tr>${r.map((c) => `<td>${inl(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`)
      continue
    }
    /* Heading depth caps at two visual levels: chat answers are short, and a
       six-deep outline in a message column reads as noise. # and ## share the
       section size; everything deeper shares the subsection size. */
    const h = /^(#{1,6})\s+(.*)/.exec(l)
    if (h) {
      const tag = h[1]!.length <= 2 ? 'h2' : 'h3'
      o.push(`<${tag}>${inl(h[2]!)}</${tag}>`)
      i++
      continue
    }
    if (/^>\s?/.test(l)) {
      const q: string[] = []
      while (i < L.length && /^>\s?/.test(L[i]!)) q.push(L[i++]!.replace(/^>\s?/, ''))
      o.push(`<blockquote>${flow(q, inl)}</blockquote>`)
      continue
    }
    /* A "---" the model wrote between sections is a break, and it should read as
       one -- but drawn as a rule, and models write a lot of them, an answer ends
       up striped with full-width lines. Leaving it as literal "---" was wrong
       too. So it becomes the thing it means: a wider gap, no ink. Runs of them
       collapse into one. */
    if (/^ {0,3}(-{3,}|\*{3,}|_{3,})[ \t]*$/.test(l)) {
      if (o[o.length - 1] !== BREAK) o.push(BREAK)
      i++
      continue
    }
    if (LI_RE.test(l)) {
      const items: Item[] = []
      while (i < L.length) {
        const m = LI_RE.exec(L[i]!)
        if (m) {
          /* A checklist is a list of states, so the mark renders as one -- the
             answer is already written, there is nothing here to toggle. */
          const tk = TASK_RE.exec(m[3]!)
          items.push({
            ind: m[1]!.replace(/\t/g, '    ').length,
            ord: /\d/.test(m[2]!),
            num: parseInt(m[2]!, 10) || 1,
            task: tk ? tk[1] !== ' ' : null,
            text: [tk ? m[3]!.slice(tk[0].length) : m[3]!],
          })
          i++
          continue
        }
        /* An indented fence is a block that belongs to the item, not more of its
           text: breaking here hands it to the fence scanner. */
        if (/^[ \t]*(?:```|~~~)/.test(L[i]!)) break
        /* A blank line between items is a spacing habit, not the end of the
           list: only a blank followed by something that is not an item closes
           it. Getting this wrong splits one list into several <ul>s, which
           shows up as double gaps between the items. */
        if (!L[i]!.trim() && (LI_RE.test(L[i + 1] || '') || /^[ \t]+\S/.test(L[i + 1] || ''))) {
          /* A blank line before a continuation is the item's second paragraph,
             so the break the author wrote survives the flow join. */
          const cur = items[items.length - 1]
          if (cur && /^[ \t]+\S/.test(L[i + 1] || '')) cur.text[cur.text.length - 1] += '  '
          i++
          continue
        }
        /* An indented line that is not an item continues the one above it --
           but a line indented well past the item's own text is a block the
           author laid out by hand, so it keeps its line. */
        if (items.length && /^[ \t]+\S/.test(L[i]!)) {
          const cur = items[items.length - 1]!
          const deep = L[i]!.replace(/\t/g, '    ').search(/\S/) >= cur.ind + 4
          if (deep) cur.text[cur.text.length - 1] += '  '
          cur.text.push(L[i++]!.trim())
          continue
        }
        break
      }
      o.push(listHtml(items, inl))
      continue
    }
    if (!l.trim()) {
      i++
      continue
    }
    /* The first line is taken unconditionally: a line every block scanner above
       declined -- a table header whose delimiter row has not streamed in yet is
       the one that happens -- still has to be consumed, or this scanner spins on
       it forever with the tab locked up. */
    const p = [L[i++]!]
    while (i < L.length && L[i]!.trim()
      && !/^(#{1,6}\s|>\s?|\||```|~~~| {0,3}(-{3,}|\*{3,}|_{3,})[ \t]*$)/.test(L[i]!)
      && !LI_RE.test(L[i]!)) p.push(L[i++]!)
    o.push(`<p>${flow(p, inl)}</p>`)
  }
  return o.join('')
}
