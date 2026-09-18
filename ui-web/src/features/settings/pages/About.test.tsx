// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetSources } from '../../../state/sources'
import { install, mount, snap } from '../harness'
import * as store from '../store'
import { workspacePath } from './About'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))
const copied = vi.hoisted(() => ({ values: [] as string[] }))
vi.mock('../../../lib/openUrl', () => ({ open: (v: string) => { copied.values.push(v) } }))

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

  it('the check button hands itself to the chrome, which owns the version check', async () => {
    const { calls } = install()
    await mount('about')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.about.check')) })
    expect(calls).toEqual([['checkUpdate', 'gui.settings.about.check']])
  })
})
