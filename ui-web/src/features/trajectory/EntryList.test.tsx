// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import { EntryList, FOLLOW_SLACK, OVERSCAN, ROW_HEIGHT, measureColumns, windowOf } from './EntryList'
import * as store from './store'

import type { TrajectoryEntry, TrajectoryIndexState, TrajectoryListResult, TrajectorySource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const READY: TrajectoryIndexState = {
  phase: 'ready', scanned_bytes: 10, total_bytes: 10, head_truncated: 0, recovering_traces: 0,
  unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
}

const entry = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', trace_id: 't',
  span_id: id, parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main',
  sort_key: [String(at).padStart(6, '0'), 0], event_time: '2026-01-01T00:00:00Z', preview: `preview of ${id}`,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], operation_start: null,
  operation_end: null, duration_ms: null, charged_ms: null, timing_basis: 'not_recorded', duration_owner: null,
  meta: {},
  ...over,
})

let rows: TrajectoryEntry[] = []
let indexState: TrajectoryIndexState = READY

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async (): Promise<TrajectoryListResult> => ({
    epoch: 'e1', snapshot_revision: 100, entries: rows, next_cursor: null, index_state: indexState, complete: true,
  }),
  changes: async (_k, epoch, after) => ({
    epoch, from_revision: after, to_revision: after, upserts: [], removed: [], has_more: false,
    reset_required: false, index_state: indexState,
  }),
  detail: () => Promise.reject(new Error('not scripted')),
  block: () => Promise.reject(new Error('not scripted')),
}

/* The box's geometry, which happy-dom does not lay out. */
function size(el: HTMLElement, height: number, scrollHeight: number): void {
  Object.defineProperty(el, 'clientHeight', { value: height, configurable: true })
  Object.defineProperty(el, 'clientWidth', { value: 800, configurable: true })
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
}

const list = (): HTMLElement => document.querySelector('.trajectory-list') as HTMLElement

async function draw(): Promise<void> {
  render(<EntryList />)
  await act(async () => { await store.load() })
}

beforeEach(() => {
  rows = []
  indexState = READY
  store._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  absorb(['trajectory-v1'])
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ trajectory: source })
  document.body.innerHTML = '<div class="chat"></div>'
  store.install()
  unpitch()
  store.sessionChanged('gui:a')
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

/* A ResizeObserver the test drives: which nodes were observed, and a way to
   say "the box changed" after its geometry has been written. */
function stubObserver(): { observed: Element[]; fire: () => void } {
  const observed: Element[] = []
  let callback: (() => void) | null = null
  vi.stubGlobal('ResizeObserver', class {
    constructor(cb: () => void) { callback = cb }
    observe(el: Element): void { observed.push(el) }
    disconnect(): void {}
  })
  return { observed, fire: () => { callback?.() } }
}

const feed = (list: TrajectoryEntry[]): void => {
  store.set({ ...store.get(), ...store.rowsOf(list), snapshotReady: true, epoch: 'e1' })
}

describe('windowOf', () => {
  it('bounds the rows drawn to the viewport plus the overscan on each side', () => {
    expect(windowOf(0, 320, 1000)).toEqual({ first: 0, last: 10 + OVERSCAN })
    expect(windowOf(ROW_HEIGHT * 500, 320, 1000)).toEqual({ first: 500 - OVERSCAN, last: 500 + 10 + OVERSCAN })
    expect(windowOf(ROW_HEIGHT * 995, 320, 1000)).toEqual({ first: 995 - OVERSCAN, last: 1000 })
    /* An unmeasured box still shows something. */
    expect(windowOf(0, 0, 3).last).toBe(3)
  })
})

