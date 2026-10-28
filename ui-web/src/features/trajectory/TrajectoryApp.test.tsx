// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { RpcError } from '../../rpc/transport'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as details from './detailStore'
import * as scopes from './scopes'
import * as store from './store'
import { TrajectoryApp } from './TrajectoryApp'

/* This file forces many renders; under a full-suite run they share the CPU
   with every other worker, so the wall clock is not the thing under test. */
vi.setConfig({ testTimeout: 20_000 })

import type { TrajectoryChangesResult, TrajectoryEntry, TrajectoryIndexState, TrajectorySource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const READY: TrajectoryIndexState = {
  phase: 'ready', scanned_bytes: 10, total_bytes: 10, head_truncated: 0, recovering_traces: 0,
  unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
}

const one: TrajectoryEntry = {
  entry_id: 'a', revision: 1, kind: 'user.input', span_name: 'session.turn', slot: 'turn.input', trace_id: 't',
  span_id: 'turn', parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: true, origin: 'main',
  sort_key: ['000000', 0], event_time: '2026-01-01T00:00:00Z', preview: 'hello', operation_status: 'ok',
  status_evidence: [], failure_entry: false, integrity: [], operation_start: null, operation_end: null,
  duration_ms: null, charged_ms: null, timing_basis: 'zero', duration_owner: null, meta: {},
}

let indexState: TrajectoryIndexState = READY
let recording = true

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: recording }),
  list: async () => ({
    epoch: 'e1', snapshot_revision: 1, entries: [one], next_cursor: null, index_state: indexState, complete: true,
  }),
  changes: async (_k, epoch, after) => ({
    epoch, from_revision: after, to_revision: after, upserts: [], removed: [], has_more: false,
    reset_required: false, index_state: indexState,
  }),
  detail: () => Promise.reject(new Error('not scripted')),
  block: () => Promise.reject(new Error('not scripted')),
}

const status = (): string[] => [...document.querySelectorAll('.trajectory-status > span')].map((s) => s.textContent ?? '')

async function draw(): Promise<void> {
  render(<TrajectoryApp />)
  await act(async () => {
    await store.refreshState()
    await store.load()
  })
}

beforeEach(() => {
  indexState = READY
  recording = true
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
  document.body.innerHTML = ''
})

describe('the trajectory island', () => {
  it('draws the agent waterfall under an absent status line, with the pane beside it', async () => {
    await draw()
    const root = document.querySelector('.trajectory-root') as HTMLElement
    expect([...root.children].map((c) => c.className)).toEqual(['trajectory-body'])
    expect(document.querySelectorAll('[data-row="agent"]').length).toBeGreaterThan(0)
    expect(document.querySelectorAll('[data-row="turn"]').length).toBeGreaterThan(0)
  })

  it('says what the index wants said, one line each', async () => {
    indexState = { ...READY, phase: 'scanning', scanned_bytes: 2 * 1024 * 1024, total_bytes: 8 * 1024 * 1024, head_truncated: 12, failure: 'disk' }
    recording = false
    await draw()
    expect(status()).toEqual([
      'gui.trajectory.scanning {"done":"2.0","total":"8.0"}',
      'gui.trajectory.truncated {"n":12}',
      'gui.trajectory.failure {"detail":"disk"}',
      'gui.trajectory.not_recording',
    ])
    act(() => { store.liveFailed(new RpcError(-32603, 'internal')) })
    expect(status()).toContain('gui.trajectory.fault {"detail":"internal"}')
  })

  it('subscribes to the language, like every island', () => {
    const text = readFileSync('src/features/trajectory/TrajectoryApp.tsx', 'utf8') as string
    expect(text).toMatch(/useSyncExternalStore\(lang\.subscribe, lang\.get\)/)
  })
})

/* ── the Scope Tabs on the page, and where the focus goes ──────────── */

