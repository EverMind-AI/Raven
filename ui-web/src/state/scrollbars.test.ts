import { afterEach, expect, it, vi } from 'vitest'

import * as scrollbars from './scrollbars'

afterEach(() => {
  scrollbars._resetForTests()
})

it('asks nothing while the boot has not filled the slot', () => {
  expect(() => scrollbars.showBar({} as HTMLElement)).not.toThrow()
})

it('forwards a view’s ask to the module that draws the bars', () => {
  const show = vi.fn()
  scrollbars.onShowBar(show)
  const el = {} as HTMLElement
  scrollbars.showBar(el)
  expect(show).toHaveBeenCalledWith(el)
})
