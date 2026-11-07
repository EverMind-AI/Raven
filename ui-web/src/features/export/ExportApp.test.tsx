// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { RpcError } from '../../rpc/transport'
import * as confirm from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'
import { ExportApp } from './ExportApp'
import * as store from './store'

import type { ExportSource, ExportStatus, ExportTurn } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const turn = (n: number, inProgress = false): ExportTurn => ({
  trace_id: `t${n}`, number: n, start: '2026-10-01T10:00:00Z', input_preview: `input ${n}`, in_progress: inProgress,
})

const done: ExportStatus = {
  job_id: 'exj-1', state: 'done', phase: 'done', file_name: 'Plan.tar.gz',
  download_url: '/trajectory/export/download?token=abc', size_bytes: 2048,
  counts: { spans: 3, artifacts: 1, messages: 2 }, error: null,
}
const running: ExportStatus = { ...done, state: 'running', phase: 'writing', download_url: null, size_bytes: null, counts: null }

let turns: ExportTurn[]
let nextTurns: ExportTurn[] | null
let refusal: RpcError | null
let names: string[]
let statuses: Array<ExportStatus | Error>
let cancels: string[]

const src: ExportSource = {
  turns: async () => {
    const listed = turns
    if (nextTurns) turns = nextTurns
    return { title: 'Plan', turns: listed }
  },
  start: async (_key, _ids, name) => {
    names.push(name)
    if (refusal) {
      const err = refusal
      refusal = null
      throw err
    }
    return { job_id: 'exj-1' }
  },
  status: async () => {
    const next = statuses.length > 1 ? statuses.shift()! : statuses[0]!
    if (next instanceof Error) throw next
    return next
  },
  cancel: async (jobId) => { cancels.push(jobId); return { ok: true } },
  reportProbe: async () => ({ reachable: true, target: 'https://bugs.example', reason: '' }),
  reportStart: () => Promise.reject(new Error('not scripted')),
  reportStatus: () => Promise.reject(new Error('not scripted')),
  reportItem: () => Promise.reject(new Error('not scripted')),
  reportDecide: () => Promise.reject(new Error('not scripted')),
  reportUpload: () => Promise.reject(new Error('not scripted')),
  reportRetry: () => Promise.reject(new Error('not scripted')),
  reportCancel: async () => ({ ok: true }),
}

const FRAME = '<div id="exportVeil" data-open="false"><div id="exportModal" tabindex="-1"><div id="exportBody"></div></div></div>'

const $ = <T extends Element = HTMLElement>(sel: string): T | null => document.querySelector<T>(sel)
const settle = async (ms = 0): Promise<void> => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const press = (sel: string): void => { act(() => { fireEvent.click($(sel)!) }) }
const primary = (): HTMLButtonElement => $<HTMLButtonElement>('.export-primary')!

async function show(): Promise<void> {
  document.body.innerHTML = FRAME
  render(<ExportApp />, { container: $('#exportBody')! })
  act(() => { store.open('export', 'gui:a') })
  await settle()
}

