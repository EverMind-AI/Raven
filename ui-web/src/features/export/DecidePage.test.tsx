// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { RpcError } from '../../rpc/transport'
import * as confirm from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'
import { ExportApp } from './ExportApp'
import * as store from './store'

import type { ExportSource, ExportTurn, ReportContexts, ReportItem, ReportStatus } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const turn = (n: number): ExportTurn => ({
  trace_id: `t${n}`, number: n, start: '2026-10-01T10:00:00Z', input_preview: `input ${n}`, in_progress: false,
})
const item = (id: string, kind: ReportItem['kind'], linked: string[] = []): ReportItem => ({
  id, kind, category: kind === 'confirmed' ? 'private-key-block' : 'high-entropy', masked_sample: `${id}...masked`,
  count: 2, sources: [], linked, hint: null,
})

let items: ReportItem[]
let history: ReportStatus['history']
let contexts: Record<string, ReportContexts>
let decide: () => Promise<{ ok: boolean }>

const deciding = (): ReportStatus => ({
  report_id: 'rs-1', bug_report_id: 'br-1', phase: 'deciding', error: null, history, items, summary: null, receipt: null, record_dir: null,
})

const src: ExportSource = {
  turns: async () => ({ title: 'Plan', turns: [turn(1), turn(2)] }),
  start: () => Promise.reject(new Error('not scripted')),
  status: () => Promise.reject(new Error('not scripted')),
  cancel: async () => ({ ok: true }),
  reportStart: async () => ({ report_id: 'rs-1' }),
  reportStatus: async () => deciding(),
  reportItem: async (_id, itemId) => contexts[itemId] ?? { groups: [], total_groups: 0 },
  reportDecide: () => decide(),
  reportUpload: () => Promise.reject(new Error('not scripted')),
  reportRetry: () => Promise.reject(new Error('not scripted')),
  reportCancel: async () => ({ ok: true }),
}

const FRAME = '<div id="exportVeil" data-open="false"><div id="exportModal" tabindex="-1"><div id="exportBody"></div></div></div>'
const $ = <T extends Element = HTMLElement>(sel: string): T => document.querySelector<T>(sel)!
const $$ = (sel: string): HTMLElement[] => [...document.querySelectorAll<HTMLElement>(sel)]
const settle = async (ms = 0): Promise<void> => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const button = (label: string): HTMLButtonElement => $$('button').find((b) => b.textContent === label) as HTMLButtonElement
const tap = (el: Element): void => { act(() => { fireEvent.click(el) }) }
const rows = (): HTMLElement[] => $$('.export-item')
const sub = (id: string): string => rows().find((row) => row.textContent?.includes(`${id}...masked`))!.querySelector('.ds')!.textContent!

async function atDecisions(): Promise<void> {
  document.body.innerHTML = FRAME
  render(<ExportApp />, { container: $('#exportBody') })
  act(() => { store.open('report', 'gui:a') })
  await settle()
  tap($('.export-primary'))
  act(() => { store.editProblem('description', 'it hangs') })
  tap($('.export-primary'))
  await settle()
}

