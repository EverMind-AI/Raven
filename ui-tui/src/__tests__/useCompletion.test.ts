import { render } from 'ink-testing-library'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SlashCommand } from '../app/slash/types.js'
import type { GatewayClient } from '../gatewayClientStub.js'

import { SLASH_COMMANDS } from '../app/slash/registry.js'
import { completionRequestForInput, slashCompletions, useCompletion } from '../hooks/useCompletion.js'

describe('completionRequestForInput', () => {
  it('returns null for slash commands (handled locally now)', () => {
    expect(completionRequestForInput('/help')).toBeNull()
  })

  it('does not route absolute paths through slash completion', () => {
    expect(
      completionRequestForInput('/home/d/Desktop/agenda/CrimsonRed/.raven/plans/2026-05-04-HANDOFF-NEXT.md')
    ).toMatchObject({
      method: 'complete.path',
      params: { word: '/home/d/Desktop/agenda/CrimsonRed/.raven/plans/2026-05-04-HANDOFF-NEXT.md' },
      replaceFrom: 0
    })
  })

  it('keeps path completion for trailing absolute path tokens', () => {
    expect(completionRequestForInput('read /home/d/Desktop/file.md')).toMatchObject({
      method: 'complete.path',
      params: { word: '/home/d/Desktop/file.md' },
      replaceFrom: 5
    })
  })

  it('leaves plain text alone', () => {
    expect(completionRequestForInput('hello there')).toBeNull()
  })

  it('returns null for /model (preserved short-circuit)', () => {
    expect(completionRequestForInput('/model')).toBeNull()
    expect(completionRequestForInput('/model ')).toBeNull()
    expect(completionRequestForInput('/model gpt-4')).toBeNull()
  })
})

const MINI_REGISTRY: SlashCommand[] = [
  { name: 'quit', aliases: ['exit', 'q'], help: 'exit raven', run: vi.fn() },
  { name: 'status', help: 'show live session info', run: vi.fn() },
  { name: 'save', help: 'save the current transcript to JSON', run: vi.fn() },
  { name: 'sessions', run: vi.fn() },
  { name: 'skills', help: 'browse skill commands', run: vi.fn() }
]

describe('slashCompletions', () => {
  it('returns [] for non-slash input', () => {
    expect(slashCompletions('hello', MINI_REGISTRY)).toEqual([])
    expect(slashCompletions('', MINI_REGISTRY)).toEqual([])
    expect(slashCompletions('  ', MINI_REGISTRY)).toEqual([])
  })

  it('/model behaves like any command: matches on name, closes on args', () => {
    const reg: SlashCommand[] = [{ name: 'model', help: 'change or show model', run: vi.fn() }]
    expect(slashCompletions('/model', reg).map(i => i.display)).toEqual(['/model'])
    // close-on-args still applies once a space (argument) is typed.
    expect(slashCompletions('/model ', reg)).toEqual([])
    expect(slashCompletions('/model gpt-4', reg)).toEqual([])
  })

  it('returns [] when no commands match the prefix', () => {
    expect(slashCompletions('/zzz', MINI_REGISTRY)).toEqual([])
    expect(slashCompletions('/xyz123', MINI_REGISTRY)).toEqual([])
  })

  it('prefix-matches multiple commands in registry order', () => {
    const items = slashCompletions('/s', MINI_REGISTRY)
    const names = items.map(i => i.display)

    expect(names).toContain('/status')
    expect(names).toContain('/save')
    expect(names).toContain('/sessions')
    expect(names).toContain('/skills')
    expect(names.indexOf('/status')).toBeLessThan(names.indexOf('/save'))
    expect(names.indexOf('/save')).toBeLessThan(names.indexOf('/sessions'))
  })

  it('exact-name match returns only that command', () => {
    const items = slashCompletions('/status', MINI_REGISTRY)

    expect(items).toHaveLength(1)
    expect(items[0]!.display).toBe('/status')
  })

  it('case-insensitive prefix match on name', () => {
    const items = slashCompletions('/S', MINI_REGISTRY)

    expect(items.map(i => i.display)).toContain('/status')
    expect(items.map(i => i.display)).toContain('/save')
  })

  it('matches via alias (case-insensitive) — /Q → /quit', () => {
    const items = slashCompletions('/Q', MINI_REGISTRY)

    expect(items).toHaveLength(1)
    expect(items[0]!.display).toBe('/quit')
  })

  it('alias prefix match resolves to canonical command', () => {
    const items = slashCompletions('/ex', MINI_REGISTRY)

    expect(items).toHaveLength(1)
    expect(items[0]!.display).toBe('/quit')
  })

  it('deduplicates when name AND alias both match', () => {
    const reg: SlashCommand[] = [{ name: 'save', aliases: ['sv'], help: 'save', run: vi.fn() }]
    const items = slashCompletions('/s', reg)

    expect(items).toHaveLength(1)
    expect(items[0]!.display).toBe('/save')
  })

  it('sets display to /<name>', () => {
    const items = slashCompletions('/stat', MINI_REGISTRY)

    expect(items[0]!.display).toBe('/status')
  })

  it('sets text to /<name> so the accept handler yields /<name>', () => {
    const items = slashCompletions('/stat', MINI_REGISTRY)

    expect(items[0]!.text).toBe('/status')
  })

  it('includes help as meta when available', () => {
    const items = slashCompletions('/stat', MINI_REGISTRY)

    expect(items[0]!.meta).toBe('show live session info')
  })

  it('omits meta when command has no help', () => {
    const items = slashCompletions('/sess', MINI_REGISTRY)

    expect(items[0]!.display).toBe('/sessions')
    expect(items[0]!.meta).toBeUndefined()
  })

  it('bare / returns all commands in registry order', () => {
    const items = slashCompletions('/', MINI_REGISTRY)

    expect(items).toHaveLength(MINI_REGISTRY.length)
    expect(items[0]!.display).toBe('/quit')
  })

  it('works against the real SLASH_COMMANDS registry', () => {
    const items = slashCompletions('/hel', SLASH_COMMANDS)

    expect(items.length).toBeGreaterThanOrEqual(1)
    expect(items[0]!.display).toBe('/help')
    expect(items[0]!.text).toBe('/help')
  })

  it('returns [] once the input has arguments after the command name', () => {
    expect(slashCompletions('/sessions list', MINI_REGISTRY)).toEqual([])
    expect(slashCompletions('/status ', MINI_REGISTRY)).toEqual([])
  })
})

