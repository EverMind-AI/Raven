// @vitest-environment happy-dom
import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { _resetForTests as rackReset, sync as rackSync } from '../composer/sheets'
import { back as subBack, openDagNode, _resetForTests as subReset } from '../subagents/store'
import { forget, run, start, sync, touch, _resetForTests } from './mount'
import { _resetForTests as sessionReset, setCurrent } from '../../shell/session'

import type { Shell } from '../../shell/bridge'
import type { DagRun } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const opened: Array<[string, string]> = []

function wire(): void {
  const shell: Shell = {
    T: (key) => key,
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  window.DS = {
    transcript: { openDagNode: (runId: string, nodeId: string) => opened.push([runId, nodeId]) },
    agents: {},
    subagents: {},
  }
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div>'
    + '<div class="dock-in"></div></div></div>'
}

const graph = (id: string, over: Partial<DagRun> = {}): DagRun => ({
  run_id: id,
  session: 'a',
  order: ['one', 'two'],
  nodes: new Map([
    ['one', { id: 'one', subagent: 'Researcher', depends_on: [], status: 'pending', started_at: null, ended_at: null }],
    ['two', { id: 'two', subagent: 'Coder', depends_on: ['one'], status: 'pending', started_at: null, ended_at: null }],
  ]),
  summary: null,
  done: false,
  folded: false,
  ...over,
})

/* SVG elements have no `.click()` in this DOM, and a node box is an SVG group. */
const click = (el: Element): void => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })) }

const rack = (): HTMLElement => document.getElementById('sheetRack')!
const sheets = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.dsheet')]

beforeEach(() => {
  sessionReset()
  setCurrent('a')
  opened.length = 0
  _resetForTests()
  subReset()
  rackReset()
  wire()
})

