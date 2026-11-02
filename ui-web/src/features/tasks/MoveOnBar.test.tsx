// @vitest-environment happy-dom
/* The Move on bar over the composer: the matter the next message moves on, by
   title, id and summary, in the conversation it was picked in; its cross puts
   it away. */

import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { setCurrent } from '../../lib/session'
import { MoveOnBar } from './MoveOnBar'
import * as tasks from './store'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const fix = { id: 'g_1', title: 'Fix bug A', summary: 'Cause found' }

beforeEach(() => {
  setTranslator((key) => key)
  tasks._resetForTests()
  setCurrent('s1')
})

afterEach(() => {
  cleanup()
  resetTranslator()
  setCurrent(null)
})

describe('the move-on bar', () => {
  it('draws nothing until a group is moved on', () => {
    const { container } = render(<MoveOnBar />)

    expect(container.innerHTML).toBe('')
  })

  it('names the group by title, id and summary, and its cross puts it away', () => {
    const { container } = render(<MoveOnBar />)
    act(() => { tasks.moveOn('s1', fix) })

    const bar = container.querySelector<HTMLElement>('.tkmoveon')!
    expect(bar.getAttribute('aria-label')).toBe('gui.tasks.board.move_on_bar')
    expect(bar.querySelector('.tkmoveontitle')!.textContent).toBe('Fix bug A')
    expect(bar.querySelector('.tkmoveonid')!.textContent).toBe('g_1')
    expect(bar.querySelector('.tkmoveonsum')!.textContent).toBe('Cause found')

    fireEvent.click(bar.querySelector('.tkmoveonx')!)
    expect(tasks.moveOnFor('s1')).toBeNull()
    expect(container.querySelector('.tkmoveon')).toBeNull()
  })

  it('shows only in the conversation it was picked in', () => {
    const { container } = render(<MoveOnBar />)
    act(() => { tasks.moveOn('s1', fix) })

    act(() => { setCurrent('s2') })
    expect(container.querySelector('.tkmoveon')).toBeNull()
    act(() => { setCurrent('s1') })
    expect(container.querySelector('.tkmoveon')).not.toBeNull()
  })
})
