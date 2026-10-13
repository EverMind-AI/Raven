import { useState } from 'react'

import { t } from '../../i18n/t'
import * as menu from '../../state/menu'
import * as store from './store'

import type { KbFolder } from './types'
import type { JSX } from 'react'

/* The folders of one base, down the side of its documents.
 *
 * One level deep: Root and the folders under it, and nothing under those. A
 * folder here is a label a reader sorts by, and a tree of them is a filing
 * system with its own rules -- where a move into a descendant goes, how deep a
 * path may be -- none of which earns its keep for sorting a few dozen files.
 *
 * Root is not a record and never can be deleted or renamed, because it is not
 * a folder: it is what a document with no folder is in. That is also why every
 * document written before folders existed is already somewhere sensible.
 */

function FolderGlyph({ open }: { open: boolean }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      {open ? (
        <path d="M3.5 8.5V18a1.5 1.5 0 0 0 1.5 1.5h14a1.5 1.5 0 0 0 1.5-1.5V9.5A1.5 1.5 0 0 0 19 8h-7l-1.8-2.2A1.5 1.5 0 0 0 9 5.2H5A1.5 1.5 0 0 0 3.5 6.7z" />
      ) : (
        <path d="M3.5 6.7A1.5 1.5 0 0 1 5 5.2h4a1.5 1.5 0 0 1 1.2.6L12 8h7a1.5 1.5 0 0 1 1.5 1.5V18a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18z" />
      )}
    </svg>
  )
}

function Row({
  name,
  count,
  on,
  onPick,
  onMenu,
}: {
  name: string
  count: number
  on: boolean
  onPick: () => void
  onMenu?: (at: DOMRect) => void
}): JSX.Element {
  return (
    <div className={`knowledge-frow${on ? ' knowledge-cur' : ''}`}>
      <button className="knowledge-fname" onClick={onPick}>
        <FolderGlyph open={on} />
        <span title={name}>{name}</span>
      </button>
      <span className="knowledge-fcount">{count}</span>
      {onMenu && (
        <button
          className="knowledge-fdots"
          aria-haspopup="menu"
          aria-label={t('gui.knowledge.more')}
          onClick={(e) => onMenu(e.currentTarget.getBoundingClientRect())}
        >
          &#8943;
        </button>
      )}
    </div>
  )
}

export function FolderTree({
  folders,
  folder,
  rootCount,
}: {
  folders: KbFolder[]
  folder: string
  /** How many documents are in Root itself, which is not a folder record. */
  rootCount: number
}): JSX.Element {
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')

  const make = (): void => {
    void store.createFolder(name)
    setName('')
    setNaming(false)
  }

  /* A folder's own menu. Rename is not here: the one thing a reader does to a
     folder from this panel is get rid of it, and its documents come back to
     Root rather than going with it. */
  const raise = (target: KbFolder, at: DOMRect): void => {
    menu.show(at.left, at.bottom + 4, [
      {
        label: t('gui.knowledge.folder_delete'),
        bad: true,
        fn: () => void store.removeFolder(target),
      },
    ])
  }

  return (
    <aside className="knowledge-tree">
      <div className="knowledge-treehd">
        <b>{t('gui.knowledge.folders')}</b>
        <button
          className="knowledge-fadd"
          aria-label={t('gui.knowledge.folder_new')}
          title={t('gui.knowledge.folder_new')}
          onClick={() => setNaming(true)}
        >
          +
        </button>
        <button
          className="knowledge-fhide"
          aria-label={t('gui.knowledge.folders_hide')}
          title={t('gui.knowledge.folders_hide')}
          onClick={() => store.toggleTree()}
        >
          &#171;
        </button>
      </div>

      <Row name={t('gui.knowledge.root')} count={rootCount} on={folder === ''} onPick={() => store.setFolder('')} />
      <div className="knowledge-under">
        {folders.map((row) => (
          <Row
            key={row.id}
            name={row.name}
            count={row.documents}
            on={folder === row.id}
            onPick={() => store.setFolder(row.id)}
            onMenu={(at) => raise(row, at)}
          />
        ))}
        {naming && (
          <input
            className="knowledge-fnew"
            autoFocus
            placeholder={t('gui.knowledge.folder_name')}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') make()
              if (e.key === 'Escape') setNaming(false)
            }}
            /* Committed when the field is left, the way every other inline
               name on this page is: a click away from a field is not a way of
               cancelling. Escape is. */
            onBlur={() => (name.trim() ? make() : setNaming(false))}
          />
        )}
      </div>
    </aside>
  )
}
