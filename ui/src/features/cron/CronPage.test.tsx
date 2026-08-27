// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CronApp } from './CronPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { CronJob, CronSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function job(over: Partial<CronJob> = {}): CronJob {
  return {
    id: 'j1',
    name: 'morning digest',
    what: 'summarize the night',
    freq: 'day',
    at: '08:00',
    on: true,
    deliver: 'app',
    when: 'daily 08:00',
    next: 'tomorrow 08:00',
    runs: [],
    ...over,
  }
}

/* The island runs against the same two seams production wires up: a fake
   shell on window.RavenShell (T returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on window.DS.cron. */
function install(rows: CronJob[], over: Partial<CronSource> = {}) {
  const calls: string[] = []
  const source: CronSource = {
    rows: async () => rows,
    toggle: async () => calls.push('toggle'),
    remove: async () => calls.push('remove'),
    save: async (d) => ({ ...job(), ...d }),
    runs: async () => [],
    runNow: async () => calls.push('runNow'),
    openRun: async () => calls.push('openRun'),
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    /* Confirms immediately: the dialog itself is legacy chrome, not island. */
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
  }
  window.RavenShell = fakeShell
  window.DS = { cron: source }
  document.body.innerHTML =
    '<section id="cronPage"><div id="cronBody"></div></section>' +
    '<div class="veil" id="jobVeil" data-open="false"></div>' +
    '<div id="menu" data-open="false"></div>'
  return { source, calls, shellCalls }
}

async function mount() {
  const view = render(<CronApp />, { container: document.getElementById('cronBody')! })
  await act(async () => {
    store.open()
  })
  return view
}


const rowNamed = (name: string): HTMLElement =>
  [...document.querySelectorAll<HTMLElement>('.surow')].find((r) => r.querySelector('.nm b')!.textContent === name)!
const chipCount = (label: string): string | null =>
  [...document.querySelectorAll<HTMLElement>('.cronfilter button')].find((b) => b.textContent!.startsWith(label))!
    .querySelector('.n')!.textContent

/* The overflow menu of whichever host drew it, through the shared #menu the page
   keeps -- the same host production uses. */
async function pickMenu(open: HTMLElement, label: string): Promise<void> {
  await act(async () => {
    open.click()
  })
  const item = [...document.querySelectorAll<HTMLElement>('#menu button')].find((b) => b.textContent === label)
  expect(item, `menu item ${label}`).toBeTruthy()
  await act(async () => {
    item!.click()
  })
}

async function tab(name: string): Promise<void> {
  await act(async () => {
    ;[...document.querySelectorAll<HTMLElement>('.tabbar button')].find((b) => b.textContent!.startsWith(name))!.click()
  })
}

afterEach(() => {
  act(() => {
    store.backToList()
    store.closeSheet()
  })
  cleanup()
  vi.restoreAllMocks()
})

