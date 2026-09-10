// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { XaApp } from './XaPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { XaActArgs, XaRow, XaSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toastWriter = vi.hoisted(() => ({ items: [] as string[] }))
vi.mock('../../shell/toast', () => ({
  show: (text: string) => {
    toastWriter.items.push(text)
  },
}))

function row(over: Partial<XaRow> = {}): XaRow {
  return {
    name: 'claude_code',
    preset: 'claude_code',
    kind: 'cli',
    configured: true,
    enabled: true,
    probe_status: 'ready',
    probe_detail: '',
    has_api_key: false,
    description: 'Claude Code CLI',
    test_running: false,
    last_test_ok: null,
    last_test_at_ms: null,
    last_test_detail: '',
    upgrade_to: null,
    ...over,
  }
}

/* The island runs against the same two seams production wires: a fake
   shell on window.RavenShell (T returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on window.DS.xa. */
function install(rows: XaRow[], over: Partial<XaSource> = {}) {
  const acts: Array<[string, string, XaActArgs]> = []
  const loads: boolean[] = []
  const source: XaSource = {
    load: async (probe) => {
      loads.push(!!probe)
      return rows
    },
    act: async (op, r, args) => {
      acts.push([op, r.name, args || {}])
      /* Both real sources answer with the row as the write left it -- the rpc
         one by reloading the roster, the fixture one by mutating in place. A
         fake that only recorded the call left the row looking untouched, which
         is not a state the page can be in, and hid the second press behind the
         stage the first one should have changed. */
      if (op === 'toggle') r.enabled = !!(args as { enabled?: boolean } | undefined)?.enabled
      return rows
    },
    ...over,
  }
  const toasts: string[] = []
  toastWriter.items = toasts
  const confirms: string[] = []
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    /* Records as well as confirms: "no dialog stands between the reader and the
       switch" is a claim only a spy can carry. */
    confirmAsk: (title, _b, _l, fn) => {
      confirms.push(title)
      fn()
    },
    showPage: () => {},
    closeDetail: () => {
      const d = document.getElementById('detail')
      if (d) d.dataset.open = 'false'
    },
  }
  window.RavenShell = fakeShell
  window.DS = { xa: source }
  document.body.innerHTML =
    '<section id="xaPage"><div id="xaBody"></div></section>' +
    '<aside id="detail" data-open="false"><b id="dTitle">—</b><div id="dBody"></div></aside>' +
    '<div id="menu" data-open="false"></div>'
  return { source, acts, loads, toasts, confirms }
}

async function mount() {
  const view = render(<XaApp />, { container: document.getElementById('xaBody')! })
  await act(async () => {
    store.open()
  })
  return view
}

const rowsOf = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.surow')]
const rowNamed = (name: string): HTMLElement => rowsOf().find((r) => r.querySelector('.nm b')!.textContent === name)!
const groupOf = (name: string): string => rowNamed(name).closest('.sugrp')!.querySelector('.hd b')!.textContent!
const rowActs = (name: string): Array<string | null> =>
  [...rowNamed(name).querySelectorAll('.suact button')].map((b) => b.textContent)
const subOf = (name: string): string | null => rowNamed(name).querySelector('.sufacts')?.textContent ?? null
const dotOf = (name: string): string | null => rowNamed(name).querySelector('.nm .led')!.className
const cardName = (): string | null => document.querySelector('#dBody .pmdmeta .l1 b')!.textContent
const wayIn = (): string | null => document.querySelector('#dBody .pmdmeta .l2')!.textContent
const cardAct = (): Array<string | null> =>
  [...document.querySelectorAll('#dBody .pmdhead .dact button')].map((b) => b.textContent)
const capsOnCard = (): Array<string | null> =>
  [...document.querySelectorAll('#dBody .pmsec > .cap')].map((c) => c.textContent)
/* The "whether it works" section, found by its caption rather than by position:
   the card has three sections now and which of them are drawn depends on the
   row. */
const testSec = (): HTMLElement | null =>
  [...document.querySelectorAll<HTMLElement>('#dBody .pmsec')].find(
    (sec) => sec.querySelector('.cap')?.textContent === 'gui.agent.sec_test',
  ) ?? null
const testBtns = (): Array<string | null> => [...(testSec()?.querySelectorAll('.sutest button') ?? [])].map((b) => b.textContent)
const testVerdict = (): string | null => testSec()?.querySelector('.vd')?.textContent ?? null
const testDot = (): string | null => testSec()?.querySelector('.led')?.className ?? null
const testProse = (): Array<string | null> => [...(testSec()?.querySelectorAll('.pmdesc') ?? [])].map((n) => n.textContent)

/* The catalogue group starts folded, so a test about a row inside it has to
   open it first -- the same click the reader makes. */
const expandGroup = async (label: string): Promise<void> => {
  const grp = [...document.querySelectorAll('.sugrp')].find((g) => g.querySelector('.hd b')!.textContent === label)
  await click(grp!.querySelector('.hd .gfold'))
}

/* Opening the card is clicking the row: the row is the door, and there is no
   second Configure button beside it. */
async function openCard(name: string): Promise<void> {
  await act(async () => {
    rowNamed(name).click()
  })
}

/* Enter on a control, the way a keyboard reaches it: keydown first (which is
   what the row listens for), then the activation the browser derives from it.
   `preventDefault` on the keydown is exactly what suppresses that activation,
   so the order matters and a plain `.click()` would not see the bug. */
const pressEnter = async (el: Element | null | undefined): Promise<void> => {
  expect(el, 'the control under test').toBeTruthy()
  await act(async () => {
    const target = el as HTMLElement
    const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    const prevented = !target.dispatchEvent(ev)
    if (!prevented) target.click()
  })
}

/* React installs its own `value` setter on the element, so assigning `.value`
   and firing `input` leaves its state untouched and the field reverts on the
   next render -- a test that did that watched its own typing disappear. The
   native setter is what React's onChange reads back. */
