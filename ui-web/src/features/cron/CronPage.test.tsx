// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as lang from '../../state/lang'
import * as settingsDialog from '../../state/settings'
import { resetSources, setSources } from '../../state/sources'
import { domSnapshot } from '../../test/domSnapshot'
import { mountPageRoot } from '../../test/pageRoot'
import { CronApp } from './CronPage'
import * as store from './store'

import type { CronJob, CronSource } from './types'

/* The overflow menu's rows render from src/App.tsx into the shared #menu
   host, so the page's own root has to be standing for this island's menus to
   appear. */
mountPageRoot()

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

/* The island runs against the same two seams production wires up: a stand-in
   translator on setTranslator (it returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on sources.cron. */
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
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(confirmStore, 'ask').mockImplementation((_t, _b, _l, fn) => fn())
  setSources({ cron: source })
  document.body.innerHTML =
    '<div id="cronBody"></div>' +
    '<div id="menu" data-open="false"></div>'
  return { source, calls }
}

/* The section as the dialog hosts it: the island in its own box, and the fetch
   arriving at the section costs (features/cron/store.ts's `enter`, which
   state/settings.ts spends -- here it is called by hand, because the dialog is
   not what this file is about). */
async function mount() {
  const view = render(<CronApp />, { container: document.getElementById('cronBody')! })
  await act(async () => {
    store.backToList()
    await store.refresh()
  })
  return view
}

/* Scoped to the list: the picked job carries the same name in its own header,
   so an unscoped query answers two elements once a row is open. */
const side = (): HTMLElement => document.querySelector('.two-pane-side') as HTMLElement
const rowText = (name: string): HTMLElement => within(side()).getByText(name)
const rowNamed = (name: string): HTMLElement =>
  [...document.querySelectorAll<HTMLElement>('.two-pane-row')]
    .find((r) => r.querySelector('.nm')!.textContent === name)!
const listNames = (): Array<string | null> =>
  [...side().querySelectorAll('.two-pane-row .nm')].map((b) => b.textContent)

afterEach(() => {
  act(() => {
    store.backToList()
    store.closeSheet()
  })
  cleanup()
  vi.restoreAllMocks()
  resetSources()
  /* Applying a language is module state, so the one case that flips it must
     not leave the page in it. */
  lang._resetForTests()
})

/* The name box, and leaving it -- which is what commits an edit: the detail
   saves on the native `change` moment rather than on a button. */
/* With no jobs installed the page offers no way in -- a job is asked for in a
   conversation -- so these draft tests open the sheet the way the list's "+"
   does once a job exists. */
async function startDraft(): Promise<void> {
  await act(async () => { store.openSheet() })
}

const nameBox = (): HTMLInputElement =>
  document.querySelector<HTMLInputElement>('.two-pane-main input.cronname')!

