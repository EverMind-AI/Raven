/* The files a span names, shown under its raw record: one section per
 * `*.artifact_path` attribute, headed by the attribute's name, holding the
 * file's content -- a JSON tree when it parses, text otherwise.
 *
 * The directory is read whole (the `files` block, page by page) and every
 * heading is drawn, so no file is ever out of reach. Contents are read one
 * file at a time by the cursor the directory gave, only for open sections
 * near the pane's viewport and never more than two at once, so a span with
 * hundreds of large files costs what the files on screen cost. When the
 * pane's budget lets a content go, its section says so and waits for the
 * reader to ask for it again; nothing reads it back on its own.
 */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { reasonKey } from './blocks'
import * as details from './detailStore'

import type { FileEntry } from './detailStore'
import type { JSX, ReactNode } from 'react'

/** How far beyond the pane's edges a section still counts as in view, in pixels. */
export const FILES_OVERSCAN_PX = 400
/** How many files are read at once. */
export const FILES_CONCURRENCY = 2

interface FileItem {
  kind?: 'json' | 'text' | 'none'
  value?: unknown
  text?: string
  reason?: string
  size?: number | null
  shown_bytes?: number
  truncated?: 'file_limit' | 'response_limit' | null
}

/** A byte count in the words of a file list: "820 B", "14.2 KiB", "3.1 MiB". */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

function cutNote(item: FileItem): string | null {
  const size = typeof item.size === 'number' ? formatSize(item.size) : null
  if (item.truncated === 'file_limit') {
    return size ? t('gui.trajectory.details.file_cut_file', { size }) : t('gui.trajectory.details.file_cut_file_nosize')
  }
  if (item.truncated === 'response_limit') {
    const shown = formatSize(item.shown_bytes ?? 0)
    return size
      ? t('gui.trajectory.details.file_cut_response', { shown, size })
      : t('gui.trajectory.details.file_cut_response_nosize', { shown })
  }
  return null
}