beforeEach(() => {
  vi.useFakeTimers()
  items = [item('k', 'confirmed'), item('a', 'suspected', ['b']), item('b', 'suspected', ['a']), item('d', 'suspected')]
  history = { state: 'on', auto_kept: 2, auto_redacted: 1, warning: null }
  contexts = {
    k: { groups: [{ label: 'a tool result', text: 'key: [REDACTED]', spans: [[5, 15]], count: 1 }], total_groups: 1 },
    a: {
      groups: [
        { label: 'the session transcript', text: '\u{1F600} token aB3x end', spans: [[8, 12]], count: 3 },
        { label: 'a model input', text: 'aB3x', spans: [[0, 4]], count: 1 },
      ],
      total_groups: 5,
    },
  }
  decide = async () => ({ ok: true })
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

describe('the decisions page', () => {
  it('says what the history decided on its own', async () => {
    await atDecisions()
    expect(store.get().page).toBe('decide')
    expect($('.export-history').textContent).toBe('gui.export.history_auto:{"n":3,"kept":2,"redacted":1}')
  })

  it('says the history is off, or why it could not be read', async () => {
    history = { state: 'off', auto_kept: 0, auto_redacted: 0, warning: null }
    await atDecisions()
    expect($('.export-history').textContent).toBe('gui.export.history_off')
    cleanup()
    store._resetForTests()
    history = { state: 'corrupt', auto_kept: 0, auto_redacted: 0, warning: 'the redaction history could not be read' }
    await atDecisions()
    expect($('.export-history [data-bad]').textContent).toBe('the redaction history could not be read')
  })

  it('lists every item masked, confirmed first with an acknowledgement, and Next waits for it', async () => {
    await atDecisions()
    expect(rows().map((row) => row.querySelector('.nm')!.textContent)).toEqual(['k...masked', 'a...masked', 'b...masked', 'd...masked'])
    expect(sub('k')).toBe('gui.export.confirmed')
    expect(sub('a')).toContain('gui.export.count:{"n":2}')
    expect(sub('a')).toContain('gui.export.kept')
    expect(sub('a')).toContain('gui.export.linked:{"n":1}')
    expect(rows()[0]!.querySelector('.export-ack input')).not.toBeNull()
    expect(rows()[1]!.querySelector('.export-ack')).toBeNull()
    expect($('.export-primary').hasAttribute('disabled')).toBe(true)
    tap(rows()[0]!.querySelector('.export-ack input')!)
    expect($('.export-primary').hasAttribute('disabled')).toBe(false)
  })

  it('shows the focused item\'s contexts with each hit marked at its code points', async () => {
    await atDecisions()
    expect($('.two-pane-main .export-ctx mark').textContent).toBe('[REDACTED]')
    tap(rows()[1]!.querySelector('.two-pane-hit')!)
    await settle()
    const blocks = $$('.export-ctx')
    expect(blocks.map((block) => [...block.querySelectorAll('mark')].map((m) => m.textContent))).toEqual([['aB3x'], ['aB3x']])
    expect(blocks[0]!.textContent).toBe('\u{1F600} token aB3x end')
    expect($$('.export-ctxlab')[0]!.textContent).toContain('gui.export.occurrences:{"n":3}')
    expect(document.body.textContent).toContain('gui.export.contexts_more:{"total":5,"shown":2}')
  })

  it('keeps or redacts everything, or the focused item with its links, and never a confirmed one', async () => {
    await atDecisions()
    expect(button('gui.export.keep').disabled).toBe(true)
    tap(button('gui.export.redact_all'))
    expect(['a', 'b', 'd'].map(sub).every((text) => text.includes('gui.export.redacted'))).toBe(true)
    tap(rows()[1]!.querySelector('.two-pane-hit')!)
    await settle()
    tap(button('gui.export.keep'))
    expect(sub('a')).toContain('gui.export.kept')
    expect(sub('b')).toContain('gui.export.kept')
    expect(sub('d')).toContain('gui.export.redacted')
    tap(button('gui.export.keep_all'))
    expect(sub('d')).toContain('gui.export.kept')
  })

  it('outlines the items a refused decision names', async () => {
    decide = () => Promise.reject(new RpcError(-32027, 'clash', { item_ids: ['a', 'b'] }))
    await atDecisions()
    tap(rows()[0]!.querySelector('.export-ack input')!)
    tap($('.export-primary'))
    await settle()
    expect(rows().filter((row) => row.hasAttribute('data-bad')).map((row) => row.querySelector('.nm')!.textContent))
      .toEqual(['a...masked', 'b...masked'])
    expect($('[role="alert"]').textContent).toBe('gui.export.conflict')
  })

  it('pages a long list two hundred rows at a time', async () => {
    items = Array.from({ length: 201 }, (_, i) => item(`s${i}`, 'suspected'))
    await atDecisions()
    expect(rows()).toHaveLength(200)
    expect($('.export-pager span').textContent).toBe('gui.export.page_of:{"page":1,"pages":2}')
    tap(button('gui.export.next_page'))
    expect(rows().map((row) => row.querySelector('.nm')!.textContent)).toEqual(['s200...masked'])
    expect(button('gui.export.next_page').disabled).toBe(true)
  })
})
