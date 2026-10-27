// @vitest-environment happy-dom
/* The Scope Tab strip and the line under it: when the strip shows, what a tab
   is called and says on hover, the marks that follow a run's state, closing,
   the keyboard, the overflow arrows, the line naming the run on screen, and
   the catalogue's words for all of it. */
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { useSyncExternalStore } from 'react'
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import * as lang from '../../state/lang'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as details from './detailStore'
import * as scopes from './scopes'
import { ScopeInfo, ScopeTabs } from './ScopeTabs'
import * as list from './store'

import type { TrajectoryChangesResult, TrajectoryEntry, TrajectoryIndexState, TrajectorySource } from './types'
import type { JSX } from 'react'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const READY: TrajectoryIndexState = {
  phase: 'ready', scanned_bytes: 10, total_bytes: 10, head_truncated: 0, recovering_traces: 0,
  unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
}

const entry = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', trace_id: 't',
  span_id: id, parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main',
  sort_key: [String(at).padStart(6, '0'), 0], event_time: '2026-01-01T00:00:00Z', preview: id,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], operation_start: null,
  operation_end: null, duration_ms: null, charged_ms: null, timing_basis: 'not_recorded', duration_owner: null,
  meta: {},
  ...over,
})

const door = (id: string, at: number, opens: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
  entry(id, at, { kind: 'subagent.run', span_name: 'subagent.run', slot: 'subagent.run', opens_subagent_scope: opens, ...over })
const step = (id: string, at: number, scope: string): TrajectoryEntry => entry(id, at, { subagent_scope: scope })

const A = 't:runA'
const B = 't:runB'
const C = 't:runC'

let rows: TrajectoryEntry[] = []

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async () => ({ epoch: 'e1', snapshot_revision: 100, entries: rows, next_cursor: null, index_state: READY, complete: true }),
  changes: () => Promise.reject(new Error('not scripted')),
  detail: () => new Promise(() => {}),
  block: () => new Promise(() => {}),
}

const q = <T extends Element = HTMLElement>(sel: string): T | null => document.querySelector<T>(sel)
const tabs = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.trajectory-scope-tab')]
const titles = (): string[] => tabs().map((b) => b.querySelector('.trajectory-scope-title')?.textContent ?? '')
const batch = (over: Partial<TrajectoryChangesResult>): TrajectoryChangesResult => ({
  epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [], removed: [], has_more: false, reset_required: false,
  index_state: READY, ...over,
})

async function draw(): Promise<void> {
  render(<><ScopeTabs /><ScopeInfo /></>)
  await act(async () => {
    await list.refreshState()
    list.setView('trajectory')
    await list.load()
  })
}

