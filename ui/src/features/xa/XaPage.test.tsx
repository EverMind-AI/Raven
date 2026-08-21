// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { XaApp } from './XaPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { XaRow, XaSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

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
  const acts: Array<[string, string]> = []
  const loads: boolean[] = []
  const source: XaSource = {
    load: async (probe) => {
      loads.push(!!probe)
      return rows
    },
    act: async (op, r) => {
      acts.push([op, r.name])
      return rows
    },
    ...over,
  }
  const toasts: string[] = []
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    toast: (text) => toasts.push(text),
    menuAt: () => {},
    confirmAsk: (_t, _b, _l, fn) => fn(),
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
    '<aside id="detail" data-open="false"><b id="dTitle">—</b><div id="dBody"></div></aside>'
  return { source, acts, loads, toasts }
}

async function mount() {
  const view = render(<XaApp />, { container: document.getElementById('xaBody')! })
  await act(async () => {
    store.open()
  })
  return view
}

afterEach(() => {
  act(() => {
    store.sheetDismissed()
  })
  cleanup()
})

describe('xa island', () => {
  it('splits the rows into connected and available sections', async () => {
    install([row(), row({ name: 'codex', configured: false, probe_status: 'missing', probe_detail: 'codex: command not found' })])
    await mount()
    expect(await screen.findByText('claude_code')).toBeTruthy()
    expect(screen.getByText('codex')).toBeTruthy()
    expect(screen.getByText('gui.agent.hero')).toBeTruthy()
    /* The missing binary reads as the reason, not as switched off. */
    expect(screen.getByText('codex: command not found')).toBeTruthy()
    expect(document.querySelectorAll('.pcard').length).toBe(1)
    expect(document.querySelectorAll('.card').length).toBe(1)
  })

  it('shows the empty note when nothing is connected', async () => {
    install([row({ configured: false })])
    await mount()
    expect(await screen.findByText('gui.agent.none')).toBeTruthy()
  })

  it('probes through the source and disables the button while it runs', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => {
      release = r
    })
    const { loads } = install([row()], {
      load: async (probe) => {
        loads.push(!!probe)
        if (loads.length > 1) await gate
        return [row()]
      },
    })
    await mount()
    const probe = screen.getByText('gui.agent.probe')
    let done: Promise<void>
    act(() => {
      probe.click()
      done = Promise.resolve()
    })
    expect(screen.getByText('gui.agent.probing')).toBeTruthy()
    expect((screen.getByText('gui.agent.probing') as HTMLButtonElement).disabled).toBe(true)
    await act(async () => {
      release()
      await done!
    })
    expect(screen.getByText('gui.agent.probe')).toBeTruthy()
    expect(loads).toEqual([true, true])
  })

  it('connects a cli agent straight from its card', async () => {
    const { acts } = install([row({ name: 'hermes', configured: false })])
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.connect').click()
    })
    expect(acts).toEqual([['connect', 'hermes']])
  })

  it('routes an http agent to the sheet instead of a one-click add', async () => {
    const { acts } = install([row({ name: 'miro', kind: 'openai', configured: false })])
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.connect').click()
    })
    expect(acts).toEqual([])
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(screen.getByText('gui.agent.key_hint')).toBeTruthy()
  })

  it('runs a test from the row', async () => {
    const { acts } = install([row()])
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.test').click()
    })
    expect(acts).toEqual([['test', 'claude_code']])
  })

  it('saves the sheet form as an update and follows a rename', async () => {
    const calls: Array<Record<string, unknown>> = []
    const renamed = row({ name: 'cc2' })
    const { acts } = install([row()], {
      act: async (op, r, args) => {
        calls.push({ op, name: r.name, ...args })
        return [renamed]
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.configure').click()
    })
    const name = document.querySelector<HTMLInputElement>('.agf input')!
    name.value = 'cc2'
    await act(async () => {
      screen.getByText('gui.agent.save').click()
    })
    expect(calls).toEqual([{ op: 'update', name: 'claude_code', new_name: 'cc2', description: 'Claude Code CLI', api_key: '' }])
    /* The sheet moved with the row: still open, on the renamed agent. */
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(document.querySelector('.pmdmeta b')!.textContent).toBe('cc2')
    expect(acts).toEqual([])
  })

  it('keeps the sheet on screen for the whole length of a rename', async () => {
    let land: (() => void) | null = null
    const renamed = row({ name: 'cc2' })
    install([row()], {
      act: async () => {
        await new Promise<void>((r) => {
          land = r
        })
        return [renamed]
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.configure').click()
    })
    const body = document.getElementById('dBody')!
    const name = document.querySelector<HTMLInputElement>('.agf input')!
    name.value = 'cc2'
    await act(async () => {
      screen.getByText('gui.agent.save').click()
    })
    /* Mid-flight: the drawer says open because nothing closed it, so the sheet
       has to still be in it. The row it renders is the one the rows still
       carry -- the old name -- because the write has not answered yet. */
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(body.querySelector('.pmdmeta b')!.textContent).toBe('claude_code')
    await act(async () => {
      land!()
    })
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(body.querySelector('.pmdmeta b')!.textContent).toBe('cc2')
  })

  it('leaves the sheet where it is when a rename is rejected', async () => {
    const { toasts } = install([row()], {
      act: async () => {
        throw new Error('nope')
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.configure').click()
    })
    const name = document.querySelector<HTMLInputElement>('.agf input')!
    name.value = 'cc2'
    await act(async () => {
      screen.getByText('gui.agent.save').click()
    })
    /* A name the rows will never carry would have closed the drawer. */
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(document.querySelector('.pmdmeta b')!.textContent).toBe('claude_code')
    expect(toasts.length).toBe(1)
  })

  it('toggles and disconnects from the sheet foot', async () => {
    const { acts } = install([row()])
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.configure').click()
    })
    await act(async () => {
      screen.getByText('gui.agent.disable').click()
    })
    expect(acts).toContainEqual(['toggle', 'claude_code'])
    await act(async () => {
      screen.getByText('gui.agent.disconnect').click()
    })
    expect(acts).toContainEqual(['remove', 'claude_code'])
    /* Disconnect closes the sheet before the write, like the legacy flow. */
    expect(document.getElementById('detail')!.dataset.open).toBe('false')
  })

  it('toasts the failure and keeps the page when the source rejects', async () => {
    const { toasts } = install([row()], {
      act: async () => {
        throw new Error('gateway gone')
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.test').click()
    })
    expect(toasts.some((x) => x.includes('gui.agent.failed') && x.includes('gateway gone'))).toBe(true)
    expect(screen.getByText('claude_code')).toBeTruthy()
  })

  it('repaints its own sheet after another page borrowed the drawer', async () => {
    install([row()])
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.configure').click()
    })
    expect(screen.getByText('gui.agent.sec_status')).toBeTruthy()
    /* What the skills opener does when it takes over the shared drawer:
       wipes #dBody wholesale and re-sets the already-true open flag. */
    const dBody = document.getElementById('dBody')!
    await act(async () => {
      dBody.innerHTML = '<div>SKILL DETAIL</div>'
      document.getElementById('detail')!.dataset.open = 'true'
    })
    await act(async () => {
      screen.getByText('gui.agent.configure').click()
    })
    expect(dBody.textContent).toContain('gui.agent.sec_status')
    expect(dBody.textContent).not.toContain('SKILL DETAIL')
  })

  it('drops the sheet when legacy chrome closes the drawer', async () => {
    install([row()])
    await mount()
    await act(async () => {
      screen.getByText('gui.agent.configure').click()
    })
    /* Esc and the X only flip the flag; the observer must follow. */
    await act(async () => {
      document.getElementById('detail')!.dataset.open = 'false'
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(store.getState().sheet).toBe(null)
  })
})

