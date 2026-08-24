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
    '<div class="veil" id="jobVeil" data-open="false"></div>'
  return { source, calls, shellCalls }
}

async function mount() {
  const view = render(<CronApp />, { container: document.getElementById('cronBody')! })
  await act(async () => {
    store.open()
  })
  return view
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

  it('raises the failing banner for an enabled job whose last run failed', async () => {
    install([job({ runs: [{ at: 'today', ok: false, note: 'boom' }] })])
    await mount()
    expect(await screen.findByText('gui.cron.failing {"n":1}')).toBeTruthy()
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
    await act(async () => {
      ;(await screen.findByText('gui.cron.delete')).click()
    })
    expect(await screen.findByText('← gui.cron.back')).toBeTruthy()
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
