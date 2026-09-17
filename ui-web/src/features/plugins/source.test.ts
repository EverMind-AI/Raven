// @vitest-environment happy-dom
/* The four row builders one `ext.list` read fans out into, and the hub's i18n
 * objects flattened to the one language the island sees. Untested while they
 * lived in the legacy layer, and each carries a rule a reading of the code
 * alone does not make obvious.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { pmNormEntry } from './source'
import { mkMcpRow, mkPluginRow, mkSkillRow, mkToolRow } from '../installed/source'
import * as plugins from './store'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as pageStore from '../../state/page'
import * as confirmStore from '../../state/confirm'
import type { DetailEntry } from './types'
import type { ExtMcpRow, ExtPluginRow, ExtSkillRow, ExtToolRow } from '../installed/source'

/* The contract's own row shapes, filled in for the fields a case is not about
   -- every builder here reads a handful of them. */
const tool = (over: Partial<ExtToolRow>): ExtToolRow =>
  ({ name: 'read_file', description: '', enabled: true, ...over }) as ExtToolRow
const skill = (over: Partial<ExtSkillRow>): ExtSkillRow =>
  ({ name: 'review', source: 'local', description: '', ...over }) as ExtSkillRow
const plugin = (over: Partial<ExtPluginRow>): ExtPluginRow =>
  ({ id: 'p', version: '1', enabled: true, ...over }) as ExtPluginRow
const mcp = (over: Partial<ExtMcpRow>): ExtMcpRow =>
  ({ name: 'a', transport: 'stdio', enabled: true, state: 'connected', tool_count: 0, ...over }) as ExtMcpRow

setTranslator((key: string, vars?: Record<string, unknown> | null, fallback?: string | null) =>
(vars ? `${key}(${Object.entries(vars).map(([k, v]) => `${k}=${v}`).join(',')})` : (fallback ?? key)))
vi.spyOn(pageStore, 'show').mockImplementation(() => {})
vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})

beforeEach(() => {
})

describe('one tool row', () => {
  it('names an unknown tool by its own id', () => {
    expect(mkToolRow(tool({ name: 'read_file', description: 'read' })).name).toBe('read_file')
  })

  it('sorts a tool into a group, and tells the net ones apart by reach', () => {
    expect(mkToolRow(tool({ name: 'read_file' }))).toMatchObject({ group: 'file', reach: 'local' })
    expect(mkToolRow(tool({ name: 'web_search' }))).toMatchObject({ group: 'net', reach: 'net' })
    expect(mkToolRow(tool({ name: 'ask_user' }))).toMatchObject({ group: 'ask', reach: 'local' })
    expect(mkToolRow(tool({ name: 'exec' }))).toMatchObject({ group: 'run', reach: 'local' })
  })

  it('marks the three that rewrite the reader disk', () => {
    expect(mkToolRow(tool({ name: 'exec' })).danger).toBe(true)
    expect(mkToolRow(tool({ name: 'read_file' })).danger).toBe(false)
  })

  /* A key-gated tool used to be absent, which reads as removed. The row exists
     so the reader learns what it wants, and it cannot be switched on. */
  it('shows a key-gated tool as off, with what it needs', () => {
    const row = mkToolRow(tool({ name: 'web_search', needs: { setting: 'websearch.apiKey', env: 'TAVILY_API_KEY' } }))
    expect(row).toMatchObject({ needs: { setting: 'websearch.apiKey' }, on: false })
    row.on = true
    expect(row.on).toBe(true)
  })

  /* Everything else reads its switch off the disabled list, so a page can
     toggle a row without knowing there is a config file behind it. */
  it('reads an ordinary tool switch off the disabled list', () => {
    expect(mkToolRow(tool({ name: 'read_file' })).on).toBe(true)
  })
})

describe('one skill row', () => {
  it('takes its glyph from the name, uppercased', () => {
    expect(mkSkillRow(skill({ name: 'review', source: 'local' }))).toMatchObject({ id: 'review', glyph: 'R' })
  })

  it('carries the hub id only when it came from the hub', () => {
    expect(mkSkillRow(skill({ name: 'a', source: 'local' }))).toMatchObject({ hub: false, hubId: '' })
    expect(mkSkillRow(skill({ name: 'b', source: 'hub', hub: true, hub_id: 'h1' }))).toMatchObject({ hub: true, hubId: 'h1' })
  })

  /* A skill is method, not access: there is nothing to switch off, and saying
     so is what the setter does. */
  it('refuses to be switched off, and says why', () => {
    const toast = vi.fn()
    setTranslator((key: string) => key)
    const row = mkSkillRow(skill({ name: 'review', source: 'local' })) as unknown as { state: string }
    expect(row.state).toBe('on')
    row.state = 'off'
    expect(row.state).toBe('on')
    expect(toast).not.toHaveBeenCalled()
  })
})

