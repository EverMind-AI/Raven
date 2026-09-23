/* Syntax colour for a fenced listing, through Shiki.
 *
 * Synchronous, because the prose renderer is: `md()` is a pure string function
 * every caller paints straight away, and a highlighter that answered later would
 * need every one of them to paint twice. Shiki's core can be built synchronously
 * when its grammars are imported rather than fetched and its regex engine is the
 * JavaScript one rather than the WebAssembly one -- which is also what the page's
 * single inlined bundle needs, since it can load nothing after it has started.
 *
 * The grammars are the languages a model writes a listing in, and no more: each
 * one is bundled whether it is used or not. C++ is read with the C grammar
 * rather than carrying its own, which alone is a third of a megabyte.
 *
 * The colours are the page's own tokens (`--code-*`, styles/page.css), so a
 * listing follows the theme the way every other colour does: the theme below
 * names roles, never values. The roles are the design's (Figma: Raven /
 * AssistantMessage, the code variant) -- a keyword or a selector in purple, a
 * property in amber, a string in green, a tag or a type in blue.
 */

import { createHighlighterCoreSync } from 'shiki/core'
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript'
import c from 'shiki/langs/c.mjs'
import css from 'shiki/langs/css.mjs'
import diff from 'shiki/langs/diff.mjs'
import dockerfile from 'shiki/langs/dockerfile.mjs'
import go from 'shiki/langs/go.mjs'
import html from 'shiki/langs/html.mjs'
import ini from 'shiki/langs/ini.mjs'
import java from 'shiki/langs/java.mjs'
import javascript from 'shiki/langs/javascript.mjs'
import json from 'shiki/langs/json.mjs'
import markdown from 'shiki/langs/markdown.mjs'
import python from 'shiki/langs/python.mjs'
import rust from 'shiki/langs/rust.mjs'
import shellscript from 'shiki/langs/shellscript.mjs'
import sql from 'shiki/langs/sql.mjs'
import toml from 'shiki/langs/toml.mjs'
import tsx from 'shiki/langs/tsx.mjs'
import typescript from 'shiki/langs/typescript.mjs'
import xml from 'shiki/langs/xml.mjs'
import yaml from 'shiki/langs/yaml.mjs'

import type { HighlighterCore, ThemeRegistration } from 'shiki/core'

const THEME: ThemeRegistration = {
  name: 'raven',
  type: 'light',
  colors: { 'editor.foreground': 'var(--chat-ink)', 'editor.background': 'transparent' },
  tokenColors: [
    { scope: ['comment', 'punctuation.definition.comment'], settings: { foreground: 'var(--code-comment)', fontStyle: 'italic' } },
    {
      scope: [
        'keyword', 'storage', 'entity.other.attribute-name.pseudo-class',
        'entity.other.attribute-name.class.css', 'entity.other.attribute-name.id.css',
        'variable.language',
      ],
      settings: { foreground: 'var(--code-keyword)' },
    },
    {
      scope: [
        'support.type.property-name', 'meta.property-name', 'variable.css', 'variable.other.property',
        'variable.other.object.property', 'support.function', 'entity.other.attribute-name',
        'constant.numeric', 'constant.language', 'constant.other',
      ],
      settings: { foreground: 'var(--code-property)' },
    },
    { scope: ['string', 'markup.inline.raw', 'markup.fenced_code', 'markup.inserted'], settings: { foreground: 'var(--code-string)' } },
    {
      scope: [
        'entity.name.tag', 'entity.name.function', 'entity.name.type', 'entity.name.class',
        'support.class', 'support.type', 'meta.function-call', 'markup.heading',
      ],
      settings: { foreground: 'var(--code-tag)' },
    },
    { scope: ['markup.deleted', 'invalid'], settings: { foreground: 'var(--code-deleted)' } },
  ],
}

/* What a fence's info string may say for a grammar under another name. Shiki's
   own aliases (js, ts, sh, bash, py, yml, md, ...) are the grammars' business;
   these are the two the bundle answers with a neighbour. */
const ALIAS: Record<string, string> = { cpp: 'c', 'c++': 'c', cc: 'c', h: 'c', hpp: 'c', jsx: 'tsx' }

let made: HighlighterCore | null = null
let failed = false

function highlighter(): HighlighterCore | null {
  if (made || failed) return made
  try {
    made = createHighlighterCoreSync({
      themes: [THEME],
      langs: [
        c, css, diff, dockerfile, go, html, ini, java, javascript, json, markdown, python, rust,
        shellscript, sql, toml, tsx, typescript, xml, yaml,
      ],
      engine: createJavaScriptRegexEngine({ forgiving: true }),
    })
  } catch {
    failed = true
  }
  return made
}

const esc = (s: string): string =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/* A streamed answer repaints on every chunk, and every repaint renders every
   listing in it again; the same source always colours the same way. */
const CACHE_MAX = 200
const cache = new Map<string, string>()

/**
 * The listing's body as coloured HTML -- escaped text in spans, one per token,
 * lines joined as they were -- to go inside the renderer's own `<pre>`. Null
 * when the language is not one the bundle reads, and the caller then escapes
 * the text as it always has.
 */
export function highlight(code: string, lang: string): string | null {
  const hl = highlighter()
  if (!hl) return null
  const want = lang.toLowerCase()
  const id = ALIAS[want] || want
  const loaded = hl.getLoadedLanguages()
  if (!loaded.includes(id)) return null
  const key = `${id}\u0000${code}`
  const hit = cache.get(key)
  if (hit != null) return hit
  let out: string
  try {
    const lines = hl.codeToTokensBase(code, { lang: id, theme: 'raven' })
    out = lines.map((line) => line.map((tok) => {
      const text = esc(tok.content)
      const italic = tok.fontStyle && tok.fontStyle & 1 ? ';font-style:italic' : ''
      return tok.color && tok.color !== 'var(--chat-ink)'
        ? `<span style="color:${tok.color}${italic}">${text}</span>`
        : text
    }).join('')).join('\n')
  } catch {
    return null
  }
  if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value as string)
  cache.set(key, out)
  return out
}