beforeEach(() => {
  rows = []
  list._resetForTests()
  details._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  absorb(['trajectory-v1'])
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ trajectory: source })
  document.body.innerHTML = '<div class="chat"></div>'
  list.install()
  details.install()
  unpitch()
  list.sessionChanged('gui:a')
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('the strip', () => {
  it('is not there for a conversation with no sub-agent, and is the main session alone until a tab is opened', async () => {
    rows = [entry('m0', 0), entry('m1', 1)]
    await draw()
    expect(q('.trajectory-scope-wrap')).toBeNull()
    cleanup()
    list._resetForTests()
    list.sessionChanged('gui:a')
    rows = [entry('m0', 0), door('d', 1, A), step('a0', 2, A)]
    await draw()
    expect(q('[role="tablist"]')?.getAttribute('aria-label')).toBe('gui.trajectory.scope.strip')
    expect(titles()).toEqual(['gui.trajectory.scope.main'])
    expect(tabs()[0]!.getAttribute('aria-selected')).toBe('true')
    expect(q('.trajectory-scope-info')).toBeNull()
  })

  it('names a tab by agent and task, else the task, the task id, the graph tool, or the bare word', async () => {
    rows = [
      door('d1', 0, 'x:1', { meta: { agent: 'coder', label: 'Fix the parser', task_id: 'k1' } }),
      door('d2', 1, 'x:2', { meta: { label: 'Write the docs', task_id: 'k2' } }),
      door('d3', 2, 'x:3', { meta: { task_id: 'k3' } }),
      entry('d4', 3, { meta: { tool: 'run_subagent_dag' }, opens_subagent_scope: 'x:4' }),
      door('d5', 4, 'x:5', { meta: { agent: 'coder', label: '  ' } }),
    ]
    await draw()
    act(() => { for (const id of ['x:1', 'x:2', 'x:3', 'x:4', 'x:5']) list.openScope(id) })
    expect(titles()).toEqual([
      'gui.trajectory.scope.main', 'coder · Fix the parser', 'Write the docs', 'k3', 'run_subagent_dag', 'gui.trajectory.scope.untitled',
    ])
  })

  it('says on hover what is known about the run, one fact a line', async () => {
    rows = [
      door('d', 0, A, {
        turn_number: 3, duration_ms: 65_000, operation_status: 'ok',
        meta: { agent: 'coder', instance: 'coder-2', label: 'Fix the parser and add the missing tests' },
      }),
      door('bare', 1, B, { turn_number: null, operation_status: 'ok' }),
    ]
    await draw()
    act(() => { list.openScope(A); list.openScope(B) })
    expect(tabs()[1]!.title.split('\n')).toEqual([
      'Fix the parser and add the missing tests',
      'gui.trajectory.scope.agent {"name":"coder"}',
      'gui.trajectory.scope.instance {"name":"coder-2"}',
      'gui.trajectory.scope.dispatched_in {"n":3}',
      'gui.trajectory.status.ok',
      '1m 5s',
    ])
    expect(tabs()[2]!.title).toBe('gui.trajectory.status.ok')
  })

  it('marks a running, failed or cancelled run, and drops the mark when the run ends, without a reload', async () => {
    rows = [
      door('d1', 0, A, { operation_status: 'running' }),
      door('d2', 1, B, { operation_status: 'error' }),
      door('d3', 2, C, { operation_status: 'cancelled' }),
      door('d4', 3, 'x:ok', { operation_status: 'ok' }),
      door('d5', 4, 'x:unknown', { operation_status: 'unknown' }),
    ]
    await draw()
    act(() => { for (const id of [A, B, C, 'x:ok', 'x:unknown']) list.openScope(id) })
    const marks = (): Array<string | null> => tabs().slice(1).map((b) => {
      const mark = b.querySelector('.trajectory-scope-mark')
      return mark ? `${mark.className} ${mark.getAttribute('aria-label')}` : null
    })
    expect(marks()).toEqual([
      'trajectory-scope-mark trajectory-scope-mark-running gui.trajectory.status.running',
      'trajectory-scope-mark trajectory-scope-mark-error gui.trajectory.status.error',
      'trajectory-scope-mark trajectory-scope-mark-cancelled gui.trajectory.status.cancelled',
      null,
      null,
    ])
    act(() => { list.applyChanges(batch({ upserts: [door('d1', 0, A, { operation_status: 'ok', duration_ms: 4000 })] })) })
    expect(marks()[0]).toBeNull()
  })

  it('keeps the title a tab was opened with when its door goes, and says when its records are gone', async () => {
    rows = [door('d', 0, A, { meta: { label: 'Fix the parser' }, operation_status: 'running' }), step('a0', 1, A), door('e', 2, B)]
    await draw()
    act(() => { list.openScope(A) })
    act(() => {
      list.applyChanges(batch({ removed: [{ entry_id: 'd', revision: 101, replaced_by: null }, { entry_id: 'a0', revision: 101, replaced_by: null }] }))
    })
    expect(titles()[1]).toBe('Fix the parser')
    expect(scopes.scopeState(list.get(), A)).toBe('gone')
    expect(q('.trajectory-scope-state')?.textContent).toBe('gui.trajectory.scope.gone')
    expect(q('.trajectory-scope-locate')).toBeNull()
  })

  it('closes a tab with its own button, named after the tab', async () => {
    rows = [door('d', 0, A, { meta: { label: 'Fix' } }), door('e', 1, B, { meta: { label: 'Docs' } })]
    await draw()
    act(() => { list.openScope(A); list.openScope(B) })
    const closers = [...document.querySelectorAll<HTMLElement>('.trajectory-scope-close')]
    expect(closers.map((c) => c.getAttribute('aria-label'))).toEqual([
      'gui.trajectory.scope.close_named {"title":"Fix"}', 'gui.trajectory.scope.close_named {"title":"Docs"}',
    ])
    act(() => { fireEvent.click(closers[0]!) })
    expect(titles()).toEqual(['gui.trajectory.scope.main', 'Docs'])
    expect(list.get().scope).toBe(B)
  })

  it('lets only the tab on screen and its close button into the Tab order, and walks the tabs with the arrows, Home and End', async () => {
    rows = [door('d', 0, A), door('e', 1, B)]
    await draw()
    act(() => { list.openScope(A); list.openScope(B) })
    const order = (): Array<string | null> => [...document.querySelectorAll<HTMLElement>('.trajectory-scope-tab, .trajectory-scope-close')]
      .map((el) => el.getAttribute('tabindex'))
    expect(order()).toEqual(['-1', '-1', '-1', '0', '0'])
    const strip = q('[role="tablist"]') as HTMLElement
    const key = (k: string): void => { act(() => { fireEvent.keyDown(document.activeElement ?? strip, { key: k }) }) }
    act(() => { tabs()[2]!.focus() })
    key('ArrowLeft')
    expect([list.get().scope, document.activeElement]).toEqual([A, tabs()[1]])
    key('Home')
    expect([list.get().scope, document.activeElement]).toEqual([null, tabs()[0]])
    key('ArrowLeft')
    expect(list.get().scope).toBeNull()
    key('End')
    expect([list.get().scope, document.activeElement]).toEqual([B, tabs()[2]])
    key('ArrowRight')
    expect(list.get().scope).toBe(B)
    act(() => { fireEvent.click(tabs()[0]!) })
    expect(list.get().scope).toBeNull()
    expect(order()).toEqual(['0', '-1', '-1', '-1', '-1'])
  })

  it('shows an arrow at either end once the tabs outrun the strip, and moves it by most of its width', async () => {
    rows = [door('d', 0, A), door('e', 1, B)]
    const widths = { client: 300, scroll: 900 }
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} })
    await draw()
    const strip = q('.trajectory-scope-tabs') as HTMLElement
    Object.defineProperty(strip, 'clientWidth', { get: () => widths.client, configurable: true })
    Object.defineProperty(strip, 'scrollWidth', { get: () => widths.scroll, configurable: true })
    act(() => { list.openScope(A) })
    const arrows = (): HTMLButtonElement[] => [...document.querySelectorAll<HTMLButtonElement>('.trajectory-scope-arrow')]
    expect(arrows()).toHaveLength(2)
    expect(arrows().map((a) => a.disabled)).toEqual([true, false])
    act(() => { fireEvent.click(arrows()[1]!) })
    expect(strip.scrollLeft).toBe(240)
    expect(arrows().map((a) => a.disabled)).toEqual([false, false])
  })
})

