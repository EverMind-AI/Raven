/* The working-directory chip in the bar under the field, and the click that
 * opens its popover.
 *
 * The whole of it is state/workdir.ts's `paint`: the folder's name (or the
 * word for the default), the path on its title, whether a folder is named at
 * all, and whether the reader may still change it. On a draft the chip is a
 * live control; in a conversation it is a report -- disabled, because the
 * engine takes a working directory only at the create -- and the title carries
 * the path and says so. aria-expanded is the popover's up-or-down.
 *
 * Until the first draw the chip carries the catalogue's word for the default,
 * because there is no served literal here: the chip was not in the markup the
 * page used to be served with, so there is nothing for it to keep showing.
 *
 * The click toggles rather than opens, for the reason the permission chip's
 * does: the popover has no close button, and the chip is the way back out.
 */

import { useSyncExternalStore } from 'react'

import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as wd from '../state/workdir'

import type { JSX } from 'react'

/** A folder, in the same 24-unit outline the shield next door is drawn in. */
export const FOLDER = 'M3.5 7.5A2 2 0 0 1 5.5 5.5h4l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z'

export function WorkdirChip(): JSX.Element {
  const s = useSyncExternalStore(wd.subscribe, wd.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const p = s.paint
  return (
    <button
      className={p?.set ? 'chip chrome-wd-set' : 'chip'}
      id="wdChip"
      aria-expanded={s.open ? 'true' : 'false'}
      aria-haspopup="true"
      disabled={!!p?.locked}
      title={p ? p.title : undefined}
      aria-label={p ? `${t('gui.wd.title')}: ${p.label}` : undefined}
      onClick={() => wd.toggle()}
    >
      <svg className="pico" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d={FOLDER} />
      </svg>
      <span id="wdName">{p ? p.label : t('gui.wd.none')}</span>
    </button>
  )
}
