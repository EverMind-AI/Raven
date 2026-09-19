// @vitest-environment happy-dom
/* One status at a time, against the exact shape proto.js's `runCard` draws:
 * completed/running/failed get an icon-or-tick plus a duration, everything
 * else a bare word and nothing more. */

import { cleanup, render } from '@testing-library/react'
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
})
