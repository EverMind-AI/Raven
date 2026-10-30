// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as confirm from '../../../state/confirm'
import * as settingsDialog from '../../../state/settings'
import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import { askInChat } from '../../composer/startTaskWith'
import * as store from '../store'
import { blocklist, stripFrontmatter } from './Skills'

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

describe('skills page', () => {
  it('splits built-in from workspace, counts the switched-on ones, and filters as you type', async () => {
    install()
    await mount('skills')
    const cards = [...document.querySelectorAll('.settings-card .settings-t')].map((el) => el.textContent)
    expect(cards).toEqual([
      'gui.settings.skills.hub_card',
      'gui.settings.skills.builtin · 1',
      'gui.settings.skills.workspace · gui.settings.skills.on_of {"on":1,"total":2}',
    ])
    await act(async () => { fireEvent.change(document.getElementById('skq')!, { target: { value: 'database' } }) })
    expect(screen.queryByText('git-flow')).toBeNull()
    expect(screen.getByText('sql-style')).toBeTruthy()
  })

  it('a switch writes the whole blocklist with the name added or removed', async () => {
    const { calls } = install()
    await mount('skills')
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'git-flow' })) })
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'sql-style' })) })
    expect(calls).toEqual([
      ['set', { key: 'skillForge.blocklist', value: ['sql-style', 'git-flow'] }],
      ['set', { key: 'skillForge.blocklist', value: [] }],
    ])
  })

  it('the Skill Hub switch writes the enabled flag and starts off without an endpoint', async () => {
    const { calls } = install()
    await mount('skills')
    const hub = screen.getByRole('switch', { name: 'gui.settings.skills.hub_title' })
    expect(hub.getAttribute('aria-checked')).toBe('false')
    await act(async () => { fireEvent.click(hub) })
    expect(calls).toEqual([['set', { key: 'skillForge.router.hub.enabled', value: true }]])
  })

  it('the Skill Hub switch draws the server answer, not the raw file', async () => {
    /* The raw file is no guide to what the runtime does: here it names an
       endpoint the server reads as off (a hand-written value the loader
       refuses), and below it names none while the loader nests a legacy router
       under skillForge and searches it. */
    const off = snap({ skillHub: { on: false } })
    off.raw.skillForge = { blocklist: [], router: { hub: { endpoint: 'https://hub.internal.test', enabled: ' true ' } } }
    install(off)
    await mount('skills')
    expect(screen.getByRole('switch', { name: 'gui.settings.skills.hub_title' }).getAttribute('aria-checked')).toBe('false')
    cleanup()
    store._resetForTests()

    const on = snap({ skillHub: { on: true } })
    on.raw.skillRouter = { hub: { endpoint: 'https://hub.internal.test' } }
    const { calls } = install(on)
    await mount('skills')
    const hub = screen.getByRole('switch', { name: 'gui.settings.skills.hub_title' })
    expect(hub.getAttribute('aria-checked')).toBe('true')
    /* Read as off, the switch would send true on every click and the page
       could never turn the source off. */
    await act(async () => { fireEvent.click(hub) })
    expect(calls).toEqual([['set', { key: 'skillForge.router.hub.enabled', value: false }]])
  })

  it('reads the blocklist from a snake_case skill_forge block', async () => {
    /* A null camel block is dropped by the loader, so the snake one is what
       runs; the page has to read the first non-null spelling, not truthiness. */
    expect(blocklist({ skillForge: null, skill_forge: { blocklist: ['git-flow'] } })).toEqual(['git-flow'])
    const data = snap()
    delete data.raw.skillForge
    data.raw.skill_forge = { blocklist: ['git-flow', 'notes'] }
    expect(blocklist(data.raw)).toEqual(['git-flow', 'notes'])
    const { calls } = install(data)
    await mount('skills')
    const cards = [...document.querySelectorAll('.settings-card .settings-t')].map((el) => el.textContent)
    expect(cards).toEqual([
      'gui.settings.skills.hub_card',
      'gui.settings.skills.builtin · gui.settings.skills.on_of {"on":0,"total":1}',
      'gui.settings.skills.workspace · gui.settings.skills.on_of {"on":1,"total":2}',
    ])
    expect(screen.getByRole('switch', { name: 'git-flow' }).getAttribute('aria-checked')).toBe('false')
    /* The write carries the list the page read, not [] beside the real one. */
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'git-flow' })) })
    expect(calls).toEqual([['set', { key: 'skillForge.blocklist', value: ['notes'] }]])
  })

  it('the detail inspects the skill, renders its body, lists its files and opens one on the host', async () => {
    const { calls } = install()
    await mount('skills')
    await act(async () => { fireEvent.click(screen.getByText('git-flow')) })
    expect(calls[0]).toEqual(['inspectSkill', 'git-flow'])
    expect(document.querySelector('.settings-md')!.textContent).toContain('Hi')
    expect(stripFrontmatter('---\nname: x\n---\n\n# Body\n')).toBe('\n# Body\n')
    expect(stripFrontmatter('# No header\n')).toBe('# No header\n')
    expect(screen.getByText('notes.md')).toBeTruthy()
    expect(screen.getByText('gui.settings.skills.builtin')).toBeTruthy()
    expect(screen.queryByText('gui.settings.skills.uninstall')).toBeNull()
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.skills.open_file {"file":"notes.md"}')) })
    expect(calls[1]).toEqual(['openSkillFile', { name: 'git-flow', file: 'notes.md' }])
  })

  it('a hub skill shows its version, install line and uninstall, which asks first and then removes', async () => {
    const { calls } = install(undefined, {
      inspectSkill: async (name) => ({ name, description: 'd', path: `/w/skills/${name}/SKILL.md`, body: '', files: ['SKILL.md'], always: true, hub: true,
        install: { installed_at: '2026-08-21T08:38:00Z', version: 'v2', trigger: 'use_skill', source: 'skillhub', score_safety: 0.7 } }),
    })
    await mount('skills')
    await act(async () => { fireEvent.click(screen.getByText('sql-style')) })
    expect(screen.getByText('v2')).toBeTruthy()
    expect(screen.getByText('gui.settings.skills.always')).toBeTruthy()
    expect(document.querySelector('.settings-skmeta')!.textContent).toContain('gui.settings.skills.trig_use · gui.settings.skills.safety {"n":"0.7"}')
    vi.spyOn(confirm, 'ask').mockImplementation((_t, body, _l, fn) => { calls.push(['ask', body]); fn() })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.skills.uninstall')) })
    expect(calls.slice(-2)).toEqual([
      ['ask', 'gui.settings.skills.uninstall_body {"path":"/w/skills/sql-style"}'],
      ['uninstallSkill', 'sql-style'],
    ])
    expect(store.get().skill).toBeNull()
  })

  /* Installing is not this page's: use_skill fetches a Skill Hub bundle on
     Raven's own judgment. The button leads into the chat with the request
     half written, through the composer verb the app lends the dialog at boot
     -- lent here as the real one, so the case sees the dialog close, the
     field filled and focused, and nothing asked of the gateway. */
  it('the add button closes the dialog and puts the request in the composer, asking the gateway nothing', async () => {
    const { calls } = install()
    await mount('skills')
    document.body.insertAdjacentHTML('beforeend', '<textarea id="ta"></textarea>')
    const ta = document.getElementById('ta') as HTMLTextAreaElement
    const heard: string[] = []
    ta.addEventListener('input', () => heard.push(ta.value))
    store.lendAskInChat(askInChat)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.skills.add')) })
    expect(settingsDialog.close).toHaveBeenCalledTimes(1)
    expect(ta.value).toBe('gui.settings.skills.add_prompt\n')
    expect(heard).toEqual(['gui.settings.skills.add_prompt\n'])
    expect(document.activeElement).toBe(ta)
    expect(calls).toEqual([])
  })

  it('unlent, the add button does nothing', async () => {
    const { calls } = install()
    await mount('skills')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.skills.add')) })
    expect(settingsDialog.close).not.toHaveBeenCalled()
    expect(calls).toEqual([])
  })
})
