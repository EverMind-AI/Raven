// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it, vi } from 'vitest'

import type { SubagentRow, SubagentsListResult } from '../rpc/generated.js'

import {
  failureDetailLine,
  flattenSubagentRows,
  mergeProbeColumns,
  runningTestNames,
  SubagentsHub
} from '../components/subagentsHub.js'
import { DEFAULT_THEME } from '../theme.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const waitForFrame = async (h: Pick<Harness, 'frame'>, text: string) => {
  for (let i = 0; i < 30; i++) {
    if (h.frame().includes(text)) {
      return
    }
    await delay(30)
  }
  expect(h.frame()).toContain(text)
}

// Polls for an RPC call rather than asserting right after a fixed delay: under
// a loaded test run the keypress that triggers the call can itself arrive late.
const waitForRpcCall = async (request: ReturnType<typeof vi.fn>, method: string) => {
  for (let i = 0; i < 30; i++) {
    if (request.mock.calls.some(c => c[0] === method)) {
      return
    }
    await delay(30)
  }
}

const waitForMockCall = async (fn: ReturnType<typeof vi.fn>) => {
  for (let i = 0; i < 30; i++) {
    if (fn.mock.calls.length > 0) {
      return
    }
    await delay(30)
  }
}

// Backspaces sent in one write land in the same stdin data event, which the
// terminal parser reads as a single (unrecognised) multi-byte sequence
// rather than N separate keypresses -- one `type` call per backspace forces
// each into its own chunk.
const backspaceAll = async (h: Pick<Harness, 'type'>, times: number) => {
  for (let i = 0; i < times; i++) {
    await h.type(BACKSPACE)
  }
}

const ESC_RE = new RegExp(String.fromCharCode(27), 'g')

// ink emits cursor-forward moves (CSI nC) in place of spaces for alignment, so
// strip every CSI sequence and collapse whitespace into single spaces before
// matching on screen text.
const normalize = (raw: string) =>
  raw
    .replace(new RegExp(`${String.fromCharCode(27)}\\[[0-9;?<>=]*[a-zA-Z]`, 'g'), ' ')
    .replace(new RegExp(`${String.fromCharCode(27)}\\][^\\u0007]*\\u0007?`, 'g'), ' ')
    .replace(ESC_RE, ' ')
    .replace(/\s+/g, ' ')

const ESC = String.fromCharCode(27)
const DOWN = `${String.fromCharCode(27)}[B`
const UP = `${String.fromCharCode(27)}[A`
const TAB = '\t'
const ENTER = '\r'
const CTRL_S = String.fromCharCode(19)
const BACKSPACE = String.fromCharCode(127)

// Rows the handlers would return; two configured, one bare preset.
const ROWS: SubagentRow[] = [
  {
    configured: true,
    description: 'coding',
    enabled: true,
    group: 'installed',
    has_api_key: false,
    kind: 'cli',
    last_test_at_ms: undefined,
    last_test_detail: undefined,
    last_test_ok: undefined,
    name: 'Coder',
    preset: 'claude_code',
    probe_detail: 'installed at /usr/bin/claude',
    probe_status: 'ready',
    test_running: false
  },
  {
    configured: true,
    description: 'research',
    enabled: false,
    group: 'uninstalled',
    has_api_key: false,
    kind: 'cli',
    last_test_at_ms: undefined,
    last_test_detail: undefined,
    last_test_ok: false,
    name: 'Guard',
    preset: 'openclaw',
    probe_detail: 'not found on PATH',
    probe_status: 'missing',
    test_running: false
  },
  {
    configured: false,
    description: 'opencode cli',
    enabled: false,
    group: 'installed',
    has_api_key: false,
    kind: 'cli',
    last_test_at_ms: undefined,
    last_test_detail: undefined,
    last_test_ok: undefined,
    name: 'opencode',
    preset: 'opencode',
    probe_detail: 'installed at /root/.opencode/bin/opencode',
    probe_status: 'ready',
    test_running: false
  }
]

const CODER_ROW = ROWS[0]!
const GUARD_ROW = ROWS[1]!
const OPENCODE_ROW = ROWS[2]!

// A kind: 'openai' preset, unconfigured -- the only shape that shows the
// API key field. No has_api_key, so no stored-key hint expected.
const OPENAI_PRESET_ROW: SubagentRow = {
  configured: false,
  description: 'openai preset agent',
  enabled: false,
  group: 'installed',
  has_api_key: false,
  kind: 'openai',
  last_test_at_ms: undefined,
  last_test_detail: undefined,
  last_test_ok: undefined,
  name: 'openai-agent',
  preset: 'openai_agent',
  probe_detail: 'not configured',
  probe_status: 'missing',
  test_running: false
}

// A configured kind: 'openai' row with a key already on file -- the case
// the security requirement is about: the field must open blank.
const OPENAI_CONFIGURED_ROW: SubagentRow = {
  configured: true,
  description: 'my openai agent',
  enabled: true,
  group: 'installed',
  has_api_key: true,
  kind: 'openai',
  last_test_at_ms: undefined,
  last_test_detail: undefined,
  last_test_ok: undefined,
  name: 'MyOpenAI',
  preset: 'openai_agent',
  probe_detail: 'ready',
  probe_status: 'ready',
  test_running: false
}

