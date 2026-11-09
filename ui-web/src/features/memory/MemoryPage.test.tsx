// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as settingsDialog from '../../state/settings'
import { resetSources, setSources } from '../../state/sources'
import { domSnapshot } from '../../test/domSnapshot'
import { MemoryApp } from './MemoryPage'
import * as store from './store';

import type { MemItem, MemStats, MemorySource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function item(over: Partial<MemItem> = {}): MemItem {
  return {
    id: 'm1',
    kind: 'episode',
    subject: 'shipped the island',
    summary: 'what the session did',
    body: 'the long form of the episode',
    timestamp: '2026-08-19T08:00:00Z',
    session_id: 's1',
    ...over,
  }
}

/* The island runs against the same two seams production wires: a stand-in
   translator on setTranslator (it returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on sources.memory. */
function install(over: Partial<MemorySource> = {}, stats: MemStats | null = null) {
  const source: MemorySource = {
    stats: async () => stats,
    list: async () => ({ items: [item()], total: 1 }),
    setBackend: async () => ({ applied: true }),
    setCloudKey: async () => ({ applied: true }),
    probeCloud: async () => ({ status: 'ok' }),
    switchStatus: async () => ({ active: false, turns: 0 }),
    applyRuntime: async () => ({ ok: true }),
    startLocal: async () => ({ ok: true, running: true, base_url: '' }),
    ...over,
  }
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(confirmStore, 'ask').mockImplementation((_t, _b, _l, fn) => fn())
  setSources({ memory: source })
  document.body.innerHTML = '<div id="memoryBody"></div>'
  return { source }
}

/* The section as the dialog hosts it: the island in its own box, and the two
   reads arriving at the section costs (features/memory/store.ts's `enter`,
   which state/settings.ts spends -- here it is called by hand, because the
   dialog is not what this file is about). */
async function mount() {
  const view = render(<MemoryApp />, { container: document.getElementById('memoryBody')! })
  await act(async () => {
    store.setKind('episode')
    await store.refreshStats()
    await store.load()
  })
  return view
}

/* Scoped to the list: the picked memory carries the same subject in its own
   header, so an unscoped query answers two elements once a row is open. */
const row = (text: string): HTMLElement =>
  within(document.querySelector('.two-pane-side') as HTMLElement).getByText(text)

afterEach(() => {
  cleanup()
  act(() => { store._resetForTests() })
  vi.restoreAllMocks()
  resetSources()
})

describe('memory island', () => {
  it('waits as the rows it becomes rather than as a line of grey text', async () => {
    let land: ((r: { items: MemItem[]; total: number }) => void) | null = null
    install({ list: () => new Promise((resolve) => { land = resolve }) })
    render(<MemoryApp />, { container: document.getElementById('memoryBody')! })
    await act(async () => { store.setKind('episode'); void store.load(); await Promise.resolve() })
    expect(document.querySelectorAll('.two-pane-wait .two-pane-row').length).toBe(7)

    await act(async () => { land!({ items: [item()], total: 1 }); await Promise.resolve() })
    expect(document.querySelector('.two-pane-wait')).toBeNull()
    expect(row('shipped the island')).toBeTruthy()
  })

  it('shows the rows and the stat band the source answers', async () => {
    install(
      { list: async () => ({ items: [item(), item({ id: 'm2', subject: 'fixed the flake' })], total: 2 }) },
      { episodes: 12, profiles: 1, agent_cases: 3, agent_skills: 4 },
    )
    await mount()
    expect(await screen.findByText('shipped the island')).toBeTruthy()
    expect(screen.getByText('fixed the flake')).toBeTruthy()
    expect(await screen.findByText('12')).toBeTruthy()
    expect(screen.getByText('gui.mem.n_total {"n":2}')).toBeTruthy()
  })

  it('shows the empty state once, in the right column, when the kind has nothing', async () => {
    install({ list: async () => ({ items: [], total: 0 }) })
    await mount()
    const main = document.querySelector('.two-pane-main') as HTMLElement
    expect(within(main).getByText('gui.mem.empty_t')).toBeTruthy()
    expect(within(main).getByText('gui.mem.empty')).toBeTruthy()
    expect(screen.queryByText('gui.mem.err_t')).toBeNull()
    expect(screen.queryByText('gui.plug.retry')).toBeNull()
    expect(screen.queryByText(/gui\.mem\.n_total/)).toBeNull()
    expect(document.querySelector('.two-pane-list')!.textContent).toBe('')
  })

  /* A page turn keeps the rows up while the next page is read; the count line
     under them went away for that moment, and the pager slid down under the
     pointer that had just clicked it, then back up when the rows landed. */
  it('keeps the count line through a page turn, so the pager does not move', async () => {
    let land: ((r: { items: MemItem[]; total: number }) => void) | null = null
    install({
      list: ({ page }) => page === 1
        ? Promise.resolve({ items: [item()], total: 45 })
        : new Promise((resolve) => { land = resolve }),
    })
    await mount()
    expect(screen.getByText('gui.mem.n_total {"n":45}')).toBeTruthy()
    await act(async () => { screen.getByText('gui.mem.next').click() })
    expect(store.get().phase).toBe('loading')
    expect(screen.getByText('gui.mem.n_total {"n":45}')).toBeTruthy()
    expect(screen.getByText('gui.mem.page {"p":2,"n":3}')).toBeTruthy()
    await act(async () => { land!({ items: [item({ id: 'm2', subject: 'page two' })], total: 45 }); await Promise.resolve() })
    expect(row('page two')).toBeTruthy()
  })

  it('renders the demo down note, without the stat band', async () => {
    install({
      list: async () => {
        // What the fixture source answers: the demo has no memory engine.
        throw { down: true }
      },
    })
    await mount()
    expect(await screen.findByText('gui.mem.down')).toBeTruthy()
    expect(screen.queryByText('gui.mem.tab_episode')).toBeNull()
  })

  it('says why the page is empty instead of showing an empty stat band', async () => {
    /* No memory plugin, or one the config does not name: not a failure, and
       four zeros read as "your memories are gone" rather than "they are not
       kept here". */
    const note = 'Long-term memory runs on mem0, and this page reads EverOS only.'
    install({ list: async () => ({ items: [], total: 0, note }) })
    await mount()
    expect(await screen.findByText(note)).toBeTruthy()
    expect(screen.queryByText('gui.mem.tab_episode')).toBeNull()
  })

  /* The whole frame, not a red paragraph under the search: the switch and the
     list have nothing to work on while the read fails. */
  it('renders a live failure across the frame with its detail, and recovers on retry', async () => {
    let failed = false
    install({
      list: async () => {
        if (!failed) {
          failed = true
          throw new Error('engine offline')
        }
        return { items: [item()], total: 1 }
      },
    })
    await mount()
    expect(await screen.findByText('gui.mem.err_t')).toBeTruthy()
    expect(screen.getByText('gui.mem.err_sub')).toBeTruthy()
    expect(screen.getByText('engine offline')).toBeTruthy()
    /* Down is not empty: the empty store's words are not in this state. */
    expect(screen.queryByText('gui.mem.empty_t')).toBeNull()
    expect(document.querySelector('.two-pane-side')).toBeNull()
    await act(async () => {
      screen.getByText('gui.plug.retry').click()
    })
    expect(await screen.findByText('shipped the island')).toBeTruthy()
  })

  /* Beside the list rather than in the shared drawer: inside the settings
     dialog a drawer is a layer over a layer, and the list it covered is what a
     reader comparing two memories needs to keep. */
  it('shows the picked memory beside the list, and drops it on a kind switch', async () => {
    install()
    await mount()
    await act(async () => {
      row('shipped the island').click()
    })
    expect(screen.getByText('gui.mem.sec_detail')).toBeTruthy()
    expect(screen.getByText('the long form of the episode')).toBeTruthy()
    /* The pick belongs to the kind that was listed, so switching kind has to
       drop it -- otherwise the reader is left reading a row from a tab they
       have left. */
    await act(async () => {
      screen.getByText('gui.mem.tab_case').click()
    })
    expect(screen.queryByText('gui.mem.sec_detail')).toBeNull()
  })

  it('says to pick one until a row is picked', async () => {
    install()
    await mount()
    expect(await screen.findByText('gui.mem.pick')).toBeTruthy()
  })

  it('switches kind through a stat and reloads with it', async () => {
    const asked: string[] = []
    install({
      list: async (req) => {
        asked.push(req.kind)
        return { items: [], total: 0 }
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.mem.tab_case').click()
    })
    expect(asked).toContain('agent_case')
  })

  /* Which EverOS the section reads. The switch is a settings write: the section
     follows the file per read, and the loop serving chat memory follows
     through applyRuntime without a reload. */
  describe('memory source switch', () => {
    const both = {
      episodes: 1,
      profiles: 0,
      agent_cases: 0,
      agent_skills: 0,
      backend: 'everos',
      available_backends: ['everos', 'everos-cloud'],
    }

    it('marks the backend the file names and enables the one this install has', async () => {
      install({}, both)
      await mount()
      expect(screen.getByText('gui.mem.src_local').getAttribute('aria-pressed')).toBe('true')
      expect(screen.getByText('gui.mem.src_cloud').getAttribute('aria-pressed')).toBe('false')
      expect((screen.getByText('gui.mem.src_cloud') as HTMLButtonElement).disabled).toBe(false)
    })

    it('writes the other backend, rereads the store and points the loop at it', async () => {
      const writes: string[] = []
      const settingsRefresh = vi.spyOn(settingsDialog, 'requestRefresh').mockImplementation(() => {})
      const runtime = vi.fn(async () => ({ ok: true }))
      install(
        {
          stats: async () => ({ ...both, backend: writes.length ? 'everos-cloud' : 'everos' }),
          setBackend: async (name: string) => {
            writes.push(name)
            return { applied: true }
          },
          applyRuntime: runtime,
        },
        both,
      )
      await mount()
      await act(async () => {
        ;(screen.getByText('gui.mem.src_cloud') as HTMLButtonElement).click()
      })
      expect(writes).toEqual(['everos-cloud'])
      /* The settings dialog owes the reader the Cloud card after this switch;
         `selected` is part of the snapshot it cached at open. */
      expect(settingsRefresh).toHaveBeenCalled()
      /* The write is the apply: the settings writer points the running loop at
         the file, so the section does not apply it a second time -- and no
         generation reload happens, so the socket stays up. */
      expect(runtime).not.toHaveBeenCalled()
      expect(screen.queryByText('gui.mem.reload_wait')).toBeNull()
      expect(screen.getByText('gui.mem.src_cloud').getAttribute('aria-pressed')).toBe('true')
    })

    /* The old store's numbers and rows belong to the backend the reader just
       left. They used to sit under the new segment until the new read landed,
       which is what made the switch look stuck. */
    it('clears the old store and shows the wait while the new backend is read', async () => {
      let backend = 'everos'
      let land: (() => void) | null = null
      install(
        {
          stats: async () => ({ ...both, backend }),
          list: async () => ({ items: [item()], total: 1 }),
          setBackend: (name: string) => new Promise<{ applied: boolean }>((resolve) => {
            land = () => {
              backend = name
              resolve({ applied: true })
            }
          }),
        },
        both,
      )
      await mount()
      expect(await screen.findByText('shipped the island')).toBeTruthy()
      await act(async () => {
        ;(screen.getByText('gui.mem.src_cloud') as HTMLButtonElement).click()
        await Promise.resolve()
      })
      expect(screen.getByText('gui.mem.switching')).toBeTruthy()
      expect(screen.queryByText('shipped the island')).toBeNull()
      expect(document.querySelector('.two-pane-wait .two-pane-row')).toBeTruthy()
      expect(screen.getByText('gui.mem.src_cloud').getAttribute('aria-pressed')).toBe('true')
      await act(async () => { land!(); await Promise.resolve(); await Promise.resolve() })
      expect(screen.queryByText('gui.mem.switching')).toBeNull()
      expect(await screen.findByText('shipped the island')).toBeTruthy()
    })

    it('says the switch waits when the gateway has a turn in flight', async () => {
      const writes: string[] = []
      const queued = 'Saved. A turn is running; the memory backend switches when that turn finishes.'
      install(
        {
          stats: async () => ({ ...both, backend: writes.length ? 'everos-cloud' : 'everos' }),
          setBackend: async (name: string) => {
            writes.push(name)
            return { applied: true, warning: queued }
          },
        },
        both,
      )
      await mount()
      await act(async () => {
        ;(screen.getByText('gui.mem.src_cloud') as HTMLButtonElement).click()
      })
      expect(await screen.findByText(queued)).toBeTruthy()
    })

    it('puts the switch back when the write is refused, and keeps the rows readable', async () => {
      install(
        {
          stats: async () => both,
          setBackend: async () => {
            throw new Error('switch denied')
          },
        },
        both,
      )
      await mount()
      await act(async () => {
        ;(screen.getByText('gui.mem.src_cloud') as HTMLButtonElement).click()
      })
      expect(await screen.findByText('switch denied')).toBeTruthy()
      /* The optimistic pick is a claim the file did not back; the next read
         puts the segment where the store still is, and the section keeps the
         rows it had rather than taking the whole frame over. */
      await waitFor(() => {
        expect(screen.getByText('gui.mem.src_local').getAttribute('aria-pressed')).toBe('true')
      })
      expect(await screen.findByText('shipped the island')).toBeTruthy()
      expect(screen.queryByText('gui.mem.err_t')).toBeNull()
    })

    it('shows a backend whose plugin is missing as disabled, not as absent', async () => {
      install({}, { ...both, available_backends: ['everos'] })
      await mount()
      const cloud = screen.getByText('gui.mem.src_cloud') as HTMLButtonElement
      expect(cloud.disabled).toBe(true)
      expect(cloud.title).toBe('gui.mem.src_missing')
    })

    it('keeps the switch reachable while the selected backend has nothing to read', async () => {
      const note = 'the everos-cloud-memory distribution is not installed'
      install(
        { list: async () => ({ items: [], total: 0, note }) },
        { ...both, backend: 'everos-cloud', available_backends: ['everos'] },
      )
      await mount()
      expect(await screen.findByText(note)).toBeTruthy()
      expect(screen.getByText('gui.mem.src_local')).toBeTruthy()
    })

    /* A working key can still be replaced. Without this link the only way to
       change it would be the settings card the section no longer draws. */
    it('offers a change-key link when the cloud store is readable, and opens the panel', async () => {
      install({}, { ...both, backend: 'everos-cloud', cloud_key_set: true })
      await mount()
      expect(await screen.findByText('shipped the island')).toBeTruthy()
      expect(screen.getByText('gui.mem.cloud_change')).toBeTruthy()
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.cloud_change'))
      })
      expect(screen.getByText('gui.mem.cloud_change_t')).toBeTruthy()
      expect(screen.getByLabelText('gui.mem.cloud_ph')).toBeTruthy()
    })

    /* The cloud backend with no key: every read answers 401, so the store's
       error frame used to blame the service for a step the reader had not
       taken. The key panel takes the column instead, and nothing else. */
    it('asks for the cloud key in place of the store, with no error frame under it', async () => {
      install(
        {
          list: async () => {
            throw new Error('everos refused the request: Missing authorization header')
          },
        },
        { ...both, backend: 'everos-cloud', cloud_key_set: false },
      )
      await mount()
      expect(await screen.findByText('gui.mem.cloud_t')).toBeTruthy()
      expect(screen.getByText('gui.mem.cloud_sub')).toBeTruthy()
      expect(screen.getByText('gui.mem.cloud_save')).toBeTruthy()
      expect(screen.queryByText('gui.mem.err_t')).toBeNull()
      expect(screen.queryByText('gui.plug.retry')).toBeNull()
    })

    it('writes the key, then rereads the store so the panel goes away', async () => {
      const writes: string[] = []
      let keySet = false
      install(
        {
          stats: async () => ({ ...both, backend: 'everos-cloud', cloud_key_set: keySet }),
          list: async () => ({ items: [item()], total: 1 }),
          setCloudKey: async (key: string) => {
            writes.push(key)
            keySet = true
            return { applied: true }
          },
        },
        { ...both, backend: 'everos-cloud', cloud_key_set: false },
      )
      await mount()
      expect(await screen.findByText('gui.mem.cloud_t')).toBeTruthy()
      await act(async () => {
        fireEvent.change(screen.getByLabelText('gui.mem.cloud_ph'), { target: { value: 'ecm-123456' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.cloud_save'))
      })
      expect(writes).toEqual(['ecm-123456'])
      expect(await screen.findByText('shipped the island')).toBeTruthy()
      expect(screen.queryByText('gui.mem.cloud_t')).toBeNull()
    })

    /* A key that saves but does not work is the common typo. The write makes
       `cloud_key_set` true either way, so writing alone would trade the panel
       for a store that cannot be read; the probe is what keeps the reader
       here, with the service's own sentence. */
    it('keeps the panel and shows the service sentence when the key is rejected', async () => {
      install(
        {
          stats: async () => ({ ...both, backend: 'everos-cloud', cloud_key_set: false }),
          list: async () => ({ items: [item()], total: 1 }),
          setCloudKey: async () => ({ applied: true }),
          probeCloud: async () => ({ status: 'missing', hint: 'API key rejected (401)' }),
        },
        { ...both, backend: 'everos-cloud', cloud_key_set: false },
      )
      await mount()
      await act(async () => {
        fireEvent.change(screen.getByLabelText('gui.mem.cloud_ph'), { target: { value: 'ecm-wrong' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.cloud_save'))
      })
      expect(await screen.findByText('API key rejected (401)')).toBeTruthy()
      expect(screen.getByText('gui.mem.cloud_t')).toBeTruthy()
      expect(screen.queryByText('shipped the island')).toBeNull()
      expect(screen.queryByText('gui.mem.err_t')).toBeNull()
    })

    /* A bad key already on file has no Save click to hang the panel on: the
       section opens, the store answers 401, and the reader still needs the
       field rather than an "unreachable service" frame. */
    it('reopens the key panel when the key on file is refused', async () => {
      install(
        {
          stats: async () => ({ ...both, backend: 'everos-cloud', cloud_key_set: true }),
          list: async () => {
            throw {
              data: {
                detail:
                  'internal_error: everos refused the request: The API key in the request is missing or invalid. Request id: abc',
              },
            }
          },
        },
        { ...both, backend: 'everos-cloud', cloud_key_set: true },
      )
      await mount()
      expect(await screen.findByText('gui.mem.cloud_bad_key')).toBeTruthy()
      expect(screen.getByText('gui.mem.cloud_change_t')).toBeTruthy()
      expect(screen.queryByText('gui.mem.err_t')).toBeNull()
    })

    /* Retrying from the refused state used to clear the flag that keeps the
       panel up, so the section flashed to the store for the length of the check
       and back. The panel must stay put while the verdict is pending. */
    it('keeps the panel up while a refused key is checked again', async () => {
      let land: ((v: { status: string; hint: string }) => void) | null = null
      install(
        {
          stats: async () => ({ ...both, backend: 'everos-cloud', cloud_key_set: true }),
          list: async () => {
            throw {
              data: {
                detail:
                  'internal_error: everos refused the request: The API key in the request is missing or invalid. Request id: abc',
              },
            }
          },
          setCloudKey: async () => ({ applied: true }),
          probeCloud: () => new Promise<{ status: string; hint: string }>((resolve) => { land = resolve }),
        },
        { ...both, backend: 'everos-cloud', cloud_key_set: true },
      )
      await mount()
      expect(await screen.findByText('gui.mem.cloud_bad_key')).toBeTruthy()
      await act(async () => {
        fireEvent.change(screen.getByLabelText('gui.mem.cloud_ph'), { target: { value: 'ecm-again' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.cloud_save'))
        await Promise.resolve()
      })
      expect(screen.getByText('gui.mem.cloud_change_t')).toBeTruthy()
      expect(screen.getByText('gui.mem.cloud_checking')).toBeTruthy()
      await act(async () => { land!({ status: 'missing', hint: 'still rejected' }); await Promise.resolve() })
      expect(screen.getByText('still rejected')).toBeTruthy()
      expect(screen.getByText('gui.mem.cloud_change_t')).toBeTruthy()
    })

    it('shows the check in flight on the save button', async () => {
      let land: ((v: { status: string }) => void) | null = null
      install(
        {
          stats: async () => ({ ...both, backend: 'everos-cloud', cloud_key_set: false }),
          setCloudKey: async () => ({ applied: true }),
          probeCloud: () => new Promise<{ status: string }>((resolve) => { land = resolve }),
        },
        { ...both, backend: 'everos-cloud', cloud_key_set: false },
      )
      await mount()
      await act(async () => {
        fireEvent.change(screen.getByLabelText('gui.mem.cloud_ph'), { target: { value: 'ecm-123456' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.cloud_save'))
        await Promise.resolve()
      })
      expect(screen.getByText('gui.mem.cloud_checking')).toBeTruthy()
      await act(async () => { land!({ status: 'ok' }); await Promise.resolve() })
    })

    /* The local server is a process, not a setting: when it is not there the
       section used to show four zeros. The start panel is the one action that
       brings the store back without restarting Raven. */
    it('offers to start the local server when it is not running', async () => {
      let running = false
      const starts = vi.fn(async () => {
        running = true
        return { ok: true, running: true, base_url: 'http://localhost:18791' }
      })
      install(
        {
          stats: async () => ({ ...both, backend: 'everos', local_running: running }),
          list: async () => ({ items: [item()], total: 1 }),
          startLocal: starts,
        },
        { ...both, backend: 'everos', local_running: false },
      )
      await mount()
      expect(await screen.findByText('gui.mem.local_t')).toBeTruthy()
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.local_start'))
      })
      expect(starts).toHaveBeenCalled()
      expect(await screen.findByText('shipped the island')).toBeTruthy()
      expect(screen.queryByText('gui.mem.local_t')).toBeNull()
    })

    /* Start is the loop's switch to a freshly started local backend: the
       server comes up through that backend's own start, so it is one call.
       A second applyRuntime would build and start the backend again and retire
       the one that just started the server. A successful Start also clears the
       notice that said the file and the loop disagreed. */
    it('starts through one call that also points the loop, and clears the disagreement', async () => {
      let running = false
      let selected = 'everos-cloud'
      const applied = vi.fn(async () => ({ ok: true }))
      install(
        {
          stats: async () => ({ ...both, backend: selected, local_running: running }),
          setBackend: async (name: string) => {
            selected = name
            return { applied: true, warning: 'applies at the next start' }
          },
          startLocal: async () => {
            running = true
            return { ok: true, running: true, base_url: 'http://localhost:18791', state: 'applied' }
          },
          applyRuntime: applied,
        },
        { ...both, backend: 'everos-cloud', local_running: false },
      )
      await mount()
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.src_local'))
      })
      expect(await screen.findByText('applies at the next start')).toBeTruthy()
      running = false
      await act(async () => {
        fireEvent.click(await screen.findByText('gui.mem.local_start'))
      })
      await waitFor(() => expect(screen.queryByText('applies at the next start')).toBeNull())
      expect(applied).not.toHaveBeenCalled()
    })

    /* A turn running when Start is pressed holds the switch until it ends, and
       the server with it: that is a wait, not a failure, and not a success. */
    it('says when a Start waits for the running turn', async () => {
      const sentence = 'A turn is running; the local EverOS starts when that turn finishes.'
      install(
        {
          stats: async () => ({ ...both, backend: 'everos', local_running: false }),
          startLocal: async () => ({ ok: false, running: false, base_url: '', detail: sentence, state: 'queued' }),
        },
        { ...both, backend: 'everos', local_running: false },
      )
      await mount()
      await act(async () => {
        fireEvent.click(await screen.findByText('gui.mem.local_start'))
      })
      const said = await screen.findByText(sentence)
      expect(said.className).not.toContain('memerr')
      expect(screen.queryByText('gui.mem.local_failed')).toBeNull()
    })

    /* A root the user manages is theirs to start: the backend's own start never
       spawns it and says so through notify, and that sentence is the reply's
       detail. The section shows it as written -- whose step it is -- rather
       than a refusal from the plugin's write guard. */
    it('shows what the backend said when Start cannot bring the server up', async () => {
      const said = 'Long-term memory is off: the EverOS you manage is not running at http://127.0.0.1:18791. '
        + 'Start it yourself and Raven will use it; Raven does not start or stop it.'
      install(
        {
          stats: async () => ({ ...both, backend: 'everos', local_running: false }),
          startLocal: async () => ({ ok: false, running: false, base_url: '', detail: said, state: 'applied' }),
        },
        { ...both, backend: 'everos', local_running: false },
      )
      await mount()
      await act(async () => {
        fireEvent.click(await screen.findByText('gui.mem.local_start'))
      })
      expect(await screen.findByText(said)).toBeTruthy()
      expect(screen.getByText('gui.mem.local_start')).toBeTruthy()
    })

    /* The plugin says it never starts a root the user manages, so a Start there
       could only fail. The panel names the step as the user's own and offers
       the check that finds the server once they have taken it. */
    it('offers no Start where the plugin does not start the server', async () => {
      let running = false
      const starts = vi.fn(async () => ({ ok: false, running: false, base_url: '' }))
      install(
        {
          stats: async () => ({ ...both, backend: 'everos', local_running: running, local_startable: false }),
          list: async () => ({ items: [item()], total: 1 }),
          startLocal: starts,
        },
        { ...both, backend: 'everos', local_running: false, local_startable: false },
      )
      await mount()
      expect(await screen.findByText('gui.mem.local_foreign_sub')).toBeTruthy()
      expect(screen.queryByText('gui.mem.local_start')).toBeNull()
      running = true
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.local_recheck'))
      })
      expect(await screen.findByText('shipped the island')).toBeTruthy()
      expect(starts).not.toHaveBeenCalled()
    })

    /* A key saved while a turn runs reaches the loop when the turn ends. Read
       as done, the notice cleared and the section drew the cloud store as
       serving chat while the turn still used the old backend. */
    it('keeps the queued sentence when the key is applied behind a running turn', async () => {
      const sentence = 'A turn is running; the memory backend switches when that turn finishes.'
      let keySet = false
      install(
        {
          stats: async () => ({ ...both, backend: 'everos-cloud', cloud_key_set: keySet }),
          list: async () => ({ items: [item()], total: 1 }),
          setCloudKey: async () => {
            keySet = true
            return { applied: true }
          },
          applyRuntime: async () => ({ ok: true, backend: 'everos-cloud', detail: sentence, state: 'queued' }),
        },
        { ...both, backend: 'everos-cloud', cloud_key_set: false },
      )
      await mount()
      await act(async () => {
        fireEvent.change(screen.getByLabelText('gui.mem.cloud_ph'), { target: { value: 'ecm-123456' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByText('gui.mem.cloud_save'))
      })
      expect(await screen.findByText(sentence)).toBeTruthy()
    })

    /* The note is the one explanation of a selected backend whose plugin is
       gone: a key panel asks for a key for a client that cannot exist, and a
       Start button offers a process the install cannot run. memory.stats
       reports cloud_key_set/local_running false there too, so the panels must
       yield when available_backends says the backend is not importable. */
    it('keeps the missing-plugin note over the cloud key panel', async () => {
      const note = 'the everos-cloud-memory distribution is not installed'
      install(
        { list: async () => ({ items: [], total: 0, note }) },
        {
          ...both,
          backend: 'everos-cloud',
          available_backends: ['everos'],
          cloud_key_set: false,
          local_running: false,
          note,
        },
      )
      await mount()
      expect(await screen.findByText(note)).toBeTruthy()
      expect(screen.queryByText('gui.mem.cloud_t')).toBeNull()
    })

    it('keeps the missing-plugin note over the local start panel', async () => {
      const note = 'the everos-memory distribution is not installed'
      install(
        { list: async () => ({ items: [], total: 0, note }) },
        {
          ...both,
          backend: 'everos',
          available_backends: ['everos-cloud'],
          cloud_key_set: false,
          local_running: false,
          note,
        },
      )
      await mount()
      expect(await screen.findByText(note)).toBeTruthy()
      expect(screen.queryByText('gui.mem.local_t')).toBeNull()
    })
  })

  it('keeps its rendered shape', async () => {
    install(
      { list: async () => ({ items: [item(), item({ id: 'm2', subject: 'fixed the flake' })], total: 2 }) },
      { episodes: 12, profiles: 1, agent_cases: 3, agent_skills: 4 },
    )
    await mount()
    await screen.findByText('shipped the island')
    expect(domSnapshot(document.getElementById('memoryBody')!)).toMatchSnapshot()
  })
})

/* A switch is asked about, not assumed. The rebind waits for a turn to end
   itself, but a reader who clicks mid-conversation is told that before
   anything is written -- and can back out. */
describe('a switch while a conversation is in progress', () => {
  const both = {
    episodes: 1,
    profiles: 0,
    agent_cases: 0,
    agent_skills: 0,
    backend: 'everos',
    available_backends: ['everos', 'everos-cloud'],
  }

  it('asks first, writes nothing until the answer, then switches', async () => {
    const writes: string[] = []
    const asked: Array<{ title: string; body: string; label: string; fn: () => void }> = []
    install({
      stats: async () => both,
      switchStatus: async () => ({ active: true, turns: 1 }),
      setBackend: async (name: string) => {
        writes.push(name)
        return { applied: true }
      },
    })
    vi.spyOn(confirmStore, 'ask').mockImplementation((title, body, label, fn) => {
      asked.push({ title, body, label, fn })
    })

    await mount()
    await act(async () => {
      store.switchBackend('everos-cloud')
    })
    await waitFor(() => expect(asked).toHaveLength(1))

    expect(asked[0]!.title).toBe('gui.mem.switch_busy_t')
    expect(asked[0]!.body).toBe('gui.mem.switch_busy_sub')
    expect(asked[0]!.label).toBe('gui.mem.switch_confirm')
    expect(writes).toEqual([])
    expect(store.get().stats?.backend).toBe('everos')

    await act(async () => {
      asked[0]!.fn()
    })
    await waitFor(() => expect(writes).toEqual(['everos-cloud']))
  })

  it('switches without a question when the status probe cannot answer', async () => {
    const writes: string[] = []
    install({
      stats: async () => both,
      switchStatus: async () => {
        throw new Error('an older gateway')
      },
      setBackend: async (name: string) => {
        writes.push(name)
        return { applied: true }
      },
    })

    await mount()
    await act(async () => {
      store.switchBackend('everos-cloud')
    })

    await waitFor(() => expect(writes).toEqual(['everos-cloud']))
  })

  /* The notice is the only sign that the file and the running loop disagree;
     entering the section again re-reads the file, so it must not erase it. */
  it('keeps a failed switch notice across a re-entry of the section', async () => {
    install({ setBackend: async () => ({ applied: true, warning: 'the loop is not available' }) }, both)
    await mount()
    await act(async () => {
      ;(screen.getByText('gui.mem.src_cloud') as HTMLButtonElement).click()
    })
    expect(await screen.findByText('the loop is not available')).toBeTruthy()

    await act(async () => {
      settingsDialog.enterSection('memory')
      await Promise.resolve()
    })
    expect(store.get().backendNotice).toBe('the loop is not available')
  })
})
