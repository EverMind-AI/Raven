// @vitest-environment happy-dom
import { render } from '@testing-library/react'
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