describe('slashCompletions supported filter', () => {
  const REG: SlashCommand[] = [
    { help: 'live info', name: 'status', run: vi.fn() }, // supported undefined → shown
    { help: 'save', name: 'save', run: vi.fn(), supported: false }, // hidden
    { name: 'sessions', run: vi.fn(), supported: true } // explicit → shown
  ]

  it('hides a supported:false command even when its prefix matches', () => {
    const names = slashCompletions('/s', REG).map(i => i.display)

    expect(names).not.toContain('/save')
    expect(names).toContain('/status')
    expect(names).toContain('/sessions')
  })

  it('hides a supported:false command on exact-name match too', () => {
    expect(slashCompletions('/save', REG)).toEqual([])
  })

  it('shows commands whose supported flag is true or absent (default shown)', () => {
    expect(slashCompletions('/status', REG).map(i => i.display)).toContain('/status')
    expect(slashCompletions('/sessions', REG).map(i => i.display)).toContain('/sessions')
  })

  // The owner-reviewed curation: 34 drop commands hidden, 19 keep shown.
  const DROP_NAMES = [
    'redraw',
    'fortune',
    'terminal-setup',
    'save',
    'statusbar',
    'queue',
    'steer',
    'background',
    'image',
    'personality',
    'compress',
    'voice',
    'skin',
    'indicator',
    'yolo',
    'reasoning',
    'fast',
    'busy',
    'verbose',
    'usage',
    'stop',
    'reload-mcp',
    'reload',
    'browser',
    'rollback',
    'agents',
    'replay',
    'replay-diff',
    'reload-skills',
    'skills',
    'tools',
    'setup',
    'heapdump',
    'mem'
  ]

  const KEEP_NAMES = [
    'help',
    'quit',
    'mouse',
    'new',
    'status',
    'resume',
    'title',
    'compact',
    'details',
    'copy',
    'paste',
    'logs',
    'history',
    'undo',
    'retry',
    'sessions',
    'branch',
    'export',
    'model'
  ]

  it('hides every drop command from the real registry (bare /)', () => {
    const all = slashCompletions('/', SLASH_COMMANDS).map(i => i.display)

    for (const drop of DROP_NAMES) {
      expect(all, `${drop} should be hidden`).not.toContain(`/${drop}`)
    }
  })

  it('keeps every keep command visible in the real registry (bare /)', () => {
    const all = slashCompletions('/', SLASH_COMMANDS).map(i => i.display)

    for (const keep of KEEP_NAMES) {
      expect(all, `${keep} should be visible`).toContain(`/${keep}`)
    }
  })
})

function HookSpy({
  input,
  blocked,
  gw,
  out
}: {
  input: string
  blocked: boolean
  gw: GatewayClient
  out: { completions: { display: string }[]; gwCallCount: number }
}) {
  const { completions } = useCompletion(input, blocked, gw)

  out.completions = completions
  out.gwCallCount = (gw.request as ReturnType<typeof vi.fn>).mock.calls.length

  return React.createElement(React.Fragment, null)
}

describe('useCompletion slash branch — no complete.slash RPC', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('slash input populates completions without calling gw.request', async () => {
    const gw = { request: vi.fn(() => Promise.resolve(null)), getLogTail: vi.fn(() => '') } as unknown as GatewayClient
    const out = { completions: [] as { display: string }[], gwCallCount: 0 }

    render(React.createElement(HookSpy, { input: '/s', blocked: false, gw, out }))

    // Slash branch is synchronous — no debounce timer needed.
    // Advance timers well past the 60ms debounce to confirm no delayed RPC fires either.
    await vi.runAllTimersAsync()

    expect(gw.request).not.toHaveBeenCalled()
    expect(out.completions.length).toBeGreaterThan(0)
    expect(out.completions.some(c => c.display === '/status')).toBe(true)
  })

  it('path-like input calls gw.request("complete.path", ...) after the debounce', async () => {
    const gw = {
      request: vi.fn(() =>
        Promise.resolve({ items: [{ text: 'src/foo.ts', display: 'src/foo.ts' }], replace_from: 2 })
      ),
      getLogTail: vi.fn(() => '')
    } as unknown as GatewayClient
    const out = { completions: [] as { display: string }[], gwCallCount: 0 }

    render(React.createElement(HookSpy, { input: './src/', blocked: false, gw, out }))

    await vi.runAllTimersAsync()

    expect(gw.request).toHaveBeenCalledWith('complete.path', expect.objectContaining({ word: './src/' }))
  })
})
