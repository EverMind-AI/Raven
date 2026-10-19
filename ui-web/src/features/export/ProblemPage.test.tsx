// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as confirm from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'
import { ExportApp } from './ExportApp'
import * as store from './store'

import type { ExportSource, ExportTurn, Problem, ReportProbe, ReportStatus } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const turn = (n: number): ExportTurn => ({
  trace_id: `t${n}`, number: n, start: '2026-10-01T10:00:00Z', input_preview: `input ${n}`, in_progress: false,
})

let filed: Problem[]
let probes: number
let onProbe: () => Promise<ReportProbe>
const REACHABLE: ReportProbe = { reachable: true, target: 'https://bugs.example', reason: '' }
const DOWN: ReportProbe = { reachable: false, target: 'https://bugs.example', reason: 'could not reach bugs.example' }
const collecting: ReportStatus = {
  report_id: 'rs-1', bug_report_id: null, phase: 'collecting', error: null,
  history: { state: 'on', auto_kept: 0, auto_redacted: 0, warning: null }, items: null, summary: null, receipt: null, record_dir: null,
}

const src: ExportSource = {
  turns: async () => ({ title: 'Plan', turns: [turn(1), turn(2)] }),
  start: () => Promise.reject(new Error('not scripted')),
  status: () => Promise.reject(new Error('not scripted')),
  cancel: async () => ({ ok: true }),
  reportProbe: () => { probes += 1; return onProbe() },
  reportStart: async (_key, _ids, problem) => { filed.push(problem); return { report_id: 'rs-1' } },
  reportStatus: async () => collecting,
  reportItem: () => Promise.reject(new Error('not scripted')),
  reportDecide: () => Promise.reject(new Error('not scripted')),
  reportUpload: () => Promise.reject(new Error('not scripted')),
  reportRetry: () => Promise.reject(new Error('not scripted')),
  reportCancel: async () => ({ ok: true }),
}

const FRAME = '<div id="exportVeil" data-open="false"><div id="exportModal" tabindex="-1"><div id="exportBody"></div></div></div>'
const $ = <T extends Element = HTMLElement>(sel: string): T => document.querySelector<T>(sel)!
const settle = async (ms = 0): Promise<void> => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const press = (sel: string): void => { act(() => { fireEvent.click($(sel)) }) }
const fields = (): HTMLTextAreaElement[] => [...document.querySelectorAll<HTMLTextAreaElement>('.export-text')]
const type = (el: HTMLInputElement | HTMLTextAreaElement, value: string): void => {
  act(() => {
    el.value = value
    fireEvent.input(el)
  })
}

async function atProblem(): Promise<void> {
  document.body.innerHTML = FRAME
  render(<ExportApp />, { container: $('#exportBody') })
  act(() => { store.open('report', 'gui:a') })
  await settle()
  press('.export-primary')
}