describe('the entry list', () => {
  it('measures the scroller the moment it exists, however many times it comes and goes', () => {
    const ro = stubObserver()
    render(<EntryList />)
    expect(document.querySelector('.trajectory-list')).toBeNull()
    expect(ro.observed).toHaveLength(0)
    act(() => { feed(Array.from({ length: 100 }, (_, k) => entry(`r${k}`, k))) })
    const first = list()
    expect(first).not.toBeNull()
    expect(ro.observed).toEqual([first])
    act(() => { feed([]) })
    expect(document.querySelector('.trajectory-list')).toBeNull()
    act(() => { feed(Array.from({ length: 3 }, (_, k) => entry(`s${k}`, k))) })
    /* React keeps the div across the two states, so the node may be the same
       one; what matters is that it was observed again after the empty state
       let it go. */
    const second = list()
    expect(ro.observed).toHaveLength(2)
    expect(ro.observed[1]).toBe(second)
  })

  it('narrows its columns under 480px and fills a tall viewport, once measured', () => {
    const ro = stubObserver()
    render(<EntryList />)
    act(() => { feed(Array.from({ length: 200 }, (_, k) => entry(`r${k}`, k))) })
    const el = list()
    expect(el.hasAttribute('data-narrow')).toBe(false)
    const before = document.querySelectorAll('.trajectory-row').length
    expect(before).toBe(20 + OVERSCAN)
    Object.defineProperty(el, 'clientWidth', { value: 400, configurable: true })
    Object.defineProperty(el, 'clientHeight', { value: 2000, configurable: true })
    act(() => { ro.fire() })
    expect(el.getAttribute('data-narrow')).toBe('')
    expect(document.querySelectorAll('.trajectory-row').length).toBe(Math.ceil(2000 / ROW_HEIGHT) + OVERSCAN)
  })

  it('draws a window of a thousand rows, never all of them', async () => {
    rows = Array.from({ length: 1000 }, (_, k) => entry(`r${k}`, k))
    await draw()
    const drawn = document.querySelectorAll('.trajectory-row').length
    expect(drawn).toBeGreaterThan(0)
    expect(drawn).toBeLessThanOrEqual(20 + 2 * OVERSCAN)
    expect((document.querySelector('.trajectory-rows') as HTMLElement).style.height).toBe(`${1000 * ROW_HEIGHT}px`)
    const first = document.querySelector('.trajectory-row') as HTMLElement
    expect(first.getAttribute('aria-setsize')).toBe('1000')
    expect(first.getAttribute('aria-posinset')).toBe('1')
  })

  it('shows the turn only where a turn starts, the mark only on a failure, and the kind and preview', async () => {
    rows = [
      entry('a', 0, { kind: 'user.input', turn_start: true, turn_number: 3 }),
      entry('b', 1, { kind: 'tool.output', failure_entry: true, preview: null }),
      entry('c', 2, { kind: 'browser.frame', span_name: 'browser.action', origin: 'subagent' }),
    ]
    await draw()
    const drawn = [...document.querySelectorAll('.trajectory-row')] as HTMLElement[]
    expect(drawn.map((r) => r.querySelector('.trajectory-turn')?.textContent)).toEqual(['3', '', ''])
    expect(drawn.map((r) => !!r.querySelector('.trajectory-fail-dot'))).toEqual([false, true, false])
    expect(drawn[1]!.querySelector('.trajectory-fail-dot')?.getAttribute('aria-label')).toBe('gui.trajectory.failed')
    expect(drawn.map((r) => r.querySelector('.trajectory-tag')?.className)).toEqual([
      'trajectory-tag trajectory-k-user-input',
      'trajectory-tag trajectory-k-tool-output',
      'trajectory-tag trajectory-k-other',
    ])
    expect(drawn[0]!.querySelector('.trajectory-tag')?.textContent).toBe('gui.trajectory.kind.user_input')
    expect(drawn[2]!.querySelector('.trajectory-tag')?.textContent).toBe('browser.frame')
    expect(drawn[2]!.querySelector('.trajectory-tag')?.getAttribute('title')).toBe('browser.action')
    expect(drawn[2]!.querySelector('.trajectory-turn')?.className).toBe('trajectory-turn trajectory-turn-sub')
    expect(drawn[0]!.querySelector('.trajectory-text')?.textContent).toBe('preview of a')
    expect(drawn[1]!.querySelector('.trajectory-text')?.textContent).toBe('gui.trajectory.no_preview')
  })

  it('selects through the store on a click and on the arrow keys, and only there', async () => {
    rows = [entry('a', 0), entry('b', 1), entry('c', 2)]
    await draw()
    expect(store.get().selectedId).toBeNull()
    act(() => { fireEvent.click(document.querySelector('[data-entry="b"]') as HTMLElement) })
    expect(store.get().selectedId).toBe('b')
    expect(document.querySelector('[data-entry="b"]')?.className).toBe('trajectory-row trajectory-row-on')
    expect(list().getAttribute('aria-activedescendant')).toBe('trajectory-row-1')
    act(() => { fireEvent.keyDown(list(), { key: 'ArrowDown' }) })
    expect(store.get().selectedId).toBe('c')
    act(() => { fireEvent.keyDown(list(), { key: 'ArrowDown' }) })
    expect(store.get().selectedId).toBe('c')
    act(() => { fireEvent.keyDown(list(), { key: 'Home' }) })
    expect(store.get().selectedId).toBe('a')
    act(() => { fireEvent.keyDown(list(), { key: 'End' }) })
    expect(store.get().selectedId).toBe('c')
    act(() => { fireEvent.keyDown(list(), { key: 'ArrowUp' }) })
    expect(store.get().selectedId).toBe('b')
    /* Rows arriving do not move it. */
    act(() => {
      store.applyChanges({
        epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [entry('d', 3)], removed: [],
        has_more: false, reset_required: false, index_state: READY,
      })
    })
    expect(store.get().selectedId).toBe('b')
  })

  it('follows the tail while the reader is near it, and holds an anchored row otherwise', async () => {
    rows = Array.from({ length: 50 }, (_, k) => entry(`r${k}`, k))
    await draw()
    const el = list()
    size(el, 320, 50 * ROW_HEIGHT)
    /* Near the bottom: following. */
    el.scrollTop = 50 * ROW_HEIGHT - 320 - FOLLOW_SLACK
    act(() => { fireEvent.scroll(el) })
    expect(store.get().follow).toBe(true)
    act(() => {
      store.applyChanges({
        epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [entry('r50', 50)], removed: [],
        has_more: false, reset_required: false, index_state: READY,
      })
    })
    expect(el.scrollTop).toBe(el.scrollHeight)
    /* Scrolled up: anchored on the row at the top, with its offset. */
    el.scrollTop = 10 * ROW_HEIGHT + 5
    act(() => { fireEvent.scroll(el) })
    expect(store.get().follow).toBe(false)
    expect(store.get().anchor).toEqual({ id: 'r10', offset: 5 })
    /* Two rows inserted above: the same row stays under the eye. */
    act(() => {
      store.applyChanges({
        epoch: 'e1', from_revision: 101, to_revision: 102, upserts: [entry('r-1', -1), entry('r-2', -2)], removed: [],
        has_more: false, reset_required: false, index_state: READY,
      })
    })
    expect(store.get().index.r10).toBe(12)
    expect(el.scrollTop).toBe(12 * ROW_HEIGHT + 5)
  })

  it('brings a row selected from the bar into view without touching the follow flag', async () => {
    rows = Array.from({ length: 200 }, (_, k) => entry(`r${k}`, k))
    await draw()
    const el = list()
    size(el, 320, 200 * ROW_HEIGHT)
    el.scrollTop = 0
    act(() => { fireEvent.scroll(el) })
    expect(store.get().follow).toBe(false)
    act(() => { store.select('r150', { source: 'bar' }) })
    expect(el.scrollTop).toBe(150 * ROW_HEIGHT)
    expect(store.get().follow).toBe(false)
    /* A click in the list itself does not scroll it. */
    el.scrollTop = 0
    act(() => { store.select('r120', { source: 'click' }) })
    expect(el.scrollTop).toBe(0)
  })

  it('keeps a hidden row a link revealed in view, even while the list was following its tail', async () => {
    rows = Array.from({ length: 200 }, (_, k) => (k === 50
      ? entry('reply50', k, { kind: 'turn.end', slot: 'turn.output', meta: { hidden: 'empty_reply' } })
      : entry(`r${k}`, k)))
    await draw()
    const el = list()
    size(el, 320, 199 * ROW_HEIGHT)
    el.scrollTop = 199 * ROW_HEIGHT - 320
    act(() => { fireEvent.scroll(el) })
    expect(store.get().follow).toBe(true)
    size(el, 320, 200 * ROW_HEIGHT)
    act(() => { store.select('reply50', { source: 'link' }) })
    expect(store.get().visibleIndex.reply50).toBe(50)
    expect(el.scrollTop).toBe(50 * ROW_HEIGHT)
  })

  it('draws the rows of a short list swapped in under a deep scroll position', () => {
    render(<EntryList />)
    act(() => { feed(Array.from({ length: 600 }, (_, k) => entry(`r${k}`, k))) })
    const el = list()
    size(el, 320, 600 * ROW_HEIGHT)
    el.scrollTop = 500 * ROW_HEIGHT
    act(() => { fireEvent.scroll(el) })
    act(() => { feed([]) })
    /* The browser puts a scroller whose content shrank back at the top, and says nothing. */
    el.scrollTop = 0
    act(() => { feed(Array.from({ length: 15 }, (_, k) => entry(`s${k}`, k))) })
    expect(document.querySelectorAll('.trajectory-row')).toHaveLength(15)
  })

  it('says why the list is empty: scanning, or nothing recorded', async () => {
    indexState = { ...READY, phase: 'scanning', scanned_bytes: 1024 * 1024, total_bytes: 4 * 1024 * 1024 }
    await draw()
    expect(document.querySelector('.trajectory-empty')?.textContent).toBe('gui.trajectory.scanning {"done":"1.0","total":"4.0"}')
    cleanup()
    store._resetForTests()
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    indexState = READY
    await draw()
    expect(document.querySelector('.trajectory-empty')?.textContent).toBe('gui.trajectory.empty')
  })
})