/* Built-in agents arrived on main while this island was in review (the unified
   agent registry), so the island has to answer for them too: they are rows with
   no row of their own -- `configured` false, `builtin` true -- and the legacy
   renderer this replaces gave them their own section and their own card. */
describe('xa island, built-in agents', () => {
  const builtin = (over: Partial<XaRow> = {}): XaRow =>
    row({ name: 'research-raven', preset: undefined, kind: 'builtin', configured: false, builtin: true, description: 'Deep retrieval', ...over })

  it('gives them their own section and keeps them out of the other two', async () => {
    install([builtin(), row(), row({ name: 'codex', configured: false })])
    await mount()
    expect(await screen.findByText('research-raven')).toBeTruthy()
    expect(screen.getByText('gui.agent.builtin')).toBeTruthy()
    expect(screen.getByText('gui.agent.builtin_h')).toBeTruthy()
    /* One built-in card and one configured card, both .pcard; the available
       one is still the only .card. A built-in leaking into either list would
       show up as a count here. */
    expect(document.querySelectorAll('.pcard').length).toBe(2)
    expect(document.querySelectorAll('.card').length).toBe(1)
    const counts = [...document.querySelectorAll('.csec .hd .n')].map((n) => n.textContent)
    expect(counts).toEqual(['1', '1', '1'])
  })

  it('reads as running or switched off, never as unmeasured', async () => {
    install([builtin()])
    await mount()
    const card = document.querySelector('.pcard')!
    expect(card.querySelector('.mo')!.textContent).toBe('gui.agent.inprocess')
    expect(card.querySelector('.led')!.className).toBe('led')
    expect(card.querySelector('.kd')!.textContent).toBe('gui.agent.kind_builtin')
  })

  it('reads as off when switched off, without touching the probe verdict', async () => {
    install([builtin({ enabled: false, probe_status: 'unknown' })])
    await mount()
    const card = document.querySelector('.pcard')!
    expect(card.querySelector('.mo')!.textContent).toBe('gui.agent.disabled')
    expect(card.querySelector('.led')!.className).toBe('led off')
  })

  it('offers the switch and the sheet, and nothing that would mean nothing', async () => {
    const h = install([builtin()])
    await mount()
    const labels = [...document.querySelectorAll('.pcard .ctl button')].map((b) => b.textContent)
    expect(labels).toEqual(['gui.agent.disable', 'gui.agent.configure'])
    await act(async () => {
      ;(document.querySelector('.pcard .ctl button') as HTMLElement).click()
    })
    expect(h.acts).toEqual([['toggle', 'research-raven']])
  })

  it('opens a sheet whose only action is the switch', async () => {
    install([builtin()])
    await mount()
    await act(async () => {
      ;(document.querySelector('.pcard') as HTMLElement).click()
    })
    const head = [...document.querySelectorAll('#dBody .dact button')].map((b) => b.textContent)
    expect(head).toEqual(['gui.agent.disable'])
    /* No disconnect: there is no row to remove, and the legacy sheet guarded
       that section on `configured` for exactly this reason. */
    expect(document.querySelector('#dBody .danger')).toBeNull()
  })
})

