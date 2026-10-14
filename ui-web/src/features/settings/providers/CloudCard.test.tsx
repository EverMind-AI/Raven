// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, snap, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { EverosCloudCard } from './CloudCard'

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

const cloud = {
  available: true, selected: true, api_key_set: true, key_source: 'file', base_url: 'http://fake', status: 'ok', hint: 'http://fake',
}

/* No page mounts the card (pages/Model.tsx stopped drawing it, the Memory
   section has yet to be designed), so it is rendered here on its own. Order
   mirrors what a page does: render, then let the store open and patch the
   probe's answer in. */
const show = async (data: ReturnType<typeof snap>): Promise<ReturnType<typeof install>> => {
  const installed = install(data)
  render(createElement(EverosCloudCard), { container: document.body.appendChild(document.createElement('div')) })
  await act(async () => { await store.open() })
  return installed
}

const withCloud = (over: Partial<typeof cloud> = {}): ReturnType<typeof snap> => {
  const data = snap()
  data.everosCloud = { ...cloud, ...over }
  return data
}

describe('the EverOS Cloud card', () => {
  it('names the service, its state and its endpoint when the cloud backend is the selected one', async () => {
    await show(withCloud())

    expect(screen.getByText('gui.settings.cloud.title')).toBeTruthy()
    expect(screen.getByText('gui.settings.cloud.chip_connected')).toBeTruthy()
    expect(screen.getByText('http://fake')).toBeTruthy()
  })

  it('draws nothing when the cloud backend is not the selected one', async () => {
    await show(withCloud({ selected: false }))

    expect(screen.queryByText('gui.settings.cloud.title')).toBe(null)
  })

  it('says the key comes from the environment when settings.everosCloud reports key_source env', async () => {
    await show(withCloud({ key_source: 'env' }))

    expect(screen.getByText('gui.settings.cloud.key_env')).toBeTruthy()
  })

  it('says a key is needed when the probe found none', async () => {
    await show(withCloud({ api_key_set: false, status: 'missing', hint: 'no API key' }))

    expect(screen.getByText('gui.settings.cloud.chip_needs_key')).toBeTruthy()
  })

  it('writes plugins.config.everos-cloud-memory.api_key through settings.set', async () => {
    const { calls } = await show(withCloud())

    const input = screen.getByLabelText('gui.settings.cloud.key') as HTMLInputElement
    await act(async () => { fireEvent.change(input, { target: { value: 'ecm-key-rot8' } }) })
    const save = input.closest('.settings-taglist')!.querySelector('button.mini') as HTMLButtonElement
    await act(async () => { fireEvent.click(save) })

    expect(calls).toContainEqual(['set', { key: 'plugins.config.everos-cloud-memory.api_key', value: 'ecm-key-rot8' }])
  })
})