interface Harness {
  // frame()'s `output` is an append-only concatenation of every byte ink
  // ever wrote (ink's cell-diffing can skip re-emitting a cell whose content
  // already matches an older frame, so nothing ever "clears" from it). A
  // scenario that must prove text stopped appearing after having legitimately
  // appeared cannot be proven this way -- assert on a value (a pure function,
  // an RPC call's arguments) instead, or on a substring that never
  // legitimately appears at all unless the behaviour under test regresses.
  frame: () => string
  gw: { request: ReturnType<typeof vi.fn> }
  onClose: ReturnType<typeof vi.fn>
  type: (s: string) => Promise<void>
  unmount: () => void
}

interface MountOptions {
  listError?: string
  // Overrides the default subagents.list response, e.g. to make a mutation's
  // probe:false refresh return different rows than the initial (probing)
  // load -- called on every subagents.list request, regardless of params.
  listImpl?: (params: Record<string, unknown>) => SubagentsListResult | undefined
  requestImpl?: (method: string, params: Record<string, unknown>) => unknown
  rows?: SubagentRow[]
}

const mount = (opts: MountOptions = {}): Harness => {
  const onClose = vi.fn()
  const request = vi.fn((method: string, params: Record<string, unknown>) => {
    if (method === 'subagents.list') {
      if (opts.listImpl) {
        const r = opts.listImpl(params)

        if (r !== undefined) {
          return Promise.resolve(r)
        }
      }

      return opts.listError ? Promise.reject(new Error(opts.listError)) : Promise.resolve({ rows: opts.rows ?? ROWS })
    }

    if (opts.requestImpl) {
      const r = opts.requestImpl(method, params)

      if (r !== undefined) {
        return Promise.resolve(r)
      }
    }

    return Promise.resolve({})
  })
  const gw = { request } as unknown as { request: typeof request }

  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(<SubagentsHub gw={gw as never} onClose={onClose} t={DEFAULT_THEME} />, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  return {
    frame: () => normalize(output),
    gw: gw as never,
    onClose,
    type: async (s: string) => {
      stdin.write(s)
      await delay(60)
    },
    unmount: () => {
      instance.unmount()
      instance.cleanup()
    }
  }
}

// Row-flattening and offset computation as a pure function: order of the
// three groups, the section sizes (offsets), and index-to-row mapping, all
// asserted directly with no rendering and no keystrokes -- so this coverage
// is not subject to the terminal-emulation timing issues that make the
// keystroke-driven cases below fragile under a loaded parallel test run.
describe('flattenSubagentRows', () => {
  it('orders installed, then not-installed, then presets', () => {
    const { installedCount, presetsCount, uninstalledCount } = flattenSubagentRows(ROWS)

    expect(installedCount).toBe(1)
    expect(uninstalledCount).toBe(1)
    expect(presetsCount).toBe(1)
  })

  it('maps flat index to the row that section-and-position implies', () => {
    const { flat } = flattenSubagentRows(ROWS)

    expect(flat[0]?.name).toBe('Coder')
    expect(flat[1]?.name).toBe('Guard')
    expect(flat[2]?.name).toBe('opencode')
  })

  it('drops an empty group from the counts without disturbing the others', () => {
    const { flat, installedCount, presetsCount, uninstalledCount } = flattenSubagentRows(ROWS.filter(r => r.configured))

    expect(flat.map(r => r.name)).toEqual(['Coder', 'Guard'])
    expect(installedCount).toBe(1)
    expect(uninstalledCount).toBe(1)
    expect(presetsCount).toBe(0)
  })

  it('handles an empty row list', () => {
    expect(flattenSubagentRows([])).toEqual({ flat: [], installedCount: 0, presetsCount: 0, uninstalledCount: 0 })
  })

  it('keeps rows within the same group in their original relative order', () => {
    const second: SubagentRow = { ...GUARD_ROW, name: 'Second' }
    const { flat } = flattenSubagentRows([GUARD_ROW, second])

    expect(flat.map(r => r.name)).toEqual(['Guard', 'Second'])
  })

  it('an unconfigured row lands in presets regardless of its group field', () => {
    const bothUnconfigured: SubagentRow = { ...OPENCODE_ROW, group: 'uninstalled' }
    const { flat, installedCount, presetsCount, uninstalledCount } = flattenSubagentRows([bothUnconfigured])

    expect(installedCount).toBe(0)
    expect(uninstalledCount).toBe(0)
    expect(presetsCount).toBe(1)
    expect(flat[0]?.name).toBe('opencode')
  })
})

describe('SubagentsHub', () => {
  it('calls subagents.list once on mount', async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    const listCalls = h.gw.request.mock.calls.filter(c => c[0] === 'subagents.list')
    expect(listCalls).toHaveLength(1)
    expect(listCalls[0]?.[1]).toEqual({})

    h.unmount()
  })

  it('renders all three section headers, and each row name', async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    const frame = h.frame()
    expect(frame).toContain('INSTALLED')
    expect(frame).toContain('NOT INSTALLED')
    expect(frame).toContain('AVAILABLE PRESETS')
    expect(frame).toContain('Coder')
    expect(frame).toContain('Guard')
    expect(frame).toContain('opencode')

    h.unmount()
  })

  it('keeps a row wider than the overlay on one line, with its toggle intact', async () => {
    // A row is two flex children in a row Box. While the gap between them was
    // its own `<Text> </Text>`, an over-wide row shrank that spacer, whose wrap
    // measure came back two rows tall: the row rendered as two screen lines
    // (the second blank) and the name column was shrunk hard enough to lose its
    // `[on ]`. Both symptoms are visible only in a rendered frame, so this is
    // asserted through one rather than through a pure helper.
    const long: SubagentRow = {
      ...CODER_ROW,
      probe_detail: 'installed at /Evermind/sh_evermind/chenhongda/.venvs/nanobot-evermind/bin/python'
    }
    const h = mount({ rows: [long] })
    await waitForFrame(h, 'Coder')

    expect(h.frame()).toContain('Coder · claude_code [on ]')

    h.unmount()
  })

  it('omits a section header when it has no rows', async () => {
    const h = mount({ rows: ROWS.filter(r => r.configured) })
    await waitForFrame(h, 'Coder')

    expect(h.frame()).not.toContain('AVAILABLE PRESETS')

    h.unmount()
  })

  it("'space' on Coder toggles it off, then re-lists without probing", async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    h.gw.request.mockClear()
    await h.type(' ')
    await waitForRpcCall(h.gw.request, 'subagents.toggle')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.toggle', { enabled: false, name: 'Coder' })
    await waitForRpcCall(h.gw.request, 'subagents.list')
    // A mutation's follow-up list must not pay for the network probe the
    // initial load already paid for (measured an 11s stall against an
    // unreachable endpoint otherwise) -- probe: false skips it server-side.
    expect(h.gw.request).toHaveBeenCalledWith('subagents.list', { probe: false })

    h.unmount()
  })

  it('a toggle carries forward the probe status already on screen instead of showing it as unknown', async () => {
    let listCalls = 0
    const h = mount({
      listImpl: () => {
        listCalls += 1

        // First call is the initial (probing) load; every later call is a
        // mutation's probe:false refresh, which the real server answers with
        // probe_status: 'unknown' / probe_detail: '' for every row.
        return listCalls === 1
          ? undefined
          : { rows: ROWS.map(r => ({ ...r, probe_detail: '', probe_status: 'unknown' })) }
      }
    })
    await waitForFrame(h, 'installed at /usr/bin/claude')

    await h.type(' ')
    await waitForRpcCall(h.gw.request, 'subagents.list')
    await waitForRpcCall(h.gw.request, 'subagents.toggle')
    // Give the merged re-render a moment to land.
    await delay(90)

    expect(h.frame()).toContain('installed at /usr/bin/claude')

    h.unmount()
  })

  // Regression: a cli row's group is probe-derived, so a probe:false refresh
  // legitimately reports every cli row as group: 'uninstalled' (the backend's
  // own test asserts this). mergeProbeColumns must carry the prior group
  // forward for a cli row the same way it carries probe_status/probe_detail,
  // or every installed cli agent visibly jumps to NOT INSTALLED after any
  // mutation until the overlay is reopened or 'r' is pressed.
  it('a cli row does not relocate to NOT INSTALLED after a mutation refresh reports it as unknown/uninstalled', async () => {
    let listCalls = 0
    const h = mount({
      listImpl: () => {
        listCalls += 1

        return listCalls === 1
          ? undefined
          : { rows: [{ ...CODER_ROW, group: 'uninstalled', probe_detail: '', probe_status: 'unknown' }] }
      },
      rows: [CODER_ROW]
    })
    await waitForFrame(h, 'INSTALLED')

    await h.type(' ')
    await waitForRpcCall(h.gw.request, 'subagents.toggle')
    await delay(90)

    const frame = h.frame()
    expect(frame).toContain('INSTALLED')
    expect(frame).not.toContain('NOT INSTALLED')

    h.unmount()
  })

  it("'space' on the opencode preset row does not toggle it", async () => {
    // A single-row fixture puts the target at index 0 by construction, so the
    // test exercises the refusal itself rather than arrow-key delivery timing
    // (already covered by the "selection" contract, not one of these cases).
    const h = mount({ rows: [OPENCODE_ROW] })
    await waitForFrame(h, 'opencode')

    h.gw.request.mockClear()
    await h.type(' ')
    // A refusal makes no RPC call at all, so there is nothing to poll for;
    // give any (incorrect) call time to surface before asserting its absence.
    await delay(90)

    expect(h.gw.request).not.toHaveBeenCalledWith('subagents.toggle', expect.anything())

    h.unmount()
  })

  it("'t' on Guard tests it as a configured entry", async () => {
    const h = mount({ rows: [GUARD_ROW] })
    await waitForFrame(h, 'Guard')

    h.gw.request.mockClear()
    await h.type('t')
    await waitForRpcCall(h.gw.request, 'subagents.test')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.test', { name: 'Guard', source: 'config' })

    h.unmount()
  })

  // End-to-end coverage for the keypress -> selection -> action wiring itself
  // (the pure-function tests above cover the flattening/offset math, but not
  // whether `key.downArrow` actually moves `idx`, or whether the `offset`
  // values threaded into rendering match it). Uses the three-row fixture and
  // a real down-arrow rather than a pre-selected single-row fixture, since
  // the thing under test here is that navigation reaches the second row.
  it("'down' then 't' tests the second row (Guard), not the first (Coder)", async () => {
    const h = mount()
    await waitForFrame(h, 'Guard')

    await h.type(DOWN)
    h.gw.request.mockClear()
    await h.type('t')
    await waitForRpcCall(h.gw.request, 'subagents.test')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.test', { name: 'Guard', source: 'config' })

    h.unmount()
  })

  it("'t' on the opencode preset tests it with source 'preset'", async () => {
    const h = mount({ rows: [OPENCODE_ROW] })
    await waitForFrame(h, 'opencode')

    h.gw.request.mockClear()
    await h.type('t')
    await waitForRpcCall(h.gw.request, 'subagents.test')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.test', { name: 'opencode', source: 'preset' })

    h.unmount()
  })

  it('esc cancels a running test instead of closing the overlay', async () => {
    const h = mount({
      requestImpl: method => (method === 'subagents.test' ? new Promise(() => {}) : undefined)
    })
    await waitForFrame(h, 'Coder')

    await h.type('t')
    await waitForRpcCall(h.gw.request, 'subagents.test')
    h.gw.request.mockClear()
    await h.type(ESC)
    // A lone Escape sits in the terminal parser's 50ms flush window before it
    // is delivered as key.escape (it could be the start of a CSI sequence).
    await delay(60)
    await waitForRpcCall(h.gw.request, 'subagents.test_cancel')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.test_cancel', { name: 'Coder' })
    expect(h.onClose).not.toHaveBeenCalled()

    h.unmount()
  })

  it('esc closes the overlay when no test is running', async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    await h.type(ESC)
    await delay(60)
    await waitForMockCall(h.onClose)

    expect(h.onClose).toHaveBeenCalled()

    h.unmount()
  })

  it("'q' closes the overlay", async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    await h.type('q')
    await waitForMockCall(h.onClose)

    expect(h.onClose).toHaveBeenCalled()

    h.unmount()
  })

  // Regression (Task 6): 'd' used to switch `stage` to 'confirm-delete', a
  // stage the list-only useInput guard (`if (stage !== 'list') return`) and
  // render both ignored - every later key, including q and esc, was silently
  // swallowed while the list kept drawing as if nothing had happened. There
  // was no way out short of killing the TUI. Task 7 gives 'd' a real
  // confirm-delete stage; these assert its own exits ('n' and Esc) still let
  // input flow afterward, not just that some `stage` value is set - a
  // state-only assertion would not have caught the original bug, since the
  // bug was that input handling stopped.
  it("'d' opens a delete confirmation, and 'n' backs out without trapping the overlay - 'q' still closes it afterward", async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    await h.type('d')
    await waitForFrame(h, 'Coder? y/n')

    await h.type('n')
    h.gw.request.mockClear()
    await h.type('q')
    await waitForMockCall(h.onClose)

    expect(h.onClose).toHaveBeenCalled()
    expect(h.gw.request).not.toHaveBeenCalledWith('subagents.remove', expect.anything())

    h.unmount()
  })

  it("'d' then 'n' does not trap the overlay - a following key still reaches its RPC", async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    await h.type('d')
    await waitForFrame(h, 'Coder? y/n')
    await h.type('n')

    h.gw.request.mockClear()
    await h.type(' ')
    await waitForRpcCall(h.gw.request, 'subagents.toggle')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.toggle', { enabled: false, name: 'Coder' })

    h.unmount()
  })

  it('a rejected subagents.list renders the error text and no row names', async () => {
    const h = mount({ listError: 'gateway unreachable' })
    await waitForFrame(h, 'gateway unreachable')

    const frame = h.frame()
    expect(frame).toContain('gateway unreachable')
    expect(frame).not.toContain('Coder')
    expect(frame).not.toContain('Guard')
    expect(frame).not.toContain('opencode')

    h.unmount()
  })

  it('the footer names both custom-agent escape hatches', async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    const frame = h.frame()
    expect(frame).toContain('/subagents')
    expect(frame).toContain('~/.raven/config.json')

    h.unmount()
  })
})