const typeInto = async (el: Element | null | undefined, text: string): Promise<void> => {
  expect(el, 'the field under test').toBeTruthy()
  const field = el as HTMLInputElement | HTMLTextAreaElement
  const proto = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')!.set!
  await act(async () => {
    setter.call(field, text)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

/* React delegates `onBlur` from the root and listens for `focusout`, not for
   the non-bubbling `blur` -- dispatching the latter reached nothing, and a case
   asserting "no write happened" passed for that reason rather than the one it
   was written for. */
const blur = async (el: Element | null | undefined): Promise<void> => {
  expect(el, 'the field under test').toBeTruthy()
  await act(async () => {
    ;(el as HTMLElement).dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
}

const click = async (el: Element | null | undefined): Promise<void> => {
  expect(el, 'the control under test').toBeTruthy()
  await act(async () => {
    ;(el as HTMLElement).click()
  })
}

afterEach(() => {
  act(() => {
    store.sheetDismissed()
  })
  cleanup()
})

describe('xa island', () => {
  /* Four groups, one per answer to "what would it take to use this". Two of
     them replaced a single "available" that held the switch worth one click
     and the CLI this machine has never had, in one list. Provenance is still a
     tag and not a section: a broken agent used to sit three groups below a
     healthy built-in one. */
  it('splits the page by what it would take to use each row', async () => {
    install([
      row(),
      row({ name: 'off_one', configured: true, enabled: false }),
      row({ name: 'miro', kind: 'openai', configured: false, has_api_key: false }),
      row({ name: 'codex', configured: false, probe_status: 'missing', probe_detail: 'codex: command not found' }),
    ])
    await mount()
    expect(await screen.findByText('claude_code')).toBeTruthy()
    expect(groupOf('claude_code')).toBe('gui.agent.g_on')
    expect(groupOf('off_one')).toBe('gui.agent.g_switch')
    /* On the machine and missing only a credential: a question about Raven's
       config, not about the machine. */
    expect(groupOf('miro')).toBe('gui.agent.g_setup')
    expect([...document.querySelectorAll('.sugrp .hd b')].map((b) => b.textContent)).toEqual([
      'gui.agent.g_on',
      'gui.agent.g_switch',
      'gui.agent.g_setup',
      'gui.agent.g_install',
    ])
    /* And the last one is the machine's question, folded away until asked. */
    expect(rowsOf().map((r) => r.querySelector('.nm b')!.textContent)).not.toContain('codex')
    await expandGroup('gui.agent.g_install')
    expect(groupOf('codex')).toBe('gui.agent.g_install')
  })

  /* Every verb on the page is one of two, whatever the row is underneath.
     Install / enable / test / switch-to each named a step of the same errand and
     left the reader to sequence them. */
  it('offers connect or disconnect and nothing else, whatever the row is', async () => {
    install([
      row(),
      row({ name: 'raven', kind: 'builtin', builtin: true, configured: false }),
      row({ name: 'Raven-PPT', vendored: true, configured: false, enabled: false, probe_status: 'missing' }),
      row({ name: 'Raven-Code', vendored: true, configured: false, enabled: true }),
      row({ name: 'off_one', configured: true, enabled: false }),
      row({ name: 'codex', configured: false }),
      row({ name: 'miro', kind: 'openai', configured: false, has_api_key: false }),
    ])
    await mount()
    const said = rowsOf().flatMap((r) => [...r.querySelectorAll('.suact button')].map((b) => b.textContent))
    expect(new Set(said)).toEqual(new Set(['gui.agent.connect', 'gui.agent.disconnect']))
    expect(rowsOf().every((r) => r.querySelectorAll('.suact button').length <= 1)).toBe(true)
  })

  /* Most of what this ordering used to say is now said by which group the row
     is in. What is left is the one distinction the groups do not draw: an entry
     Raven writes on its own, before a credential the reader has to go and
     find. */
  it('orders a group by what connecting costs', async () => {
    install([
      row({ name: 'unbuilt', vendored: true, configured: false, enabled: false, probe_status: 'missing' }),
      row({ name: 'needs_key', kind: 'openai', configured: false, has_api_key: false }),
      row({ name: 'preset', configured: false }),
      row({ name: 'switched_off', configured: true, enabled: false }),
    ])
    await mount()
    const named = (label: string): Array<string | null> =>
      [
        ...[...document.querySelectorAll('.sugrp')]
          .find((g) => g.querySelector('.hd b')!.textContent === label)!
          .querySelectorAll('.nm b'),
      ].map((b) => b.textContent)
    expect(named('gui.agent.g_switch')).toEqual(['switched_off'])
    expect(named('gui.agent.g_setup')).toEqual(['preset', 'needs_key'])
    expect(named('gui.agent.g_install')).toEqual([])
    await expandGroup('gui.agent.g_install')
    expect(named('gui.agent.g_install')).toEqual(['unbuilt'])
  })

  it('prints no prose: no subtitle, no group hints, no descriptions on rows', async () => {
    install([row()])
    await mount()
    const page = document.getElementById('xaPage')!
    /* The page is titled with its own name, the same one the rail row carries.
       It was a sentence about what the page is for -- "other agents Raven can
       hand work to" -- which is a subtitle wearing the title's slot. */
    expect(page.querySelector('.pmhero h3')!.textContent).toBe('gui.page.agents')
    expect(page.querySelector('.pmhero p')).toBeNull()
    expect(page.querySelector('.sugrp > .hint')).toBeNull()
    expect(page.textContent).not.toContain('Claude Code CLI')
  })

  /* A heading with nothing under it is a heading about nothing. */
  it('leaves out a group that has no rows', async () => {
    install([row({ configured: false })])
    await mount()
    expect(await screen.findByText('claude_code')).toBeTruthy()
    expect([...document.querySelectorAll('#xaPage .sugrp .hd b')].map((b) => b.textContent)).toEqual([
      'gui.agent.g_setup',
    ])
    expect(document.querySelector('#xaPage .empty-note')).toBeNull()
  })

  /* The addable rows are still ordered by what connecting costs; what is gone is
     the caption that said so. Behaviour does not need narrating. */
  it('does not caption the ordering', async () => {
    install([row(), row({ name: 'codex', configured: false })])
    await mount()
    expect(document.querySelector('#xaPage .sugrp .hd .tool')).toBeNull()
    expect(document.querySelector('#xaPage .free')).toBeNull()
  })

  /* No re-check button: opening the page re-measures availability, which is
     what that button was for. The one control a heading may carry is its own
     visibility, so the claim is about action buttons, not about buttons --
     asserting the latter passed only while no group on screen could fold. */
  it('has no action button in any group heading', async () => {
    /* Three groups on screen, one of them the folding one, so this cannot pass
       by there being nothing that could have carried a button. */
    install([
      row(),
      row({ name: 'codex', configured: false }),
      row({ name: 'not_here', configured: false, probe_status: 'missing' }),
    ])
    await mount()
    expect(document.querySelectorAll('#xaPage .sugrp').length).toBe(3)
    expect([...document.querySelectorAll('#xaPage .sugrp .hd button')].map((b) => b.className)).toEqual(['gfold'])
    expect(document.querySelector('#xaPage')!.textContent).not.toContain('gui.agent.probe')
  })

  it('re-measures availability every time the page opens', async () => {
    const { loads } = install([row()])
    await mount()
    await act(async () => {
      store.open()
    })
    expect(loads).toEqual([true, true])
  })

  /* Health is the dot, not a sentence. The group heading says whether the agent
     is connected and the button says what to do about it; a third line
     repeating either in grey was the standing small print. */
  describe('the four groups', () => {
    /* The distinction the two-group page could not draw. Both of these are one
       write to Raven's config away from being connected -- but writing an entry
       for a command that is not on the machine connects nothing, and only the
       probe knows which is which. */
    it('separates a preset this machine has from one it has never had', async () => {
      install([
        row({ name: 'here', configured: false, probe_status: 'ready' }),
        row({ name: 'not_here', configured: false, probe_status: 'missing' }),
      ])
      await mount()
      expect(groupOf('here')).toBe('gui.agent.g_setup')
      await expandGroup('gui.agent.g_install')
      expect(groupOf('not_here')).toBe('gui.agent.g_install')
    })

    /* `attention` is "the binary answered and nothing has verified what it can
       do". That is a row worth connecting and then testing, not one worth
       filing under "you would have to install it". */
    it('keeps an unverified binary out of the install group', async () => {
      install([row({ name: 'unsure', configured: false, probe_status: 'attention' })])
      await mount()
      expect(groupOf('unsure')).toBe('gui.agent.g_setup')
    })

    it('puts a switched-off agent in its own group, not among the ones to set up', async () => {
      install([row({ name: 'off_one', configured: true, enabled: false })])
      await mount()
      expect(groupOf('off_one')).toBe('gui.agent.g_switch')
    })

    /* A configured agent keeps its entry after its binary is removed, so "off"
       is a state a missing executable can be in. The switch is not the answer
       there: `subagents.toggle`'s enable gate probes the agent and refuses the
       write when nothing answers, so "ready to enable" would promise the one
       click that cannot work. The probe decides before the stage does. */
    it('does not call a switched-off agent ready when its binary is gone', async () => {
      install([
        row({ name: 'gone_cli', kind: 'cli', configured: true, enabled: false, probe_status: 'missing' }),
        row({ name: 'gone_acp', kind: 'acp', configured: true, enabled: false, probe_status: 'missing' }),
        row({ name: 'here_off', kind: 'cli', configured: true, enabled: false, probe_status: 'ready' }),
      ])
      await mount()
      expect(groupOf('here_off')).toBe('gui.agent.g_switch')
      expect(rowsOf().map((r) => r.querySelector('.nm b')!.textContent)).toEqual(['here_off'])
      await expandGroup('gui.agent.g_install')
      expect(groupOf('gone_cli')).toBe('gui.agent.g_install')
      expect(groupOf('gone_acp')).toBe('gui.agent.g_install')
    })

    /* An endpoint's probe says `missing` for "unreachable" -- a connection
       error, a timeout -- which is a network fact with nothing to install
       behind it, and the enable gate does not ping that kind. So the same probe
       word that files a command-backed row under installing leaves an openai
       row where its config state puts it: the switch when it is keyed and off,
       the credential when it has none. */
    it('keeps an unreachable openai endpoint out of the install group', async () => {
      install([
        row({ name: 'far_off', kind: 'openai', configured: true, enabled: false, has_api_key: true, probe_status: 'missing' }),
        row({ name: 'far_keyless', kind: 'openai', configured: true, enabled: false, has_api_key: false, probe_status: 'missing' }),
        row({ name: 'gone_cli', kind: 'cli', configured: true, enabled: false, probe_status: 'missing' }),
      ])
      await mount()
      expect(groupOf('far_off')).toBe('gui.agent.g_switch')
      expect(groupOf('far_keyless')).toBe('gui.agent.g_setup')
      /* The command-backed row beside them still goes where it went, so this
         cannot pass by the install branch having been deleted outright. */
      expect(rowsOf().map((r) => r.querySelector('.nm b')!.textContent)).not.toContain('gone_cli')
      await expandGroup('gui.agent.g_install')
      expect(groupOf('gone_cli')).toBe('gui.agent.g_install')
    })

    it('counts a shipped folder by whether its venv was built, not by its origin', async () => {
      install([
        row({ name: 'built', vendored: true, configured: false, enabled: false, probe_status: 'ready' }),
        row({ name: 'unbuilt', vendored: true, configured: false, enabled: false, probe_status: 'missing' }),
      ])
      await mount()
      expect(groupOf('built')).toBe('gui.agent.g_switch')
      await expandGroup('gui.agent.g_install')
      expect(groupOf('unbuilt')).toBe('gui.agent.g_install')
    })

    it('leaves the built-in agent connected, where it always was', async () => {
      install([row({ name: 'raven', kind: 'builtin', builtin: true, configured: false })])
      await mount()
      expect(groupOf('raven')).toBe('gui.agent.g_on')
    })
  })

  describe('the catalogue folds', () => {
    const fold = (label: string): Element | null =>
      [...document.querySelectorAll('.sugrp')]
        .find((g) => g.querySelector('.hd b')!.textContent === label)
        ?.querySelector('.hd .gfold') ?? null

    it('starts shut and opens on the heading control', async () => {
      install([row({ name: 'not_here', configured: false, probe_status: 'missing' })])
      await mount()
      expect(fold('gui.agent.g_install')!.textContent).toBe('gui.grp.show')
      expect(fold('gui.agent.g_install')!.getAttribute('aria-expanded')).toBe('false')
      expect(rowsOf()).toEqual([])
      await click(fold('gui.agent.g_install'))
      expect(fold('gui.agent.g_install')!.textContent).toBe('gui.grp.hide')
      expect(rowsOf().map((r) => r.querySelector('.nm b')!.textContent)).toEqual(['not_here'])
      await click(fold('gui.agent.g_install'))
      expect(rowsOf()).toEqual([])
    })

    /* Shut, not empty: the heading still counts what is behind it, or a reader
       cannot tell a folded group from one that has nothing in it. */
    it('still says how many are behind it', async () => {
      install([
        row({ name: 'a', configured: false, probe_status: 'missing' }),
        row({ name: 'b', configured: false, probe_status: 'missing' }),
      ])
      await mount()
      const grp = [...document.querySelectorAll('.sugrp')].find(
        (g) => g.querySelector('.hd b')!.textContent === 'gui.agent.g_install',
      )!
      expect(grp.querySelector('.hd .n')!.textContent).toBe('2')
    })

    it('is the only group with a fold on it', async () => {
      install([
        row(),
        row({ name: 'off_one', configured: true, enabled: false }),
        row({ name: 'preset', configured: false }),
        row({ name: 'not_here', configured: false, probe_status: 'missing' }),
      ])
      await mount()
      expect(fold('gui.agent.g_on')).toBeNull()
      expect(fold('gui.agent.g_switch')).toBeNull()
      expect(fold('gui.agent.g_setup')).toBeNull()
      expect(fold('gui.agent.g_install')).not.toBeNull()
    })

    /* A build is minutes of downloads with nothing at the end to announce it,
       so the row carrying `building` is the one thing on this page a reader is
       watching. Folding the group it sits in hides the progress behind the
       click that started it. */
    it('does not fold while something inside it is installing', async () => {
      install([
        row({ name: 'Raven-PPT', vendored: true, configured: false, building: true, enabled: false }),
        row({ name: 'not_here', configured: false, probe_status: 'missing' }),
      ])
      await mount()
      expect(fold('gui.agent.g_install')).toBeNull()
      expect(rowsOf().map((r) => r.querySelector('.nm b')!.textContent)).toContain('Raven-PPT')
    })
  })

  /* The one row in the setup group whose connect is not the group's verb: a
     preset that changed transport is removed and added back, and the card asks
     first. */
  it('tags the row whose connect is a migration', async () => {
    install([
      row({ name: 'moved', configured: true, enabled: false, upgrade_to: 'acp' }),
      row({ name: 'ordinary', configured: false }),
    ])
    await mount()
    const tags = (name: string): Array<string | null> =>
      [...rowNamed(name).querySelectorAll('.nm .kd')].map((k) => k.textContent)
    expect(tags('moved')).toEqual(['gui.agent.kind_cli', 'gui.agent.tag_stale'])
    expect(tags('ordinary')).toEqual(['gui.agent.kind_cli'])
    expect(groupOf('moved')).toBe('gui.agent.g_setup')
  })

  describe('the row says three things: who, how, and what to do', () => {
    it('carries no second line at all', async () => {
      install([
        row(),
        row({ name: 'off_one', enabled: false }),
        row({ name: 'raven', kind: 'builtin', builtin: true, configured: false }),
        row({ name: 'broken', configured: false, probe_status: 'missing', probe_detail: 'codex: not found' }),
      ])
      await mount()
      /* `broken` is in the folded group, and the claim is about every row on
         the page, so it has to be on the page. */
      await expandGroup('gui.agent.g_install')
      expect(rowsOf().map((r) => r.querySelector('.sufacts'))).toEqual([null, null, null, null])
      expect(subOf('broken')).toBeNull()
      expect(document.querySelector('#xaPage')!.textContent).not.toContain('codex: not found')
    })

    it('still colours the dot from the probe verdict, for what is connected', async () => {
      install([
        row(),
        row({ name: 'ill', probe_status: 'missing', probe_detail: 'gone' }),
        row({ name: 'unsure', probe_status: 'attention' }),
      ])
      await mount()
      expect(dotOf('claude_code')).toBe('led')
      expect(dotOf('ill')).toBe('led bad')
      expect(dotOf('unsure')).toBe('led warn')
    })

    /* Not-dispatchable is what every row outside the connected group is, so a
       red dot there was an alarm about the group's own definition -- and the
       clay stripe it drags along made an unconnected agent look broken. */
    it('keeps the alarm colours out of every group but the connected one', async () => {
      install([
        row({ name: 'needs_key', kind: 'openai', configured: false, has_api_key: false }),
        row({ name: 'unbuilt', vendored: true, configured: false, enabled: false, probe_status: 'missing' }),
      ])
      await mount()
      await expandGroup('gui.agent.g_install')
      expect(dotOf('needs_key')).toBe('led off')
      expect(dotOf('unbuilt')).toBe('led off')
      expect([...document.querySelectorAll('#xaPage .surow.bad')]).toEqual([])
    })

    /* Where an agent came from changes nothing about using it, and both the row
       and the card are about using it. */
    it('never says whose install shipped it', async () => {
      install([row({ name: 'Raven-Code', vendored: true, configured: false, enabled: true })])
      await mount()
      expect([...rowNamed('Raven-Code').querySelectorAll('.kd')].map((k) => k.textContent)).toEqual([
        'gui.agent.kind_cli',
      ])
      await openCard('Raven-Code')
      expect(wayIn()).toBe('gui.agent.kind_cli')
    })
  })

  describe('what connect does for one row', () => {
    it('writes an entry from the preset for an agent already on the machine', async () => {
      const { acts } = install([row({ configured: false })])
      await mount()
      await click(rowNamed('claude_code').querySelector('.suact button'))
      expect(acts).toEqual([['connect', 'claude_code', {}]])
    })

    /* The shipped folders are the case the reader cannot sequence themselves:
       nothing is downloaded yet, and the row is not a preset that can be added.
       Connect is the installer, and it says what that costs before the click. */
    it('runs the installer for a shipped build whose venv was never made', async () => {
      const { acts } = install([
        row({ name: 'Raven-PPT', vendored: true, configured: false, enabled: false, probe_status: 'missing' }),
      ])
      await mount()
      await expandGroup('gui.agent.g_install')
      const button = rowNamed('Raven-PPT').querySelector('.suact button')!
      expect(button.getAttribute('title')).toBe('gui.agent.install_note')
      await click(button)
      expect(acts).toEqual([['build', 'Raven-PPT', {}]])
    })

    /* Built once, switched off later: the installer would run for minutes and
       change nothing, so the same button is the switch. The probe verdict is
       what separates the two. */
    it('switches a built shipped build back on rather than rebuilding it', async () => {
      const { acts } = install([
        row({ name: 'Raven-Code', vendored: true, configured: false, enabled: false, probe_status: 'ready' }),
      ])
      await mount()
      await click(rowNamed('Raven-Code').querySelector('.suact button'))
      expect(acts).toEqual([['toggle', 'Raven-Code', { enabled: true }]])
    })

    it('flips the switch for a configured agent that is switched off', async () => {
      const { acts } = install([row({ enabled: false })])
      await mount()
      await click(rowNamed('claude_code').querySelector('.suact button'))
      expect(acts).toEqual([['toggle', 'claude_code', { enabled: true }]])
    })

    /* The one connect this page cannot finish on its own: only the reader has
       the credential. So the row opens the card, where the field is, and writes
       nothing on the way. */
    it('opens the card for an http agent instead of writing a keyless entry', async () => {
      const { acts } = install([row({ name: 'miro', kind: 'openai', configured: false, has_api_key: false })])
      await mount()
      await click(rowNamed('miro').querySelector('.suact button'))
      expect(acts).toEqual([])
      expect(document.getElementById('detail')!.dataset.open).toBe('true')
      expect(cardName()).toBe('miro')
    })
  })

  describe('what disconnect does', () => {
    /* Only marks it unavailable in the registry: the entry, the folder and the
       sessions it already ran all stay, and connect puts it back. */
    it('is a switch, with no dialog in front of it', async () => {
      const { acts, confirms } = install([row()])
      await mount()
      await click(rowNamed('claude_code').querySelector('.suact button'))
      expect(acts).toEqual([['toggle', 'claude_code', { enabled: false }]])
      expect(confirms).toEqual([])
    })

    it('is the same switch for a shipped build, which has no entry of its own', async () => {
      const { acts } = install([row({ name: 'Raven-Code', vendored: true, configured: false, enabled: true })])
      await mount()
      await click(rowNamed('Raven-Code').querySelector('.suact button'))
      expect(acts).toEqual([['toggle', 'Raven-Code', { enabled: false }]])
    })
  })

  /* A preset that has moved to another transport cannot be applied by flipping
     `enabled`: there is no write that changes a transport, so it is a remove
     plus an add from the preset. Disconnect-then-connect left `upgrade_to`
     standing and the old command line in place -- an agent the card offered to
     migrate and never did. */
  describe('a stale transport', () => {
    const stale = () =>
      row({
        name: 'Raven-Research',
        kind: 'cli',
        configured: true,
        enabled: true,
        probe_status: 'ready',
        upgrade_to: 'acp',
        preset: 'raven-research',
      })

    it('connects a stale agent by migrating it, not by switching a flag', async () => {
      const { acts, confirms } = install([stale()])
      await mount()
      /* Out of service first, which is what a reader does when the card says the
         preset moved. */
      await click(rowNamed('Raven-Research').querySelector('.suact button'))
      expect(acts).toEqual([['toggle', 'Raven-Research', { enabled: false }]])

      await click(rowNamed('Raven-Research').querySelector('.suact button'))

      expect(acts[1]).toEqual(['migrate', 'Raven-Research', {}])
      /* It drops the handles of runs in flight, so this one asks. */
      expect(confirms).toEqual(['gui.agent.migrate_do'])
    })

    it('offers the same migration from the card', async () => {
      const { acts } = install([stale()])
      await mount()
      await openCard('Raven-Research')
      await click(document.querySelector('#dBody .dact button'))
      expect(acts).toEqual([['toggle', 'Raven-Research', { enabled: false }]])
      await click(document.querySelector('#dBody .dact button'))
      expect(acts[1]).toEqual(['migrate', 'Raven-Research', {}])
    })

    it('leaves an agent with no stale preset on the ordinary flag', async () => {
      const { acts, confirms } = install([
        row({ name: 'Raven-Code', kind: 'cli', configured: true, enabled: false, probe_status: 'ready' }),
      ])
      await mount()
      await click(rowNamed('Raven-Code').querySelector('.suact button'))
      expect(acts).toEqual([['toggle', 'Raven-Code', { enabled: true }]])
      expect(confirms).toEqual([])
    })
  })

  /* A row is a focusable button holding the real buttons, so Enter on one of
     those bubbles to the row -- whose handler called preventDefault and opened
     the card instead. Every row action on all three set-up pages was
     unreachable from the keyboard while the mouse worked. */
  it('lets the keyboard reach a row action instead of opening the row', async () => {
    const { acts } = install([
      row({ name: 'Raven-Code', kind: 'cli', configured: true, enabled: true, probe_status: 'ready' }),
    ])
    await mount()

    await pressEnter(rowNamed('Raven-Code').querySelector('.suact button'))

    expect(acts).toEqual([['toggle', 'Raven-Code', { enabled: false }]])
    expect(document.querySelector('#dBody .pmdhead')).toBeNull()
  })

  it('still opens the row from the keyboard when the row itself has focus', async () => {
    const { acts } = install([
      row({ name: 'Raven-Code', kind: 'cli', configured: true, enabled: true, probe_status: 'ready' }),
    ])
    await mount()

    await pressEnter(rowNamed('Raven-Code'))

    expect(document.querySelector('#dBody .pmdhead')).toBeTruthy()
    expect(acts).toEqual([])
  })

  it('leaves the built-in agent no verb at all', async () => {
    install([row({ name: 'raven', kind: 'builtin', builtin: true, configured: false })])
    await mount()
    /* Not a refusal in a dialog: an unnamed spawn and a dag node with no
       sub-agent both dispatch to it, so there is nothing to offer. */
    expect(rowActs('raven')).toEqual([])
    expect(groupOf('raven')).toBe('gui.agent.g_on')
    await openCard('raven')
    expect(cardAct()).toEqual([])
  })

  it('says an install is under way and refuses a second click', async () => {
    install([row({ name: 'Raven-PPT', vendored: true, configured: false, building: true, enabled: false })])
    await mount()
    const button = rowNamed('Raven-PPT').querySelector('.suact button')!
    expect(button.textContent).toBe('gui.agent.installing')
    expect(button.hasAttribute('disabled')).toBe(true)
    /* The button is what says it, and the dot stays neutral: the row is not
       connected, and there a colour would be an alarm about nothing. */
    expect(dotOf('Raven-PPT')).toBe('led off')
  })

  it('keeps the row action out of the door the row is', async () => {
    install([row()])
    await mount()
    await click(rowNamed('claude_code').querySelector('.suact button'))
    expect(document.getElementById('detail')!.dataset.open).toBe('false')
  })

  describe('the card', () => {
    /* The same anatomy the skill and plugin details use -- identity block, then
       described sections -- rather than one of its own. */
    it('is the module detail card, not a form', async () => {
      install([row()])
      await mount()
      await openCard('claude_code')
      const body = document.getElementById('dBody')!
      expect(body.querySelector('.pmdhead')).not.toBeNull()
      expect(cardName()).toBe('claude_code')
      expect(body.querySelector('.pmsec .cap')!.textContent).toBe('gui.plug.sec_about')
      expect(body.querySelector('.pmdesc')!.textContent).toBe('Claude Code CLI')
      /* What it is not any more: editable name and description fields, a second
         copy of the row's status line, a save button, a menu. (The verdict came
         back, in a section of its own -- see "the test in the card".) */
      expect(body.querySelector('.sufield')).toBeNull()
      expect(body.querySelector('textarea')).toBeNull()
      expect(body.querySelector('.sufoot')).toBeNull()
      expect(body.querySelector('.sustate')).toBeNull()
      expect(body.querySelector('.sumenu')).toBeNull()
    })

    it('states how Raven reaches the agent', async () => {
      install([row({ name: 'Raven-Code', kind: 'acp', vendored: true, configured: false, enabled: true })])
      await mount()
      await openCard('Raven-Code')
      expect(wayIn()).toBe('gui.agent.kind_acp')
    })

    /* The preset moved transport since this entry was written. Rewriting it
       silently would invalidate every session handle bound to it, so the card
       states it and disconnect-then-connect is what re-reads the preset. */
    it('says when the entry was written against an older transport', async () => {
      install([row({ upgrade_to: 'acp' })])
      await mount()
      await openCard('claude_code')
      expect(wayIn()).toBe('gui.agent.kind_cli · gui.agent.stale_to {"to":"gui.agent.kind_acp"}')
    })

    it('carries the same verb the row carries', async () => {
      install([row(), row({ name: 'codex', configured: false })])
      await mount()
      await openCard('claude_code')
      expect(cardAct()).toEqual(['gui.agent.disconnect'])
      await openCard('codex')
      expect(cardAct()).toEqual(['gui.agent.connect'])
    })

    it('omits the described section for an agent that describes itself nowhere', async () => {
      /* Still the rule for a row with nothing to write to. For one the reader
         owns, the empty section is where the first description gets typed --
         see "offers the about section even when there is no description". */
      install([row({ name: 'codex', configured: false, description: '' })])
      await mount()
      await openCard('codex')
      /* By caption, not by "no section at all": the card grew a second one
         (whether it works), and an assertion that counted sections would pass
         only for as long as this was the only thing on the card. */
      expect(capsOnCard()).not.toContain('gui.plug.sec_about')
    })

    /* The credential is the only thing the card takes, and only from the agents
       whose way in is one. The button lives beside the field rather than up in
       the head, where there is nothing to type into. */
    it('takes the key and connects with it, for an entry that does not exist yet', async () => {
      const { acts } = install([row({ name: 'miro', kind: 'openai', configured: false, has_api_key: false })])
      await mount()
      await openCard('miro')
      expect(cardAct()).toEqual([])
      const box = document.querySelector<HTMLInputElement>('#dBody .sukey input')!
      expect(box.type).toBe('password')
      box.value = '  sk-live  '
      await click(document.querySelector('#dBody .sukey button.key'))
      expect(acts).toEqual([['connect', 'miro', { api_key: 'sk-live' }]])
    })

    it('takes the key as an edit when the entry is already there', async () => {
      const { acts } = install([row({ name: 'miro', kind: 'openai', configured: true, has_api_key: false })])
      await mount()
      await openCard('miro')
      const box = document.querySelector<HTMLInputElement>('#dBody .sukey input')!
      box.value = 'sk-live'
      await click(document.querySelector('#dBody .sukey button.key'))
      expect(acts).toEqual([['update', 'miro', { api_key: 'sk-live' }]])
    })

    it('writes nothing for an empty key box', async () => {
      const { acts } = install([row({ name: 'miro', kind: 'openai', configured: false, has_api_key: false })])
      await mount()
      await openCard('miro')
      await click(document.querySelector('#dBody .sukey button.key'))
      expect(acts).toEqual([])
    })

    it('offers no key field to the agents that do not sign in with one', async () => {
      install([row()])
      await mount()
      await openCard('claude_code')
      expect(document.querySelector('#dBody .sukey')).toBeNull()
    })

    /* Legacy chrome owns the drawer's closers and they only flip #detail's
       data-open, so the island has to follow the flag. */
    /* Dropped after the drawer has finished fading, not in the tick the flag
       flipped: the panel takes a fifth of a second to leave, and unmounting the
       card at the start of that made an empty strip the thing that faded. */
    it('drops the card once the drawer has finished fading', async () => {
      vi.useFakeTimers()
      try {
        install([row()])
        await mount()
        await openCard('claude_code')
        await act(async () => {
          document.getElementById('detail')!.dataset.open = 'false'
        })
        expect(document.querySelector('#dBody .pmdhead')).toBeTruthy()
        await act(async () => {
          await vi.advanceTimersByTimeAsync(300)
        })
        expect(document.querySelector('#dBody .pmdhead')).toBeNull()
      } finally {
        vi.useRealTimers()
      }
    })

    /* The one the name could not tell apart: closing and pressing the SAME row
       inside the fade wrote the same string back, so the pending drop read it as
       its own card and cleared the one that had just opened. */
    it('keeps the card when the same row is reopened inside the fade', async () => {
      vi.useFakeTimers()
      try {
        install([row()])
        await mount()
        await openCard('claude_code')
        await act(async () => {
          document.getElementById('detail')!.dataset.open = 'false'
        })
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100)
        })
        await openCard('claude_code')
        await act(async () => {
          await vi.advanceTimersByTimeAsync(300)
        })
        expect(cardName()).toBe('claude_code')
      } finally {
        vi.useRealTimers()
      }
    })

    /* And a reader who opened another card inside that window keeps theirs. */
    it('keeps the card a second open replaced it with', async () => {
      vi.useFakeTimers()
      try {
        install([row(), row({ name: 'hermes' })])
        await mount()
        await openCard('claude_code')
        await act(async () => {
          document.getElementById('detail')!.dataset.open = 'false'
        })
        await openCard('hermes')
        await act(async () => {
          await vi.advanceTimersByTimeAsync(300)
        })
        expect(cardName()).toBe('hermes')
      } finally {
        vi.useRealTimers()
      }
    })

    it('repaints its own card after another page borrowed the drawer', async () => {
      install([row()])
      await mount()
      await openCard('claude_code')
      /* What a capability detail does to the shared body. */
      document.getElementById('dBody')!.innerHTML = '<div>someone else</div>'
      await openCard('claude_code')
      expect(cardName()).toBe('claude_code')
      expect(document.getElementById('dBody')!.textContent).not.toContain('someone else')
    })
  })

  /* Whether it works, which is a different question from whether it is
     installed -- and the one the page could never answer before. */
  describe('the test in the card', () => {
    it('offers it for an agent the server can look up', async () => {
      install([row()])
      await mount()
      await openCard('claude_code')
      expect(testBtns()).toEqual(['gui.agent.test_do'])
    })

    it('leaves it off the built-in agent, which is this process', async () => {
      /* `run_test` refuses one outright -- "a built-in agent runs in this
         process; there is nothing to test" -- and records nothing, so the
         button could only ever put a failure on a row that is working. */
      install([row({ name: 'raven', kind: 'builtin', builtin: true, configured: false })])
      await mount()
      await openCard('raven')
      expect(testSec()).toBeNull()
    })

    it('leaves it off a discovered folder, which neither lookup can reach', async () => {
      /* `subagents.test` takes `config` or `preset`, and a vendored row is in
         neither: no config entry to find, and never in the preset table. Both
         answer `subagent_not_found`. */
      install([row({ name: 'Raven-Research', preset: undefined, configured: false, vendored: true })])
      await mount()
      await openCard('Raven-Research')
      expect(testSec()).toBeNull()
    })

    it('keeps it out of the row, which still carries two verbs and no third', async () => {
      install([row()])
      await mount()
      expect(rowActs('claude_code')).toEqual(['gui.agent.disconnect'])
    })

    it('reports the last verdict and how long ago it was reached', async () => {
      install([row({ last_test_ok: true, last_test_at_ms: Date.now() - 3 * 3600e3 })])
      await mount()
      await openCard('claude_code')
      expect(testVerdict()).toContain('gui.agent.test_ok')
      /* Through the catalogue, not as a bare "3h": the age is the half of this
         sentence that would otherwise stay English beside a Chinese one. */
      expect(testVerdict()).toContain('gui.time.ago_h')
      expect(testDot()).toBe('led')
    })

    it('counts the age in minutes while it is fresh, and in days once it is old', async () => {
      /* Under a minute is not "0m": a test that has just answered should read
         as having just answered. */
      install([row({ last_test_ok: true, last_test_at_ms: Date.now() - 20e3 })])
      await mount()
      await openCard('claude_code')
      expect(testVerdict()).toContain('gui.time.ago_now')
      cleanup()

      install([row({ last_test_ok: true, last_test_at_ms: Date.now() - 4 * 60e3 })])
      await mount()
      await openCard('claude_code')
      expect(testVerdict()).toContain('gui.time.ago_m')
      cleanup()

      install([row({ last_test_ok: true, last_test_at_ms: Date.now() - 50 * 3600e3 })])
      await mount()
      await openCard('claude_code')
      expect(testVerdict()).toContain('gui.time.ago_d')
    })

    it('says a failure failed, and puts the reason under it', async () => {
      install([
        row({ last_test_ok: false, last_test_at_ms: Date.now() - 60e3, last_test_detail: 'exited 1: not logged in' }),
      ])
      await mount()
      await openCard('claude_code')
      expect(testVerdict()).toContain('gui.agent.test_bad')
      expect(testDot()).toBe('led bad')
      expect(testProse()).toContain('exited 1: not logged in')
    })

    it('keeps the detail off a pass, where it only repeats the verdict', async () => {
      /* A passing cli test's detail is "the agent ran and replied", which the
         line above it has already said in fewer words. */
      install([row({ last_test_ok: true, last_test_at_ms: Date.now(), last_test_detail: 'the agent ran and replied' })])
      await mount()
      await openCard('claude_code')
      expect(testProse()).not.toContain('the agent ran and replied')
    })

    it('says nothing was measured when nothing was', async () => {
      install([row()])
      await mount()
      await openCard('claude_code')
      expect(testVerdict()).toBe('gui.agent.test_never')
      expect(testDot()).toBe('led off')
    })

    it('reads a verdict with no time on it as untested', async () => {
      /* The two fields come off one record, so this is a row from a server
         that no longer exists -- and printing "Worked, {ago}" with the
         placeholder still in it is worse than saying nothing was measured. */
      install([row({ last_test_ok: true, last_test_at_ms: null })])
      await mount()
      await openCard('claude_code')
      expect(testVerdict()).toBe('gui.agent.test_never')
    })

    it('warns about the quota for the test that spends one', async () => {
      install([row({ kind: 'cli' })])
      await mount()
      await openCard('claude_code')
      expect(testProse()).toContain('gui.agent.test_note')
    })

    it('does not warn about a bill an acp handshake never sends', async () => {
      /* acp reaches its verdict in the handshake and openai in the free
         `/models` probe. Warning about a cost neither of them has is how a
         reader learns to ignore the warning on the one that does. */
      install([row({ kind: 'acp' })])
      await mount()
      await openCard('claude_code')
      expect(testProse()).not.toContain('gui.agent.test_note')
      cleanup()

      install([row({ kind: 'openai', has_api_key: true })])
      await mount()
      await openCard('claude_code')
      expect(testProse()).not.toContain('gui.agent.test_note')
    })

    it('sends the test through the source', async () => {
      const { acts } = install([row()])
      await mount()
      await openCard('claude_code')
      await click(testSec()!.querySelector('.sutest button'))
      expect(acts).toEqual([['test', 'claude_code', {}]])
    })

    /* The call IS the test: `subagents.test` holds the connection open for the
       whole run and answers with the verdict, so the rows it repaints from are
       the first news of the test being over -- and nothing before them says it
       began. A card that read only the row flag sat on "Run a test" for the
       two minutes of the test it had itself started. */
    it('says it is running from the click, not from the answer', async () => {
      let settle = (): void => {}
      const { acts } = install([row()], {
        act: async (op, r, args) => {
          acts.push([op, r.name, args || {}])
          if (op === 'test') await new Promise<void>((res) => (settle = res))
          return [row()]
        },
      })
      await mount()
      await openCard('claude_code')
      await click(testSec()!.querySelector('.sutest button'))
      expect(testBtns()).toEqual(['gui.agent.test_stop', 'gui.agent.test_running'])
      await act(async () => {
        settle()
      })
      expect(testBtns()).toEqual(['gui.agent.test_do'])
    })

    /* Against the store, not the button: the button is disabled while a test
       runs, so a click cannot reach this. The guard is for the caller -- the
       server refuses a second test for a name, and its refusal arrives as an
       ordinary result, which would repaint the card as though the running test
       had answered. */
    it('drops a second call rather than sending one the server would refuse', async () => {
      const acts: Array<[string, string, XaActArgs]> = []
      let settle = (): void => {}
      const only = row()
      install([only], {
        act: async (op, r, args) => {
          acts.push([op, r.name, args || {}])
          if (op === 'test') await new Promise<void>((res) => (settle = res))
          return [only]
        },
      })
      await mount()
      await act(async () => {
        void store.runTest(only)
        void store.runTest(only)
      })
      await act(async () => {
        settle()
      })
      expect(acts.filter(([op]) => op === 'test')).toHaveLength(1)
    })

    it('offers stop only while one is running, and cancels through it', async () => {
      let settle = (): void => {}
      const acts: Array<[string, string, XaActArgs]> = []
      install([row()], {
        act: async (op, r, args) => {
          acts.push([op, r.name, args || {}])
          if (op === 'test') await new Promise<void>((res) => (settle = res))
          return [row()]
        },
      })
      await mount()
      await openCard('claude_code')
      expect(testBtns()).toEqual(['gui.agent.test_do'])
      await click(testSec()!.querySelector('.sutest button'))
      await click(testSec()!.querySelector('.sutest button'))
      expect(acts.map(([op]) => op)).toEqual(['test', 'test_cancel'])
      await act(async () => {
        settle()
      })
    })

    it('stops saying it is running when the call fails', async () => {
      /* The rejected call, which is the path the reader most wants to press
         again from: `run` turns it into a toast, and the flag has to come off
         all the same or the button stays disabled for the rest of the session.
         (This drives `run`'s catch, not `runTest`'s `finally` -- nothing
         throws through that today; see the comment there.) */
      const { toasts } = install([row()], {
        act: async () => {
          throw { data: { detail: 'the gateway went away' } }
        },
      })
      await mount()
      await openCard('claude_code')
      await click(testSec()!.querySelector('.sutest button'))
      expect(testBtns()).toEqual(['gui.agent.test_do'])
      expect(toasts[0]).toContain('the gateway went away')
    })

    /* The name is the only handle either side has on a running test: the
       server keys `_RUNNING` by it, and the verdict store records against it.
       A rename mid-run leaves the run alive with nothing pointing at it. */
    it('does not let the name move while a test is in flight', async () => {
      const only = row()
      let settle = (): void => {}
      install([only], {
        act: async (op) => {
          if (op === 'test') await new Promise<void>((res) => (settle = res))
          return [only]
        },
      })
      await mount()
      await openCard('claude_code')
      expect(document.querySelector('#dBody .pmdmeta .xaedit')).not.toBeNull()
      await click(testSec()!.querySelector('.sutest button:last-of-type'))
      expect(document.querySelector('#dBody .pmdmeta .xaedit')).toBeNull()
      expect(cardName()).toBe('claude_code')
      /* Positive half: only the name is held. The description does not key
         anything, so taking it away would be a restriction with no reason. */
      expect(document.querySelector('#dBody .pmdesc .xaedit')).not.toBeNull()
      await act(async () => {
        settle()
      })
      expect(document.querySelector('#dBody .pmdmeta .xaedit')).not.toBeNull()
    })

    it('shows a test another client started, which is only on the row', async () => {
      install([row({ test_running: true })])
      await mount()
      await openCard('claude_code')
      expect(testBtns()).toEqual(['gui.agent.test_stop', 'gui.agent.test_running'])
    })
  })

  describe('when a write fails', () => {
    it('toasts the reason the server gave, not the error code name', async () => {
      const { toasts } = install([row({ configured: false })], {
        act: async () => {
          throw { message: 'subagent_not_found', data: { detail: 'no preset named that' } }
        },
      })
      await mount()
      await click(rowNamed('claude_code').querySelector('.suact button'))
      expect(toasts).toEqual(['gui.agent.failed {"detail":"no preset named that"}'])
    })

    it('falls back to the code name when the frame carries no reason', async () => {
      const { toasts } = install([row({ configured: false })], {
        act: async () => {
          throw { message: 'subagent_not_found' }
        },
      })
      await mount()
      await click(rowNamed('claude_code').querySelector('.suact button'))
      expect(toasts).toEqual(['gui.agent.failed {"detail":"subagent_not_found"}'])
    })

    it('reads the reason when the load that opens the page is the thing that fails', async () => {
      const { toasts } = install([], {
        load: async () => {
          throw { data: { detail: 'gateway said no' } }
        },
      })
      await mount()
      expect(toasts).toEqual(['gui.agent.failed {"detail":"gateway said no"}'])
    })
  })
  /* Editing the two facts on the card the reader owns. `subagents.update` has
     always taken both and the store has always moved the open sheet onto a new
     name; the page was the only piece missing, and it went out with the form
     the card replaced (!374, which named three other removals and not this
     one). The terminal kept its own. */
  describe('the name and the description are the reader\'s', () => {
    const nameBox = (): HTMLElement | null =>
      document.querySelector('#dBody .pmdhead .l1 .xaedit')
    const aboutBox = (): HTMLElement | null => document.querySelector('#dBody .pmdesc .xaedit')

    it('renames on Enter, through update and not connect', async () => {
      const { acts } = install([row({ name: 'Coder', configured: true })])
      await mount()
      await openCard('Coder')

      await click(nameBox())
      const field = document.querySelector<HTMLInputElement>('#dBody .pmdhead .l1 input')!
      await typeInto(field, 'My Coder')
      await act(async () => {
        field.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
      })

      expect(acts).toEqual([['update', 'Coder', { new_name: 'My Coder' }]])
    })

    it('restores on Escape and writes nothing', async () => {
      /* Escape is the way out. Without it the only exit from a field opened by
         mistake would be to type the old value back. */
      const { acts } = install([row({ name: 'Coder' })])
      await mount()
      await openCard('Coder')

      await click(nameBox())
      const field = document.querySelector<HTMLInputElement>('#dBody .pmdhead .l1 input')!
      await typeInto(field, 'Nope')
      await act(async () => {
        field.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
      })

      expect(acts).toEqual([])
      expect(nameBox()!.textContent).toBe('Coder')
    })

    it('writes nothing when the text was not changed', async () => {
      /* Opening a field and clicking away is not an edit, and a write there
         would move the sheet and reload the roster for nothing. */
      const { acts } = install([row({ name: 'Coder' })])
      await mount()
      await openCard('Coder')

      await click(nameBox())
      await blur(document.querySelector<HTMLInputElement>('#dBody .pmdhead .l1 input'))

      expect(acts).toEqual([])
      /* And the field really closed, so the blur reached the commit and the
         commit is what decided not to write. */
      expect(document.querySelector('#dBody .pmdhead .l1 input')).toBeNull()
    })

    it('keeps an emptied name, because a nameless agent is unaddressable', async () => {
      const { acts } = install([row({ name: 'Coder' })])
      await mount()
      await openCard('Coder')

      await click(nameBox())
      const field = document.querySelector<HTMLInputElement>('#dBody .pmdhead .l1 input')!
      await typeInto(field, '   ')
      await act(async () => {
        field.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
      })

      expect(acts).toEqual([])
    })

    it('edits the description on blur, since Enter is a newline there', async () => {
      const { acts } = install([row({ name: 'Coder', description: 'old' })])
      await mount()
      await openCard('Coder')

      await click(aboutBox())
      const field = document.querySelector<HTMLTextAreaElement>('#dBody .pmdesc textarea')!
      await typeInto(field, 'writes code\nand tests')
      await blur(field)

      expect(acts).toEqual([['update', 'Coder', { description: 'writes code\nand tests' }]])
    })

    it('offers the about section even when there is no description to read', async () => {
      /* The empty one is where the first description gets written. Without it
         the only agents that could be described are the ones already were. */
      install([row({ name: 'Coder', description: '' })])
      await mount()
      await openCard('Coder')

      expect(aboutBox()).not.toBeNull()
      expect(aboutBox()!.textContent).toBe('gui.agent.about_none')
    })

    it('leaves a preset nobody has connected alone', async () => {
      /* There is no entry to update, and a field that wrote one would connect
         the agent -- which is not what typing a name asks for. Connect first,
         rename after. */
      install([row({ name: 'OpenCode', configured: false, vendored: false, description: 'the preset prose' })])
      await mount()
      await openCard('OpenCode')

      expect(nameBox()).toBeNull()
      expect(aboutBox()).toBeNull()
      expect(document.querySelector('#dBody .pmdesc')!.textContent).toBe('the preset prose')
    })

    it('describes a discovered folder but does not offer to rename it', async () => {
      /* `subagents.update` materialises a discovered row from its own manifest,
         so a description lands. A rename does not: the server refuses one on a
         materialised row -- "its name binds it to the shipped launcher" -- and
         a control for a write that is always refused is a control that lies. */
      const { acts } = install([row({ name: 'Raven-Code', configured: false, vendored: true, description: 'ships with raven' })])
      await mount()
      await openCard('Raven-Code')

      expect(nameBox()).toBeNull()
      expect(aboutBox()).not.toBeNull()

      await click(aboutBox())
      const field = document.querySelector<HTMLTextAreaElement>('#dBody .pmdesc textarea')!
      await typeInto(field, 'the shipped coder')
      await blur(field)

      expect(acts).toEqual([['update', 'Raven-Code', { description: 'the shipped coder' }]])
    })
  })

  /* The mark is the agent's brand, and the preset is what picks it -- never the
     row's name. A configured row's name is the reader's to change, so keying on
     it would cost a renamed agent its identity, and would hand a brand to a
     hand-written row that merely spells itself like a preset. The generic glyph
     is what a row with no preset gets: one of the shipped products, or an agent
     the reader wrote. A glyph rather than an initial in a coloured square,
     which is what this page drew for every row before it had marks at all. */
  describe('the brand mark', () => {
    const markImg = (name: string): HTMLImageElement | null =>
      rowNamed(name).querySelector<HTMLImageElement>('.agent-mark img')

    it('draws each row its own brand, chosen by preset', async () => {
      install([
        row({ name: 'Coder', preset: 'claude_code' }),
        row({ name: 'Writer', preset: 'codex' }),
        row({ name: 'Researcher', preset: 'mirothinker', kind: 'openai', has_api_key: true }),
      ])
      await mount()

      expect(markImg('Coder')!.getAttribute('src')).toBe('assets/agents/claudecode-color.svg')
      expect(markImg('Writer')!.getAttribute('src')).toBe('assets/agents/codex-color.svg')
      expect(markImg('Researcher')!.getAttribute('src')).toBe('assets/agents/miromind.svg')
    })

    /* The case the keying decision exists for: every preset row ships under a
       name the reader may replace, and this repository's own roster has three
       that were replaced. */
    it('keeps the brand when the row has been renamed', async () => {
      install([row({ name: 'my own helper', preset: 'claude_code' })])
      await mount()

      expect(markImg('my own helper')!.getAttribute('src')).toBe('assets/agents/claudecode-color.svg')
    })

    /* The other half of it. Spelling a name like a preset is not being that
       preset, and a brand on such a row would be a claim about a command line
       nobody verified. */
    it('withholds the brand from a row that only shares a preset name', async () => {
      install([row({ name: 'codex', preset: undefined })])
      await mount()

      /* Both halves, because "no brand" is also true of a page that draws no
         marks at all: the slot has to be there, holding the generic glyph, or
         this passes for the wrong reason. */
      const slot = rowNamed('codex').querySelector('.agent-mark')
      expect(slot).not.toBeNull()
      expect(slot!.querySelector('svg')).not.toBeNull()
      expect(markImg('codex')).toBeNull()
    })

    it('draws the generic glyph, never an initial, for a row with no preset', async () => {
      install([row({ name: 'Raven-Code', preset: undefined, configured: false, vendored: true, kind: 'acp' })])
      await mount()

      const slot = rowNamed('Raven-Code').querySelector('.agent-mark')
      expect(slot).not.toBeNull()
      expect(slot!.querySelector('svg')).not.toBeNull()
      expect(markImg('Raven-Code')).toBeNull()
      expect(rowNamed('Raven-Code').querySelector('.pmtile')).toBeNull()
    })

    /* One identity, drawn the same in both places. The card headed itself with
       an initial while the row beside it drew a brand. */
    it('heads the card with the same mark the row draws', async () => {
      install([row({ name: 'Coder', preset: 'claude_code' })])
      await mount()
      await openCard('Coder')

      const head = document.querySelector<HTMLImageElement>('#dBody .pmdhead .agent-mark img')
      expect(head).not.toBeNull()
      expect(head!.getAttribute('src')).toBe('assets/agents/claudecode-color.svg')
      expect(document.querySelector('#dBody .pmdhead .pmtile')).toBeNull()
    })
  })

})