describe('the Scope Tabs in the island', () => {
  const A = 't:runA'
  const B = 't:runB'
  const C = 't:runC'
  const E = 't:runE'
  const row = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
    ({ ...one, entry_id: id, span_id: id, sort_key: [String(at).padStart(6, '0'), 0], turn_start: false, preview: id, ...over })
  const door = (id: string, at: number, opens: string): TrajectoryEntry =>
    row(id, at, { kind: 'subagent.run', span_name: 'subagent.run', slot: 'subagent.run', opens_subagent_scope: opens, meta: { label: id } })
  /* Four runs, one of which recorded no step, and the turn the first one's report started. */
  const ROWS: TrajectoryEntry[] = [
    row('m0', 0, { turn_start: true }),
    door('doorA', 1, A), door('doorB', 2, B), door('doorC', 3, C), door('doorE', 4, E),
    row('a0', 5, { subagent_scope: A }), row('a1', 6, { subagent_scope: A }),
    row('b0', 7, { subagent_scope: B }), row('c0', 8, { subagent_scope: C }),
    row('back', 9, { turn_start: true, turn_number: 2, origin: 'subagent', dispatched_by_subagent_scope: A }),
    row('plain', 10),
  ]
  let fed = 100
  const scoped: TrajectorySource = {
    ...source,
    list: async () => ({ epoch: 'e1', snapshot_revision: 100, entries: ROWS, next_cursor: null, index_state: indexState, complete: true }),
    detail: () => new Promise(() => {}),
  }
  const unrelated = (): TrajectoryChangesResult => {
    fed += 1
    return {
      epoch: 'e1', from_revision: fed - 1, to_revision: fed, upserts: [row('plain', 10, { preview: `plain ${fed}` })], removed: [],
      has_more: false, reset_required: false, index_state: READY,
    }
  }
  const tab = (at: number): HTMLElement => document.getElementById(scopes.scopeTabId(at)) as HTMLElement
  const closer = (at: number): HTMLElement => tab(at).parentElement!.querySelector('.trajectory-scope-close') as HTMLElement
  const listBox = (): HTMLElement => document.querySelector('.trajectory-list') as HTMLElement
  /* Turns start folded; a test that wants a row opens every closed turn first. */
  const entryRow = (id: string): HTMLElement => {
    for (const turn of document.querySelectorAll<HTMLElement>('[data-row="turn"]:not([data-open])')) fireEvent.click(turn)
    return document.querySelector(`[data-entry="${id}"]`) as HTMLElement
  }
  const active = (): Element | null => document.activeElement
  /* Focus first, then click: a pointer press focuses a button before its click. */
  const press = (el: HTMLElement): void => { act(() => { el.focus(); fireEvent.click(el) }) }
  /* A batch that touches nothing the reader is on, after which the focus must not move. */
  const stays = (): void => {
    const was = active()
    act(() => { store.applyChanges(unrelated()) })
    expect(active()).toBe(was)
  }

  beforeEach(() => {
    fed = 100
    details._resetForTests()
    setSources({ trajectory: scoped })
    details.install()
  })

  const shape = (): string[] => [...(document.querySelector('.trajectory-root') as HTMLElement).children].map((c) => c.className)

  it('puts the strip over everything, and the line naming the run under it', async () => {
    recording = false
    await draw()
    expect(shape()).toEqual(['trajectory-scope-wrap', 'trajectory-status', 'trajectory-body'])
    act(() => { store.openScope(A) })
    expect(shape()).toEqual(['trajectory-scope-wrap', 'trajectory-scope-info', 'trajectory-status', 'trajectory-body'])
  })

  it('walks the tabs with the arrows, the focus with them', async () => {
    await draw()
    act(() => { store.openScope(A); store.openScope(B); store.openScope(C) })
    act(() => { tab(3).focus() })
    const seen: Array<[string | null, boolean]> = []
    for (const key of ['ArrowLeft', 'ArrowLeft', 'ArrowLeft', 'ArrowRight', 'End']) {
      act(() => { fireEvent.keyDown(active() as HTMLElement, { key }) })
      const at = [null, A, B, C].indexOf(store.get().scope)
      seen.push([store.get().scope, active() === tab(at)])
    }
    expect(seen).toEqual([[B, true], [A, true], [null, true], [A, true], [C, true]])
  })

  it('hands the focus to the tab the close lands on, from the middle, the end and the last one', async () => {
    await draw()
    act(() => { store.openScope(A); store.openScope(B); store.openScope(C); store.switchScope(B) })
    press(closer(2))
    expect([store.get().scope, active()]).toEqual([C, tab(2)])
    stays()
    press(closer(2))
    expect([store.get().scope, active()]).toEqual([A, tab(1)])
    stays()
    press(closer(1))
    expect([store.get().scope, active()]).toEqual([null, tab(0)])
    stays()
  })

  it('leaves the focus where it was when a tab is closed from outside the strip', async () => {
    await draw()
    act(() => { store.openScope(A); store.openScope(B) })
    act(() => { listBox().focus() })
    act(() => { fireEvent.click(closer(1)) })
    expect([store.get().scope, active()]).toEqual([B, listBox()])
    stays()
  })

  it('keeps the focus on the strip across tabs whose panes differ, and hands it to the list when the reader closes the pane', async () => {
    await draw()
    const row = entryRow('m0')
    act(() => { fireEvent.click(row) })
    expect(details.get().open).toBe(true)
    act(() => { store.openScope(A) })
    press(tab(1))
    expect(details.get().open).toBe(false)
    press(tab(0))
    expect([details.get().open, active()]).toEqual([true, tab(0)])
    press(tab(1))
    expect([details.get().open, active()]).toEqual([false, tab(1)])
    press(tab(0))
    act(() => { fireEvent.click(document.querySelector('.trajectory-close') as HTMLElement) })
    expect([details.get().open, active()]).toEqual([false, listBox()])
  })

  it('keeps the focus in the list on a tab opened from it, or hands it to the tab when that tab has nothing to list', async () => {
    await draw()
    act(() => { store.select('doorA', { source: 'click' }); listBox().focus() })
    act(() => { fireEvent.keyDown(listBox(), { key: 'Enter' }) })
    expect([store.get().scope, active()]).toEqual([A, listBox()])
    stays()
    act(() => { store.switchScope(null) })
    act(() => { store.select('doorE', { source: 'click' }); listBox().focus() })
    act(() => { fireEvent.keyDown(listBox(), { key: 'Enter' }) })
    expect(store.get().scope).toBe(E)
    /* Every agent is on screen at once now, so opening a tab changes the
       selection, not what the page can draw. */
    expect(active()).toBe(listBox())
    stays()
  })

  it('hands the focus to the new tab from the pane\'s button, and to the list on the way back to the door', async () => {
    await draw()
    act(() => { store.select('doorB', { source: 'click' }) })
    press(document.querySelector('.trajectory-scope-jump') as HTMLElement)
    expect([store.get().scope, active()]).toEqual([B, tab(1)])
    stays()
    press(document.querySelector('.trajectory-scope-locate') as HTMLElement)
    expect([store.get().scope, store.get().selectedId, active()]).toEqual([null, 'doorB', listBox()])
    stays()
  })

  it('moves the focus nowhere, now or later, for a move that does nothing', async () => {
    await draw()
    act(() => { listBox().focus() })
    const plain = entryRow('plain')
    act(() => { fireEvent.doubleClick(plain) })
    expect(scopes.locate('t:no-door')).toBe(false)
    act(() => { store.closeScope('t:never-opened') })
    expect([store.get().scope, active()]).toEqual([null, listBox()])
    stays()
  })
})
