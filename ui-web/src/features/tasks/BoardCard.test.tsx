// @vitest-environment happy-dom
/* One status at a time, against the exact shape proto.js's `runCard` draws:
 * completed/running/failed get an icon-or-tick plus a duration, everything
 * else a bare word and nothing more. */

import { cleanup, render } from '@testing-library/react'
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { BoardCard } from './BoardCard'

import type { DagNode } from '../dag/types'

beforeEach(() => {
  setTranslator((key, vars) => key + (vars ? ` ${JSON.stringify(vars)}` : ''))
})

afterEach(() => {
  cleanup()
  resetTranslator()
})

const node = (over: Partial<DagNode> & Pick<DagNode, 'status'>): DagNode => ({
  id: 'n1', subagent: 'raven', depends_on: [], started_at: null, ended_at: null,
  ...over,
})

describe('the board card', () => {
  it('titles by the summary, or the id when there is none', () => {
    render(<BoardCard node={node({ status: 'pending', node_summary: 'read pages' })} now={1000} />)
    expect(document.querySelector('.tkrunl1')?.textContent).toBe('read pages')

    cleanup()
    render(<BoardCard node={node({ status: 'pending', id: 'scan-news' })} now={1000} />)
    expect(document.querySelector('.tkrunl1')?.textContent).toBe('scan-news')
  })

  it('names the agent on its own line', () => {
    render(<BoardCard node={node({ status: 'pending', subagent: 'writer' })} now={1000} />)
    expect(document.querySelector('.tkrunag')?.textContent).toBe('writer')
  })

  it('pending: the word alone, no duration', () => {
    render(<BoardCard node={node({ status: 'pending' })} now={1000} />)
    expect(document.querySelector('.tkrunrt')?.textContent).toBe('gui.tasks.node_st_pending')
  })

  it('running: a spinner and the live tick, no word', () => {
    render(<BoardCard node={node({ status: 'running', started_at: 1000 })} now={5000} />)
    const rt = document.querySelector('.tkrunrt')
    expect(rt?.querySelector('.tkrunspin')).not.toBeNull()
    expect(rt?.querySelector('.tkruntick')?.textContent).toBe('4s')
  })

  it('completed: a check and the duration, no word', () => {
    render(<BoardCard node={node({ status: 'completed', started_at: 1000, ended_at: 4300 })} now={9999} />)
    const rt = document.querySelector('.tkrunrt')
    expect(rt?.querySelector('.tkrunicon')).not.toBeNull()
    expect(rt?.querySelector('.tkrunspin')).toBeNull()
    expect(rt?.textContent).toBe('3s')
  })

  it('failed: the word and the duration, no icon', () => {
    render(<BoardCard node={node({ status: 'failed', started_at: 1000, ended_at: 3000 })} now={9999} />)
    expect(document.querySelector('.tkrunrt')?.textContent).toBe('gui.tasks.node_st_failed 2s')
    expect(document.querySelector('.tkrunicon')).toBeNull()
  })

  it('skipped: the word alone', () => {
    render(<BoardCard node={node({ status: 'skipped' })} now={1000} />)
    expect(document.querySelector('.tkrunrt')?.textContent).toBe('gui.tasks.node_st_skipped')
  })

  it('cancelled: the word alone', () => {
    render(<BoardCard node={node({ status: 'cancelled' })} now={1000} />)
    expect(document.querySelector('.tkrunrt')?.textContent).toBe('gui.tasks.node_st_cancelled')
  })

  it('interrupted: the word alone, never a duration even with both stamps', () => {
    render(<BoardCard node={node({ status: 'interrupted', started_at: 1000, ended_at: 3000 })} now={9999} />)
    expect(document.querySelector('.tkrunrt')?.textContent).toBe('gui.tasks.node_st_interrupted')
  })

  it('exception: the contract-filled word, in the same slot the other words use', () => {
    render(<BoardCard node={node({ status: 'exception' })} now={1000} />)
    expect(document.querySelector('.tkrunrt')?.textContent).toBe('gui.tasks.node_st_exception')
  })

  /* A node a branch left out gets a dotted circle and its own word, in the slot a skipped node's
     word sits in -- and, like it, never a duration: it never started. */
  it('not executed: a dotted circle and the word, never a duration even with both stamps', () => {
    render(<BoardCard node={node({ status: 'not_executed', started_at: 1000, ended_at: 3000 })} now={9999} />)
    const rt = document.querySelector('.tkrun > .tkrunrt')
    expect(rt?.textContent).toBe('gui.tasks.node_st_not_executed')
    expect(rt?.querySelector('.tkrunicon circle[stroke-dasharray]')).not.toBeNull()
    expect(rt?.querySelector('.tkrunspin')).toBeNull()
  })

  /* The word a node a branch left out wears is the longest of any status, so the title has to give
     up part of its first line for it. The word itself stays in the corner, like every other, where
     nothing clips it; what the title holds is a hidden copy of it, the same size, for that first line
     to flow around. */
  it('keeps the word of a node a branch left out in the corner, and a hidden copy of it in the title', () => {
    const { container } = render(<BoardCard node={node({ status: 'not_executed', node_summary: 'tag the thread' })} now={1000} />)
    const corner = container.querySelector('.tkrun > .tkrunrt')
    expect(corner?.textContent).toBe('gui.tasks.node_st_not_executed')
    expect(container.querySelector('.tkrun')?.lastElementChild).toBe(corner)
    const copy = container.querySelector('.tkrunl1 > span > .tkrunsp')
    expect(copy?.getAttribute('aria-hidden')).toBe('true')
    expect(copy?.innerHTML).toBe(corner?.outerHTML)
    expect(copy?.nextSibling?.textContent).toBe('tag the thread')
  })

  it('shows that word once to a reader of the card', () => {
    const { container } = render(<BoardCard node={node({ status: 'not_executed' })} now={1000} />)
    const read = [...container.querySelectorAll('.tkrunrt')].filter((n) => !n.closest('[aria-hidden="true"]'))
    expect(read).toHaveLength(1)
  })

  it('keeps the cluster of every other status beside the title, with no copy in it', () => {
    for (const status of ['pending', 'running', 'completed', 'failed', 'skipped', 'cancelled', 'interrupted', 'exception']) {
      const { container } = render(<BoardCard node={node({ status })} now={1000} />)
      expect(container.querySelector('.tkrun > .tkrunrt'), status).not.toBeNull()
      expect(container.querySelector('.tkrunl1 > span'), status).toBeNull()
      expect(container.querySelector('.tkrunsp'), status).toBeNull()
      cleanup()
    }
  })

  it('an unrecognised status falls back to the raw token, like the prototype does', () => {
    render(<BoardCard node={node({ status: 'made-up' })} now={1000} />)
    expect(document.querySelector('.tkrunrt')?.textContent).toBe('made-up')
  })

  it('shows the @instance handle whenever the node has one', () => {
    render(<BoardCard node={node({ status: 'pending', instance: 'writer-1' })} now={1000} />)
    expect(document.querySelector('.tkrunhandle')?.textContent).toBe('@writer-1')
  })

  it('says nothing when the node has no instance', () => {
    render(<BoardCard node={node({ status: 'pending' })} now={1000} />)
    expect(document.querySelector('.tkrunhandle')).toBeNull()
  })

  it('shows the tool-count chip from tool_call_count, and hides it on null or zero', () => {
    render(<BoardCard node={node({ status: 'completed', tool_call_count: 7 })} now={1000} />)
    expect(document.querySelector('.tkrunchip')?.textContent).toBe('gui.tasks.tools_n {"n":7}')

    cleanup()
    render(<BoardCard node={node({ status: 'completed', tool_call_count: 0 })} now={1000} />)
    expect(document.querySelector('.tkrunchip')).toBeNull()

    cleanup()
    render(<BoardCard node={node({ status: 'completed' })} now={1000} />)
    expect(document.querySelector('.tkrunchip')).toBeNull()
  })

  /* A decision node shows its selection after its status. The card has no room for a third line, so
     the selection rides the agent line, after the tool-count chip. */
  describe('a decision node', () => {
    it('shows what it selected, after its agent line', () => {
      render(<BoardCard node={node({ status: 'completed', tool_call_count: 2, selected_routes: ['question'] })} now={1000} />)
      const line = document.querySelector('.tkrunl2')
      expect(line?.querySelector('.tkrunsel')?.textContent).toBe('\u2192 question')
      expect(line?.lastElementChild?.className).toBe('tkrunsel')
    })

    it('lists a many-selection in the order it was given, and titles the chip with all of it', () => {
      render(<BoardCard node={node({ status: 'completed', selected_routes: ['bug', 'docs', 'question'] })} now={1000} />)
      expect(document.querySelector('.tkrunsel')?.textContent).toBe('\u2192 bug, docs, question')
      expect(document.querySelector('.tkrunsel')?.getAttribute('title')).toBe('\u2192 bug, docs, question')
    })

    it('shows an empty selection as none: it chose nothing, which is a decision and not a blank', () => {
      render(<BoardCard node={node({ status: 'completed', selected_routes: [] })} now={1000} />)
      expect(document.querySelector('.tkrunsel')?.textContent).toBe('\u2192 gui.tasks.node_sel_none')
    })

    it('says nothing while it has not decided', () => {
      render(<BoardCard node={node({ status: 'running' })} now={1000} />)
      expect(document.querySelector('.tkrunsel')).toBeNull()
    })
  })

  /* The one assertion that holds a run that never branches to what it looked like before there was a
     branch to draw: the whole card, markup and all, for a node carrying none of the new fields. */
  it('draws a node with no branch fields exactly as it always did', () => {
    const plain = node({
      status: 'completed', started_at: 1000, ended_at: 4300, node_summary: 'read pages', instance: 'w1', tool_call_count: 3,
    })
    const { container } = render(<BoardCard node={plain} now={9999} />)
    expect(container.innerHTML).toBe(
      '<div class="tkrun"><span class="tkrunl1">read pages</span><span class="tkrunl2">'
      + '<span class="tkrunag">raven</span><span class="tkrunhandle">@w1</span>'
      + '<span class="tkrunchip">gui.tasks.tools_n {"n":3}</span></span><span class="tkrunrt">'
      + '<svg class="tkrunicon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M5 12.5l4.5 4.5L19 7"></path></svg><span>3s</span></span></div>',
    )

    cleanup()
    const { container: waiting } = render(<BoardCard node={node({ status: 'pending' })} now={1000} />)
    expect(waiting.innerHTML).toBe(
      '<div class="tkrun"><span class="tkrunl1">n1</span><span class="tkrunl2">'
      + '<span class="tkrunag">raven</span></span>'
      + '<span class="tkrunrt">gui.tasks.node_st_pending</span></div>',
    )
  })
})

