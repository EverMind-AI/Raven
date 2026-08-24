/* The default-model picker: what is open, and what picking one means.
 *
 * The popover is one node over the whole page rather than a page's own root, so
 * a single React root lives at the body and renders nothing while the picker is
 * closed. Opening is a call, not a route: the composer's chip and the settings
 * island both ask for it, and the settings island passes a callback because it
 * paints the chosen model in its own tree.
 */

import { ds, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type { ModelSource, Provider } from './types'

export interface OpenAt {
  /* What the popover is anchored to and must not close on a click inside.
     Null while closed. */
  host: HTMLElement | null
  /* Called after every local change to the pick, forward or rolled back, so a
     caller painting the model in its own tree stays in step. */
  after: (() => void) | null
  /* The footer only appears for the composer chip. The settings island's own
     button is already on the settings page. */
  footer: boolean
}

const CLOSED: OpenAt = { host: null, after: null, footer: false }

let at: OpenAt = CLOSED
let epoch = 0
let selected = 'minimax-m3'
const subs = new Set<() => void>()

export const source = (): ModelSource => ds<ModelSource>('model')

/* Installed by the live layer only. The offline demo's chip opens a plain menu
   of its own (demo/150-chrome.js), so the opener below has to be callable and
   do nothing there rather than throw at a name the page publishes. */
const installed = (): boolean => !!(window.DS && window.DS.model)

export function subscribe(fn: () => void): () => void {
  subs.add(fn)
  return () => subs.delete(fn)
}

const announce = (): void => {
  epoch += 1
  subs.forEach((fn) => fn())
}

export const openAt = (): OpenAt => at
export const version = (): number => epoch
export const isOpen = (): boolean => !!at.host
export const current = (): string => selected

export function setCurrent(model: string): void {
  if (selected === model) return
  selected = model
  announce()
}

/* Every open replaces the one before it rather than stacking: the chip and the
   settings button can both be reached while a picker is up. */
export function open(anchor?: HTMLElement | null, after?: () => void): void {
  if (!installed()) return
  const authed = source()
    .providers()
    .filter((p) => p.on && p.models.length)
  if (!authed.length) {
    toast(t('gui.picker.no_account'))
    return
  }
  const host = anchor || document.getElementById('modelChip')
  if (!host) return
  at = { host, after: after || null, footer: !anchor }
  announce()
}

export function close(): void {
  if (!at.host) return
  at = CLOSED
  announce()
}

/* Only providers with an account and something to offer. Exported because the
   list decides both columns and the initial selection. */
export const authed = (): Provider[] => source().providers().filter((p) => p.on && p.models.length)

/* Optimistic: the chip has to say the new model before the round trip, because
   the next turn already uses it. A rejected write puts the old one back and
   says so rather than leaving the page claiming a model the config never took. */
export async function choose(m: string): Promise<void> {
  const src = source()
  const prev = current()
  const after = at.after
  close()
  setCurrent(m)
  after?.()
  try {
    await src.persist(m)
    toast(`已切换到 ${short(m)}`)
  } catch (e) {
    setCurrent(prev)
    after?.()
    toast(`切换失败：${detail(e)}`)
  }
}

/* Provider-qualified names arrive as `vendor/model`; the page has never shown
   the vendor half, which the provider column already says. */
export const short = (m: string): string => String(m || '').split('/').pop() as string

const detail = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string } | null
  return err?.data?.detail || err?.message || String(e)
}

export function _resetForTests(): void {
  at = CLOSED
  epoch = 0
  selected = 'minimax-m3'
  subs.clear()
}
