// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as confirm from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'
import { ExportApp } from './ExportApp'
import * as store from './store'

import type { ExportSource, ExportTurn, ReportStatus } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const turn = (n: number): ExportTurn => ({
  trace_id: `t${n}`, number: n, start: '2026-10-01T10:00:00Z', input_preview: `input ${n}`, in_progress: false,
})

const SUMMARY: NonNullable<ReportStatus['summary']> = {
  classification: 'needs_review', warnings: ['a value was kept'], size_bytes: null,
  auto_replaced: 4, kept: 2, redacted: 1, acknowledged: 1, completeness: 'complete',
}

let answers: ReportStatus[]
let uploads: boolean[]
let retries: number
let cancels: string[]

const status = (phase: ReportStatus['phase'], patch: Partial<ReportStatus> = {}): ReportStatus => ({
  report_id: 'rs-1', bug_report_id: 'br-20261014-abc', phase, error: null,
  history: { state: 'on', auto_kept: 0, auto_redacted: 0, warning: null }, items: null, summary: SUMMARY,
  receipt: null, record_dir: null, ...patch,
})

const src: ExportSource = {
  turns: async () => ({ title: 'Plan', turns: [turn(1), turn(2)] }),
  start: () => Promise.reject(new Error('not scripted')),
  status: () => Promise.reject(new Error('not scripted')),
  cancel: async () => ({ ok: true }),
  reportStart: async () => ({ report_id: 'rs-1' }),
  reportStatus: async () => (answers.length > 1 ? answers.shift()! : answers[0]!),
  reportItem: () => Promise.reject(new Error('not scripted')),
  reportDecide: () => Promise.reject(new Error('not scripted')),
  reportUpload: async (_id, risk) => { uploads.push(risk); return { ok: true } },
  reportRetry: async () => { retries += 1; return { ok: true } },
  reportCancel: async (id) => { cancels.push(id); return { ok: true } },
}

const FRAME = '<div id="exportVeil" data-open="false"><div id="exportModal" tabindex="-1"><div id="exportBody"></div></div></div>'
const $ = <T extends Element = HTMLElement>(sel: string): T | null => document.querySelector<T>(sel)
const settle = async (ms = 0): Promise<void> => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const tap = (el: Element): void => { act(() => { fireEvent.click(el) }) }
const primary = (): HTMLButtonElement => $<HTMLButtonElement>('.export-primary')!
const text = (): string => document.body.textContent ?? ''

async function atUpload(...first: ReportStatus[]): Promise<void> {
  answers = first
  document.body.innerHTML = FRAME
  render(<ExportApp />, { container: $('#exportBody')! })
  act(() => { store.open('report', 'gui:a') })
  await settle()
  tap(primary())
  act(() => { store.editProblem('description', 'it hangs') })
  tap(primary())
  await settle()
}

beforeEach(() => {
  vi.useFakeTimers()
  uploads = []
  retries = 0
  cancels = []
  setSources({ export: src })
  setTranslator((key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key))
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  confirm._resetForTests()
  resetSources()
  resetTranslator()
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('the upload page', () => {
  it('sums up what will ship and marks the decisions skipped when there were none', async () => {
    await atUpload(status('ready'))
    expect(store.get().page).toBe('upload')
    expect($('.export-summary')!.textContent).toContain('br-20261014-abc')
    expect(text()).toContain('gui.export.summary_counts:{"auto":4,"kept":2,"redacted":1,"acknowledged":1}')
    expect(text()).not.toContain('gui.export.summary_size')
    expect($('.export-warnings')!.textContent).toBe('a value was kept')
    expect(text()).not.toContain('complete')
    expect($('.export-step[data-state="skipped"]')!.textContent).toContain('gui.export.step_decide')
  })

  it('uploads a report that needs review only with the risk accepted', async () => {
    await atUpload(status('ready'), status('uploaded', { receipt: { receipt_id: 'rcpt-9', duplicate: true }, summary: { ...SUMMARY, size_bytes: 2048 } }))
    expect(primary().disabled).toBe(true)
    tap($('.export-risk input')!)
    expect(primary().disabled).toBe(false)
    tap(primary())
    await settle()
    expect(uploads).toEqual([true])
    expect(text()).toContain('gui.export.uploaded:{"receipt":"rcpt-9"}')
    expect(text()).toContain('gui.export.duplicate')
    expect(text()).toContain('2.0 KB')
    expect(primary().textContent).toBe('gui.export.finish')
    tap(primary())
    expect(store.get().open).toBe(false)
    expect(cancels).toEqual(['rs-1'])
  })

  it('needs no risk accepted for a clean report, and shows the upload under way', async () => {
    await atUpload(status('ready', { summary: { ...SUMMARY, classification: 'clean' } }), status('uploading'))
    expect($('.export-risk')).toBeNull()
    tap(primary())
    await settle()
    expect(text()).toContain('gui.export.uploading')
    expect(primary().disabled).toBe(true)
  })

  it('offers a retry for a failure that may pass, and the command line for a kept report', async () => {
    await atUpload(
      status('upload_failed', { error: { message: 'unreachable', retryable: true, status: null }, record_dir: '/state/bugreports/br-1' }),
      status('uploaded', { receipt: { receipt_id: 'rcpt-1', duplicate: false } }),
    )
    expect(text()).toContain('gui.export.upload_failed:{"error":"unreachable"}')
    expect(text()).toContain('gui.export.kept_local:{"dir":"/state/bugreports/br-1","id":"br-20261014-abc"}')
    expect(primary().textContent).toBe('gui.export.retry')
    tap(primary())
    await settle()
    expect(retries).toBe(1)
    expect(text()).not.toContain('gui.export.kept_local')
  })

  it('offers no retry for a refused upload', async () => {
    await atUpload(status('upload_failed', { error: { message: 'bad request', retryable: false, status: 400 }, record_dir: '/kept' }))
    expect(primary().textContent).toBe('gui.export.upload')
    expect(primary().disabled).toBe(true)
    expect(text()).toContain('gui.export.kept_local')
  })
})