/* What the tasks stylesheet does to that card. happy-dom lays nothing out, so this reads what the
   cascade gives each box: the mechanism, which is all it can show, not the geometry. The mechanism
   that matters most is where the word sits: the title clips, and a word lifted over its top edge
   loses part of its ink, so no box that clips may hold the word. */
describe('the card of a node a branch left out, as the stylesheet draws it', () => {
  let sheet: HTMLStyleElement | null = null

  function drawn(status: string): HTMLElement {
    const { container } = render(
      <div className="tkfork">
        <div className="nd" data-st={status}>
          <BoardCard node={node({ status, node_summary: 'tag the thread' })} now={1000} />
        </div>
      </div>,
    )
    sheet = document.createElement('style')
    sheet.textContent = readFileSync('src/features/tasks/styles.css', 'utf8') as string
    document.head.append(sheet)
    return container
  }

  afterEach(() => {
    sheet?.remove()
    sheet = null
  })

  const style = (n: Element | null): CSSStyleDeclaration => getComputedStyle(n as Element)

  it('keeps the word out of every box that clips', () => {
    const card = drawn('not_executed')
    expect(style(card.querySelector('.tkrunl1')).overflow).toBe('hidden')
    const word = [...card.querySelectorAll('.tkrunrt')].find((n) => !n.closest('[aria-hidden="true"]')) ?? null
    for (let n = word; n && n !== card; n = n.parentElement) {
      expect(style(n).overflow, n.className).not.toMatch(/hidden|clip|scroll|auto/)
    }
  })

  it('gives the copy its room: floated, in flow and invisible, with no padding left on the title', () => {
    const card = drawn('not_executed')
    const copy = card.querySelector('.tkrunsp')
    expect(copy).not.toBeNull()
    expect(style(copy).float).toBe('right')
    expect(style(copy).visibility).toBe('hidden')
    expect(style(copy?.querySelector('.tkrunrt') ?? null).position).toBe('static')
    expect(style(card.querySelector('.tkrunl1')).paddingRight).toBe('0px')
  })

  it('leaves the title of every other status as it was', () => {
    const card = drawn('completed')
    expect(style(card.querySelector('.tkrunl1')).paddingRight).toBe('46px')
    expect(style(card.querySelector('.tkrunl1')).marginRight).not.toBe('-5px')
    expect(style(card.querySelector('.tkrunrt')).position).toBe('absolute')
  })
})
