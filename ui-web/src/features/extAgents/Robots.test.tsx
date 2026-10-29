// @vitest-environment happy-dom
/* Native-page robot registration against the typed offline RPC transport. */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { setTranslator } from '../../i18n/t'
import { demoFixtures } from '../../rpc/fixtures'
import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import { resetSources, setSources } from '../../state/sources'
import { mountPageRoot } from '../../test/pageRoot'
import { ExtAgentsApp } from './ExtAgentsPage'
import * as machines from './machines'
import { robotStatus } from './Robots'
import { extAgentsSource, machinesSource, robotsSource } from './source'
import * as store from './store'

import type { RobotRow } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let unmountPage: () => void
let transport: FixtureTransport

beforeEach(() => {
  setTranslator((key) => key)
  transport = new FixtureTransport(demoFixtures)
  setGateway(transport)
  setSources({ robots: robotsSource, machines: machinesSource, extAgents: extAgentsSource })
  unmountPage = mountPageRoot()
})

afterEach(() => {
  cleanup()
  act(() => unmountPage())
  store._resetForTests()
  machines._resetForTests()
  resetSources()
  setGateway(null)
})

async function open(): Promise<void> {
  render(<ExtAgentsApp />, { container: document.getElementById('extAgentsBody')! })
  await act(async () => store.openRobots())
}

async function click(name: string): Promise<void> {
  await act(async () => fireEvent.click(screen.getByRole('button', { name })))
}

describe('personal robot registration', () => {
  it('registers, edits, removes, and restores through the native source', async () => {
    await open()
    await click('gui.extAgents.robot_add')
    const form = screen.getByRole('form', { name: 'gui.extAgents.robot_add' })
    fireEvent.change(within(form).getByLabelText('gui.extAgents.robot_name'), { target: { value: 'Fixture device' } })
    fireEvent.click(within(form).getByLabelText('gui.extAgents.robot_personal_confirm'))
    await act(async () => fireEvent.submit(form))
    expect(screen.getByRole('article', { name: 'Fixture device' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'gui.extAgents.robot_check' })).toBeNull()
    await click('gui.extAgents.robot_edit')
    const edit = screen.getByRole('form', { name: 'gui.extAgents.robot_edit' })
    fireEvent.change(within(edit).getByLabelText('gui.extAgents.robot_name'), { target: { value: 'Edited fixture device' } })
    await act(async () => fireEvent.submit(edit))
    expect(screen.getByRole('article', { name: 'Edited fixture device' })).toBeTruthy()
    await click('gui.extAgents.robot_remove')
    expect(screen.queryByRole('article')).toBeNull()
    await click('gui.extAgents.robot_trash')
    await click('gui.extAgents.robot_restore')
    await click('gui.extAgents.robot_back')
    expect(screen.getByRole('article', { name: 'Edited fixture device' })).toBeTruthy()
    expect(transport.calls.filter((call) => call.method === 'robots.check')).toHaveLength(0)
    expect((await robotsSource.list(false))).toHaveLength(1)
  })

  it('blocks company registration and limits non-HTTP robots to metadata', async () => {
    await open()
    await click('gui.extAgents.robot_add')
    fireEvent.change(screen.getByLabelText('gui.extAgents.robot_domain'), { target: { value: 'company' } })
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'gui.save' }).disabled).toBe(true)
    expect(screen.queryByLabelText('gui.extAgents.robot_name')).toBeNull()
    expect(screen.getByRole('alert').textContent).toBe('gui.extAgents.robot_company_blocked')
    fireEvent.change(screen.getByLabelText('gui.extAgents.robot_domain'), { target: { value: 'personal' } })
    fireEvent.change(screen.getByLabelText('gui.extAgents.robot_type'), { target: { value: 'agv' } })
    expect(screen.getByRole<HTMLOptionElement>('option', { name: 'gui.extAgents.robot_http' }).disabled).toBe(true)
    expect(screen.queryByLabelText('gui.extAgents.robot_status_url')).toBeNull()
    expect(transport.calls.filter((call) => call.method === 'robots.save')).toHaveLength(0)
  })

  it('checks HTTP status only on demand and clears the endpoint when changed to metadata', async () => {
    await robotsSource.save({ domain: 'personal', confirm_personal: true, name: 'HTTP fixture', type: 'sparkbot', connection_mode: 'http_status', status_url: 'http://10.0.0.2:8080/health' })
    await open()
    expect(screen.getByRole('article', { name: 'HTTP fixture' }).textContent).toContain('gui.extAgents.robot_disconnected')
    expect(transport.calls.filter((call) => call.method === 'robots.check')).toHaveLength(0)
    await click('gui.extAgents.robot_check')
    expect(transport.calls.filter((call) => call.method === 'robots.check')).toHaveLength(1)
    expect(screen.getByRole('article', { name: 'HTTP fixture' }).textContent).toContain('Offline fixtures never contact physical devices.')
    await click('gui.extAgents.robot_edit')
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'gui.extAgents.robot_trash' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'gui.extAgents.robot_check' }).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('gui.extAgents.robot_type'), { target: { value: 'agv' } })
    expect(screen.queryByLabelText('gui.extAgents.robot_status_url')).toBeNull()
    const form = screen.getByRole('form', { name: 'gui.extAgents.robot_edit' })
    await act(async () => fireEvent.submit(form))
    const record = (await robotsSource.list(false))[0]!
    expect(record.connection_mode).toBe('metadata')
    expect(record.status_url).toBe('')
    expect(record.observation).toBeNull()
    expect(screen.queryByRole('button', { name: 'gui.extAgents.robot_check' })).toBeNull()
  })

  it('degrades to an explicit unavailable state on an older gateway', async () => {
    setGateway(new FixtureTransport({}))
    await open()
    expect(screen.getByRole('status').textContent).toBe('gui.extAgents.robot_unavailable')
    expect(screen.queryByRole('article')).toBeNull()
  })

  it('expires successful observations without another network check', () => {
    const row: RobotRow = {
      id: 'a'.repeat(32), revision: 1, domain: 'personal', name: 'Fixture', type: 'sparkbot', connection_mode: 'http_status',
      model: '', location: '', note: '', status_url: '', expected_project: '', updated_at: '', deleted_at: null, stale: false, status: 'reachable',
      observation: { status: 'reachable', checked_epoch: 100, checked_at: '', http_status: 200, detail: '', observed: {} },
    }
    expect(robotStatus(row, 110_000)).toBe('reachable')
    expect(robotStatus(row, 161_000)).toBe('unknown')
    expect(robotStatus(row, 94_000)).toBe('unknown')
  })
})
