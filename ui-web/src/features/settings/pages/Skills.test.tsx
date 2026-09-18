// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as confirm from '../../../state/confirm'
import { resetSources } from '../../../state/sources'
import { install, mount } from '../harness'
import * as store from '../store'
import { stripFrontmatter } from './Skills'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

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
    expect(cards).toEqual(['gui.settings.skills.builtin · 1', 'gui.settings.skills.workspace · gui.settings.skills.on_of {"on":1,"total":2}'])
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
})