describe('cron island', () => {
  it('shows every row the source answers, through the hero', async () => {
    install([job(), job({ id: 'j2', name: 'weekly report', on: false })])
    await mount()
    expect(await screen.findByText('morning digest')).toBeTruthy()
    expect(screen.getByText('weekly report')).toBeTruthy()
    expect(screen.getByText('gui.cron.hero')).toBeTruthy()
  })

  it('shows the empty note when the source has nothing', async () => {
    install([])
    await mount()
    expect(await screen.findByText('gui.cron.none')).toBeTruthy()
  })

  /* Three chips with counts, and they filter. The banner they replace could
     only say "something failed" -- it had no way to show the paused jobs, and
     no way to narrow the list to the failures it was pointing at. */
  it('counts and filters by state', async () => {
    install([
      job({ runs: [{ at: 'today', ok: false, note: 'boom' }] }),
      job({ id: 'b', name: 'weekly report', runs: [{ at: 'today', ok: true, note: 'fine' }] }),
      job({ id: 'c', name: 'paused one', on: false }),
    ])
    await mount()
    expect(await screen.findByText('morning digest')).toBeTruthy()
    expect(chipCount('gui.cron.f_all')).toBe('3')
    expect(chipCount('gui.cron.f_fail')).toBe('1')
    expect(chipCount('gui.cron.f_paused')).toBe('1')
    await act(async () => {
      ;[...document.querySelectorAll<HTMLElement>('.cronfilter button')][1]!.click()
    })
    expect([...document.querySelectorAll('.surow .nm b')].map((b) => b.textContent)).toEqual(['morning digest'])
    await act(async () => {
      ;[...document.querySelectorAll<HTMLElement>('.cronfilter button')][2]!.click()
    })
    expect([...document.querySelectorAll('.surow .nm b')].map((b) => b.textContent)).toEqual(['paused one'])
  })

  /* The last result is the row's third line, and the whole line opens the
     session that run wrote -- without opening the job page underneath it. */
  it('opens the run session from the row without opening the job', async () => {
    const { calls } = install([job({ runs: [{ at: 'today 08:00', ok: false, note: 'boom' }] })])
    await mount()
    const line = rowNamed('morning digest').querySelector('.sufoot2 .rl') as HTMLElement
    expect(line.textContent).toContain('gui.cron.failed')
    expect(line.textContent).toContain('boom')
    await act(async () => {
      line.click()
    })
    expect(calls).toContain('openRun')
    expect(screen.queryByText('← gui.cron.back')).toBeNull()
  })

  it('opens the job page from a row and comes back to the list', async () => {
    install([job()])
    await mount()
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    const back = await screen.findByText('← gui.cron.back')
    await act(async () => {
      back.click()
    })
    expect(await screen.findByText('gui.cron.hero')).toBeTruthy()
  })

  it('lands a refused save under the schedule control', async () => {
    install([job({ freq: 'once', at_local: '' })], {
      save: async () => {
        throw new Error('no instant')
      },
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    await act(async () => {
      ;(await screen.findByText('gui.cron.save')).click()
    })
    expect(await screen.findByText('gui.job.need_instant')).toBeTruthy()
  })

  it('refuses a blank draft with a note under each empty field', async () => {
    install([])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.cron_new')).click()
    })
    await act(async () => {
      ;(await screen.findByText('gui.save')).click()
    })
    expect(await screen.findByText('gui.job.need_name')).toBeTruthy()
    expect(screen.getByText('gui.job.need_what')).toBeTruthy()
  })

  it('keeps the reader on the job page after a successful save', async () => {
    install([job()], {
      save: async (d) => ({ ...job(), ...d, name: d.name || 'morning digest' }),
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    await act(async () => {
      ;(await screen.findByText('gui.cron.save')).click()
    })
    expect(await screen.findByText('← gui.cron.back')).toBeTruthy()
  })

  it('refetches the run history when the shell refreshes the page', async () => {
    const history = [{ at: 'today 08:00', ok: true, note: 'first' }]
    install([job()], { runs: async () => [...history] })
    await mount()
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    await tab('gui.cron.tab_runs')
    expect(await screen.findByText('first')).toBeTruthy()
    history.push({ at: 'today 09:00', ok: false, note: 'landed later' })
    await act(async () => {
      await store.refresh()
    })
    expect(await screen.findByText('landed later')).toBeTruthy()
  })

  it('refetches the run history on a language flip, and only then', async () => {
    const runs = vi.fn(async () => [{ at: 'today 08:00', ok: true, note: 'stamped' }])
    install([job()], { runs })
    await mount()
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    await tab('gui.cron.tab_runs')
    await screen.findByText('stamped')
    const before = runs.mock.calls.length
    /* What the legacy language flip calls through the shim. */
    await act(async () => {
      store.langRedraw()
    })
    expect(runs.mock.calls.length).toBe(before + 1)
    /* The island's own repaint must not refetch. */
    await act(async () => {
      store.redraw()
    })
    expect(runs.mock.calls.length).toBe(before + 1)
  })

  it('keeps the reader on the job page when a delete fails handled', async () => {
    install([job()], {
      remove: async () => {
        // What the live source throws after toasting the reason itself.
        throw { handled: true }
      },
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    await pickMenu(document.querySelector('.sumenu') as HTMLElement, 'gui.cron.delete')
    expect(await screen.findByText('← gui.cron.back')).toBeTruthy()
  })

  /* Destructive verbs are not row verbs and not page-face verbs: the list row
     and the job page both keep them behind it. */
  it('keeps delete off both faces', async () => {
    install([job()])
    await mount()
    expect([...rowNamed('morning digest').querySelectorAll('button')].map((b) => b.textContent))
      .not.toContain('gui.cron.delete')
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    const face = [...document.querySelectorAll('#cronBody button')].map((b) => b.textContent)
    expect(face.length).toBeGreaterThan(0)
    expect(face).not.toContain('gui.cron.delete')
  })

  /* The hourly interval the backend has always accepted. The form sent the
     default every time, so "hourly" could only ever mean once an hour. */
  it('takes an interval for an hourly job', async () => {
    const saved: unknown[] = []
    install([job({ freq: 'hour', every_ms: 3600000 })], {
      save: async (d) => {
        saved.push({ every_ms: d.every_ms })
        return { ...job(), ...d }
      },
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    const n = document.querySelector<HTMLInputElement>('.everyn input')!
    expect(n.value).toBe('1')
    await act(async () => {
      n.value = '6'
      n.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => {
      ;(await screen.findByText('gui.cron.save')).click()
    })
    expect(saved).toEqual([{ every_ms: 6 * 3600000 }])
  })

  /* Delivery is a global setting the save payload never carried, so the page
     states it and points at the setting rather than offering a choice that
     gets thrown away. */
  it('states delivery as a fact, with no per-job control', async () => {
    install([job()])
    await mount()
    await act(async () => {
      ;(await screen.findByText('morning digest')).click()
    })
    expect(screen.getByText('gui.job.deliver_global')).toBeTruthy()
    expect(document.querySelector('#cronBody select')).toBeNull()
  })

  it('asks the source to toggle and refreshes from it', async () => {
    const { source, calls } = install([job()])
    const rowsSpy = vi.spyOn(source, 'rows')
    await mount()
    const swi = (await screen.findAllByRole('switch'))[0]!
    await act(async () => {
      swi.click()
    })
    expect(calls).toContain('toggle')
    expect(rowsSpy.mock.calls.length).toBeGreaterThan(1)
  })
})