describe('SubagentsHub form and delete confirm', () => {
  it("'enter' on an unconfigured preset opens an add-mode form pre-filled with its name", async () => {
    const h = mount({ rows: [OPENCODE_ROW] })
    await waitForFrame(h, 'opencode')

    await h.type(ENTER)
    await waitForFrame(h, 'Add subagent')

    expect(h.frame()).toContain('opencode')

    h.unmount()
  })

  it('submitting the add form calls subagents.add with the preset, name, and description', async () => {
    const h = mount({ rows: [OPENCODE_ROW] })
    await waitForFrame(h, 'opencode')

    await h.type(ENTER)
    await waitForFrame(h, 'Add subagent')
    await h.type(TAB)
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.add')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.add', {
      description: 'opencode cli',
      name: 'opencode',
      preset: 'opencode'
    })

    h.unmount()
  })

  it("'enter' on a configured row opens edit mode pre-filled with its name and description", async () => {
    const h = mount({ rows: [CODER_ROW] })
    await waitForFrame(h, 'Coder')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')

    const frame = h.frame()
    expect(frame).toContain('Coder')
    expect(frame).toContain('coding')

    h.unmount()
  })

  it('submitting an edited name calls subagents.update with the original name, new_name, and description', async () => {
    const h = mount({ rows: [CODER_ROW] })
    await waitForFrame(h, 'Coder')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    await h.type('X')
    await h.type(TAB)
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.update')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.update', {
      description: 'coding',
      name: 'Coder',
      new_name: 'CoderX'
    })

    h.unmount()
  })

  // Regression: mergeProbeColumns joins on name, so a rename's follow-up
  // list (keyed by the new name) used to miss the prior row entirely and the
  // renamed agent reverted to probe_status: unknown even though nothing
  // about its installed state changed.
  it('a rename carries the prior probe status forward under the new name', async () => {
    let listCalls = 0
    const h = mount({
      listImpl: () => {
        listCalls += 1

        return listCalls === 1
          ? undefined
          : {
              rows: [{ ...CODER_ROW, group: 'uninstalled', name: 'CoderX', probe_detail: '', probe_status: 'unknown' }]
            }
      },
      rows: [CODER_ROW]
    })
    await waitForFrame(h, 'Coder')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    await h.type('X')
    await h.type(TAB)
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.update')
    await waitForFrame(h, 'CoderX')
    await delay(90)

    const frame = h.frame()
    expect(frame).toContain('installed at /usr/bin/claude')
    expect(frame).not.toContain('NOT INSTALLED')

    h.unmount()
  })

  it("renders no 'API key' field for a kind: 'cli' row", async () => {
    const h = mount({ rows: [GUARD_ROW] })
    await waitForFrame(h, 'Guard')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')

    expect(h.frame()).not.toContain('API key')

    h.unmount()
  })

  it("renders an 'API key' field for a kind: 'openai' row, and typed characters never appear in the frame", async () => {
    const h = mount({ rows: [OPENAI_PRESET_ROW] })
    await waitForFrame(h, 'openai-agent')

    await h.type(ENTER)
    await waitForFrame(h, 'Add subagent')
    expect(h.frame()).toContain('API key')

    // Name -> Description -> API key.
    await h.type(TAB)
    await h.type(TAB)

    const secret = 'sk-verysecret999'
    await h.type(secret)
    await waitForFrame(h, '•')

    const frame = h.frame()
    expect(frame).not.toContain(secret)
    expect(frame).toContain('•'.repeat(secret.length))

    h.unmount()
  })

  it('with has_api_key true, the key field renders empty and the label says the stored key is kept', async () => {
    const h = mount({ rows: [OPENAI_CONFIGURED_ROW] })
    await waitForFrame(h, 'MyOpenAI')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')

    const frame = h.frame()
    expect(frame).toContain('stored')
    expect(frame).toContain('keeps it')
    expect(frame).not.toContain('•')

    h.unmount()
  })

  it('submitting with the key field left blank omits api_key from subagents.update', async () => {
    const h = mount({ rows: [OPENAI_CONFIGURED_ROW] })
    await waitForFrame(h, 'MyOpenAI')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    // Name -> Description -> API key (the last field for a kind: 'openai' row).
    await h.type(TAB)
    await h.type(TAB)
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.update')

    const call = h.gw.request.mock.calls.find(c => c[0] === 'subagents.update')
    expect(call?.[1]).not.toHaveProperty('api_key')

    h.unmount()
  })

  it('submitting with a typed key includes api_key', async () => {
    const h = mount({ rows: [OPENAI_CONFIGURED_ROW] })
    await waitForFrame(h, 'MyOpenAI')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    await h.type(TAB)
    await h.type(TAB)
    await h.type('sk-newkey123')
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.update')

    const call = h.gw.request.mock.calls.find(c => c[0] === 'subagents.update')
    expect(call?.[1]).toMatchObject({ api_key: 'sk-newkey123' })

    h.unmount()
  })

  // The "an openai row that just gained a key moves to INSTALLED rather than
  // keeping its stale group" behaviour used to be covered here as a rendered-
  // frame test. It flaked under full-suite load: proving a row moved OUT of a
  // section it started in requires checking that a substring stopped
  // appearing, and this harness's frame() is an append-only concatenation of
  // every byte ink ever wrote (see the mergeProbeColumns doc comment and the
  // fix-wave-b report), so a transitional or late-flushed byte from the
  // pre-edit frame can still land after any checkpoint under contention. The
  // behaviour itself is exactly a `mergeProbeColumns` rule -- see
  // 'always takes the fresh group for an openai row...' below, which asserts
  // it directly on the pure function with no rendering involved.

  // Regression: a pasted key commonly carries a leading/trailing space (or
  // newline, though a literal newline in this harness would be interpreted
  // as Enter/submit rather than typed text) as a clipboard artifact. The
  // blank check trimmed before deciding whether to send api_key at all, but
  // the value actually sent was the untrimmed field -- so a key with
  // surrounding whitespace was stored verbatim and every later dispatch
  // using it would fail auth, with no visible symptom (the masked display
  // looks identical either way).
  it('submitting a key with leading/trailing whitespace sends the trimmed value', async () => {
    const h = mount({ rows: [OPENAI_CONFIGURED_ROW] })
    await waitForFrame(h, 'MyOpenAI')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    await h.type(TAB)
    await h.type(TAB)
    await h.type('  sk-padded-key  ')
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.update')

    const call = h.gw.request.mock.calls.find(c => c[0] === 'subagents.update')
    expect(call?.[1]).toMatchObject({ api_key: 'sk-padded-key' })

    h.unmount()
  })

  // Regression: the backend now trims name/new_name and rejects blank-after-
  // trim server-side, but the client should not send padding it can cheaply
  // normalise itself -- mirrors the api-key trim above.
  it('submitting a name with leading/trailing whitespace sends the trimmed value', async () => {
    const h = mount({ rows: [OPENCODE_ROW] })
    await waitForFrame(h, 'opencode')

    await h.type(ENTER)
    await waitForFrame(h, 'Add subagent')
    await backspaceAll(h, 'opencode'.length)
    await h.type('  opencode  ')
    await h.type(TAB)
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.add')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.add', {
      description: 'opencode cli',
      name: 'opencode',
      preset: 'opencode'
    })

    h.unmount()
  })

  it('submitting a blank-after-trim name shows an inline error instead of making a doomed round trip', async () => {
    const h = mount({ rows: [OPENCODE_ROW] })
    await waitForFrame(h, 'opencode')

    await h.type(ENTER)
    await waitForFrame(h, 'Add subagent')
    await backspaceAll(h, 'opencode'.length)
    await h.type('   ')
    h.gw.request.mockClear()
    await h.type(TAB)
    await h.type(ENTER)
    await waitForFrame(h, 'name cannot be blank')

    expect(h.frame()).toContain('Add subagent')
    expect(h.gw.request).not.toHaveBeenCalledWith('subagents.add', expect.anything())

    h.unmount()
  })

  it('enter submits from the first field, not only from the last one', async () => {
    // The hint has always read "Enter/Ctrl+S save", but Enter used to be a
    // no-op anywhere except the last field, so a two-field cli form could only
    // be saved by tabbing to Description first.
    const h = mount({ rows: [CODER_ROW] })
    await waitForFrame(h, 'Coder')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.update')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.update', {
      description: 'coding',
      name: 'Coder',
      new_name: 'Coder'
    })

    h.unmount()
  })

  it('enter submits from a middle field too, without tabbing to the end', async () => {
    // An openai entry has three fields, so Description is neither first nor
    // last: Enter there must still save rather than fall through to the
    // "only the last field commits" rule.
    const h = mount({ rows: [OPENAI_CONFIGURED_ROW] })
    await waitForFrame(h, 'MyOpenAI')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    await h.type(TAB)
    await h.type(ENTER)
    await waitForRpcCall(h.gw.request, 'subagents.update')

    // No api_key: the key field opened blank and Enter left it that way, so the
    // stored key must be kept rather than overwritten with an empty string.
    expect(h.gw.request).toHaveBeenCalledWith('subagents.update', {
      description: 'my openai agent',
      name: 'MyOpenAI',
      new_name: 'MyOpenAI'
    })

    h.unmount()
  })

  it('ctrl+s also submits, from any field', async () => {
    const h = mount({ rows: [CODER_ROW] })
    await waitForFrame(h, 'Coder')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    await h.type(CTRL_S)
    await waitForRpcCall(h.gw.request, 'subagents.update')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.update', {
      description: 'coding',
      name: 'Coder',
      new_name: 'Coder'
    })

    h.unmount()
  })

  it('esc on the form returns to the list without calling any mutating RPC, and further input still reaches it', async () => {
    const h = mount({ rows: [CODER_ROW] })
    await waitForFrame(h, 'Coder')

    await h.type(ENTER)
    await waitForFrame(h, 'Edit subagent')
    h.gw.request.mockClear()
    await h.type(ESC)
    await delay(60)

    expect(h.gw.request).not.toHaveBeenCalledWith('subagents.add', expect.anything())
    expect(h.gw.request).not.toHaveBeenCalledWith('subagents.update', expect.anything())

    await h.type(' ')
    await waitForRpcCall(h.gw.request, 'subagents.toggle')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.toggle', { enabled: false, name: 'Coder' })

    h.unmount()
  })

  it('a rejected subagents.add keeps the form open, shows the error, and can be resubmitted', async () => {
    const h = mount({
      requestImpl: method => (method === 'subagents.add' ? Promise.reject(new Error('add rejected')) : undefined),
      rows: [OPENCODE_ROW]
    })
    await waitForFrame(h, 'opencode')

    await h.type(ENTER)
    await waitForFrame(h, 'Add subagent')
    await h.type(TAB)
    await h.type(ENTER)
    await waitForFrame(h, 'add rejected')

    expect(h.frame()).toContain('Add subagent')

    const callsSoFar = h.gw.request.mock.calls.filter(c => c[0] === 'subagents.add').length
    await h.type(ENTER)

    for (let i = 0; i < 30; i++) {
      if (h.gw.request.mock.calls.filter(c => c[0] === 'subagents.add').length > callsSoFar) {
        break
      }
      await delay(30)
    }

    expect(h.gw.request.mock.calls.filter(c => c[0] === 'subagents.add').length).toBeGreaterThan(callsSoFar)

    h.unmount()
  })

  it("'d' then 'y' calls subagents.remove with the selected name", async () => {
    const h = mount({ rows: [CODER_ROW] })
    await waitForFrame(h, 'Coder')

    await h.type('d')
    await waitForFrame(h, 'Coder? y/n')
    await h.type('y')
    await waitForRpcCall(h.gw.request, 'subagents.remove')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.remove', { name: 'Coder' })

    h.unmount()
  })

  it("'d' then anything but 'y' calls no mutating RPC", async () => {
    const h = mount({ rows: [CODER_ROW] })
    await waitForFrame(h, 'Coder')

    await h.type('d')
    await waitForFrame(h, 'Coder? y/n')
    h.gw.request.mockClear()
    await h.type('n')
    await delay(90)

    expect(h.gw.request).not.toHaveBeenCalledWith('subagents.remove', expect.anything())

    h.unmount()
  })
})