describe('what the rows say about themselves', () => {
  it('draws the visible rows only, marks a revealed one, reads a missing record in red and names a side question', async () => {
    const list = [
      entry('ask', 0, { kind: 'user.input', turn_start: true, turn_number: 1, turn_span_id: 'turn' }),
      entry('side', 1, { kind: 'llm.input', meta: { purpose: 'watch_work' } }),
      entry('main', 2, { kind: 'llm.input', meta: { purpose: 'main' } }),
      entry('gone', 3, { kind: 'tool.output', integrity: ['artifact_missing'], preview: 'stale words' }),
      entry('reply', 4, { kind: 'turn.end', slot: 'turn.output', turn_span_id: 'turn', charged_ms: 9000, meta: { hidden: 'redundant_reply' } }),
      entry('enq', 5, { kind: 'memory.enqueue.summary', meta: { hidden: 'empty_internal' } }),
    ]
    render(<EntryList />)
    act(() => { feed(list) })
    const ids = (): string[] => [...document.querySelectorAll('.trajectory-row')].map((r) => (r as HTMLElement).dataset.entry as string)
    expect(ids()).toEqual(['ask', 'side', 'main', 'gone'])
    expect(document.querySelector('.trajectory-row[data-entry="ask"]')?.getAttribute('aria-setsize')).toBe('4')
    /* The turn's number carries the whole turn's time, the hidden reply's charge. */
    expect(document.querySelector('.trajectory-row[data-entry="ask"] .trajectory-turn')?.getAttribute('title')).toBe('gui.trajectory.turn_total {"n":1,"dur":"9s"}')
    expect(document.querySelector('.trajectory-row[data-entry="side"] .trajectory-purpose')?.textContent).toBe('watch_work')
    expect(document.querySelector('.trajectory-row[data-entry="main"] .trajectory-purpose')).toBeNull()
    const missing = document.querySelector('.trajectory-row[data-entry="gone"] .trajectory-text') as HTMLElement
    expect(missing.classList.contains('trajectory-text-missing')).toBe(true)
    expect(missing.textContent).toBe('gui.trajectory.missing_record')
    expect(missing.getAttribute('title')).toBe('artifact_missing')
    /* A link to the hidden reply shows it, marked, until the switch or the conversation changes. */
    act(() => { store.select('reply', { source: 'link' }) })
    expect(ids()).toEqual(['ask', 'side', 'main', 'gone', 'reply'])
    expect(document.querySelector('.trajectory-row[data-entry="reply"]')?.hasAttribute('data-revealed')).toBe(true)
    act(() => { store.setPrefs({ showHidden: true }) })
    expect(ids()).toEqual(['ask', 'side', 'main', 'gone', 'reply', 'enq'])
    expect(document.querySelector('.trajectory-row[data-entry="reply"]')?.hasAttribute('data-revealed')).toBe(false)
    act(() => { store.setPrefs({ showHidden: false }) })
    expect(ids()).toEqual(['ask', 'side', 'main', 'gone'])
  })

  it('sizes its columns from the words they hold and falls back where nothing can be measured', () => {
    const cols = measureColumns(['Tool output', 'LLM input'], '11px system-ui', '11px monospace')
    expect(cols).toEqual({ turn: 72, kind: 176 })
    const ctx = { font: '', measureText: (s: string) => ({ width: s.length * 6 }) }
    vi.spyOn(document, 'createElement').mockImplementationOnce(() => ({ getContext: () => ctx }) as unknown as HTMLElement)
    expect(measureColumns(['Tool output', 'LLM input'], '11px system-ui', '11px monospace')).toEqual({ turn: 18 + 12, kind: 66 + 20 })
    render(<EntryList />)
    act(() => { feed([entry('a', 0)]) })
    expect((document.querySelector('.trajectory-list') as HTMLElement).style.getPropertyValue('--trajectory-cols')).toMatch(/^\d+px 14px \d+px minmax\(0, 1fr\)$/)
  })

  it('anchors to the first visible row after one a switch hid', () => {
    const list = [entry('a', 0), entry('hid', 1, { meta: { hidden: 'empty_internal' } }), entry('b', 2)]
    render(<EntryList />)
    act(() => { feed(list); store.setPrefs({ showHidden: true }) })
    act(() => { store.setPlace(false, { id: 'hid', offset: 4 }) })
    const el = document.querySelector('.trajectory-list') as HTMLElement
    act(() => { store.setPrefs({ showHidden: false }) })
    /* The hidden anchor row gives way to `b`, now at position 1. */
    expect(el.scrollTop).toBe(1 * ROW_HEIGHT + 4)
  })
})
