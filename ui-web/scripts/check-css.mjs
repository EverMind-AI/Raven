// Color-token gate for the page stylesheet. Zero dependencies, same contract
// as check-page.mjs: runnable in CI with nothing installed.
//
//   node ui-web/scripts/check-css.mjs
//
// Rule: a color literal -- #hex, rgb()/rgba(), hsl()/hsla() -- may appear only
//   1. on a line that defines a custom property (`--token: ...`), which is
//      where every theme's palette is written; or
//   2. in a rule the allowlist below names, where the literal IS the design:
//      theme-preview swatches render their theme's own colors as data, mask
//      images address a luminance ramp rather than a color, and one
//      color-mix() derives a rim from structural white; or
//   3. inside a shadow, filter or gradient AND carrying an alpha component,
//      which is what makes it a ramp over whatever is behind it rather than a
//      color chosen for one theme.
// Everything else must go through var(--token). This is what keeps "which
// color is this role" answerable in one place per theme.
//
// The check is deliberately position-independent. An earlier version exempted
// everything above the first ordinary rule, on the theory that the token
// blocks live at the top -- which made the gate depend on where a rule sits
// rather than on what it says: a colored rule written above the reset passed,
// and the identical rule at the end of the file failed. The `--token:` guard
// already covers every definition wherever it appears, so there is nothing
// for a region to add.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const path = join(fileURLToPath(new URL('..', import.meta.url)), 'src', 'styles', 'page.css')
const css = readFileSync(path, 'utf8')
const lines = css.split('\n')

// Properties whose literals paint a ramp rather than name a role: a shadow is
// black at 6%, not a theme color. The exemption is NOT the property alone --
// `box-shadow: 0 0 0 2px #b4402f` is a role color frozen to one theme, and a
// focus ring or invalid-field outline is written as a shadow more often than
// as a border. What earns the exemption is the alpha: a literal that carries
// one is a ramp over whatever is behind it, and every such literal in this
// file has one today.
const RAMP_PROPS = /^(box-shadow|text-shadow|filter|backdrop-filter|-webkit-backdrop-filter)$/

// Selectors and values that carry a literal on purpose. Checked before the
// ramp rule, since a couple of them are opaque by design. Additions here are
// review-visible.
const ALLOW = [
  /\.pmtile\b/,               // permission-mode theme tiles: palette as data
  /\.thpick\b/,               // theme picker light/dark preview miniatures
  /color-mix\(in oklab, #fff/, // structural rim derivation
  /mask(-image)?\s*:/,        // alpha ramps addressed by luminance, not color
  /-webkit-mask(-image)?\s*:/,
]

// Two objects, one pattern, and they must stay that way: a global regex makes
// `.test()` a stateful reader -- it writes `lastIndex` and the next call
// resumes from there -- so sharing one with the scan below silently skipped
// any declaration shorter than the offset the previous one left behind.
const LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(/
const LITERAL_G = new RegExp(LITERAL.source, 'g')

// Does every literal in this value carry an alpha component? `rgba()/hsla()`
// and the `rgb(r g b / a)` slash form do by construction; a hex does when it
// is 4 or 8 digits. Anything else is opaque, which means it is a color choice.
const allLiteralsHaveAlpha = (decl) => {
  LITERAL_G.lastIndex = 0
  for (let m; (m = LITERAL_G.exec(decl)); ) {
    const hit = m[0]
    if (hit.startsWith('#')) {
      const digits = hit.slice(1).length
      if (digits !== 4 && digits !== 8) return false
      continue
    }
    if (/^(rgba|hsla)/.test(hit)) continue
    // rgb(/hsl(: alpha only via the slash form, inside this call's parens.
    const close = decl.indexOf(')', m.index)
    if (close < 0 || !decl.slice(m.index, close).includes('/')) return false
  }
  return true
}

// Scanned declaration by declaration rather than line by line: a `box-shadow`
// often wraps across three lines, and a line-based reader sees the wrapped
// tail as an anonymous literal with no property attached to judge it by.
const noComments = css.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
const lineOf = (index) => noComments.slice(0, index).split('\n').length

const offenders = []
// A stack, not one string: a rule nested in `@media` has two enclosing
// selectors and the inner one is the one that says what the rule is for.
// Reading only the outermost made every rule inside a media block look like
// it was selected by the media query.
const stack = []
let buf = ''
let bufAt = 0

const flush = () => {
  const decl = buf.trim()
  buf = ''
  if (!decl || !LITERAL.test(decl)) return
  const colon = decl.indexOf(':')
  const prop = colon < 0 ? '' : decl.slice(0, colon).trim()
  if (prop.startsWith('--')) return          // a token definition, wherever it sits
  const where = stack.join(' ')
  if (ALLOW.some((re) => re.test(where) || re.test(decl))) return
  if ((RAMP_PROPS.test(prop) || /gradient\(/.test(decl)) && allLiteralsHaveAlpha(decl)) return
  offenders.push(`${lineOf(bufAt)}: ${stack[stack.length - 1].trim().slice(0, 44)} { ${decl.slice(0, 66)} }`)
}

// Quote-aware: a `;` inside a quoted value does not end a declaration. Without
// this a data-URI token (`url('data:image/svg+xml;utf8,...')`) is cut in two,
// the tail is judged as its own declaration with `utf8,<svg` for a property,
// and the failure tells the reader to use a token on a line that already is
// one. `url()` is the only unquoted construct that could hide a `;`, and this
// file always quotes it.
let quote = ''
for (let i = 0; i < noComments.length; i++) {
  const ch = noComments[i]
  if (quote) {
    if (ch === quote && noComments[i - 1] !== '\\') quote = ''
    buf += ch
    continue
  }
  if (ch === "'" || ch === '"') {
    quote = ch
    buf += ch
  } else if (ch === '{') {
    stack.push(buf.trim())
    buf = ''
  } else if (ch === '}') {
    flush()
    stack.pop()
    buf = ''
  } else if (ch === ';') {
    flush()
  } else {
    if (!buf.trim()) bufAt = i
    buf += ch
  }
}

// The overlay ladder: any fixed-surface layer must come from a --z token.
// Component-internal stacking stays literal but small; the threshold keeps
// the two regimes from blurring.
for (let i = 0; i < lines.length; i++) {
  const line = lines[i]
  const m = line.match(/z-index:\s*(-?\d+)/)
  if (!m) continue
  if (/--z-[a-z-]+\s*:/.test(line)) continue // ladder definition
  if (/z-index:\s*var\(/.test(line)) continue
  if (Math.abs(parseInt(m[1], 10)) >= 10) {
    offenders.push(`${i + 1}: raw overlay z-index -- use the --z ladder: ${line.trim().slice(0, 80)}`)
  }
}

if (offenders.length) {
  console.error(`check-css: ${offenders.length} raw color literal(s) outside the token contract:`)
  for (const o of offenders) console.error('  ' + o)
  console.error('Use var(--token) (define the token per theme block), or add the rule to the allowlist in this script with a reason.')
  process.exit(1)
}
console.log('check-css: OK (color literals confined to tokens and the literal-by-design allowlist)')
