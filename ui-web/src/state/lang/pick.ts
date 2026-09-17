/* Picking the page's language: the persist behind a pick, and the two reads
 * that apply one at boot.
 *
 * state/lang/store.ts moves the language and the catalogue together and tells
 * everything that draws itself to draw again; what
 * is here is the half that talks to the gateway -- one key, both front ends,
 * because `config.language` also drives the TUI (which polls it) and the
 * language the agent replies in.
 */

import { t } from '../../i18n/t'
import { gateway } from '../../rpc/gateway'
import { show as toast } from '../toast'
import * as lang from './store'

/* The language the gateway last agreed to, kept where a page that cannot reach
   it can still read it. `load` runs only after the connect succeeds, so on a
   failed connect nothing would set the language at all -- and the sign-in
   notice, the one message that explains the empty page, would arrive in English
   on a Chinese install. The store reads this key back as it loads, which is how
   a remembered pick is in force before the first frame; what is here is the
   write. Wrapped like the look settings, because private mode throws on access
   rather than answering null. */
const LANG_KEY = 'raven.gui.lang'

export function remember(v: string): void {
  try { localStorage.setItem(LANG_KEY, v) } catch { /* private mode */ }
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

   A pick for the language already in force is not a flip and repaints nothing:
   the page resolved one before its first frame (state/lang/store.ts), so the
   two answers cannot differ any more than they agree. */
export async function pick(next: lang.Lang, { persist }: { persist?: boolean } = {}): Promise<void> {
  if (next === lang.get().lang) return
  const prev = lang.get().lang
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
    toast(t('gui.op.lang_failed', { detail: (err.data && err.data.detail) || err.message || String(e) }))
  }
}
