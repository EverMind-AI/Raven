// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { workspacePath } from './About'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))
const copied = vi.hoisted(() => ({ values: [] as string[] }))
vi.mock('../../../lib/openUrl', () => ({ open: (v: string) => { copied.values.push(v) } }))


beforeEach(() => {
  setSources({ settings: settingsSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
  copied.values = []
})

describe('about page', () => {
  it('shows the running version and both paths, and copies a path on its button', async () => {
    install()
    await mount('about')
    expect(screen.getByText('0.2.1')).toBeTruthy()
    expect(screen.getByText('/home/me/.raven/config.json')).toBeTruthy()
    expect(screen.getByText('/home/me/.raven/workspace')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getAllByLabelText('gui.settings.copy_path')[0]!) })
    expect(copied.values).toEqual(['/home/me/.raven/config.json'])
  })

  it('reads the storage location from agents.defaults.workspace when set', () => {
    const raw = snap().raw
    ;(raw.agents as { defaults: Record<string, unknown> }).defaults.workspace = '/data/raven'
    expect(workspacePath(raw, '/home/me/.raven/config.json')).toBe('/data/raven')
    expect(workspacePath({}, '/home/me/.raven/config.json')).toBe('/home/me/.raven/workspace')
    expect(workspacePath({}, '')).toBe('~/.raven/workspace')
  })

  it('offers the upgrade only once a newer version is known, and never starts it itself', async () => {
    /* A47 has two steps: check, and then the row offers the upgrade. The check
       used to call `askUpgrade` itself the moment it found a newer version,
       which made one click do two things. */
    const { calls } = install(snap(), { checkUpdate: async () => '0.3.0' })
    await mount('about')
    expect(screen.queryByText(/gui\.settings\.about\.upgrade/)).toBeNull()

    await act(async () => { fireEvent.click(screen.getByText('gui.settings.about.check')) })
    /* The check alone starts nothing: the upgrade seam is untouched until the
       reader asks for it. */
    expect(calls.map((c) => c[0])).toEqual([])

    const go = screen.getByText(/gui\.settings\.about\.upgrade/)
    expect(go.textContent).toContain('0.3.0')
    await act(async () => { fireEvent.click(go) })
    expect(calls.map((c) => c[0])).toEqual(['upgrade'])
  })

  it('stays on one button when the build is already current', async () => {
    install(snap(), { checkUpdate: async () => null })
    await mount('about')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.about.check')) })
    expect(screen.queryByText(/gui\.settings\.about\.upgrade/)).toBeNull()
  })

  it('the check button hands itself to the chrome, which owns the version check', async () => {
    const { calls } = install()
    await mount('about')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.about.check')) })
    expect(calls).toEqual([['checkUpdate', 'gui.settings.about.check']])
  })
})
