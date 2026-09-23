/* The workspace tag beside a conversation's title: the folder it runs in.
 *
 * The composer's workspace chip (./WorkdirChip.tsx) is gone once a
 * conversation starts, because the folder cannot change any more -- but which
 * folder it is stays worth knowing, and the title row is where a fact about
 * the whole conversation belongs. Drawn for every conversation: the folder's
 * last segment, or the word for the default, so a reader is never left
 * guessing whether a missing tag means the default or a tag that failed to
 * draw. A draft has the chip on the bar instead.
 *
 * The whole of it is state/workdir.ts's `paint`, which the rail's draw fills
 * on every change of the conversation on screen: the word, and the path (or
 * the default's sentence) on the title.
 */

import { useSyncExternalStore } from 'react'

import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as wd from '../state/workdir'
import { FOLDER } from './WorkdirChip'

import type { JSX } from 'react'

export function WorkdirTag(): JSX.Element {
  const s = useSyncExternalStore(wd.subscribe, wd.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const p = s.paint
  const on = !!p && p.locked
  return (
    <span
      className={on && !p.set ? 'chrome-wd-tag chrome-wd-default' : 'chrome-wd-tag'}
      id="wdTag"
      hidden={!on}
      title={on ? p.title : undefined}
      aria-label={on ? `${t('gui.wd.title')}: ${p.label}` : undefined}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d={FOLDER} />
      </svg>
      <span>{on ? p.label : ''}</span>
    </span>
  )
}