/* The vendored builds: folders this install shipped, discovered on every start.
   They arrive with `configured` false and `builtin` false, which put them in the
   "connect one" section behind a Connect button they cannot honour -- they have
   no preset to install from, and there is no row to write. */
describe('xa island, vendored builds', () => {
  const vend = (over: Partial<XaRow> = {}): XaRow =>
    row({
      name: 'Raven-Research',
      preset: undefined,
      kind: 'cli',
      configured: false,
      vendored: true,
      description: 'A vendored build',
      ...over,
    })

  it('gives them their own section, not the connectable one', async () => {
    install([vend(), row(), row({ name: 'codex', configured: false })])
    await mount()

    expect(await screen.findByText('Raven-Research')).toBeTruthy()
    expect(screen.getByText('gui.agent.vendored')).toBeTruthy()
    expect(screen.getByText('gui.agent.vendored_h')).toBeTruthy()
    /* `.card` is the connectable shape. One is expected (codex); a vendored row
       drawn as a second would be the regression. */
    expect(document.querySelectorAll('.card').length).toBe(1)
    const counts = [...document.querySelectorAll('.csec .hd .n')].map((n) => n.textContent)
    expect(counts).toEqual(['1', '1', '1'])
  })

  it('offers neither connect nor a switch', async () => {
    /* Connect has no preset to connect from. The switch is the subtler one:
       `subagents.toggle` creates an override row for a *built-in* name and
       answers `subagent_not_found` for anything else, and a vendored row is
       neither in config nor a built-in name -- so the button errored. Its
       membership belongs to the folder, which is what the section hint says. */
    install([vend()])
    await mount()
    await screen.findByText('Raven-Research')

    const labels = [...document.querySelectorAll('.pcard button')].map((b) => b.textContent)
    expect(labels).not.toContain('gui.agent.connect')
    expect(labels).not.toContain('gui.agent.disable')
    expect(labels).not.toContain('gui.agent.enable')
    expect(labels).toContain('gui.agent.configure')
  })

  it('a built-in row keeps its switch', async () => {
    /* The variant is per-section, not global: a built-in row's switch is real
       and is the only way to take one off the roster. */
    install([row({ name: 'raven', preset: undefined, kind: 'builtin', configured: false, builtin: true })])
    await mount()
    await screen.findByText('raven')

    expect([...document.querySelectorAll('.pcard button')].map((b) => b.textContent)).toContain('gui.agent.disable')
  })

  it('an unbuilt one offers Install, a ready one does not', async () => {
    /* The action the section exists to make reachable: the folder is on disk and
       what it lacks is a few hundred MB of dependencies, which used to mean
       leaving the page for a terminal. A ready row has nothing to install, so
       offering the button there would be a button that does nothing. */
    install([vend({ enabled: false, probe_status: 'missing', probe_detail: 'venv not built' }), vend({ name: 'Raven-PPT' })])
    await mount()
    await screen.findByText('Raven-Research')

    const labelsOf = (name: string) => {
      const el = [...document.querySelectorAll('.pcard')].find((c) => c.textContent?.includes(name))
      return [...(el ? el.querySelectorAll('button') : [])].map((b) => b.textContent)
    }
    expect(labelsOf('Raven-Research')).toContain('gui.agent.install')
    expect(labelsOf('Raven-PPT')).not.toContain('gui.agent.install')
  })

  it('the card offers no Install for a folder switched off in its own manifest', async () => {
    /* Same gate as the sheet, and it needs its own test: a *ready* row and a
       manifest-disabled row both fail `!row.enabled`, so the earlier pair agreed
       with each other and a revert to that condition stayed green here. */
    install([vend({ enabled: false, probe_status: 'ready', probe_detail: 'installed at /x' })])
    await mount()
    await screen.findByText('Raven-Research')

    const labels = [...document.querySelectorAll('.pcard button')].map((b) => b.textContent)
    expect(labels).not.toContain('gui.agent.install')
  })

  it('a build in flight disables the button and says so', async () => {
    /* `subagents.build` returns the moment the build starts, so the row's own
       flag is the only thing that knows it is still going. A second click would
       start a second `uv sync` against the same directory. */
    install([vend({ enabled: false, building: true })])
    await mount()
    await screen.findByText('Raven-Research')

    const btn = [...document.querySelectorAll('.pcard button')].find((b) => b.textContent === 'gui.agent.installing')
    expect(btn).toBeTruthy()
    expect((btn as HTMLButtonElement).disabled).toBe(true)
  })

  it('an unbuilt one is drawn with what it is missing, not dropped', async () => {
    /* The whole reason it is listed at all: "present, not set up" is what the
       reader needs to see, and dropping the row would say "not shipped". */
    install([vend({ enabled: false, probe_status: 'missing', probe_detail: 'venv not built in Raven-X' })])
    await mount()

    expect(await screen.findByText('Raven-Research')).toBeTruthy()
    expect(screen.getByText(/venv not built in Raven-X/)).toBeTruthy()
  })

  it('a row the user also wrote by hand keeps its configured card', async () => {
    /* Their edit, their delete button. The server reports such a row as
       configured rather than vendored, and this pins that the island does not
       second-guess it by name. */
    install([row({ name: 'Raven-Research', kind: 'cli', configured: true })])
    await mount()
    await screen.findByText('Raven-Research')

    expect(document.querySelectorAll('.pcard').length).toBe(1)
    expect(screen.queryByText('gui.agent.vendored')).toBeNull()
  })
})

