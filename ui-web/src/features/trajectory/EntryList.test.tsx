// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as details from './detailStore'
import { DOOR_PREVIEW_MIN, EntryList, FOLLOW_SLACK, OVERSCAN, ROW_HEIGHT, measureColumns, windowOf } from './EntryList'
import { DBL_MS } from './geometry'
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
  details._resetForTests()
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

  it.each(['bar', 'link'] as const)('keeps following the tail after a row selected from the %s was already in view', async (source) => {
    rows = Array.from({ length: 50 }, (_, k) => entry(`r${k}`, k))
    await draw()
    const el = list()
    size(el, 320, 50 * ROW_HEIGHT)
    el.scrollTop = 50 * ROW_HEIGHT - 320
    act(() => { fireEvent.scroll(el) })
    expect(store.get().follow).toBe(true)
    act(() => { store.select('r48', { source }) })
    expect(el.scrollTop).toBe(50 * ROW_HEIGHT - 320)
    size(el, 320, 51 * ROW_HEIGHT)
    act(() => {
      store.applyChanges({
        epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [entry('r50', 50)], removed: [],
        has_more: false, reset_required: false, index_state: READY,
      })
    })
    expect(store.get().follow).toBe(true)
    expect(el.scrollTop).toBe(51 * ROW_HEIGHT)
  })

  it('brings the selected row back into view when a link shows it again after a switch hid it', async () => {
    rows = Array.from({ length: 200 }, (_, k) => (k === 50
      ? entry('reply50', k, { kind: 'turn.end', slot: 'turn.output', meta: { hidden: 'empty_reply' } })
      : entry(`r${k}`, k)))
    await draw()
    const el = list()
    size(el, 320, 199 * ROW_HEIGHT)
    el.scrollTop = 0
    act(() => { fireEvent.scroll(el) })
    expect(store.get().follow).toBe(false)
    act(() => { store.select('reply50', { source: 'link' }) })
    expect(el.scrollTop).toBe(50 * ROW_HEIGHT)
    act(() => { store.setPrefs({ minChargedMs: 50 }) })
    expect(store.get().visibleIndex.reply50).toBeUndefined()
    el.scrollTop = 0
    act(() => { fireEvent.scroll(el) })
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
  it('draws the visible rows only, marks a revealed one and reads a missing record in red without appending purposes', async () => {
    const list = [
      entry('ask', 0, { kind: 'user.input', turn_start: true, turn_number: 1, turn_span_id: 'turn' }),
      entry('side', 1, { kind: 'llm.input', meta: { purpose: 'watch_work' } }),
      entry('main', 2, { kind: 'llm.output', meta: { purpose: 'permission_judge' } }),
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
    expect(document.querySelector('.trajectory-row[data-entry="side"] .trajectory-kind')?.textContent).toBe('gui.trajectory.kind.llm_input')
    expect(document.querySelector('.trajectory-row[data-entry="main"] .trajectory-kind')?.textContent).toBe('gui.trajectory.kind.llm_output')
    expect(document.querySelector('.trajectory-list')?.textContent).not.toContain('watch_work')
    expect(document.querySelector('.trajectory-list')?.textContent).not.toContain('permission_judge')
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

describe('the place of a Scope Tab', () => {
  const A = 't:runA'
  const B = 't:runB'
  const step = (id: string, at: number, scope: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
    entry(id, at, { subagent_scope: scope, ...over })

  it('puts each tab back where it was, the tail or an anchored row', async () => {
    rows = [
      ...Array.from({ length: 40 }, (_, k) => step(`a${k}`, k, A)),
      ...Array.from({ length: 30 }, (_, k) => step(`b${k}`, 100 + k, B)),
    ]
    await draw()
    act(() => { store.openScope(A) })
    const el = list()
    size(el, 320, 40 * ROW_HEIGHT)
    el.scrollTop = 12 * ROW_HEIGHT + 5
    act(() => { fireEvent.scroll(el) })
    expect(store.get().anchor).toEqual({ id: 'a12', offset: 5 })
    act(() => { store.openScope(B) })
    expect(el.scrollTop).toBe(el.scrollHeight)
    act(() => { store.switchScope(A) })
    expect(el.scrollTop).toBe(12 * ROW_HEIGHT + 5)
    act(() => { store.switchScope(B) })
    expect(el.scrollTop).toBe(el.scrollHeight)
  })

  it('comes back on the replacement, the next row, or the tail when the anchor went while the reader was away', async () => {
    rows = [
      ...Array.from({ length: 6 }, (_, k) => step(`a${k}`, k, A)),
      step('ah', 6, A, { meta: { hidden: 'empty_internal' } }),
      ...Array.from({ length: 30 }, (_, k) => step(`b${k}`, 100 + k, B)),
    ]
    await draw()
    const batch = (removed: Array<{ entry_id: string; replaced_by: string | null }>, upserts: TrajectoryEntry[] = [], n = 1) => ({
      epoch: 'e1', from_revision: 100 + n - 1, to_revision: 100 + n, upserts,
      removed: removed.map((r) => ({ ...r, revision: 100 + n })), has_more: false, reset_required: false, index_state: READY,
    })
    const goAndBack = (): HTMLElement => {
      act(() => { store.openScope(B) })
      const el = list()
      size(el, 64, 30 * ROW_HEIGHT)
      el.scrollTop = 96
      act(() => { store.switchScope(A) })
      return el
    }
    act(() => { store.openScope(A); store.setPlace(false, { id: 'a2', offset: 7 }) })
    act(() => { store.applyChanges(batch([{ entry_id: 'a2', replaced_by: 'a2b' }], [step('a2b', 2, A)])) })
    let el = goAndBack()
    expect(el.scrollTop).toBe(2 * ROW_HEIGHT + 7)
    act(() => { store.setPlace(false, { id: 'a2b', offset: 0 }); store.applyChanges(batch([{ entry_id: 'a2b', replaced_by: null }], [], 2)) })
    el = goAndBack()
    expect(store.get().anchor).toEqual({ id: 'a3', offset: 0 })
    expect(el.scrollTop).toBe(2 * ROW_HEIGHT)
    /* Only the hidden row is left after the last visible one, which goes. */
    act(() => { store.setPlace(false, { id: 'a5', offset: 3 }); store.applyChanges(batch([{ entry_id: 'a5', replaced_by: null }], [], 3)) })
    el = goAndBack()
    expect(store.get().follow).toBe(true)
    expect(el.scrollTop).toBe(el.scrollHeight)
  })

  it('follows the tail when a switch hides the anchored row and every row after it', () => {
    const list = [entry('a', 0), entry('b', 1), entry('hid', 2, { meta: { hidden: 'empty_internal' } })]
    render(<EntryList />)
    act(() => { feed(list); store.setPrefs({ showHidden: true }) })
    act(() => { store.setPlace(false, { id: 'hid', offset: 4 }) })
    const el = document.querySelector('.trajectory-list') as HTMLElement
    size(el, 64, 3 * ROW_HEIGHT)
    act(() => { store.setPrefs({ showHidden: false }) })
    expect([store.get().follow, store.get().anchor]).toEqual([true, null])
    expect(el.scrollTop).toBe(el.scrollHeight)
    localStorage.removeItem(store.PREFS_KEY)
  })
})

describe('the rows that lead to a Scope Tab', () => {
  const A = 't:runA'
  const run = (id: string, at: number, opens: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
    entry(id, at, { kind: 'subagent.run', span_name: 'subagent.run', slot: 'subagent.run', opens_subagent_scope: opens, ...over })
  const row = (id: string): HTMLElement => document.querySelector(`[data-entry="${id}"]`) as HTMLElement
  /* A turn that spawned a run, the run's steps (one starting a turn of its own record), and the turn its report started. */
  const ROWS = (): TrajectoryEntry[] => [
    entry('ask', 0, { turn_start: true, kind: 'user.input', slot: 'turn.input' }),
    run('door', 1, A, { operation_status: 'ok', duration_ms: 65_000 }),
    entry('plain', 2),
    entry('a0', 3, { subagent_scope: A, turn_start: true }),
    entry('a1', 4, { subagent_scope: A }),
    entry('back', 5, { turn_start: true, turn_number: 2, kind: 'user.input', slot: 'turn.input', origin: 'subagent', dispatched_by_subagent_scope: A }),
    entry('backEnd', 6, { turn_number: 2, kind: 'turn.end', slot: 'turn.output', origin: 'subagent', dispatched_by_subagent_scope: A }),
  ]

  it('opens a door\'s tab or a report turn\'s source tab on a double click, and does nothing for any other row', async () => {
    rows = ROWS()
    await draw()
    for (const id of ['plain', 'backEnd', 'ask']) act(() => { fireEvent.doubleClick(row(id)) })
    expect([store.get().scope, store.get().scopes]).toEqual([null, []])
    act(() => { fireEvent.doubleClick(row('door')) })
    expect(store.get().scope).toBe(A)
    act(() => { store.switchScope(null) })
    act(() => { fireEvent.doubleClick(row('back')) })
    expect(store.get().scope).toBe(A)
  })

  it('opens the same on Enter over the selected row, and leaves Enter alone on any other row', async () => {
    rows = ROWS()
    await draw()
    const press = (): boolean => {
      let prevented = false
      act(() => { prevented = !fireEvent.keyDown(list(), { key: 'Enter' }) })
      return prevented
    }
    for (const id of ['plain', 'backEnd']) {
      act(() => { store.select(id, { source: 'click' }) })
      expect(press()).toBe(false)
    }
    expect(store.get().scope).toBeNull()
    act(() => { store.select('door', { source: 'click' }) })
    expect(press()).toBe(true)
    expect(store.get().scope).toBe(A)
    act(() => { store.select('back', { source: 'click' }) })
    act(() => { store.switchScope(A) })
    act(() => { store.switchScope(null) })
    expect(store.get().selectedId).toBe('back')
    expect(press()).toBe(true)
    expect(store.get().scope).toBe(A)
  })

  it('says how a door\'s run stands, for how long, and that it opens; no other row does', async () => {
    rows = ROWS()
    await draw()
    expect(row('door').classList.contains('trajectory-row-door')).toBe(true)
    const cell = row('door').querySelector('.trajectory-scope-door') as HTMLElement
    expect([...cell.children].map((c) => c.textContent)).toEqual(['gui.trajectory.status.ok', '1m 5s', ''])
    expect([...cell.children].map((c) => c.classList[0])).toEqual(['trajectory-scope-door-state', 'trajectory-scope-door-dur', 'trajectory-scope-opens'])
    /* The door's own columns: its kind column may give way to its tag, its preview keeps a floor, and the state takes the fifth. */
    const cols = list().style.getPropertyValue('--trajectory-cols-door')
    expect(cols).toMatch(new RegExp(`^\\d+px 14px minmax\\(min-content, \\d+px\\) minmax\\(${DOOR_PREVIEW_MIN}px, 1fr\\) auto$`))
    expect(cell.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('gui.trajectory.scope.opens')
    expect(row('plain').querySelector('.trajectory-scope-door')).toBeNull()
    expect(row('plain').classList.contains('trajectory-row-door')).toBe(false)
  })

  it('marks a report turn\'s first row with a dot that says so, and keeps the bare mark on the rest of a dispatched turn', async () => {
    rows = ROWS()
    await draw()
    const turn = row('back').querySelector('.trajectory-turn') as HTMLElement
    expect(turn.classList.contains('trajectory-turn-sub')).toBe(false)
    const dot = turn.querySelector('.trajectory-scope-from') as HTMLElement
    expect([dot.getAttribute('aria-label'), dot.title]).toEqual(['gui.trajectory.scope.reported', 'gui.trajectory.scope.reported'])
    expect(row('backEnd').querySelector('.trajectory-turn')?.classList.contains('trajectory-turn-sub')).toBe(true)
    expect(row('backEnd').querySelector('.trajectory-scope-from')).toBeNull()
  })

  it('shows no turn number in a sub-agent tab', async () => {
    rows = ROWS()
    await draw()
    expect(row('ask').querySelector('.trajectory-turn')?.textContent).toBe('1')
    act(() => { store.openScope(A) })
    expect(row('a0').querySelector('.trajectory-turn')?.textContent).toBe('')
  })

  it('says why a sub-agent tab is empty: still running, recorded nothing, or no longer on record', async () => {
    rows = [run('d1', 0, 'x:run', { operation_status: 'running' }), run('d2', 1, 'x:done'), entry('g0', 2, { subagent_scope: 'x:gone' })]
    await draw()
    const empty = (): string => document.querySelector('.trajectory-empty')?.textContent ?? ''
    act(() => { store.openScope('x:run') })
    expect(empty()).toBe('gui.trajectory.scope.empty_running')
    act(() => { store.openScope('x:done') })
    expect(empty()).toBe('gui.trajectory.scope.empty_done')
    act(() => { store.select('g0', { source: 'link' }) })
    act(() => {
      store.applyChanges({
        epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [], removed: [{ entry_id: 'g0', revision: 101, replaced_by: null }],
        has_more: false, reset_required: false, index_state: READY,
      })
    })
    expect(empty()).toBe('gui.trajectory.scope.gone')
    act(() => { store.switchScope(null) })
    expect(document.querySelector('.trajectory-empty')).toBeNull()
  })
})

describe('a click on a row that opens a tab', () => {
  const A = 't:runA'
  const ROWS = (): TrajectoryEntry[] => [
    entry('ask', 0, { turn_start: true }),
    entry('door', 1, { kind: 'subagent.run', span_name: 'subagent.run', opens_subagent_scope: A }),
    entry('plain', 2),
    entry('a0', 3, { subagent_scope: A }),
  ]
  const row = (id: string): HTMLElement => document.querySelector(`[data-entry="${id}"]`) as HTMLElement
  const wait = (ms: number): void => { act(() => { vi.advanceTimersByTime(ms) }) }

  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  async function drawn(): Promise<void> {
    rows = ROWS()
    details.install()
    render(<EntryList />)
    await act(async () => { await store.load() })
  }

  it('waits out a double click before it selects while the pane is closed, and opens the pane then', async () => {
    await drawn()
    act(() => { fireEvent.click(row('door')) })
    expect([store.get().selectedId, details.get().open]).toEqual([null, false])
    wait(DBL_MS)
    expect([store.get().selectedId, store.get().selectedBy, details.get().open]).toEqual(['door', 'click', true])
  })

  it('opens the tab on a double click and never selects in the main session', async () => {
    await drawn()
    act(() => { fireEvent.click(row('door')); fireEvent.click(row('door')); fireEvent.doubleClick(row('door')) })
    expect(store.get().scope).toBe(A)
    wait(DBL_MS * 2)
    expect([store.get().scope, store.get().selectedId, details.get().open]).toEqual([A, null, false])
    act(() => { store.switchScope(null) })
    expect([store.get().selectedId, details.get().open]).toEqual([null, false])
  })

  it('selects at once while the pane is open, and any other row always', async () => {
    await drawn()
    act(() => { fireEvent.click(row('plain')) })
    expect([store.get().selectedId, details.get().open]).toEqual(['plain', true])
    act(() => { fireEvent.click(row('door')) })
    expect(store.get().selectedId).toBe('door')
  })

  it('gives way to a keyboard move made in the wait, which keeps its own selection and pane', async () => {
    await drawn()
    act(() => { fireEvent.click(row('door')) })
    act(() => { fireEvent.keyDown(list(), { key: 'End' }) })
    expect(store.get().selectedId).toBe('plain')
    wait(DBL_MS * 2)
    expect([store.get().selectedId, store.get().selectedBy, details.get().open]).toEqual(['plain', 'keyboard', true])
  })

  it('gives way to a selection made through any other door in the wait', async () => {
    await drawn()
    const wrong: string[] = []
    for (const source of ['bar', 'link', 'migrate'] as const) {
      act(() => { store.select(null, { source: 'migrate' }); details.closeDetails() })
      act(() => { fireEvent.click(row('door')) })
      act(() => { store.select('ask', { source }) })
      wait(DBL_MS * 2)
      if (store.get().selectedId !== 'ask') wrong.push(source)
    }
    expect(wrong).toEqual([])
  })

  it('gives way to a move to another tab, also when the reader comes straight back', async () => {
    await drawn()
    act(() => { fireEvent.click(row('door')) })
    act(() => { store.openScope(A); store.switchScope(null) })
    wait(DBL_MS * 2)
    expect([store.get().scope, store.get().selectedId, details.get().open]).toEqual([null, null, false])
  })

  it('gives way to the other view and to another conversation, also on the way back', async () => {
    await drawn()
    await act(async () => { await store.refreshState() })
    act(() => { store.setView('trajectory') })
    act(() => { fireEvent.click(row('door')) })
    act(() => { store.setView('chat'); store.setView('trajectory') })
    wait(DBL_MS * 2)
    expect(store.get().selectedId).toBeNull()
    act(() => { fireEvent.click(row('door')) })
    act(() => { store.sessionChanged('gui:b'); store.sessionChanged('gui:a') })
    await act(async () => { await store.load() })
    expect(row('door')).not.toBeNull()
    wait(DBL_MS * 2)
    expect([store.get().selectedId, details.get().open]).toEqual([null, false])
  })

  it('lets the feed through: a batch in the wait does not drop the click', async () => {
    await drawn()
    act(() => { fireEvent.click(row('door')) })
    act(() => {
      store.applyChanges({
        epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [entry('plain', 2, { preview: 'changed' })], removed: [],
        has_more: false, reset_required: false, index_state: READY,
      })
    })
    wait(DBL_MS)
    expect([store.get().selectedId, details.get().open]).toEqual(['door', true])
  })
})