export function FilesView({ renderJson, renderText, settled }: {
  /** The block view's own renderers, so a file reads like the blocks around it. */
  renderJson: (value: unknown) => ReactNode
  renderText: (text: string) => ReactNode
  /** The raw record above has arrived, so the list stands where it will stay. */
  settled: boolean
}): JSX.Element | null {
  const s = useSyncExternalStore(details.subscribe, details.get)
  const dir = details.fileDir(s)
  const permitted = details.mayRead(s)
  const identityKey = s.current ? details.descriptorKey(s.current) : null

  /* The directory, asked for once the pane may read; a walk cut short is taken up again. */
  useEffect(() => {
    if (permitted && identityKey !== null && (dir === null || (!dir.done && !dir.capped && !dir.loading && dir.fault === null))) {
      void details.loadFileDir()
    }
  }, [permitted, identityKey, dir])

  /* Sections the reader folded start over with another entry. */
  const [closed, setClosed] = useState<Record<number, boolean>>({})
  const seen = useRef(identityKey)
  useEffect(() => {
    if (seen.current !== identityKey) {
      seen.current = identityKey
      setClosed({})
    }
  }, [identityKey])

  /* Where each file stands is read off the page, never added up: a section
     grows and shrinks as the tree inside it opens and folds, the window's
     width reflows it, and the list's own gaps count. What changes the page
     without changing this component -- the pane scrolled or resized, the list
     or the record above it resized -- asks for one more look. */
  const box = useRef<HTMLDivElement>(null)
  const [, look] = useState(0)
  useEffect(() => {
    const el = box.current
    const pane = el?.closest<HTMLElement>('.trajectory-pane') ?? null
    if (!el || !pane) return
    const again = (): void => { look((n) => n + 1) }
    pane.addEventListener('scroll', again, { passive: true })
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(again)
    ro?.observe(pane)
    ro?.observe(el)
    if (el.parentElement) ro?.observe(el.parentElement)
    return () => { pane.removeEventListener('scroll', again); ro?.disconnect() }
  }, [identityKey])

  const items = dir?.items ?? []
  const open = (file: FileEntry): boolean => closed[file.index] !== true

  /* The open sections whose box meets the pane's viewport, with the overscan. */
  const inView = (): FileEntry[] => {
    const el = box.current
    const pane = el?.closest<HTMLElement>('.trajectory-pane') ?? null
    if (!el || !pane) return []
    const frame = pane.getBoundingClientRect()
    const top = frame.top - FILES_OVERSCAN_PX
    const bottom = frame.top + pane.clientHeight + FILES_OVERSCAN_PX
    const byIndex = new Map(items.map((f) => [f.index, f]))
    const out: FileEntry[] = []
    for (const node of el.querySelectorAll<HTMLElement>('[data-file-index]')) {
      const file = byIndex.get(Number(node.dataset.fileIndex))
      if (!file || !open(file)) continue
      const r = node.getBoundingClientRect()
      if (r.bottom > top && r.top < bottom) out.push(file)
    }
    return out
  }

  /* Read what is in view and not held, released or failed, two at a time --
     once the record above has arrived, so the list stands where it will stay. */
  useEffect(() => {
    if (!permitted || !settled) return
    let reading = items.filter((f) => details.fileLoading(f.index, s)).length
    for (const file of inView()) {
      if (reading >= FILES_CONCURRENCY) break
      if (details.fileBody(file.index, s) || details.fileLoading(file.index, s)
        || details.isReleased(file.index, s) || details.fileFault(file.index, s) !== null) continue
      void details.loadFile(file)
      reading += 1
    }
  })

  const content = (file: FileEntry): ReactNode => {
    const fault = details.fileFault(file.index, s)
    if (fault !== null) {
      return (
        <p className="trajectory-fault" role="alert">
          {t('gui.trajectory.details.failed', { detail: fault })}
          {' '}
          <button className="trajectory-link" onClick={() => { void details.reloadFile(file) }}>{t('gui.trajectory.details.retry')}</button>
        </p>
      )
    }
    if (details.isReleased(file.index, s) && !details.fileBody(file.index, s)) {
      return (
        <button className="trajectory-link trajectory-file-released" onClick={() => { void details.reloadFile(file) }}>
          {t('gui.trajectory.details.file_released')}
        </button>
      )
    }
    const body = details.fileBody(file.index, s)
    if (!body) return <div className="trajectory-skel-line" aria-busy="true" />
    const item = body.item as FileItem
    if (item.kind === 'none') {
      const key = item.reason ? reasonKey(item.reason) : null
      return <p className="trajectory-sec-note">{key ? t(key) : (item.reason ?? '')}</p>
    }
    const note = cutNote(item)
    return (
      <>
        {note ? <p className="trajectory-sec-note trajectory-file-cut">{note}</p> : null}
        {item.kind === 'json' ? renderJson(item.value) : renderText(item.text ?? '')}
      </>
    )
  }

  return (
    <div className="trajectory-files" ref={box}>
      {dir?.fault ? (
        <p className="trajectory-fault" role="alert">
          {t('gui.trajectory.details.failed', { detail: dir.fault })}
          {' '}
          <button className="trajectory-link" onClick={() => { void details.retryFileDir() }}>{t('gui.trajectory.details.retry')}</button>
        </p>
      ) : null}
      {dir?.capped ? (
        <p className="trajectory-sec-note">{t('gui.trajectory.details.files_listed_part', { n: items.length, total: dir.total ?? '?' })}</p>
      ) : null}
      {items.map((file) => (
        <section key={file.index} className="trajectory-file" data-file-index={file.index}>
          <h4 className="trajectory-file-h">
            <button
              className="trajectory-file-toggle"
              aria-expanded={open(file)}
              onClick={() => setClosed((c) => ({ ...c, [file.index]: open(file) }))}
            >
              {file.key}
            </button>
          </h4>
          <div className="trajectory-file-path">
            {file.path}
            {typeof file.size === 'number' ? ` · ${formatSize(file.size)}` : ''}
          </div>
          {open(file) ? <div className="trajectory-file-body">{content(file)}</div> : null}
        </section>
      ))}
      {dir === null || (items.length === 0 && dir.loading) ? <div className="trajectory-skel-line" aria-busy="true" /> : null}
    </div>
  )
}
