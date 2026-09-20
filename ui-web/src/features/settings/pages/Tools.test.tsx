// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import { legacyKey, vendorKey, webVendor } from '../source'
import * as store from '../store'
import { TOOL_GROUPS, blocker } from './Tools'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))


beforeEach(() => {
  setSources({ settings: settingsSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

describe('tools page', () => {
  it('draws the eight groups, greys the meta tools with no working switch, and counts switchable tools only', async () => {
    install()
    await mount('tools')
    expect(document.querySelectorAll('.settings-card')).toHaveLength(Object.keys(TOOL_GROUPS).length)
    /* The two sides of `builtin`, drawn differently. A meta-tool has no switch
       this page could write: the greyed class is asserted with the attribute
       because `aria-disabled` on its own is invisible -- drawn like every
       other switch it reads as a control that ignores clicks. A schema-hidden
       tool is not in that group: `tools.disabledTools` still takes it away, so
       it carries a real one, and drawing it fixed told the reader its switch
       did nothing. */
    for (const id of ['tool_search', 'tool_call']) {
      const sw = screen.getByLabelText(id)
      expect(sw.getAttribute('aria-disabled')).toBe('true')
      expect(sw.tagName).toBe('SPAN')
      expect(sw.classList.contains('settings-swi-fixed')).toBe(true)
    }
    const dag = screen.getByLabelText('cancel_dag')
    expect(dag.getAttribute('aria-disabled'), 'cancel_dag answers to the switch, so it carries one').toBeNull()
    expect(dag.classList.contains('settings-swi-fixed')).toBe(false)
    expect(TOOL_GROUPS.collab).toContain('cancel_dag')
    expect(TOOL_GROUPS.search).not.toContain('cancel_dag')
    /* The note that says where `tool_search`'s switch really is. Without it the
       row reads as a tool that cannot be turned on at all. */
    expect(screen.getByText('gui.settings.tools.meta_elsewhere')).toBeTruthy()
    /* Eight known tools, none of them meta: read_file, exec, spawn, web_fetch
       (Jina reads without a key) and cancel_dag are on and unblocked;
       web_search lacks its key; deep_research and image_generate are switched
       off. cancel_dag counts because its switch works -- while it was drawn as
       built in the counter left it out, so the total under-reported what the
       reader can actually turn off. */
    expect(screen.getByText('gui.settings.tools.counter {"on":5,"total":8}')).toBeTruthy()
  })

  it('the badge says needs setup where a key or a model is missing, and the panel names the missing piece', async () => {
    install()
    await mount('tools')
    const raw = snap().raw
    expect(blocker('web_search', raw)).toBe('key')
    expect(blocker('exec', raw)).toBe('')
    expect(blocker('image_generate', raw)).toBe('model')
    expect(screen.getAllByText('gui.settings.tools.setup')).toHaveLength(1)
    await act(async () => { fireEvent.click(screen.getByText('web_search')) })
    expect(screen.getByLabelText('gui.settings.tools.vendor')).toBeTruthy()
    expect(screen.getByText('gui.settings.tools.vendor_key {"name":"Serper"}')).toBeTruthy()
  })

  it('a switch writes the whole disabled list; turning on a blocked tool also opens its panel', async () => {
    const { calls } = install()
    await mount('tools')
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'exec' })) })
    expect(calls).toEqual([['set', { key: 'tools.disabledTools', value: ['image_generate', 'deep_research', 'exec'] }]])
    calls.length = 0
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'deep_research' })) })
    expect(calls).toEqual([['set', { key: 'tools.disabledTools', value: ['image_generate'] }]])
    expect(store.get().toolOpen).toBe('deep_research')
    expect(screen.getByText('gui.settings.tools.vendor_key {"name":"MiroThinker"}')).toBeTruthy()
  })

  it('the web search panel writes the vendor and its own key slot, and a clear retires the legacy leaf too', async () => {
    const data = snap()
    ;(data.raw.tools as Record<string, unknown>).web = { search: { provider: 'tavily' }, providers: { tavily: { apiKey: '****set****' } } }
    const { calls } = install(data)
    await mount('tools')
    await act(async () => { fireEvent.click(screen.getByText('web_search')) })
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.settings.tools.vendor'), { target: { value: 'exa' } }) })
    expect(calls).toEqual([['set', { key: 'tools.web.search.provider', value: 'exa' }]])
    calls.length = 0
    const box = screen.getByLabelText('gui.settings.tools.vendor_key {"name":"Tavily"}') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'tv-key' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.update')) })
    expect(calls).toEqual([['set', { key: 'tools.web.providers.tavily.apiKey', value: 'tv-key' }]])
    calls.length = 0
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.clear')) })
    expect(calls).toEqual([['set', { key: 'tools.web.providers.tavily.apiKey', value: '' }]])
  })

  it('a serper key in the pre-vendor leaf counts as set, and a clear empties that leaf', async () => {
    const data = snap()
    ;(data.raw.tools as Record<string, unknown>).web = { search: { provider: 'serper', apiKey: '****set****' } }
    const { calls } = install(data)
    await mount('tools')
    expect(blocker('web_search', data.raw)).toBe('')
    expect(webVendor('web_fetch', data.raw)).toBe('jina')
    expect(vendorKey('serply')).toBe('tools.web.providers.serply.apiKey')
    expect(legacyKey('web_fetch', 'jina')).toBe('tools.web.jinaApiKey')
    expect(legacyKey('web_fetch', 'tavily')).toBeNull()
    await act(async () => { fireEvent.click(screen.getByText('web_search')) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.clear')) })
    expect(calls).toEqual([['set', { key: 'tools.web.search.apiKey', value: '' }]])
  })

  it('a media tool row carries the same role pill as the model page', async () => {
    const { calls } = install()
    await mount('tools')
    await act(async () => { fireEvent.click(screen.getByText('image_generate')) })
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.roles.change {"role":"gui.settings.roles.image"}')) })
    await act(async () => { fireEvent.click(screen.getByText('openai/gpt-4o')) })
    expect(calls[0]).toEqual(['set', { key: 'tools.media.image', value: { model: 'openai/gpt-4o', quality: '' } }])
  })
})
