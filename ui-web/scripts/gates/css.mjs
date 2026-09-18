/* The stylesheet, and the three ways a gate reads a rule out of it.
 *
 * Several invariants on this page are facts about paint and layout rather than
 * about the DOM -- happy-dom does no layout and applies no stylesheet, so a
 * rule that occupies space and one that does not are the same assertion there.
 * The gates that pin those facts read src/styles/page.css directly, and each
 * one had grown its own copy of the same parser.
 *
 * Comments come out before anything is matched: what sits between two rules is
 * captured as the next one's selector, prose and all, so a comment naming a
 * selector would otherwise be read as one.
 *
 * Not a CSS parser -- one regex over balanced braces, which is why an at-rule's
 * body is seen as rules and the at-rule itself is not. A gate that needs the
 * `@media` wrapper matches `css` itself (see desk-reserve-css).
 */

import { readFileSync } from 'node:fs'

/** The page stylesheet, as written. */
export const css = readFileSync(new URL('../../src/styles/page.css', import.meta.url), 'utf8')

/** Every rule whose selector matches, as `[selector, declarations]` pairs. */
export function rules(pattern, source = css) {
  const stripped = source.replace(/\/\*[\s\S]*?\*\//g, ' ')
  const found = []
  for (const match of stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = match[1].trim()
    if (!pattern || pattern.test(selector)) found.push([selector, match[2]])
  }
  return found
}

/** One rule's declarations, by exact selector, or null if there is no such rule. */
export function rule(selector, source = css) {
  for (const [found, body] of rules(null, source)) {
    if (found === selector) return body
  }
  return null
}

/** The same rule as a property -> value map, or null. */
export function decls(selector, source = css) {
  const body = rule(selector, source)
  if (body === null) return null
  const out = new Map()
  for (const decl of body.split(';')) {
    const at = decl.indexOf(':')
    if (at > 0) out.set(decl.slice(0, at).trim(), decl.slice(at + 1).trim())
  }
  return out
}