// The authoritative coverage for the merge rule, including the cli-vs-openai
// group split -- asserted directly on the pure function rather than through
// a rendered frame, per this file's established pattern (flattenSubagentRows,
// failureDetailLine) for logic that does not need a terminal to prove itself
// and that a keystroke-driven frame snapshot cannot prove reliably anyway.
describe('mergeProbeColumns', () => {
  it('keeps a prior row probe_status/probe_detail and takes everything else fresh', () => {
    const prior: SubagentRow = { ...CODER_ROW, enabled: true }
    const fresh: SubagentRow = { ...CODER_ROW, enabled: false, probe_detail: '', probe_status: 'unknown' }

    const [merged] = mergeProbeColumns([prior], [fresh])

    expect(merged?.probe_status).toBe('ready')
    expect(merged?.probe_detail).toBe('installed at /usr/bin/claude')
    expect(merged?.enabled).toBe(false)
  })

  it('leaves a row with no prior match as-is', () => {
    const fresh: SubagentRow = { ...OPENCODE_ROW, probe_detail: '', probe_status: 'unknown' }

    const [merged] = mergeProbeColumns([], [fresh])

    expect(merged).toEqual(fresh)
  })

  it('carries the prior group forward for a cli row a probe:false refresh reports as unknown/uninstalled', () => {
    const prior: SubagentRow = { ...CODER_ROW, group: 'installed', probe_status: 'ready' }
    const fresh: SubagentRow = { ...CODER_ROW, group: 'uninstalled', probe_detail: '', probe_status: 'unknown' }

    const [merged] = mergeProbeColumns([prior], [fresh])

    expect(merged?.group).toBe('installed')
  })

  it('always takes the fresh group for an openai row, since it needs no probe and is never stale', () => {
    const prior: SubagentRow = { ...OPENAI_CONFIGURED_ROW, group: 'uninstalled', has_api_key: false }
    const fresh: SubagentRow = {
      ...OPENAI_CONFIGURED_ROW,
      group: 'installed',
      has_api_key: true,
      probe_detail: '',
      probe_status: 'unknown'
    }

    const [merged] = mergeProbeColumns([prior], [fresh])

    expect(merged?.group).toBe('installed')
  })

  it('looks up the prior row by its old name when a rename is in flight, carrying its group too', () => {
    const prior: SubagentRow = { ...CODER_ROW, name: 'OldName' }
    const fresh: SubagentRow = {
      ...CODER_ROW,
      group: 'uninstalled',
      name: 'NewName',
      probe_detail: '',
      probe_status: 'unknown'
    }

    const [merged] = mergeProbeColumns([prior], [fresh], { from: 'OldName', to: 'NewName' })

    expect(merged?.probe_status).toBe('ready')
    expect(merged?.probe_detail).toBe(CODER_ROW.probe_detail)
    expect(merged?.group).toBe('installed')
  })

  it('does not apply the rename fallback to an unrelated row that simply has no prior match', () => {
    const prior: SubagentRow = { ...CODER_ROW, name: 'OldName' }
    const unrelated: SubagentRow = { ...OPENCODE_ROW, name: 'BrandNew', probe_detail: '', probe_status: 'unknown' }

    const [merged] = mergeProbeColumns([prior], [unrelated], { from: 'OldName', to: 'NewName' })

    expect(merged).toEqual(unrelated)
  })
})

