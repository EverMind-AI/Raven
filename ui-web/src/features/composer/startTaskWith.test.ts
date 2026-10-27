// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as page from '../../state/page'
import * as settings from '../../state/settings'
import { askInChat, startTaskWith } from './startTaskWith'

const PAGES = ['extAgentsPage', 'connectionsPage', 'knowledgePage']

/* The page as the two verbs reach it: the field, the draft row's button, the
   shell mark and the sections page.show writes, and the drawer it closes. The
   dialog's close is stubbed -- what it does to the veil and the rail is
   state/settings's own case. */
beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  document.body.innerHTML =
    '<div class="app"></div><button id="newBtn"></button><textarea id="ta"></textarea>'
    + PAGES.map((id) => `<section class="page" id="${id}" data-open="false"><div class="work"></div></section>`).join('')
    + '<aside class="detail" id="detail" data-open="false"></aside>'
  vi.spyOn(settings, 'close').mockImplementation(() => {})
})

afterEach(() => {
  page._resetForTests()
  vi.restoreAllMocks()
})

const ta = (): HTMLTextAreaElement => document.getElementById('ta') as HTMLTextAreaElement

/* What the dock hears: it listens for `input` on the field (mount.tsx). */
const heard = (): string[] => {
  const seen: string[] = []
  ta().addEventListener('input', () => seen.push(ta().value))
  return seen
}

describe('askInChat', () => {
  it('closes the dialog, fills the field, tells the dock, and leaves the cursor after the text', () => {
    const seen = heard()
    askInChat('find me a skill')
    expect(settings.close).toHaveBeenCalledTimes(1)
    expect(ta().value).toBe('find me a skill')
    expect(seen).toEqual(['find me a skill'])
    expect(document.activeElement).toBe(ta())
    expect([ta().selectionStart, ta().selectionEnd]).toEqual([15, 15])
  })

  it('keeps what was typed, with the sentence after it', () => {
    ta().value = 'half a thought  \n'
    askInChat('find me a skill')
    expect(ta().value).toBe('half a thought\n\nfind me a skill')
    expect(ta().selectionStart).toBe(ta().value.length)
  })

  it('does not keep a draft that is only whitespace', () => {
    ta().value = '  \n'
    askInChat('x')
    expect(ta().value).toBe('x')
  })

  it('puts a module page away first, and asks nothing of the switch when none covers the chat', () => {
    page.show('extAgentsPage')
    askInChat('x')
    expect(page.get()).toBeNull()
    expect(document.getElementById('extAgentsPage')!.dataset.open).toBe('false')
    const show = vi.spyOn(page, 'show')
    askInChat('y')
    expect(show).not.toHaveBeenCalled()
  })
})

describe('startTaskWith', () => {
  it('opens a fresh conversation and fills the field with the translated prompt', () => {
    let clicks = 0
    document.getElementById('newBtn')!.addEventListener('click', () => { clicks += 1 })
    const seen = heard()
    startTaskWith('gui.agent.ask_connect', 'codex', { reason: 'boom' })
    expect(clicks).toBe(1)
    expect(seen).toEqual(['gui.agent.ask_connect {"reason":"boom","name":"codex"}'])
    expect(document.activeElement).toBe(ta())
    expect(settings.close).not.toHaveBeenCalled()
  })
})
