// @vitest-environment happy-dom
import { render } from '@testing-library/react'
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, describe, expect, it } from 'vitest'

import { MarkdownDocument } from './MarkdownDocument'

/* The outline's shape is the document's own width. The pane is a column of the
   page, so a wide window with a narrow pane must still fold it away -- the
   measurement is stubbed here because happy-dom has no layout. */
const TEXT = '# Title\n\nlead\n\n## One\n\nbody\n\n## Two\n\nmore'

const setWidth = (width: number): (() => void) => {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => width })
  return () => {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, 'clientWidth', descriptor)
    else Reflect.deleteProperty(HTMLElement.prototype, 'clientWidth')
  }
}

afterEach(() => {
  Reflect.deleteProperty(HTMLElement.prototype, 'clientWidth')
})

describe('a markdown document outline', () => {
  it('draws the column when its own box is wide', () => {
    const restore = setWidth(1200)
    const view = render(<MarkdownDocument text={TEXT} />)
    expect(view.container.querySelector('.workspace-mddoc')?.getAttribute('data-wide')).toBe('1')
    restore()
  })

  it('folds the outline away when its own box is narrow, whatever the window is', () => {
    const restore = setWidth(640)
    const view = render(<MarkdownDocument text={TEXT} />)
    expect(view.container.querySelector('.workspace-mddoc')?.getAttribute('data-wide')).toBeNull()
    expect(view.container.querySelector('.workspace-md-outline')).not.toBeNull()
    /* Without a bar to mount into there is no second copy. */
    expect(view.container.querySelector('.workspace-md-menu')).toBeNull()
    restore()
  })

  it('mounts the fold-away outline into the bar it was given', () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const restore = setWidth(640)
    const view = render(<MarkdownDocument text={TEXT} menuSlot={host} />)
    expect(host.querySelector('.workspace-md-menu')).not.toBeNull()
    expect(view.container.querySelector('.workspace-md-menu')).toBeNull()
    restore()
    host.remove()
  })

  it('keeps the bar empty once the outline has a column of its own', () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const restore = setWidth(1200)
    render(<MarkdownDocument text={TEXT} menuSlot={host} />)
    expect(host.querySelector('.workspace-md-menu')).toBeNull()
    restore()
    host.remove()
  })
})

/* happy-dom has no layout, so the rules that make the fold-away button and its
   menu readable are pinned as written. */
describe('a markdown document outline, in the reader chrome', () => {
  const css = (): string => readFileSync('src/features/workspace/styles.css', 'utf8') as string
  const rule = (selector: string): string =>
    (css().match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{([\\s\\S]*?)\\}`)) as RegExpMatchArray)[1] as string

  it('matches the pane header tab pair, not the bar it was ported from', () => {
    const summary = rule('.workspace-md-menu summary')
    expect(summary).toMatch(/height: 28px/)
    expect(summary).toMatch(/border-radius: 8px/)
    expect(summary).not.toMatch(/min-height: 32px/)
  })

  it('opens a menu with a menu width rather than the button it hangs from', () => {
    const pop = rule('.workspace-md-pop')
    expect(pop).toMatch(/width: 216px/)
    expect(pop).toMatch(/inset-inline-start: 0/)
    expect(pop).not.toMatch(/calc\(100% - 28px\)/)
  })

  /* Chromium grows an open <details> by 8px whatever its content is positioned
     as, and the header centres its items: unpinned, the button rode up 4px on
     every open. The menu itself is out of flow and needs no taller box. */
  it('does not move the button when the menu opens', () => {
    expect(rule('.workspace-md-menu')).toMatch(/height: 28px/)
  })

  /* The document's reading cap is a column's cap, and a pane that owns the
     whole window has no column beside it to leave blank. */
  it('fills a fullscreen pane and a page of its own, keeping its padding', () => {
    expect(css()).toMatch(/\[data-solo="true"\] \.workspace-mddoc \{[^}]*width: 100%; margin-inline: 0;/)
    expect(css()).toMatch(/html\[data-preview\] \.workspace-mddoc \{[^}]*width: 100%; margin-inline: 0;/)
  })
})
