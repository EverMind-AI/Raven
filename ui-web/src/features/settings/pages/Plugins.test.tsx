// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, pkgList, settle, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { importCommand, pkgChip } from './Plugins'

import type { AgentPluginList, AgentPluginRow } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

const row = (over: Partial<AgentPluginRow> = {}, mcp: Partial<AgentPluginRow['mcp']> = {}): AgentPluginRow => {
  const base = pkgList().packages[0]!
  return { ...base, ...over, mcp: { ...base.mcp, ...mcp } }
}

const withPackages = (list: AgentPluginList): Parameters<typeof install>[1] => ({ agentPlugins: async () => list })

describe('plugins page', () => {
  it('names the most pressing state of a package in one chip', () => {
    expect(pkgChip(row())).toBe('none')
    expect(pkgChip(row({}, { allowed: false }))).toBe('restart')
    expect(pkgChip(row({}, { live: false }))).toBe('restart')
    expect(pkgChip(row({ problems: ['x: bad'] }, { allowed: false }))).toBe('restart')
    expect(pkgChip(row({ problems: ['x: bad'] }))).toBe('problem')
    expect(pkgChip(row({}, { problems: ['srv: bad'] }))).toBe('problem')
    expect(pkgChip(row({ skills_imported: false }))).toBe('unimported')
    expect(pkgChip(row({ skills: [], skills_imported: false }))).toBe('none')
  })

  it('quotes a source with a space in the import command', () => {
    expect(importCommand('/pkgs/a')).toBe('raven agent-plugins import /pkgs/a')
    expect(importCommand('/my pkgs/a')).toBe('raven agent-plugins import "/my pkgs/a"')
  })

  it('lists every package with its version and counts them', async () => {
    install()
    await mount('plugins')
    expect(screen.getByText('gui.settings.plugins.counter {"n":2}')).toBeTruthy()
    expect(screen.getByText('docs-kit')).toBeTruthy()
    expect(screen.getByText('fresh-pkg')).toBeTruthy()
    expect(screen.getByText('0.3.0')).toBeTruthy()
    expect(screen.getByText('gui.settings.plugins.unimported')).toBeTruthy()
  })

  it('opens a package on what it holds and how to import its skills', async () => {
    install()
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('fresh-pkg')) })
    expect(screen.getByText('/pkgs/fresh-pkg')).toBeTruthy()
    expect(screen.getByText('gui.settings.plugins.skills_out {"n":1}')).toBeTruthy()
    expect(screen.getByText('fresh-pkg__local')).toBeTruthy()
    expect(screen.getByText('gui.settings.plugins.server_off')).toBeTruthy()
    expect(screen.getByText('raven agent-plugins import /pkgs/fresh-pkg')).toBeTruthy()
  })

  it('the MCP switch goes through setPackageMcp and draws the answer', async () => {
    const pending = pkgList()
    pending.packages[1]!.mcp.allowed = true
    const { calls } = install(undefined, { setPackageMcp: async (name, on) => { calls.push(['setPackageMcp', { name, on }]); return pending } })
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('fresh-pkg')) })
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'gui.settings.plugins.mcp_switch' })) })
    await settle()
    expect(calls).toContainEqual(['setPackageMcp', { name: 'fresh-pkg', on: true }])
    /* Allowed but not started: the engine keeps the set it started with. */
    expect(screen.getByText('gui.plug.needs_restart')).toBeTruthy()
  })

  it('offers no MCP switch for a package without servers', async () => {
    const list = pkgList()
    list.packages = [row({ name: 'skills-only' }, { servers: [], allowed: false, live: false })]
    install(undefined, withPackages(list))
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('skills-only')) })
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('says so when no package is found, without a count', async () => {
    install(undefined, withPackages({ packages: [], problems: [] }))
    await mount('plugins')
    expect(screen.getByText('gui.settings.plugins.none')).toBeTruthy()
    expect(screen.queryByText(/gui\.settings\.plugins\.counter/)).toBeNull()
  })

  it('lists the configured paths that hold no package', async () => {
    install(undefined, withPackages(pkgList({ problems: [{ path: '/gone', reason: 'does not exist' }] })))
    await mount('plugins')
    expect(screen.getByText('gui.settings.plugins.path_problems')).toBeTruthy()
    expect(screen.getByText('/gone: does not exist')).toBeTruthy()
  })
})