describe('one plugin row', () => {
  it('names a bundled plugin differently from an installed one', () => {
    expect(mkPluginRow(plugin({ id: 'p', bundled: true })).src).toBe('gui.ext.builtin_plugin')
    expect(mkPluginRow(plugin({ id: 'p' })).src).toBe('gui.ext.plugin')
  })

  it('falls back to the id when the plugin has no display name', () => {
    expect(mkPluginRow(plugin({ id: 'python' })).name).toBe('python')
    expect(mkPluginRow(plugin({ id: 'python', display_name: 'Python' })).name).toBe('Python')
  })

  it('reads its own enabled flag while nothing has disabled it', () => {
    expect(mkPluginRow(plugin({ id: 'p', enabled: true })).state).toBe('on')
    expect(mkPluginRow(plugin({ id: 'p', enabled: false })).state).toBe('off')
  })
})

describe('one mcp row', () => {
  /* The badge's vocabulary is need/fail/on/off; the page itself renders the
     raw snapshot, which rides along on the row. */
  it('maps the server state into the badge vocabulary', () => {
    expect(mkMcpRow(mcp({ name: 'a', transport: 'stdio', enabled: true, state: 'connected' })).state).toBe('on')
    expect(mkMcpRow(mcp({ name: 'a', transport: 'stdio', enabled: true, state: 'connecting' })).state).toBe('on')
    expect(mkMcpRow(mcp({ name: 'a', transport: 'stdio', enabled: true, state: 'auth_required' })).state).toBe('need')
    expect(mkMcpRow(mcp({ name: 'a', transport: 'stdio', enabled: true, state: 'error' })).state).toBe('fail')
    expect(mkMcpRow(mcp({ name: 'a', transport: 'stdio', enabled: true, state: 'disconnected' })).state).toBe('off')
    expect(mkMcpRow(mcp({ name: 'a', transport: 'stdio', enabled: true, state: undefined })).state).toBe('off')
  })

  it('reads a disabled server as off whatever its connection is doing', () => {
    expect(mkMcpRow(mcp({ name: 'a', transport: 'stdio', enabled: false, state: 'connected' })).state).toBe('off')
  })

  it('switches through the island, which owns the write', () => {
    const toggleMcp = vi.spyOn(plugins, 'toggleMcp').mockImplementation(() => {})
    try {
      const row = mkMcpRow(mcp({ name: 'remote', transport: 'http' })) as unknown as { state: string }
      row.state = 'on'
      expect(toggleMcp).toHaveBeenCalledWith('remote', true)
      row.state = 'off'
      expect(toggleMcp).toHaveBeenCalledWith('remote', false)
    } finally {
      toggleMcp.mockRestore()
    }
  })
})

describe('one hub entry', () => {
  /* The hub serves i18n objects for its text fields; which language wins is
     decided once, so the island only ever sees plain strings. */
  it('flattens the three text fields to one language', () => {
    const entry = pmNormEntry({
      id: 'crm', name: { en: 'CRM', zh: 'C' }, summary: { en: 'sells' }, description: { en: 'long' },
    } as unknown as DetailEntry)
    expect(entry).toMatchObject({ name: 'CRM', summary: 'sells', description: 'long' })
  })

  it('leaves a field that is already a plain string alone', () => {
    const entry = pmNormEntry({ id: 'crm', name: 'CRM' } as unknown as DetailEntry)
    expect(entry.name).toBe('CRM')
  })

  it('flattens an auth field label, and only for an mcp contribution', () => {
    const entry = pmNormEntry({
      id: 'crm',
      name: 'CRM',
      contributes: [
        { kind: 'mcp', auth: { fields: [{ key: 'token', label: { en: 'Token' } }] } },
        { kind: 'tool', name: 'crm_search' },
      ],
    } as unknown as DetailEntry)
    expect(entry.contributes?.[0]?.auth?.fields?.[0]?.label).toBe('Token')
    expect(entry.contributes?.[1]).toMatchObject({ kind: 'tool', name: 'crm_search' })
  })
})