describe('keeping the tab on screen in view', () => {
  it('scrolls the strip alone, never the page around it', async () => {
    rows = [door('d', 0, A), door('e', 1, B)]
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} })
    const intoView = vi.fn()
    const was = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = intoView
    try {
      await draw()
      act(() => { list.openScope(A) })
      const strip = q('.trajectory-scope-tabs') as HTMLElement
      const rect = (left: number, width: number): DOMRect => ({ left, right: left + width, top: 0, bottom: 30, width, height: 30, x: left, y: 0, toJSON: () => ({}) })
      strip.getBoundingClientRect = () => rect(0, 300)
      strip.scrollLeft = 40
      /* The tab about to be on screen sits past the strip's right edge by 150px. */
      act(() => { list.openScope(B) })
      ;(document.getElementById(scopes.scopeTabId(2)) as HTMLElement).getBoundingClientRect = () => rect(350, 100)
      act(() => { list.switchScope(A) })
      act(() => { list.switchScope(B) })
      expect(strip.scrollLeft).toBe(190)
      /* And one past its left edge comes back the other way. */
      ;(document.getElementById(scopes.scopeTabId(0)) as HTMLElement).getBoundingClientRect = () => rect(-60, 80)
      act(() => { list.switchScope(null) })
      expect(strip.scrollLeft).toBe(130)
      expect(intoView).not.toHaveBeenCalled()
    } finally {
      Element.prototype.scrollIntoView = was
    }
  })
})