let clicks: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.useFakeTimers()
  turns = [turn(1), turn(2), turn(3, true)]
  nextTurns = null
  refusal = null
  names = []
  statuses = [done]
  cancels = []
  setSources({ export: src })
  setTranslator((key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key))
  clicks = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  confirm._resetForTests()
  resetSources()
  resetTranslator()
  vi.restoreAllMocks()
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('the export wizard', () => {
  it('raises and lowers its veil with the store', async () => {
    await show()
    expect($('#exportVeil')!.dataset.open).toBe('true')
    expect($('.export-steps')).not.toBeNull()
    press('#exportClose')
    expect($('#exportVeil')!.dataset.open).toBe('false')
    expect($('.export-app')).toBeNull()
  })

  it('draws the picker with a three-state select-all and the running turn locked', async () => {
    await show()
    const all = $<HTMLInputElement>('.export-all')!
    expect(all.checked).toBe(true)
    expect(all.indeterminate).toBe(false)
    const boxes = [...document.querySelectorAll<HTMLInputElement>('.export-turn input')]
    expect(boxes.map((box) => box.disabled)).toEqual([false, false, true])
    expect(boxes.map((box) => box.checked)).toEqual([true, true, false])
    act(() => { fireEvent.click(boxes[0]!) })
    expect(all.indeterminate).toBe(true)
    expect(all.checked).toBe(false)
    act(() => { fireEvent.click(boxes[1]!) })
    expect(primary().disabled).toBe(true)
    act(() => { fireEvent.click(all) })
    expect(store.get().selected).toEqual(['t1', 't2'])
    expect(primary().disabled).toBe(false)
  })

  it('suggests a name, warns that nothing is redacted, and exports under the typed name', async () => {
    await show()
    press('.export-primary')
    const input = $<HTMLInputElement>('.export-name')!
    expect(input.value).toMatch(/^Plan-\d{8}-\d{4}\.tar\.gz$/)
    expect($('[role="note"]')!.textContent).toBe('gui.export.unredacted')
    act(() => {
      input.value = 'mine.tar.gz'
      fireEvent.input(input)
    })
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })
    expect(names).toEqual([])
    press('.export-primary')
    await settle()
    expect(names).toEqual(['mine.tar.gz'])
  })

  it('keeps the typed name through a step back and sends what it shows', async () => {
    await show()
    press('.export-primary')
    const input = (): HTMLInputElement => $<HTMLInputElement>('.export-name')!
    const suggested = input().value
    act(() => {
      input().value = 'mine.tar.gz'
      fireEvent.input(input())
    })
    press('.export-ghost')
    expect($('.export-name')).toBeNull()
    press('.export-primary')
    expect(input().value).toBe('mine.tar.gz')
    press('.export-primary')
    await settle()
    expect(names).toEqual(['mine.tar.gz'])
    press('.export-primary')
    act(() => { store.open('export', 'gui:a') })
    await settle()
    press('.export-primary')
    expect(input().value).toBe(suggested)
  })

  it('keeps the typed name through a refused selection', async () => {
    refusal = new RpcError(-32602, 'refused', { reason: 'invalid_selection', invalid: [{ trace_id: 't2', why: 'in_progress' }] })
    await show()
    press('.export-primary')
    const input = (): HTMLInputElement => $<HTMLInputElement>('.export-name')!
    act(() => {
      input().value = 'mine.tar.gz'
      fireEvent.input(input())
    })
    press('.export-primary')
    await settle()
    expect(store.get().page).toBe('turns')
    press('.export-primary')
    expect(input().value).toBe('mine.tar.gz')
    press('.export-primary')
    await settle()
    expect(names).toEqual(['mine.tar.gz', 'mine.tar.gz'])
  })

  it('names a refused turn the conversation no longer has, and never lets it be chosen', async () => {
    refusal = new RpcError(-32602, 'refused', { reason: 'invalid_selection', invalid: [{ trace_id: 't2', why: 'not_in_session' }] })
    nextTurns = [turn(1), turn(3, true)]
    await show()
    press('.export-primary')
    press('.export-primary')
    await settle()
    expect(store.get().page).toBe('turns')
    const rows = [...document.querySelectorAll<HTMLElement>('.export-turn')]
    expect(rows).toHaveLength(3)
    const gone = rows[2]!
    expect(gone.textContent).toContain('input 2')
    expect(gone.textContent).toContain('gui.export.why_not_in_session')
    expect(gone.querySelector('input')!.disabled).toBe(true)
    expect(document.body.textContent).toContain('gui.export.refused_turns')
    act(() => { fireEvent.click($('.export-all')!) })
    act(() => { fireEvent.click($('.export-all')!) })
    expect(store.get().selected).toEqual(['t1'])
  })

  it('still names the refused turns when the conversation has none left', async () => {
    refusal = new RpcError(-32602, 'refused', { reason: 'invalid_selection', invalid: [{ trace_id: 't1', why: 'not_in_session' }] })
    turns = [turn(1)]
    nextTurns = []
    await show()
    expect(store.get().page).toBe('export')
    press('.export-primary')
    await settle()
    expect(document.body.textContent).toContain('gui.export.no_turns')
    expect($('.export-turn[data-bad]')!.textContent).toContain('gui.export.why_not_in_session')
    expect($('.export-all')).toBeNull()
    expect(primary().disabled).toBe(true)
  })

  it('starts on Enter outside a composition', async () => {
    await show()
    press('.export-primary')
    act(() => { fireEvent.keyDown($('.export-name')!, { key: 'Enter' }) })
    await settle()
    expect(names).toHaveLength(1)
  })

  it('downloads once when the job is done and keeps the link to download again', async () => {
    await show()
    press('.export-primary')
    press('.export-primary')
    await settle()
    const link = $<HTMLAnchorElement>('.export-download')!
    expect(link.getAttribute('href')).toBe('/trajectory/export/download?token=abc')
    expect(link.getAttribute('download')).toBe('Plan.tar.gz')
    expect(clicks).toHaveBeenCalledTimes(1)
    await settle(5000)
    act(() => { store.set({ title: 'redrawn' }) })
    expect(clicks).toHaveBeenCalledTimes(1)
    expect(primary().textContent).toBe('gui.export.finish')
    expect($('.export-ghost')).toBeNull()
    press('.export-primary')
    expect(store.get().open).toBe(false)
    expect(cancels).toEqual([])
  })

  it('says the progress is lost, offers to check now, and downloads once it is back', async () => {
    statuses = [new Error('socket closed'), done]
    await show()
    press('.export-primary')
    press('.export-primary')
    await settle()
    expect($('.export-lost')!.textContent).toContain('gui.export.lost')
    expect(primary().disabled).toBe(true)
    press('.export-lost .export-btn')
    await settle()
    expect($('.export-lost')).toBeNull()
    expect(clicks).toHaveBeenCalledTimes(1)
    expect(names).toHaveLength(1)
  })

  it('asks before closing over a running job', async () => {
    statuses = [running]
    await show()
    press('.export-primary')
    press('.export-primary')
    await settle()
    expect($('.export-state')!.textContent).toBe('gui.export.phase_writing')
    expect($('.export-ghost')).toBeNull()
    press('#exportClose')
    expect(confirm.get().open).toBe(true)
    expect(store.get().open).toBe(true)
    act(() => { confirm.answer(true) })
    expect(store.get().open).toBe(false)
    expect(cancels).toEqual(['exj-1'])
  })

  it('marks the turns the gateway refused on the picker', async () => {
    await show()
    act(() => {
      store.set({ invalid: { t1: 'not_in_session' }, error: { kind: 'changed' } })
    })
    expect($('.export-turn[data-bad] .export-why')!.textContent).toBe('gui.export.why_not_in_session')
    expect($('[role="alert"]')!.textContent).toBe('gui.export.selection_changed')
  })
})