describe('runningTestNames', () => {
  it('unions rows with test_running true and names tracked in local state, without duplicates', () => {
    const rows: SubagentRow[] = [
      { ...CODER_ROW, name: 'ServerOnly', test_running: true },
      { ...CODER_ROW, name: 'NotRunning', test_running: false }
    ]

    expect(runningTestNames(rows, new Map([['LocalOnly', 0]])).sort()).toEqual(['LocalOnly', 'ServerOnly'])
    expect(runningTestNames(rows, new Map([['ServerOnly', 0]]))).toEqual(['ServerOnly'])
  })

  it('is empty when nothing is running locally or on the server', () => {
    expect(runningTestNames([CODER_ROW, GUARD_ROW], new Map())).toEqual([])
  })
})

// Keystroke-driven terminal snapshots cannot reliably prove "was shown, then
// stopped being shown": this harness's frame() is a plain concatenation of
// every byte ever written (see ink's cell-level diffing, which can legally
// skip re-transmitting a cell whose previous content happens to already
// match), so a substring that appeared once can survive in `frame()` even
// after ink has visually replaced it on a real terminal. `failureDetailLine`
// is exercised directly instead -- the "moving off it hides it" half of the
// requirement is that calling it with a different row returns null.
describe('failureDetailLine', () => {
  it('formats "name: detail" for a row whose last test failed and left a detail', () => {
    const failed: SubagentRow = { ...GUARD_ROW, last_test_detail: 'connection refused: 127.0.0.1:4141' }

    expect(failureDetailLine(failed)).toBe('Guard: connection refused: 127.0.0.1:4141')
  })

  it('is null once the selection is a different row, even one that also failed but has no detail text', () => {
    expect(failureDetailLine(GUARD_ROW)).toBeNull()
  })

  it('is null for a row whose last test passed', () => {
    expect(failureDetailLine({ ...CODER_ROW, last_test_detail: 'ok', last_test_ok: true })).toBeNull()
  })

  it('is null for a row that has never been tested', () => {
    expect(failureDetailLine(CODER_ROW)).toBeNull()
  })

  it('is null when there is no selected row', () => {
    expect(failureDetailLine(undefined)).toBeNull()
  })
})

