import { useState } from 'react'

import { t } from '../../i18n/t'
import * as store from './store'

import type { KbDoc, KbFolder } from './types'
import type { JSX } from 'react'

/* Where a document goes, picked from the folders there are.
 *
 * Its own sheet rather than a row on the page's shared menu, which is what
 * raised it: that menu takes a flat list of labels and runs one when it is
 * picked (state/menu.ts), and this needs a list with the current one ticked
 * and a field to name a folder that does not exist yet. Neither fits a row.
 *
 * Root leads the list and is always there, because Root is not a folder that
 * could be missing -- it is where a document with no folder already is.
 */
export function MoveSheet({ doc, folders }: { doc: KbDoc; folders: KbFolder[] }): JSX.Element {
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')

  /* Made and moved into in one gesture: a reader who names a folder here has
     said where this document goes, and leaving it in Root afterwards would be
     answering a different question. */
  const make = async (): Promise<void> => {
    const named = name.trim()
    if (!named) return
    await store.createFolder(named)
    const made = store.get().folders.find((row) => row.name === named)
    if (made) await store.move(doc, made.id)
    else store.openMove(null)
  }

  const rows: Array<{ id: string; name: string }> = [
    { id: '', name: t('gui.knowledge.root') },
    ...folders.map((row) => ({ id: row.id, name: row.name })),
  ]

  return (
    <div className="knowledge-scrim" onClick={() => store.openMove(null)}>
      <div className="knowledge-move" onClick={(e) => e.stopPropagation()}>
        <h2>{t('gui.knowledge.move')}</h2>
        <ul className="knowledge-movelist">
          {rows.map((row) => (
            <li key={row.id}>
              <button
                className={row.id === doc.folder_id ? 'knowledge-cur' : ''}
                aria-checked={row.id === doc.folder_id}
                role="menuitemradio"
                onClick={() => void store.move(doc, row.id)}
              >
                <span>{row.name}</span>
                {row.id === doc.folder_id && <span aria-hidden="true">&#10003;</span>}
              </button>
            </li>
          ))}
        </ul>
        {naming ? (
          <input
            className="knowledge-fnew"
            autoFocus
            placeholder={t('gui.knowledge.folder_name')}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void make()
              if (e.key === 'Escape') setNaming(false)
            }}
          />
        ) : (
          <button className="mini ghost knowledge-movenew" onClick={() => setNaming(true)}>
            {t('gui.knowledge.folder_new')}
          </button>
        )}
      </div>
    </div>
  )
}
