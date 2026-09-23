// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as settingsDialog from '../../state/settings'
import { resetSources, setSources } from '../../state/sources'
import { install, modelSource, mount, settle, snap, source as settingsSource } from '../../test/settingsHarness'
import { SettingsApp } from './SettingsApp'
import * as store from './store'

import type { SectionId } from './store'
import type { McpDetail, SettingsSnapshot } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

/* The dialog on a snapshot that never lands, which is the whole of what the
   reader sees for the seconds `model.options` spends. */
async function waiting(tab: SectionId): Promise<void> {
  install(snap(), { load: () => new Promise<SettingsSnapshot>(() => {}) })
  settingsDialog.settingsTab.id = tab
  render(createElement(SettingsApp), { container: document.getElementById('spanels')! })
  void store.open()
  await settle()
}

const count = (sel: string): number => document.querySelectorAll(sel).length

/* What a section's wait is made of, as the four things it can be made of. Two
   sections drawing the same numbers means one of them has no shape of its own
   and fell through to the default. */
const shape = (): string =>
  [count('.settings-card'), count('.settings-row'), count('.settings-xrow'), count('.settings-wbar')].join('/')

const SECTIONS: SectionId[] = ['general', 'usage', 'provider', 'model', 'skills', 'tools', 'plugins', 'archive', 'about']

describe('the wait a settings section draws', () => {
  it('announces itself once, with the word the line it replaces used to say', async () => {
    await waiting('general')
    const waits = document.querySelectorAll('[role="status"]')
    /* One per wait: a nested second region would have a screen reader read the
       same wait twice. */
    expect(waits.length).toBe(1)
    expect(waits[0]!.getAttribute('aria-label')).toBe('gui.settings.loading')
    expect(waits[0]!.getAttribute('aria-busy')).toBe('true')
  })

  it('draws the section rather than a box with a word in it', async () => {
    await waiting('general')
    /* The box this replaces, whose height was a line of text where the page is
       three settings, the middle one a row of theme cards. */
    expect(document.querySelector('.settings-soonbox')).toBeNull()
    expect(count('.settings-gen')).toBe(3)
    expect(count('.settings-theme')).toBe(3)
    expect(count('.settings-wbar')).toBe(14)
  })

  it('gives every section a shape of its own, so none falls through to the default', async () => {
    const seen = new Map<string, SectionId>()
    for (const id of SECTIONS) {
      await waiting(id)
      const sig = shape()
      expect(seen.has(sig), `${id} draws the same wait as ${seen.get(sig)}`).toBe(false)
      seen.set(sig, id)
      expect(count('[role="status"]')).toBe(1)
      cleanup()
      store._resetForTests()
    }
  })

  it('waits on the usage page as the chart and tables it becomes', async () => {
    await waiting('usage')
    /* One bar per day rather than one wide bar: a chart's wait drawn flat
       reads as a progress bar. */
    expect(count('.settings-bars .settings-wbar')).toBe(30)
    expect(count('.settings-tl')).toBe(4)
    expect(count('.settings-wtr')).toBe(8)
  })

  it('waits on the catalogue as the two-column grid, not as a column of cards', async () => {
    await waiting('provider')
    /* The pane is a full-height grid: cards in its place would collapse it the
       moment the vendors landed. */
    const wait = document.querySelector('.settings-wait.settings-tp')
    expect(wait).toBeTruthy()
    expect(wait!.querySelector('.settings-tp-side')).toBeTruthy()
    expect(wait!.querySelector('.settings-tp-main')).toBeTruthy()
    expect(count('.settings-card')).toBe(0)
  })

  it('waits on the tool and plugin pages as their switch rows', async () => {
    await waiting('tools')
    expect(count('.settings-xrow')).toBe(22)
    cleanup()
    store._resetForTests()
    await waiting('plugins')
    expect(count('.settings-xrow')).toBe(6)
  })
})

describe('the waits a settings page owns', () => {
  it('draws the usage totals as bars until the counter answers, then the figures', async () => {
    let land: ((u: null) => void) | null = null
    install(undefined, { usage: () => new Promise((resolve) => { land = resolve }) })
    await mount('usage')
    /* The range picker is the reader's own state and stays live through the
       wait; what the counter answers is the part drawn as bars. */
    expect(screen.getByText('gui.settings.usage.range')).toBeTruthy()
    expect(count('.settings-bars .settings-wbar')).toBe(30)

    await act(async () => { land!(null); await Promise.resolve() })
    expect(count('.settings-wbar')).toBe(0)
    expect(screen.getByText('gui.settings.usage.unavailable')).toBeTruthy()
  })

  it('draws the archive list as rows until it is read, then the sessions', async () => {
    let land: ((rows: never[]) => void) | null = null
    install(undefined, { archived: () => new Promise((resolve) => { land = resolve }) })
    await mount('archive')
    /* Four rows in the card, at the height a row of sessions takes -- where a
       single grey word shrank the card to one line and grew it back. */
    expect(count('.settings-wait .settings-row')).toBe(4)

    await act(async () => { land!([]); await Promise.resolve() })
    expect(count('.settings-wbar')).toBe(0)
    expect(screen.getByText('gui.settings.archive.empty')).toBeTruthy()
  })

  it('draws a plugin\'s credential fields as a key row until the catalogue answers', async () => {
    let land: ((d: McpDetail) => void) | null = null
    install(undefined, { serverDetail: () => new Promise((resolve) => { land = resolve }) })
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('github')) })
    expect(count('.settings-wait .settings-row.settings-stack')).toBe(1)

    await act(async () => { land!({ known: true, fields: [], tools: [] }); await Promise.resolve() })
    expect(count('.settings-wbar')).toBe(0)
    expect(screen.getByText('gui.settings.plugins.no_credential')).toBeTruthy()
  })

  it('draws the vendor list as its rows while the vendor is asked, beside the line naming it', async () => {
    let land: ((r: { models: never[]; status: string }) => void) | null = null
    install(undefined, { fetchModels: () => new Promise((resolve) => { land = resolve }) })
    await mount('provider')
    await act(async () => { void store.sheetOpen('anthropic'); await Promise.resolve() })
    /* The line stays: it names the vendor being asked, which no shape can. */
    expect(screen.getByText('gui.settings.providers.fetching {"name":"Anthropic"}')).toBeTruthy()
    expect(count('.settings-apbody .settings-apm')).toBe(8)

    await act(async () => { land!({ models: [], status: 'ok' }); await Promise.resolve() })
    expect(count('.settings-wbar')).toBe(0)
  })

  it('draws a skill\'s page as its prose until inspect answers', async () => {
    let land: ((d: { body: string; description: string; name: string }) => void) | null = null
    install(undefined, { inspectSkill: () => new Promise((resolve) => { land = resolve }) })
    await mount('skills')
    await act(async () => { fireEvent.click(screen.getByText('git-flow')) })
    expect(document.querySelector('.settings-wait.settings-card')).toBeTruthy()
    expect(count('.settings-md .settings-wbar')).toBe(7)

    await act(async () => { land!({ name: 'git-flow', description: 'd', body: '# Hi' }); await Promise.resolve() })
    expect(count('.settings-wbar')).toBe(0)
  })
})