describe('SubagentsHub last-test-detail line', () => {
  it("renders the selected row's failure detail on screen", async () => {
    const failedRow: SubagentRow = {
      ...CODER_ROW,
      last_test_detail: 'connection refused: 127.0.0.1:4141',
      last_test_ok: false
    }
    const h = mount({ rows: [failedRow] })
    await waitForFrame(h, 'connection refused')

    expect(h.frame()).toContain('Coder: connection refused: 127.0.0.1:4141')

    h.unmount()
  })

  it('renders nothing extra for a row whose last test passed', async () => {
    const passedRow: SubagentRow = { ...CODER_ROW, last_test_detail: 'ok', last_test_ok: true }
    const h = mount({ rows: [passedRow] })
    await waitForFrame(h, 'Coder')
    await delay(60)

    expect(h.frame()).not.toContain('Coder: ok')

    h.unmount()
  })
})

describe('SubagentsHub test_running survival across close/reopen', () => {
  it('a row the server reports as test_running (never started in this session) renders a spinner-style elapsed counter', async () => {
    const runningRow: SubagentRow = { ...CODER_ROW, test_running: true }
    const h = mount({ rows: [runningRow] })
    await waitForFrame(h, 'Coder')

    for (let i = 0; i < 30; i++) {
      if (/\d+[hms]/.test(h.frame())) {
        break
      }
      await delay(30)
    }

    expect(h.frame()).toMatch(/\d+[hms]/)

    h.unmount()
  })

  it('esc cancels a test reported running by the server even though this session never pressed t', async () => {
    const runningRow: SubagentRow = { ...CODER_ROW, test_running: true }
    const h = mount({ rows: [runningRow] })
    await waitForFrame(h, 'Coder')

    h.gw.request.mockClear()
    await h.type(ESC)
    await delay(60)
    await waitForRpcCall(h.gw.request, 'subagents.test_cancel')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.test_cancel', { name: 'Coder' })
    expect(h.onClose).not.toHaveBeenCalled()

    h.unmount()
  })

  // Regression: a server-tracked test (never started by this instance's own
  // runTest) has no local promise to refresh rows once cancelled, so nothing
  // ever cleared it from `runningNames` -- every later Esc cancelled again
  // instead of closing. Esc must refresh after cancelling so a second press
  // sees the test is gone and closes.
  it('esc cancels a server-tracked test once, then closes the overlay on the next esc', async () => {
    const runningRow: SubagentRow = { ...CODER_ROW, test_running: true }
    let listCalls = 0
    const h = mount({
      listImpl: () => {
        listCalls += 1

        return listCalls === 1 ? { rows: [runningRow] } : { rows: [{ ...runningRow, test_running: false }] }
      }
    })
    await waitForFrame(h, 'Coder')

    await h.type(ESC)
    await delay(60)
    await waitForRpcCall(h.gw.request, 'subagents.test_cancel')
    for (let i = 0; i < 30; i++) {
      if (listCalls >= 2) {
        break
      }
      await delay(30)
    }
    await delay(90)

    expect(h.onClose).not.toHaveBeenCalled()

    await h.type(ESC)
    await waitForMockCall(h.onClose)

    expect(h.onClose).toHaveBeenCalled()

    h.unmount()
  })

  it('esc still closes on the next press even if the cancel RPC itself rejects', async () => {
    const runningRow: SubagentRow = { ...CODER_ROW, test_running: true }
    let listCalls = 0
    const h = mount({
      listImpl: () => {
        listCalls += 1

        return listCalls === 1 ? { rows: [runningRow] } : { rows: [{ ...runningRow, test_running: false }] }
      },
      requestImpl: method =>
        method === 'subagents.test_cancel' ? Promise.reject(new Error('cancel failed')) : undefined
    })
    await waitForFrame(h, 'Coder')

    await h.type(ESC)
    await delay(60)
    await waitForRpcCall(h.gw.request, 'subagents.test_cancel')
    for (let i = 0; i < 30; i++) {
      if (listCalls >= 2) {
        break
      }
      await delay(30)
    }
    await delay(90)

    await h.type(ESC)
    await waitForMockCall(h.onClose)

    expect(h.onClose).toHaveBeenCalled()

    h.unmount()
  })

  it('esc cancels a running test even after the selection has moved to a different row', async () => {
    const h = mount({
      requestImpl: method => (method === 'subagents.test' ? new Promise(() => {}) : undefined)
    })
    await waitForFrame(h, 'Coder')

    await h.type('t')
    await waitForRpcCall(h.gw.request, 'subagents.test')

    await h.type(DOWN)
    await waitForFrame(h, 'Guard')

    h.gw.request.mockClear()
    await h.type(ESC)
    await delay(60)
    await waitForRpcCall(h.gw.request, 'subagents.test_cancel')

    expect(h.gw.request).toHaveBeenCalledWith('subagents.test_cancel', { name: 'Coder' })
    expect(h.onClose).not.toHaveBeenCalled()

    h.unmount()
  })

  it('the footer does not mention cancelling a test when none is running', async () => {
    const h = mount()
    await waitForFrame(h, 'Coder')

    expect(h.frame()).not.toContain('Esc cancels the running test')

    h.unmount()
  })

  it('the footer explains that Esc cancels the running test while one is running', async () => {
    const runningRow: SubagentRow = { ...CODER_ROW, test_running: true }
    const h = mount({ rows: [runningRow] })
    await waitForFrame(h, 'Esc cancels the running test')

    h.unmount()
  })
})
