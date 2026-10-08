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

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { reasonKey } from './blocks'
import * as details from './detailStore'

import type { FileEntry } from './detailStore'
import type { JSX, ReactNode } from 'react'

/** How far beyond the pane's edges a section still counts as in view, in pixels. */
export const FILES_OVERSCAN_PX = 400
/** How many files are read at once. */
export const FILES_CONCURRENCY = 2
/** A section's height until it is measured: its heading alone, and with a body. */
export const FILE_HEAD_H = 44
export const FILE_BODY_EST = 240
/** The pane's height while it cannot be measured, so the first files are read. */
const DEFAULT_VIEW_H = 600

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

interface View {
  top: number
  height: number
  listTop: number
  /** The pane was found and measured: until then nothing is known to be in view. */
  measured: boolean
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

  /* Sections the reader folded, and the measured heights, start over with another entry. */
  const [closed, setClosed] = useState<Record<number, boolean>>({})
  const heights = useRef(new Map<number, number>())
  const seen = useRef(identityKey)
  useEffect(() => {
    if (seen.current !== identityKey) {
      seen.current = identityKey
      heights.current = new Map()
      setClosed({})
    }
  }, [identityKey])

  /* The pane that scrolls this list: its viewport decides which files are read.
     The list's own place is measured again whenever it may have moved -- the
     raw record above it arrives later and grows as its tree is opened -- so a
     list measured before the record arrived is not read at the wrong height. */
  const box = useRef<HTMLDivElement>(null)
  const [view, setView] = useState<View>({ top: 0, height: 0, listTop: 0, measured: false })
  const measure = useRef<() => void>(() => {})
  useEffect(() => {
    const el = box.current
    const pane = el?.closest<HTMLElement>('.trajectory-pane') ?? null
    if (!el || !pane) return
    measure.current = (): void => {
      const listTop = el.offsetTop - pane.offsetTop
      setView((v) => (v.measured && v.top === pane.scrollTop && v.height === pane.clientHeight && v.listTop === listTop
        ? v
        : { top: pane.scrollTop, height: pane.clientHeight, listTop, measured: true }))
    }
    const onChange = (): void => { measure.current() }
    onChange()
    pane.addEventListener('scroll', onChange, { passive: true })
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(onChange)
    ro?.observe(pane)
    if (el.parentElement) ro?.observe(el.parentElement)
    return () => { pane.removeEventListener('scroll', onChange); ro?.disconnect(); measure.current = () => {} }
  }, [identityKey])
  useLayoutEffect(() => { measure.current() })

  const items = dir?.items ?? []
  const open = (file: FileEntry): boolean => closed[file.index] !== true
  const sectionH = (file: FileEntry): number =>
    heights.current.get(file.index) ?? (open(file) ? FILE_HEAD_H + FILE_BODY_EST : FILE_HEAD_H)

  /* Measured once they stand; a changed height redraws once. */
  const [, remeasured] = useState(0)
  const drawnKey = items.map((f) => `${f.index}${open(f) ? (details.fileBody(f.index, s) ? 'b' : 'o') : 'c'}`).join(',')
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    let changed = false
    for (const node of el.querySelectorAll<HTMLElement>('[data-file-index]')) {
      const h = node.offsetHeight
      const index = Number(node.dataset.fileIndex)
      if (h > 0 && Number.isFinite(index) && Math.abs((heights.current.get(index) ?? 0) - h) > 1) {
        heights.current.set(index, h)
        changed = true
      }
    }
    if (changed) remeasured((n) => n + 1)
  }, [drawnKey])

  /* The open sections whose extent meets a viewport, with the overscan. */
  const inViewOf = (at: View): FileEntry[] => {
    const viewH = at.height > 0 ? at.height : DEFAULT_VIEW_H
    const from = at.top - at.listTop - FILES_OVERSCAN_PX
    const to = at.top - at.listTop + viewH + FILES_OVERSCAN_PX
    const out: FileEntry[] = []
    let y = 0
    for (const file of items) {
      const h = sectionH(file)
      if (open(file) && y + h > from && y < to) out.push(file)
      y += h
    }
    return out
  }

  /* Read what is in view and not held, released or failed, two at a time --
     once the list's place is known and the record above it has arrived. The
     place is taken from the page as it stands now: the commit that brought
     the record has moved the list, and the measured state has not caught up. */
  useEffect(() => {
    if (!permitted || !view.measured || !settled) return
    const el = box.current
    const pane = el?.closest<HTMLElement>('.trajectory-pane') ?? null
    const now: View = el && pane
      ? { top: pane.scrollTop, height: pane.clientHeight, listTop: el.offsetTop - pane.offsetTop, measured: true }
      : view
    let reading = items.filter((f) => details.fileLoading(f.index, s)).length
    for (const file of inViewOf(now)) {
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
