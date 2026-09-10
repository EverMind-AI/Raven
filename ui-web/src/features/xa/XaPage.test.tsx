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
  /* Two verbs, two groups. Provenance is a tag, not a section: a broken agent
     used to sit three groups below a healthy built-in one. */
  it('splits the page into what is on duty and what can be connected', async () => {
    install([
      row(),
      row({ name: 'miro', kind: 'openai', configured: false, has_api_key: false }),
      row({ name: 'codex', configured: false, probe_status: 'missing', probe_detail: 'codex: command not found' }),
    ])
    await mount()
    expect(await screen.findByText('claude_code')).toBeTruthy()
    expect(groupOf('claude_code')).toBe('gui.agent.g_on')
    expect(groupOf('miro')).toBe('gui.agent.g_off')
    expect(groupOf('codex')).toBe('gui.agent.g_off')
    expect([...document.querySelectorAll('.sugrp .hd b')].map((b) => b.textContent)).toEqual([
      'gui.agent.g_on',
      'gui.agent.g_off',
    ])
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

  /* The addable group is ordered by what the click costs, the same question the
     entrances page sorts on: a switch, an entry, a credential to go and find, or
     several hundred megabytes. */
  it('orders the addable group by what connecting costs', async () => {
    install([
      row({ name: 'unbuilt', vendored: true, configured: false, enabled: false, probe_status: 'missing' }),
      row({ name: 'needs_key', kind: 'openai', configured: false, has_api_key: false }),
      row({ name: 'preset', configured: false }),
      row({ name: 'switched_off', configured: true, enabled: false }),
    ])
    await mount()
    const off = [...document.querySelectorAll('.sugrp')]
      .find((g) => g.querySelector('.hd b')!.textContent === 'gui.agent.g_off')!
      .querySelectorAll('.nm b')
    expect([...off].map((b) => b.textContent)).toEqual(['switched_off', 'preset', 'needs_key', 'unbuilt'])
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
      'gui.agent.g_off',
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
     what that button was for. */
  it('has no button in any group heading', async () => {
    /* Both groups on screen, so this cannot pass by there being only one: the
       addable heading does carry a `.tool`, and it is the ordering note. */
    install([row(), row({ name: 'codex', configured: false })])
    await mount()
    expect(document.querySelectorAll('#xaPage .sugrp').length).toBe(2)
    expect(document.querySelector('#xaPage .sugrp .hd button')).toBeNull()
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
  describe('the row says three things: who, how, and what to do', () => {
    it('carries no second line at all', async () => {
      install([
        row(),
        row({ name: 'off_one', enabled: false }),
        row({ name: 'raven', kind: 'builtin', builtin: true, configured: false }),
        row({ name: 'broken', configured: false, probe_status: 'missing', probe_detail: 'codex: not found' }),
      ])
      await mount()
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

    /* Not-dispatchable is what every row in the addable group is, so a red dot
       there was an alarm about the group's own definition -- and the clay stripe
       it drags along made an unconnected agent look broken. */
    it('keeps the alarm colours out of the addable group', async () => {
      install([
        row({ name: 'needs_key', kind: 'openai', configured: false, has_api_key: false }),
        row({ name: 'unbuilt', vendored: true, configured: false, enabled: false, probe_status: 'missing' }),
      ])
      await mount()
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
    /* The button is what says it, and the dot stays neutral: the row is in the
       addable group, where a colour would be an alarm about nothing. */
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
         copy of the row's status line, a test verdict, a save button, a menu. */
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
      expect(document.querySelector('#dBody .pmsec')).toBeNull()
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
