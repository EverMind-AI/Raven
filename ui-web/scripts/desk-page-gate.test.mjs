// @vitest-environment happy-dom
/* The desk is bound to the chat, and the binding is a stylesheet rule.
 *
 * Asserted by matching the rule's own selector -- lifted out of page.css, not
 * retyped -- against the DOM shapes the shell actually produces. What that
 * pins is the thing that can be wrong: which states the desk is hidden in.
 * Editing the selector in the stylesheet moves this test with it; editing it
 * to the wrong thing reds it. */

import { readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const css = readFileSync(resolve(process.cwd(), 'src/styles/page.css'), 'utf8')

function hidingSelector() {
  const rule = css.match(/\n([^\n{}]*#deskHost)\s*\{\s*display:\s*none;?\s*\}/)
  if (!rule) throw new Error('no rule hides #deskHost in page.css')
  return rule[1].trim()
}

/* `showPage` writes `data-page` on `.app` and `data-open` on the page element
   (ui-web/src/demo/120-capabilities.js), so both shapes are built here. */
function page(open) {
  document.body.innerHTML =
    `<div class="app" data-page="${open ? 'on' : 'off'}">` +
    `  <div id="split" data-open="false"></div>` +
    `  <section id="skillsPage" data-open="${open}"></section>` +
    `</div><div id="deskHost"></div>`
  return document.getElementById('deskHost')
}

describe('the desk against the shell page flag', () => {
  it('is hidden while a module page is up', () => {
    expect(page(true).matches(hidingSelector())).toBe(true)
  })

  it('is shown on the chat', () => {
    expect(page(false).matches(hidingSelector())).toBe(false)
  })

  it('is shown when nothing has set the flag yet', () => {
    /* The attribute is written on the first `showPage` call, so before any
       navigation `.app` carries none -- and the desk must not start hidden. */
    document.body.innerHTML = '<div class="app"><div id="split"></div></div><div id="deskHost"></div>'
    expect(document.getElementById('deskHost').matches(hidingSelector())).toBe(false)
  })
})
