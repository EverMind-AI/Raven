/* One file on a page of its own: the preview the workspace pane draws, with
 * the shell left off. Opened by features/workspace/preview.ts, booted from
 * src/main.tsx when the address says so; every other step of the page's boot
 * -- the socket, the session list, the rail, the composer -- belongs to the
 * conversation and does not run here.
 */

import { Download04Icon } from '@hugeicons/core-free-icons'
import { useEffect, useState, useSyncExternalStore } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang/store'
import { TWO_VIEWS, fileURL } from './store'
import * as store from './store'
import { FileBody } from './WorkspacePage'

import type { JSX } from 'react'

const CLOSE = 'M6 6l12 12M18 6L6 18'

export function PreviewWindow({ path }: { path: string }): JSX.Element {
  useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const [file] = useState(() => store.makeFile(path))
  const [raw, setRaw] = useState(false)
  const [menuSlot, setMenuSlot] = useState<HTMLSpanElement | null>(null)
  /* The pane switches this flag on the same object it loaded; so does the
     window, which keeps the bytes read rather than re-reading for a redraw. */
  file.raw = raw
  const name = file.path.split('/').filter(Boolean).pop() || file.path
  useEffect(() => {
    document.title = name
  }, [name])
  const both = TWO_VIEWS[file.kind] === 1
  return (
    <div className="workspace-preview">
      <header className="workspace-preview-head">
        <span className="workspace-preview-name" title={file.path}>{name}</span>
        <span className="workspace-preview-kind">{t('gui.ws.file_kind', { k: file.kind })}</span>
        <span className="fsp" />
        <span className="workspace-md-slot" ref={setMenuSlot} />
        {both ? (
          <span className="workspace-preview-seg">
            <button type="button" aria-pressed={!raw} onClick={() => setRaw(false)}>
              {t('gui.ws.file_rendered')}
            </button>
            <button type="button" aria-pressed={raw} onClick={() => setRaw(true)}>
              {t('gui.ws.file_source')}
            </button>
          </span>
        ) : null}
        <a
          className="ghost-ic"
          href={fileURL(file.path)}
          download={name}
          title={t('gui.arts.download')}
          aria-label={t('gui.arts.download')}
        >
          <Icon icon={Download04Icon} size={15} />
        </a>
        <button
          type="button"
          className="ghost-ic"
          title={t('gui.close')}
          aria-label={t('gui.close')}
          onClick={() => window.close()}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
            <path d={CLOSE} />
          </svg>
        </button>
      </header>
      <div className="workspace-preview-body">
        <FileBody f={file} menuSlot={menuSlot} />
      </div>
    </div>
  )
}
