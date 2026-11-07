/* The registered machines, as the Agent Hub's machines tab reads and writes them.
 *
 * A machine is where an agent row may run (`machine` on the row); this store
 * holds the registry's rows with the state of each one's link and node, the
 * writes in flight on them, and the last refusal per row -- the same shape the
 * agents store keeps for its rows, so a card and its sheet read one thing.
 * Nothing here carries an address or a credential: the source hands over what
 * a machine is, never the way onto it, and the register form's fields travel
 * once, in `register`, and are not read back.
 */

import { t } from '../../i18n/t'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type { MachineProblem, MachineRow, RegisterForm, RegisterReason } from './types'

export interface MachinesState {
  rows: MachineRow[]
  /* A list read in flight, and whether the first one has landed: the tab
     draws a scanning placeholder before the first answer, not an empty
     message that would be wrong a moment later. */
  loading: boolean
  loaded: boolean
  /* A list that could not be read, one sentence, for the tab to show once. */
  loadFailed: string
  /* The write in flight on a machine, by id: `install` for a node install the
     server runs in the background (and this page polls), `probe` for a read
     that reaches the machine first, `remove` for a removal. */
  busy: Record<string, 'install' | 'probe' | 'remove'>
  /* The last refusal per machine, one sentence, shown where the card's state
     line is until the next write on it. */
  failed: Record<string, string>
  /* The register form's own write, and its refusal with the kind the page
     explains. */
  adding: boolean
  addFailed: string
  addReason: RegisterReason
  /* The doctor's last answer, or null before it was asked. */
  problems: MachineProblem[] | null
  /* The private keys this computer has, as `~/.ssh/<name>` paths, read once
     for the register form; null before the first answer. */
  keys: string[] | null
}

const EMPTY: MachinesState = {
  rows: [],
  loading: false,
  loaded: false,
  loadFailed: '',
  busy: {},
  failed: {},
  adding: false,
  addFailed: '',
  addReason: '',
  problems: null,
  keys: null,
}

const store = makeStore<MachinesState>(EMPTY)

export const get = store.get
export const subscribe = store.subscribe
export function set(patch: Partial<MachinesState>): void {
  store.set({ ...store.get(), ...patch })
}

const without = <V,>(map: Record<string, V>, key: string): Record<string, V> =>
  Object.fromEntries(Object.entries(map).filter(([k]) => k !== key))

const said = (e: unknown): string => {
  const detail = (e as { data?: { detail?: string } } | null)?.data?.detail
  return detail || (e instanceof Error ? e.message : String(e))
}

export function rowOf(id: string): MachineRow | undefined {
  return get().rows.find((r) => r.id === id)
}

/** Every machine with the state of its link; `refresh` reaches them first (all, or the one `id` names). */
export async function load(refresh = false, id?: string): Promise<void> {
  set({ loading: true })
  try {
    const rows = await ds('machines').list(refresh, id)
    set({ rows, loaded: true, loadFailed: '' })
  } catch (e) {
    /* A failed read keeps the rows it has; the refusal is the tab's to show
       once, not every card's. */
    set({ loadFailed: said(e), loaded: true })
  } finally {
    set({ loading: false })
  }
}

/** Reach the machine once with the form's login and write the row; true when it was written. */
export async function register(form: RegisterForm): Promise<boolean> {
  set({ adding: true, addFailed: '', addReason: '' })
  try {
    const got = await ds('machines').add(form)
    if (!got.ok) {
      set({ addFailed: got.detail, addReason: got.reason })
      return false
    }
    await load()
    return true
  } catch (e) {
    set({ addFailed: said(e), addReason: '' })
    return false
  } finally {
    set({ adding: false })
  }
}

let pollMs = 1500

/** Put this Raven's node on the machine; the gateway runs the install in the background and the list reports it.
 *
 * Polled for as long as the list says it runs, with no deadline of its own.
 * The fifteen-minute one it had kept an install that never ended from
 * being polled forever; but the gateway bounds every step of an install, so
 * one always ends and the list says how, and a deadline here can only stop
 * the polling first: measured 2026-10-22, it gave up two seconds before the
 * gateway recorded that a step had run out of its own fifteen minutes, and the
 * card spun on that last answer from then on, never told the install had
 * failed. */
export async function installNode(id: string, nodeDir = ''): Promise<void> {
  set({ busy: { ...get().busy, [id]: 'install' }, failed: without(get().failed, id) })
  try {
    const got = await ds('machines').installNode(id, nodeDir || undefined)
    if (!got.started) {
      set({ failed: { ...get().failed, [id]: t('gui.machine.install_bad_said', { said: got.detail }) }, busy: without(get().busy, id) })
      return
    }
  } catch (e) {
    set({ failed: { ...get().failed, [id]: t('gui.machine.install_bad_said', { said: said(e) }) }, busy: without(get().busy, id) })
    return
  }
  const poll = async (): Promise<void> => {
    await load()
    const row = rowOf(id)
    if (row && row.install.running) {
      setTimeout(() => void poll(), pollMs)
      return
    }
    set({ busy: without(get().busy, id) })
    if (row && row.install.ok === false) {
      set({ failed: { ...get().failed, [id]: t('gui.machine.install_bad_said', { said: row.install.detail }) } })
    }
  }
  setTimeout(() => void poll(), pollMs)
}

/** Reach this one machine and ask its node who it is: the one read that opens a link. */
export async function probe(id: string): Promise<void> {
  set({ busy: { ...get().busy, [id]: 'probe' }, failed: without(get().failed, id) })
  try {
    await load(true, id)
  } finally {
    set({ busy: without(get().busy, id) })
  }
}

/** The keys on this computer the register form proposes; a read that fails proposes none. */
export async function loadKeys(): Promise<void> {
  try {
    set({ keys: await ds('machines').localKeys() })
  } catch {
    set({ keys: [] })
  }
}

/** The registry's doctor, for every row at once. */
export async function doctor(): Promise<void> {
  try {
    set({ problems: await ds('machines').doctor() })
  } catch (e) {
    set({ loadFailed: said(e) })
  }
}

/** Take the machine out of the registry; true when it is gone. A refusal stays on the row. */
export async function remove(id: string): Promise<boolean> {
  const name = rowOf(id)?.name || id
  set({ busy: { ...get().busy, [id]: 'remove' }, failed: without(get().failed, id) })
  try {
    const got = await ds('machines').remove(id)
    if (!got.removed) {
      /* Said in the reader's language, with what to do: the entries to
         disconnect, by name; the agents to wait for; else the server's own
         sentence, which names a registry the page cannot mend. */
      const why = got.agents.length
        ? t('gui.machine.remove_named', { agents: got.agents.join(', ') })
        : /running on machine/.test(got.detail)
          ? t('gui.machine.remove_running', { said: got.detail })
          : t('gui.machine.remove_bad_said', { said: got.detail })
      set({ failed: { ...get().failed, [id]: why } })
      return false
    }
    toast(t('gui.machine.removed', { name }))
    await load()
    return true
  } catch (e) {
    set({ failed: { ...get().failed, [id]: t('gui.machine.remove_bad_said', { said: said(e) }) } })
    return false
  } finally {
    set({ busy: without(get().busy, id) })
  }
}

export function _resetForTests(pollAfterMs = 1500): void {
  pollMs = pollAfterMs
  store.set({ ...EMPTY, busy: {}, failed: {} })
}