async function retype(value: string): Promise<void> {
  const box = nameBox()
  await act(async () => {
    box.value = value
    box.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => {
    fireEvent.blur(box)
  })
}

/* The frequency dropdown, and a pick on it. */
const freqSel = (): HTMLSelectElement =>
  document.querySelector<HTMLSelectElement>('.cronwhen select')!

async function pickFreq(id: string): Promise<void> {
  await act(async () => {
    fireEvent.change(freqSel(), { target: { value: id } })
  })
}

describe('cron island', () => {
  it('waits as the rows it becomes rather than as an empty column', async () => {
    let land: ((r: CronJob[]) => void) | null = null
    install([], { rows: () => new Promise((resolve) => { land = resolve }) })
    render(<CronApp />, { container: document.getElementById('cronBody')! })
    await act(async () => { void store.refresh(); await Promise.resolve() })
    expect(side().querySelectorAll('.two-pane-wait .two-pane-row').length).toBe(7)

    await act(async () => { land!([job()]); await Promise.resolve() })
    expect(document.querySelector('.two-pane-wait')).toBeNull()
  })

  it('waits on the run history as the runs it becomes, not as an empty block', async () => {
    let land: ((r: never[]) => void) | null = null
    install([job()], { runs: () => new Promise((resolve) => { land = resolve }) })
    await mount()
    await act(async () => { rowText('morning digest').click() })
    expect(document.querySelectorAll('.cronwait .cronrun').length).toBe(4)

    await act(async () => { land!([]); await Promise.resolve() })
    expect(document.querySelector('.cronwait')).toBeNull()
    expect(screen.getByText('gui.cron.hist_none')).toBeTruthy()
  })

  it('shows every row the source answers, grouped by whether it is on', async () => {
    install([job(), job({ id: 'j2', name: 'weekly report', on: false })])
    await mount()
    expect(await screen.findByText('morning digest')).toBeTruthy()
    expect(screen.getByText('weekly report')).toBeTruthy()
    expect(screen.getByText('gui.cron.g_on')).toBeTruthy()
    expect(screen.getByText('gui.cron.g_off')).toBeTruthy()
  })

  /* No jobs: one column, pointing at the conversation, with no manual way to
     add -- neither the list's "+" nor a button of its own. A draft opened from
     elsewhere still brings the list back, since it is named against it. */
  it('shows the empty state in one column with no manual add when the source has nothing', async () => {
    install([])
    await mount()
    const main = document.querySelector('.two-pane-main') as HTMLElement
    expect(within(main).getByText('gui.cron.none_t')).toBeTruthy()
    expect(within(main).getByText('gui.cron.none')).toBeTruthy()
    expect(document.querySelector('.two-pane-side')).toBeNull()
    expect(screen.queryByLabelText('gui.cron_new')).toBeNull()
    expect(screen.queryByText('gui.cron_new')).toBeNull()
    expect(main.querySelector('button')).toBeNull()
    await startDraft()
    expect(store.get().sheet).toBeTruthy()
    expect(nameBox()).toBeTruthy()
    expect(document.querySelector('.two-pane-side')).toBeTruthy()
  })

  /* Two groups and a search, which is what the three filter chips became: the
     question a reader arrives with is "did anything break", and a job whose
     last run failed says so on its own second line rather than behind a chip
     that could only ever say how many. */
  it('groups by whether the job is on, and says which one broke', async () => {
    install([
      job({ runs: [{ at: 'today', ok: false, note: 'boom' }] }),
      job({ id: 'b', name: 'weekly report', runs: [{ at: 'today', ok: true, note: 'fine' }] }),
      job({ id: 'c', name: 'paused one', on: false }),
    ])
    await mount()
    expect(await screen.findByText('morning digest')).toBeTruthy()
    expect(listNames()).toEqual(['morning digest', 'weekly report', 'paused one'])
    expect(rowNamed('morning digest').querySelector('.ds')!.className).toContain('bad')
    expect(rowNamed('weekly report').querySelector('.ds')!.className).not.toContain('bad')
    expect(rowNamed('paused one').className).toContain('two-pane-off')
    /* The group heading already says it is off; the row does not repeat it. */
    expect(rowNamed('paused one').textContent).not.toContain('gui.cron.paused')
  })

  it('narrows the list by what is typed in the search', async () => {
    install([job(), job({ id: 'b', name: 'weekly report' })])
    await mount()
    await screen.findByText('morning digest')
    const box = side().querySelector('input') as HTMLInputElement
    await act(async () => {
      fireEvent.change(box, { target: { value: 'weekly' } })
    })
    expect(listNames()).toEqual(['weekly report'])
  })

  it('opens the job beside the list, and says to pick one until it is', async () => {
    install([job()])
    await mount()
    expect(await screen.findByText('gui.cron.pick')).toBeTruthy()
    await act(async () => {
      rowText('morning digest').click()
    })
    expect(screen.queryByText('gui.cron.pick')).toBeNull()
    /* The four blocks the job is, and no save button: the form writes on the
       way out of a box. */
    expect([...document.querySelectorAll('.two-pane-main .two-pane-lab')].map((l) => l.textContent))
      .toEqual(['gui.job.name', 'gui.job.freq', 'gui.job.what', 'gui.cron.tab_runs'])
    expect(screen.queryByText('gui.cron.save')).toBeNull()
  })

  it('lands a refused save under the schedule control', async () => {
    install([job({ freq: 'once', at_local: '' })], {
      save: async () => {
        throw new Error('no instant')
      },
    })
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    await retype('renamed')
    expect(await screen.findByText('gui.job.need_instant')).toBeTruthy()
  })

  it('refuses a blank draft with a note under each empty field', async () => {
    install([])
    await mount()
    await startDraft()
    await act(async () => {
      ;(await screen.findByText('gui.cron.create')).click()
    })
    expect(await screen.findByText('gui.job.need_name')).toBeTruthy()
    expect(screen.getByText('gui.job.need_what')).toBeTruthy()
  })

  /* The one link out of this form is inside it: "where results go" opens
     Channels, which is a section pick, which leaves this section. Dropping the
     draft there deleted a half-written job to a press that reads like a
     detour. */
  describe('a draft the reader leaves the section with', () => {
    /* The draft's own foot, so no other primary button in the column can pass
       for it. */
    const draftUp = (): boolean => !!document.querySelector('.two-pane-main .two-pane-foot button.mini.go')

    it('goes off screen and comes back with what was typed in it', async () => {
      install([])
      await mount()
      await startDraft()
      const name = nameBox()
      await act(async () => {
        name.value = 'half written'
        name.dispatchEvent(new Event('input', { bubbles: true }))
      })

      /* What the link does: the dialog leaves this section. */
      await act(async () => { settingsDialog.leaveSection() })
      expect(draftUp(), 'the draft is not drawn in the section it left').toBe(false)
      expect(store.get().sheet, 'the draft is kept').toBeTruthy()

      /* And back. */
      await act(async () => { settingsDialog.enterSection('cron') })
      expect(draftUp()).toBe(true)
      expect(nameBox().value).toBe('half written')
    })

    /* Cancel is still cancel: what the reader ends deliberately does not come
       back the next time they open the section. */
    it('is gone for good once the reader cancels it', async () => {
      install([])
      await mount()
      await startDraft()
      await act(async () => {
        ;(await screen.findByText('gui.cancel')).click()
      })
      await act(async () => { settingsDialog.enterSection('cron') })
      expect(draftUp()).toBe(false)
      expect(store.get().sheet).toBeNull()
    })
  })

  /* Leaving a box is the write, and only where something changed: a reader who
     tabs through a form they did not touch has saved nothing. */
  it('writes on the way out of a box, and only where something changed', async () => {
    const saved: string[] = []
    install([job()], {
      save: async (d) => {
        saved.push(d.name)
        return { ...job(), ...d, name: d.name || 'morning digest' }
      },
    })
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    /* Out of the box with nothing changed: nothing written. */
    await act(async () => { fireEvent.blur(nameBox()) })
    expect(saved).toEqual([])
    await retype('renamed')
    expect(saved).toEqual(['renamed'])
    /* And the reader is still on the job. */
    expect(nameBox()).toBeTruthy()
  })

  /* A blur starts the save and the answer lands later, so the reader is free
     to be somewhere else by then. Both of these are the answer overwriting a
     choice made after the request went out. */
  function delayedSave(): { land: () => void; source: Partial<CronSource> } {
    const box: { land: () => void } = { land: () => {} }
    return {
      land: () => box.land(),
      source: {
        save: async (d) => {
          /* Snapshotted at the call, the way a server answers the request it
             was given rather than the reader's later keystrokes. */
          const answer = { ...job(), ...d }
          return new Promise<CronJob>((res) => {
            box.land = () => res(answer)
          })
        },
      },
    }
  }

  const settle = async (land: () => void): Promise<void> => {
    await act(async () => {
      land()
      await new Promise((r) => setTimeout(r, 0))
    })
  }

  it('leaves the reader on the job they picked while another job was saving', async () => {
    const { land, source } = delayedSave()
    install([job(), job({ id: 'j2', name: 'weekly report' })], source)
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    await retype('renamed')
    await act(async () => {
      rowText('weekly report').click()
    })
    expect(store.get().viewId).toBe('j2')
    await settle(land)
    expect(store.get().viewId).toBe('j2')
    expect(nameBox().value).toBe('weekly report')
  })

  it('keeps what was typed after the blur while that save was still in flight', async () => {
    const { land, source } = delayedSave()
    install([job()], source)
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    await retype('renamed')
    const say = (): HTMLTextAreaElement =>
      document.querySelector<HTMLTextAreaElement>('textarea.cronsay')!
    await act(async () => {
      say().value = 'and the morning news'
      say().dispatchEvent(new Event('input', { bubbles: true }))
    })
    await settle(land)
    expect(say().value).toBe('and the morning news')
  })

  /* A frequency is picked, not typed, so it commits the moment it is picked. */
  it('writes a frequency the moment it is picked', async () => {
    const saved: Array<{ freq: string; every_ms?: number }> = []
    install([job({ freq: 'hour', every_ms: 3600000 })], {
      save: async (d) => {
        saved.push({ freq: d.freq, ...(d.every_ms ? { every_ms: d.every_ms } : {}) })
        return { ...job(), ...d }
      },
    })
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    expect(freqSel().value).toBe('hour:1')
    await pickFreq('hour:6')
    expect(saved).toEqual([{ freq: 'hour', every_ms: 6 * 3600000 }])
  })

  it('refetches the run history when the shell refreshes the page', async () => {
    const history = [{ at: 'today 08:00', ok: true, note: 'first' }]
    install([job()], { runs: async () => [...history] })
    await mount()
    await act(async () => {
      rowText('morning digest').click()
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
      rowText('morning digest').click()
    })
    await screen.findByText('stamped')
    const before = runs.mock.calls.length
    /* A real pick, which is the only thing that moves the page's language. */
    await act(async () => {
      lang.set('zh')
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
      rowText('morning digest').click()
    })
    await act(async () => {
      ;(screen.getByText('gui.cron.delete') as HTMLElement).click()
    })
    /* Still on the job: the source said why, and bouncing the reader to a list
       where the row is still there would be the second lie. */
    expect(nameBox()).toBeTruthy()
  })

  /* Deleting is not a row verb, and on the job's own page it is at the foot
     under a hairline rather than among the boxes being filled in. */
  it('keeps delete off the row and at the foot of the job', async () => {
    install([job()])
    await mount()
    expect([...rowNamed('morning digest').querySelectorAll('button')].map((b) => b.textContent))
      .not.toContain('gui.cron.delete')
    await act(async () => {
      rowText('morning digest').click()
    })
    const foot = [...document.querySelectorAll('.two-pane-foot button')].map((b) => b.textContent)
    expect(foot).toEqual(['gui.cron.run_now', 'gui.cron.open_sess', 'gui.cron.delete'])
    const form = [...document.querySelectorAll('.two-pane-sec button')].map((b) => b.textContent)
    expect(form).not.toContain('gui.cron.delete')
  })

  /* The hourly interval the backend has always accepted, folded into the
     frequency list rather than standing as a number box beside it: the box was
     a control a reader had to notice to use, so "hourly" could only ever mean
     once an hour. */
  it('offers the hourly intervals in the frequency list itself', async () => {
    install([job({ freq: 'hour', every_ms: 3600000 })])
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    expect([...freqSel().options].map((o) => o.value)).toEqual([
      'hour:1', 'hour:2', 'hour:3', 'hour:4', 'hour:6', 'hour:12',
      'day', 'week', 'month', 'once', 'cron',
    ])
    expect(document.querySelector('.everyn')).toBeNull()
  })

  /* A schedule set up in a conversation can be an interval none of the eleven
     says. Dropping it would rewrite the schedule the moment the page opened. */
  it('keeps a frequency the list cannot say on the list', async () => {
    install([job({ freq: 'hour', every_ms: 30 * 60000, when: 'every 30 min' })])
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    expect(freqSel().value).toBe('hour:1')
    expect([...freqSel().options][0]!.value).toBe('hour:1')
  })

  /* Where results go was never a property of one job: `jobToSave` does not
     read a destination and every row comes back as `app`. The form said so in
     a block of its own, which is a global setting stated inside a form about
     one job -- so the block is gone rather than restated. */
  it('says nothing about where results go', async () => {
    install([job()])
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    expect(screen.queryByText('gui.job.deliver_global')).toBeNull()
    expect([...document.querySelectorAll('.two-pane-main .two-pane-lab')].map((l) => l.textContent))
      .not.toContain('gui.job.deliver')
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

  it('keeps its rendered shape, list', async () => {
    install([
      job({ runs: [{ at: 'today', ok: false, note: 'boom' }] }),
      job({ id: 'b', name: 'weekly report', runs: [{ at: 'today', ok: true, note: 'fine' }] }),
      job({ id: 'c', name: 'paused one', on: false }),
    ])
    await mount()
    await screen.findByText('morning digest')
    expect(domSnapshot(document.getElementById('cronBody')!)).toMatchSnapshot()
  })

  it('keeps its rendered shape, job page', async () => {
    install([job({ runs: [{ at: 'today 08:00', ok: false, note: 'boom' }] })])
    await mount()
    await act(async () => {
      rowText('morning digest').click()
    })
    expect(domSnapshot(document.getElementById('cronBody')!)).toMatchSnapshot()
  })
})
