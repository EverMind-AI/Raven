/* Picking the page's language: the persist behind a pick, and the two reads
 * that apply one at boot.
 *
 * state/lang/store.ts moves the language and the catalogue together and tells
 * everything that draws itself to draw again; what
 * is here is the half that talks to the gateway -- one key, both front ends,
 * because `config.language` also drives the TUI (which polls it) and the
 * language the agent replies in.
 */

import { code as LANG, T } from '../../i18n/t'
import { gateway } from '../../rpc/gateway'
import { show as toast } from '../toast'
import * as lang from './store'

/* The language the gateway last agreed to, kept where a page that cannot reach
   it can still read it. `load` runs only after the connect succeeds, so on a
   failed connect nothing would set the language at all -- and the sign-in
   notice, the one message that explains the empty page, would arrive in English
   on a Chinese install. Wrapped like the look settings, because private mode
   throws on access rather than answering null. */
const LANG_KEY = 'raven.gui.lang'

export function remember(v: string): void {
  try { localStorage.setItem(LANG_KEY, v) } catch { /* private mode */ }
}

/* Applied before the socket is up. Whatever `load` resolves afterwards wins, so
   a language changed elsewhere still lands on this boot. The quiet setter
   because this runs ahead of the first data-driven paint: there is nothing
   drawn from JavaScript to redraw yet, and this is the one language call site
   that has never asked for one. */
export function restore(): void {
  try {
    const v = localStorage.getItem(LANG_KEY)
    if (v === 'en' || v === 'zh') lang.setQuiet(v)
  } catch { /* private mode */ }
}

export async function load(): Promise<void> {
  try {
    const r = await gateway().call('config.get', { keys: ['language'] })
    const v = r && r.config && (r.config as Record<string, unknown>).language
    if (v === 'en' || v === 'zh') { lang.set(v); remember(v) }
  } catch { /* stay on the built-in default */ }
}

/* The pick the settings dialog's radio makes. `lang.set` moves the language and
   the catalogue together and notifies its subscribers; what is added here is
   the persist and the rollback.

   The comparison is against the catalogue's column rather than against the
   store's `applied`, which is null until the first pick: a page nobody has
   picked for renders English text over Chinese markup, and asking for English
   on it has never been a flip. */
export async function pick(next: lang.Lang, { persist }: { persist?: boolean } = {}): Promise<void> {
  if (next === LANG) return
  const prev = LANG
  lang.set(next)
  if (!persist) return
  try {
    await gateway().call('config.set', { key: 'language', value: next })
    remember(next)
  } catch (e) {
    /* Put it back rather than leaving the page in a language the gateway does
       not agree with -- the same key drives the TUI and the agent's replies. */
    lang.set(prev)
    const err = e as { data?: { detail?: string }; message?: string }
    toast(T('gui.op.lang_failed', { detail: (err.data && err.data.detail) || err.message || String(e) }))
  }
}
