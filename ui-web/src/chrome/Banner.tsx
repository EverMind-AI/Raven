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

import { t } from '../i18n/t'
import * as banner from '../state/banner'

import type { JSX } from 'react'

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
      {/* No way through from here any more: the button opened the plugins
          page, and the entry on it, and both are gone. The notice still says
          the true thing -- web search is not configured -- and configuring it
          is a config-file edit. */}
      {/* Dismissable, unlike the fault above: an unconfigured capability is a
          suggestion, and the reader saying "not now" is an answer. */}
      <button className="x" aria-label={t('gui.ws.notice_dismiss')} onClick={() => banner.dismiss()}>&#10005;</button>
    </div>
  )
}