describe('the overflow arrows as the tabs change what they say', () => {
  /* The strip's own box stays 300px wide; what is inside it is as wide as its
     words, `per` pixels a character and ten for a state mark. */
  function contentSized(per: number): HTMLElement {
    const strip = q('.trajectory-scope-tabs') as HTMLElement
    Object.defineProperty(strip, 'clientWidth', { get: () => 300, configurable: true })
    Object.defineProperty(strip, 'scrollWidth', {
      get: () => [...strip.querySelectorAll('.trajectory-scope-title')].reduce((n, el) => n + (el.textContent ?? '').length * per, 0)
        + strip.querySelectorAll('.trajectory-scope-mark').length * 10,
      configurable: true,
    })
    return strip
  }
  const arrows = (): number => document.querySelectorAll('.trajectory-scope-arrow').length
  const renamed = (label: string, status: TrajectoryEntry['operation_status'] = 'ok'): TrajectoryChangesResult =>
    batch({ upserts: [door('d', 0, A, { meta: { label }, operation_status: status })] })

  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} })
  })

  it('brings the arrows when a tab\'s title grows, and takes them away when it shrinks, with the tabs and the tab on screen unchanged', async () => {
    /* The main tab reads as its 25-character key here: 125px at five a character. */
    rows = [door('d', 0, A, { meta: { label: 'Fix' } })]
    await draw()
    contentSized(5)
    act(() => { list.openScope(A) })
    expect(arrows()).toBe(0)
    act(() => { list.applyChanges(renamed('Fix the parser and the flaky login test')) })
    expect(arrows()).toBe(2)
    act(() => { list.applyChanges({ ...renamed('Fix'), from_revision: 101, to_revision: 102 }) })
    expect(arrows()).toBe(0)
  })

  it('measures again when a mark comes or goes', async () => {
    /* 125px for the main tab and 170 for this one: the strip's 300 fit until a mark takes ten more. */
    const label = 'Fix the parser and test it all now'
    rows = [door('d', 0, A, { meta: { label } })]
    await draw()
    contentSized(5)
    act(() => { list.openScope(A) })
    expect(arrows()).toBe(0)
    act(() => { list.applyChanges(renamed(label, 'running')) })
    expect(arrows()).toBe(2)
    act(() => { list.applyChanges({ ...renamed(label), from_revision: 101, to_revision: 102 }) })
    expect(arrows()).toBe(0)
  })

  it('measures again in another language', async () => {
    resetTranslator()
    function Island(): JSX.Element {
      useSyncExternalStore(lang.subscribe, lang.get)
      return <ScopeTabs />
    }
    rows = [door('d', 0, A), door('e', 1, B)]
    render(<Island />)
    await act(async () => {
      await list.refreshState()
      list.setView('trajectory')
      await list.load()
    })
    try {
      act(() => { lang.set('en') })
      contentSized(20)
      act(() => { list.openScope(A); list.openScope(B) })
      expect(arrows()).toBe(2)
      act(() => { lang.set('zh') })
      expect(arrows()).toBe(0)
      act(() => { lang.set('en') })
      expect(arrows()).toBe(2)
    } finally {
      act(() => { lang._resetForTests() })
    }
  })
})

describe('the line under the strip', () => {
  it('names the run on screen and leads back to its door in the main session', async () => {
    rows = [
      entry('m0', 0),
      door('d', 1, A, { turn_number: 2, duration_ms: 3000, operation_status: 'ok', meta: { agent: 'coder', label: 'Fix the parser' } }),
      step('a0', 2, A),
    ]
    await draw()
    act(() => { list.openScope(A) })
    const info = q('.trajectory-scope-info') as HTMLElement
    expect(info.getAttribute('aria-label')).toBe('coder · Fix the parser')
    expect([...info.children].map((c) => c.textContent)).toEqual([
      'coder', 'Fix the parser', 'gui.trajectory.status.ok', '3s', 'gui.trajectory.scope.dispatched_in {"n":2}', 'gui.trajectory.scope.locate',
    ])
    act(() => { fireEvent.click(q('.trajectory-scope-locate') as HTMLElement) })
    expect([list.get().scope, list.get().selectedId, list.get().selectedBy]).toEqual([null, 'd', 'link'])
    expect(q('.trajectory-scope-info')).toBeNull()
  })

  it('leaves out what is not known about the run', async () => {
    rows = [door('d', 0, A, { turn_number: null, operation_status: 'running' }), step('a0', 1, A)]
    await draw()
    act(() => { list.openScope(A) })
    expect([...(q('.trajectory-scope-info') as HTMLElement).children].map((c) => c.className)).toEqual([
      'trajectory-scope-state trajectory-status-running', 'trajectory-link trajectory-scope-locate',
    ])
  })
})

describe('the words', () => {
  const catalogue = (): Record<string, { en: string; zh: string }> =>
    (JSON.parse(readFileSync('../i18n/messages.json', 'utf8') as string) as { ui: Record<string, { en: string; zh: string }> }).ui
  const KEYS = [
    'strip', 'main', 'untitled', 'close_named', 'open', 'opens', 'from', 'locate', 'dispatched_in', 'agent', 'instance', 'reported',
    'empty_running', 'empty_done', 'gone',
  ]

  it('has every word in both languages, and the tab words in Chinese at most four characters', () => {
    const ui = catalogue()
    const wrong: string[] = []
    for (const name of KEYS) {
      const value = ui[`gui.trajectory.scope.${name}`]
      if (!value || !value.en.trim() || !value.zh.trim()) wrong.push(name)
    }
    for (const name of ['main', 'untitled', 'open']) {
      const zh = ui[`gui.trajectory.scope.${name}`]?.zh ?? ''
      if ([...zh].length > 4) wrong.push(`${name}: ${zh}`)
    }
    expect(wrong).toEqual([])
  })
})