beforeEach(() => {
  vi.useFakeTimers()
  filed = []
  probes = 0
  onProbe = async () => REACHABLE
  setSources({ export: src })
  setTranslator((key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key))
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  confirm._resetForTests()
  resetSources()
  resetTranslator()
  localStorage.clear()
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('the problem page', () => {
  it('needs a description before it can be filed, and defaults to a medium severity', async () => {
    await atProblem()
    expect(store.get().page).toBe('problem')
    expect($('.export-primary').hasAttribute('disabled')).toBe(true)
    expect($<HTMLSelectElement>('.export-select').value).toBe('medium')
    type(fields()[0]!, '   ')
    expect($('.export-primary').hasAttribute('disabled')).toBe(true)
    type(fields()[0]!, 'the export hangs')
    expect($('.export-primary').hasAttribute('disabled')).toBe(false)
  })

  it('files every field as typed and remembers the reporter', async () => {
    await atProblem()
    const [description, expected, actual, steps] = fields()
    type(description!, 'it hangs')
    type(expected!, 'a file')
    type(actual!, 'nothing')
    type(steps!, 'click export')
    act(() => {
      fireEvent.change($('.export-select'), { target: { value: 'critical' } })
    })
    type($<HTMLInputElement>('.export-name'), 'sam')
    press('.export-primary')
    await settle()
    expect(filed).toEqual([{
      description: 'it hangs', severity: 'critical', expected: 'a file', actual: 'nothing', steps: 'click export', reporter: 'sam',
    }])
    expect(localStorage.getItem(store.REPORTER_KEY)).toBe('sam')
  })

  it('fills the reporter from the last report', async () => {
    localStorage.setItem(store.REPORTER_KEY, 'kim')
    await atProblem()
    expect($<HTMLInputElement>('.export-name').value).toBe('kim')
  })

  it('files on Enter in the reporter field, but not while an IME composition is open', async () => {
    await atProblem()
    type(fields()[0]!, 'it hangs')
    fireEvent.keyDown($('.export-name'), { key: 'Enter', keyCode: 229 })
    await settle()
    expect(filed).toEqual([])
    act(() => { fireEvent.keyDown($('.export-name'), { key: 'Enter' }) })
    await settle()
    expect(filed).toHaveLength(1)
  })

  it('locks the fields and says the snapshot is being collected once filed', async () => {
    await atProblem()
    type(fields()[0]!, 'it hangs')
    press('.export-primary')
    await settle()
    expect(fields().every((field) => field.disabled)).toBe(true)
    expect(document.body.textContent).toContain('gui.export.collecting')
    expect(document.querySelector('.export-ghost')).toBeNull()
  })

  it('brings the fields back as they were left after a step back', async () => {
    await atProblem()
    type(fields()[0]!, 'it hangs')
    type(fields()[3]!, 'click export')
    press('.export-ghost')
    expect(store.get().page).toBe('turns')
    press('.export-primary')
    expect(fields()[0]!.value).toBe('it hangs')
    expect(fields()[3]!.value).toBe('click export')
    expect($('.export-primary').hasAttribute('disabled')).toBe(false)
  })
})


describe('the receiver check', () => {
  const notice = (): HTMLElement | null => document.querySelector<HTMLElement>('.export-probe')
  const recheck = (): HTMLButtonElement => $<HTMLButtonElement>('.export-probe .export-btn')

  it('says nothing while the receiver answers', async () => {
    await atProblem()
    expect(probes).toBe(1)
    expect(notice()).toBeNull()
    expect($('.export-primary').textContent).toBe('gui.export.next')
  })

  it('warns that the receiver is down, and starts only when the reader goes on', async () => {
    onProbe = async () => DOWN
    await atProblem()
    expect(notice()!.getAttribute('role')).toBe('status')
    expect(notice()!.textContent).toContain(
      'gui.export.probe_down:{"target":"https://bugs.example","reason":"could not reach bugs.example"}',
    )
    expect(notice()!.textContent).toContain('gui.export.probe_note')
    type(fields()[0]!, 'it hangs')
    expect($('.export-primary').textContent).toBe('gui.export.probe_continue')
    press('.export-primary')
    await settle()
    expect(filed).toHaveLength(1)
    expect(notice()).toBeNull()
  })

  it('checks again on request, with the button off while it checks', async () => {
    onProbe = async () => DOWN
    await atProblem()
    let answer: (value: ReportProbe) => void = () => undefined
    onProbe = () => new Promise<ReportProbe>((resolve) => { answer = resolve })
    act(() => { fireEvent.click(recheck()) })
    expect(probes).toBe(2)
    expect(recheck().disabled).toBe(true)
    expect(notice()!.textContent).toContain('gui.export.probe_down')
    await act(async () => { answer(REACHABLE) })
    await settle()
    expect(notice()).toBeNull()
    expect($('.export-primary').textContent).toBe('gui.export.next')
  })

  it('says when the check got no answer in time', async () => {
    onProbe = () => new Promise<ReportProbe>(() => undefined)
    await atProblem()
    await settle(store.PROBE_WAIT_MS)
    expect(notice()!.textContent).toContain('gui.export.probe_late')
    expect($('.export-primary').textContent).toBe('gui.export.probe_continue')
  })

  it('shows a start waiting on the check, with no way back meanwhile', async () => {
    let answer: (value: ReportProbe) => void = () => undefined
    onProbe = () => new Promise<ReportProbe>((resolve) => { answer = resolve })
    await atProblem()
    type(fields()[0]!, 'it hangs')
    expect(document.querySelector('.export-ghost')).not.toBeNull()
    press('.export-primary')
    expect($('.export-primary').textContent).toBe('gui.export.probe_checking')
    expect($('.export-primary').hasAttribute('disabled')).toBe(true)
    expect(notice()!.textContent).toContain('gui.export.probe_checking')
    expect(document.querySelector('.export-foot .export-ghost')).toBeNull()
    await act(async () => { answer(REACHABLE) })
    await settle()
    expect(filed).toHaveLength(1)
  })
})