afterEach(() => {
  sessionReset()
  delete window.RavenShell
  delete window.DS
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('the dag sheet', () => {
  it('raises one sheet in the rack, on the element the rack files', () => {
    act(() => { start('a', graph('r1')) })
    const el = sheets()[0]!
    /* The sheet's own element is what the rack holds: `data-sess` and the flex
       item styles land on it, not on a wrapper around it. */
    expect(el.parentElement!.id).toBe('sheetRack')
    expect(el.dataset.sess).toBe('a')
    expect(el.getAttribute('role')).toBe('group')
    expect(el.dataset.fold).toBe('false')
    /* Order included, because that is what a byte comparison against the
       imperative builder's output sees: the rack writes `data-sess` the moment
       it is handed the element, so the fold state has to be on before then. */
    expect([...el.attributes].map((a) => a.name))
      .toEqual(['class', 'role', 'aria-label', 'data-fold', 'data-sess'])
    expect([...el.querySelectorAll('.nd .id')].map((n) => n.textContent)).toEqual(['one', 'two'])
  })

  it('draws an edge and its arrowhead per dependency, tagged with the node it leaves', () => {
    act(() => { start('a', graph('r1')) })
    const el = sheets()[0]!
    expect(el.querySelectorAll('.edge').length).toBe(1)
    expect(el.querySelectorAll('.tip').length).toBe(1)
    expect(el.querySelector<HTMLElement>('.edge')!.dataset.from).toBe('one')
    /* Faint until the node it leaves has finished. */
    expect(el.querySelector('.edge.flowed')).toBeNull()
  })

  /* The claim the imperative version kept a map of elements to protect: a status
     change must not rebuild the sheet. A rebuild slid it back in from the
     bottom, reset the canvas scroll and dropped the reader's focus. */
  it('updates a node in place, without replacing the graph around it', () => {
    const d = graph('r1')
    act(() => { start('a', d) })
    const svg = sheets()[0]!.querySelector('.canvas svg')!
    const node = sheets()[0]!.querySelector('.nd')!
    d.nodes.get('one')!.status = 'running'
    act(() => { touch() })
    expect(sheets()[0]!.querySelector('.canvas svg')).toBe(svg)
    expect(sheets()[0]!.querySelector('.nd')).toBe(node)
    expect((node as HTMLElement).dataset.st).toBe('running')
    expect(node.querySelector('.workv')).toBeTruthy()
    expect(node.querySelector('.mk.wait')).toBeNull()
  })

  it('darkens an edge once the node it leaves has completed', () => {
    const d = graph('r1')
    act(() => { start('a', d) })
    d.nodes.get('one')!.status = 'completed'
    act(() => { touch() })
    expect(sheets()[0]!.querySelector('.edge.flowed')).toBeTruthy()
  })

  it('swaps the one-line gist for the summary when the run finishes', () => {
    const d = graph('r1')
    act(() => { start('a', d) })
    expect(sheets()[0]!.querySelector('.gist')).toBeTruthy()
    d.done = true
    d.summary = { completed: 2, total: 2 }
    act(() => { touch() })
    expect(sheets()[0]!.querySelector('.gist')).toBeNull()
    expect(sheets()[0]!.querySelector('.sum')).toBeTruthy()
  })

  /* Only two events ever arrive for a node, so a number drawn once would sit
     frozen for exactly the interval a reader is watching it for. */
  it('reprints a running node clock every second, and stops when nothing runs', () => {
    vi.useFakeTimers()
    const d = graph('r1')
    d.nodes.get('one')!.status = 'running'
    d.nodes.get('one')!.started_at = Date.now() - 1000
    act(() => { start('a', d) })
    const tm = (): string => sheets()[0]!.querySelector('.tm')!.textContent || ''
    const first = tm()
    act(() => { vi.advanceTimersByTime(3000) })
    const later = tm()
    expect(later).not.toBe(first)
    d.nodes.get('one')!.status = 'completed'
    d.nodes.get('one')!.ended_at = Date.now()
    act(() => { touch() })
    const settled = tm()
    /* The timer itself, not only what it printed: a finished node's time is a
       fixed string, so an interval left running would reprint the same text and
       no assertion on the text could see it. */
    expect(vi.getTimerCount()).toBe(0)
    act(() => { vi.advanceTimersByTime(5000) })
    expect(tm()).toBe(settled)
  })

  /* The clock is an effect, so what stops it when the conversation is deleted is
     the rack running the takedown this island registered -- there is no other
     door: `forget` removes the element straight from the rack. */
  it('stops its clock when the conversation is forgotten', () => {
    vi.useFakeTimers()
    const d = graph('r1')
    d.nodes.get('one')!.status = 'running'
    d.nodes.get('one')!.started_at = Date.now()
    act(() => { start('a', d) })
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    act(() => { forget('a') })
    /* The unmount is deferred off the commit, so let that task run. */
    act(() => { vi.advanceTimersByTime(1) })
    expect(sheets()).toEqual([])
    expect(run('a')).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('pauses its clock while its conversation is detached', () => {
    vi.useFakeTimers()
    const d = graph('r1')
    d.nodes.get('one')!.status = 'running'
    d.nodes.get('one')!.started_at = Date.now()
    act(() => { start('a', d) })
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    setCurrent('b')
    act(() => { rackSync(); sync() })
    expect(sheets()).toEqual([])
    expect(vi.getTimerCount()).toBe(0)

    setCurrent('a')
    act(() => { rackSync(); sync() })
    expect(sheets()).toHaveLength(1)
    expect(vi.getTimerCount()).toBeGreaterThan(0)
  })

  /* What the takedown actually buys: it is the rack that tells this island a
     sheet is gone for good, and until it does, the host and its React root are
     still on the books. Without it a conversation that had a graph could never
     raise another one. */
  it('lets a conversation raise a new graph after the last one was dropped', () => {
    vi.useFakeTimers()
    act(() => { start('a', graph('r1')) })
    act(() => { forget('a') })
    act(() => { vi.advanceTimersByTime(1) })
    expect(sheets()).toEqual([])
    act(() => { start('a', graph('r2')) })
    expect(sheets().length).toBe(1)
    expect(run('a')!.run_id).toBe('r2')
  })

  it('folds and unfolds from the header, and the flag rides on the run', () => {
    act(() => { start('a', graph('r1')) })
    const btn = sheets()[0]!.querySelectorAll<HTMLElement>('.hd .ic')[0]!
    act(() => { btn.click() })
    expect(sheets()[0]!.dataset.fold).toBe('true')
    expect(run('a')!.folded).toBe(true)
    act(() => { btn.click() })
    expect(sheets()[0]!.dataset.fold).toBe('false')
  })

  it('closes from the header, run and all', () => {
    vi.useFakeTimers()
    act(() => { start('a', graph('r1')) })
    const btn = sheets()[0]!.querySelectorAll<HTMLElement>('.hd .ic')[1]!
    act(() => { btn.click() })
    act(() => { vi.advanceTimersByTime(1) })
    expect(sheets()).toEqual([])
    expect(run('a')).toBeNull()
  })

  /* One run at a time per conversation: the previous one keeps its own card in
     the trail, and two graphs stacked over the composer is two things to read
     for one turn. */
  it('replaces the run a conversation was already watching', () => {
    act(() => { start('a', graph('r1')) })
    act(() => { start('a', graph('r2', { order: ['solo'], nodes: new Map([['solo', { id: 'solo', subagent: 'X', depends_on: [], status: 'pending', started_at: null, ended_at: null }]]) })) })
    expect(sheets().length).toBe(1)
    expect(run('a')!.run_id).toBe('r2')
    expect([...sheets()[0]!.querySelectorAll('.nd .id')].map((n) => n.textContent)).toEqual(['solo'])
  })

  /* A graph belongs to the conversation that asked for it, and the rack is what
     keeps another conversation's from sitting over the composer. */
  it('files a graph raised for another conversation without mounting it', () => {
    vi.useFakeTimers()
    setCurrent('b')
    const d = graph('r1')
    d.nodes.get('one')!.status = 'running'
    d.nodes.get('one')!.started_at = Date.now()
    act(() => { start('a', d) })
    expect(sheets()).toEqual([])
    expect(run('a')!.run_id).toBe('r1')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('opens a node through the seam the trail card already uses', () => {
    act(() => { start('a', graph('r1')) })
    act(() => { click(sheets()[0]!.querySelector('.nd')!) })
    expect(opened).toEqual([['r1', 'one']])
  })

  it('tracks the real subagents selection through a stable store snapshot', () => {
    act(() => { start('a', graph('r1')) })
    act(() => { openDagNode('r1', { id: 'one' }) })
    expect(sheets()[0]!.querySelector('.nd[data-node="one"]')!.getAttribute('data-sel')).toBe('1')
    act(() => { subBack() })
    expect(sheets()[0]!.querySelector('.nd[data-sel="1"]')).toBeNull()
  })

  it('opens a node from the keyboard, since the box is a button', () => {
    act(() => { start('a', graph('r1')) })
    const g = sheets()[0]!.querySelector<HTMLElement>('.nd')!
    expect(g.getAttribute('role')).toBe('button')
    expect(g.getAttribute('tabindex')).toBe('0')
    act(() => { g.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })) })
    expect(opened).toEqual([['r1', 'one']])
  })

  /* A handle earns its place only when it is shared: one held by a single node
     is minted per node and reads as a mangled copy of the id above it. */
  it('shows a subagent handle only where two nodes share one', () => {
    const d = graph('r1')
    d.nodes.get('one')!.instance = 'w1'
    d.nodes.get('two')!.instance = 'w1'
    act(() => { start('a', d) })
    expect([...sheets()[0]!.querySelectorAll('.nd .ag')].map((n) => n.textContent))
      .toEqual(['Researcher @w1', 'Coder @w1'])
    const solo = graph('r2')
    solo.nodes.get('one')!.instance = 'w9'
    act(() => { start('a', solo) })
    expect([...sheets()[0]!.querySelectorAll('.nd .ag')].map((n) => n.textContent))
      .toEqual(['Researcher', 'Coder'])
  })
})
