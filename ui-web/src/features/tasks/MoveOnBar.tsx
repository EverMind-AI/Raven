/* The Move on bar, over the composer: the Task Group the reader's next message
 * moves on -- its title, id and summary -- in the conversation it was picked in,
 * with a cross that puts it away. What it holds is the tasks store's (`moveOn`);
 * the send tells the server which group and spends it (src/app/install.ts lends
 * that), so the bar goes with the message it was about.
 */

import { Cancel01Icon } from '@hugeicons/core-free-icons'
import { useEffect, useState, useSyncExternalStore } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { current as sessionCurrent, onChange } from '../../lib/session'
import * as lang from '../../state/lang'
import * as store from './store'

import type { JSX } from 'react'

function useOpenConversation(): string | null {
  const [key, setKey] = useState(sessionCurrent)
  useEffect(() => {
    const off = onChange(setKey)
    return () => { off() }
  }, [])
  return key
}

export function MoveOnBar(): JSX.Element | null {
  useSyncExternalStore(lang.subscribe, lang.get)
  useSyncExternalStore(store.subscribe, store.get)
  const held = store.moveOnFor(useOpenConversation())
  if (!held) return null
  return (
    <div className="tkmoveon" role="note" aria-label={t('gui.tasks.board.move_on_bar')}>
      <span className="tkmoveonbody">
        <span className="tkmoveonhead">
          <span className="tkmoveonlab">{t('gui.tasks.board.move_on')}</span>
          <span className="tkmoveontitle">{held.title}</span>
          <span className="tkmoveonid">{held.groupId}</span>
        </span>
        {held.summary ? <span className="tkmoveonsum">{held.summary}</span> : null}
      </span>
      <button type="button" className="tkmoveonx" aria-label={t('gui.tasks.board.move_on_clear')} onClick={store.clearMoveOn}>
        <Icon icon={Cancel01Icon} size={13} />
      </button>
    </div>
  )
}
