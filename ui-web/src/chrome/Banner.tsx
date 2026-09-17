/* The notice standing above the transcript, rendered inside #bannerHost (the
 * scroller's first ground, src/chrome/ChatTop.tsx).
 *
 * Which notice stands is src/state/banner.ts's: a memory fault beats an
 * unconfigured capability, and whichever wins draws alone. This is the pair of
 * shapes that decision picks between.
 *
 * The words are read at render time, the way the writer this replaces read them
 * at draw time, so a language applied after a notice went up does not move it
 * until something asks for the notice again -- which is what happens today, and
 * why nothing here subscribes to the language.
 */

import { useSyncExternalStore } from 'react'

import * as banner from '../state/banner'
import { openPlugins } from '../features/plugins/nav'
import { t } from '../i18n/t'
import { openDetail } from '../features/plugins/store'

import type { JSX } from 'react'

/* One verb for one action: this button opens the plugins page AND the websearch
   entry on it, and a reader who lands on the page without the entry open has to
   hunt for what the notice was talking about. */
function openWebsearch(): void {
  void openPlugins()
  openDetail('market', 'websearch')
}

export function Banner(): JSX.Element | null {
  const s = useSyncExternalStore(banner.subscribe, banner.get)
  if (s.kind === 'fault') {
    /* No dismiss: the condition lasts until it is fixed, and a banner the
       reader can wave away is one they will wave away and then forget. */
    return (
      <div className="banner bad">
        <b>{t('gui.mem.down')}</b>
        <span>{s.detail}</span>
      </div>
    )
  }
  if (s.kind !== 'websearch') return null
  return (
    <div className="banner">
      <b>{t('gui.ws.notice_title')}</b>
      <span>{t('gui.ws.notice_body')}</span>
      <button onClick={() => openWebsearch()}>{t('gui.ws.notice_go')}</button>
      {/* Dismissable, unlike the fault above: an unconfigured capability is a
          suggestion, and the reader saying "not now" is an answer. */}
      <button className="x" aria-label={t('gui.ws.notice_dismiss')} onClick={() => banner.dismiss()}>&#10005;</button>
    </div>
  )
}
