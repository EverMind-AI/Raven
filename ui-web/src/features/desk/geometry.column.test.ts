/** The desk column's bottom edge: the stylesheet's half of `deskBottomLimit`. */

// @vitest-environment happy-dom
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, describe, expect, it } from 'vitest'

const CSS = readFileSync('src/styles/page.css', 'utf8') as string

/* `deskBottomLimit` measures the composer card for the floating palette, which
   has no box of its own to read the edge from. The right column has one, and
   its bottom margin is how it reaches the same line; this pins the two to one
   number so a later change to either cannot leave a pane 14px below the card.

   happy-dom lays nothing out, so this reads the insets the stylesheet states
   rather than where the boxes land. Both are measured up from the same floor --
   the split's bottom edge -- so equal insets are what puts the two bottom edges
   on one line: the composer is absolutely placed at `.dock`'s `bottom`, and the
   column ends at its own bottom margin.

   Measured on the running page before the fix, in a 1920x989 window: the card
   ended at 967px and the pane at 981px. */
describe('the desk column and the composer card', () => {
  const sheet = document.createElement('style')
  sheet.textContent = CSS

  afterEach(() => {
    sheet.remove()
    document.documentElement.classList.remove('desk-ready')
    document.body.innerHTML = ''
  })

  it("ends the column on the card's own bottom edge", () => {
    document.documentElement.classList.add('desk-ready')
    document.body.innerHTML = `
      <div class="split" data-open="true">
        <div class="chat"><div class="dock"><div class="dock-in"></div></div></div>
        <div class="ws"></div>
      </div>`
    document.head.append(sheet)

    const card = document.querySelector('.dock') as HTMLElement
    const column = document.querySelector('.ws') as HTMLElement
    const inset = getComputedStyle(column).marginBottom
    expect(inset).toMatch(/^\d+(\.\d+)?px$/)
    expect(inset).toBe(getComputedStyle(card).bottom)
  })
})
