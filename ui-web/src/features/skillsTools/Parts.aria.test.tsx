// @vitest-environment happy-dom
/* The card rows carry a switch, and a switch is an interactive control of its
 * own: nested inside the card's own role="button" the accessibility tree
 * flattens it, so the switch stops being a switch to a screen reader. These
 * cases hold the row's switch out of the card's button. */
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import { PAGES } from '../../state/pages'
import { resetSources, setSources } from '../../state/sources'
import { pkgList, snap } from '../../test/settingsHarness'
import { McpTab } from './McpTab'
import { PluginsTab } from './PluginsTab'
import { SkillsTab } from './SkillsTab'
import * as store from './store'
import { ToolsTab } from './ToolsTab'

import type { SettingsSnapshot, SkillsToolsSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../state/toast', () => ({
  show: () => {},
  subscribe: () => () => {},
  get: () => [],
}))

function sourceOf(data: SettingsSnapshot): SkillsToolsSource {
  return {
    load: async () => data,
    set: async () => data,
    everosSet: async () => data,
    addModel: async () => data,
    inspectSkill: async (name) => ({ name, description: 'desc', path: `/skills/${name}`, body: '# Hi', files: [], always: false, install: null }),
    openSkillFile: async () => {},
    uninstallSkill: async () => data,
    blockSkill: async () => data,
    serverDetail: async () => ({ known: true, fields: [], tools: [] }),
    toggleServer: async () => data,
    retryServer: async () => data,
    revokeServer: async () => data,
    configureServer: async () => data,
    authServer: async () => data,
    agentPlugins: async () => pkgList(),
    setPackageMcp: async () => pkgList(),
  }
}

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ skillsTools: sourceOf(snap()) })
  document.body.innerHTML =
    '<div class="app"></div>'
    + PAGES.map((p) => `<section class="page" id="${p.id}" data-open="false"><div class="work"><div class="wrap" id="${p.bodyId}"></div></section>`).join('')
    + '<aside class="detail" id="detail" data-open="false"><div class="dpanel"><header><b id="dTitle"></b></header><div class="body" id="dBody"></div></div></aside>'
  store.set({ snap: snap(), loaded: true, pkgs: pkgList() })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  detail._resetForTests()
  page._resetForTests()
  resetSources()
  document.body.innerHTML = ''
})

/* The switch of every row card. The Skill Hub banner carries a switch of its
   own and is not a card, so the row cards are the ones the class names
   name. */
function rowSwitches(container: HTMLElement): Element[] {
  const cards = [...container.querySelectorAll('.skillsTools-tray-card, .skillsTools-cfg-card')]
  expect(cards.length).toBeGreaterThan(0)
  return cards.flatMap((c) => [...c.querySelectorAll('[role="switch"]')])
}

describe('the row switches', () => {
  it('are not inside the card button', () => {
    const { container } = render(createElement(SkillsTab))
    const switches = rowSwitches(container)
    expect(switches.length).toBeGreaterThan(0)
    /* A role="button" flattens its descendants, so a switch inside one is no
       longer a switch to a screen reader. */
    for (const sw of switches) expect(sw.closest('[role="button"]')).toBeNull()
  })

  it('are the tab row switches, not the card', () => {
    const { container } = render(createElement(McpTab))
    for (const sw of rowSwitches(container)) expect(sw.closest('[role="button"]')).toBeNull()
  })

  it('are the tool row switches, not the card', () => {
    const { container } = render(createElement(ToolsTab))
    for (const sw of rowSwitches(container)) expect(sw.closest('[role="button"]')).toBeNull()
  })

  it('are the plugin row switches, not the card', () => {
    const { container } = render(createElement(PluginsTab))
    for (const sw of rowSwitches(container)) expect(sw.closest('[role="button"]')).toBeNull()
  })

  it('leave the card without a button role', () => {
    const { container } = render(createElement(SkillsTab))
    for (const card of container.querySelectorAll('.skillsTools-tray-card')) {
      expect(card.getAttribute('role')).not.toBe('button')
    }
  })

  it('do not open the card behind them', async () => {
    const { container } = render(createElement(SkillsTab))
    const sw = rowSwitches(container)[0]!

    await act(async () => { fireEvent.click(sw) })
    expect(store.get().modal).toBeNull()
  })

  it('open the card by its own control', async () => {
    const { container } = render(createElement(SkillsTab))
    const opener = container.querySelector('.skillsTools-tray-card .skillsTools-cname')!
    expect(opener.tagName).toBe('BUTTON')

    await act(async () => { fireEvent.click(opener) })
    expect(store.get().modal?.kind).toBe('skill')
  })

  it('open the card once when the name is clicked', async () => {
    const opened = vi.spyOn(store, 'openSkill')
    const { container } = render(createElement(SkillsTab))
    const opener = container.querySelector('.skillsTools-tray-card .skillsTools-cname')!

    await act(async () => { fireEvent.click(opener) })
    /* The name is inside the card, and the card opens on a click too: the
       name must not open it twice. */
    expect(opened).toHaveBeenCalledTimes(1)
  })

  it('open the card from the card itself', async () => {
    const { container } = render(createElement(SkillsTab))
    const card = container.querySelector('.skillsTools-tray-card')!

    await act(async () => { fireEvent.click(card) })
    expect(store.get().modal?.kind).toBe('skill')
  })
})