/* The sheet is a second surface with the same two questions, and it answered
   both wrongly: a vendored row got a Connect button in its head and a Connect
   button on its config form, and both call `subagents.add` with a preset name
   the server does not know. The built-in rows had the same dead buttons since
   before discovery existed. */
describe('xa island, the detail sheet of a packaged row', () => {
  const vend = (over: Partial<XaRow> = {}): XaRow =>
    row({ name: 'Raven-Research', preset: undefined, kind: 'cli', configured: false, vendored: true, ...over })

  const openSheet = async (r: XaRow) => {
    install([r])
    await mount()
    const card = [...document.querySelectorAll('.pcard')].find((c) => c.textContent?.includes(r.name))
    const cfg = [...(card ? card.querySelectorAll('button') : [])].find(
      (b) => b.textContent === 'gui.agent.configure',
    )
    ;(cfg as HTMLButtonElement).click()
    await screen.findByText('gui.agent.sec_config')
  }

  it('offers no connect anywhere on it', async () => {
    await openSheet(vend({ enabled: true }))

    const labels = [...document.querySelectorAll('button')].map((b) => b.textContent)
    expect(labels).not.toContain('gui.agent.connect')
    expect(screen.getByText('gui.agent.packaged_note')).toBeTruthy()
  })

  it('a built-in row gets the same treatment, which it needed already', async () => {
    await openSheet(row({ name: 'raven', preset: undefined, kind: 'builtin', configured: false, builtin: true }))

    expect([...document.querySelectorAll('button')].map((b) => b.textContent)).not.toContain('gui.agent.connect')
  })

  it('offers Install only for the state it fixes', async () => {
    await openSheet(vend({ enabled: false, probe_status: 'missing', probe_detail: 'venv not built' }))
    expect([...document.querySelectorAll('.dact button')].map((b) => b.textContent)).toContain('gui.agent.install')
  })

  it('offers no Install for a folder switched off in its own manifest', async () => {
    /* `!row.enabled` alone put a minutes-long no-op behind the only button a
       vendored row has. Its manifest said `"enabled": false`; nothing Install
       does changes that. */
    await openSheet(vend({ enabled: false, probe_status: 'ready', probe_detail: 'installed at /x' }))

    expect([...document.querySelectorAll('.dact button')].map((b) => b.textContent)).not.toContain('gui.agent.install')
  })
})
