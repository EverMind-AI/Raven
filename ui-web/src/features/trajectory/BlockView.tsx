/* One block's body, drawn from what the details store holds for it, and the
 * renderers every block and every overview preview go through.
 *
 * Six renderers for six shapes the wire names: text as it was written, JSON
 * as a tree that folds, a message sequence in its order, key-value rows,
 * a plain list, and a list of references. Every value is rendered as text
 * through React -- nothing here writes markup -- and every placeholder the
 * gateway may leave in a body (`$oversize`, `$depth_truncated`, `$omitted`,
 * `$more_keys`) is drawn as a sentence rather than as a key.
 *
 * The tab asks for its first page when it is shown and for the next when
 * the reader asks; what it copies is what it holds, and when that is less
 * than the whole -- cut by the gateway, pages still to come, or earlier
 * pages let go to stay in the window -- it says so before and after.
 */

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { copy } from '../../lib/clipboard'
import { formatDuration } from '../../lib/duration'
import { availabilityKey, basisKey, integrityKey, reasonKey, timingLabelKey } from './blocks'
import * as details from './detailStore'

import type { JsonValue, TrajectoryBlockDescriptor } from './types'
import type { JSX } from 'react'

type Obj = Record<string, unknown>

const isObj = (v: unknown): v is Obj => v !== null && typeof v === 'object' && !Array.isArray(v)

/* ── scalars and placeholders ─────────────────────────────────────────── */

/** A scalar as the reader should see it: the empty string and `false` named, never blank. */
function Scalar({ value }: { value: unknown }): JSX.Element {
  if (value === '') return <span className="trajectory-v trajectory-v-none">{t('gui.trajectory.details.empty_string')}</span>
  if (value === null) return <span className="trajectory-v trajectory-v-none">null</span>
  if (value === undefined) return <span className="trajectory-v trajectory-v-none">undefined</span>
  if (typeof value === 'boolean') return <span className="trajectory-v trajectory-v-bool">{String(value)}</span>
  if (typeof value === 'number') return <span className="trajectory-v trajectory-v-num">{String(value)}</span>
  return <span className="trajectory-v">{String(value)}</span>
}

/* The gateway's placeholders, each one sentence. */
function Placeholder({ value }: { value: Obj }): JSX.Element | null {
  if (value.$oversize === true) {
    return <span className="trajectory-ph">{t('gui.trajectory.details.oversize', { bytes: typeof value.bytes === 'number' ? value.bytes : '?' })}</span>
  }
  if (value.$depth_truncated === true) return <span className="trajectory-ph">{t('gui.trajectory.details.depth_truncated')}</span>
  return null
}

const isPlaceholder = (v: unknown): v is Obj => isObj(v) && (v.$oversize === true || v.$depth_truncated === true)

/* ── JSON ─────────────────────────────────────────────────────────────── */

/** How deep a tree opens on its own before a node needs a click. */
export const JSON_OPEN_DEPTH = 2

function JsonNode({ name, value, depth }: { name: string | null; value: unknown; depth: number }): JSX.Element {
  const [open, setOpen] = useState(depth < JSON_OPEN_DEPTH)
  const label = name === null ? null : <span className="trajectory-json-k">{name}</span>
  if (isPlaceholder(value)) {
    return <div className="trajectory-json-row">{label}<Placeholder value={value} /></div>
  }
  if (Array.isArray(value) || isObj(value)) {
    const entries: Array<[string, unknown]> = Array.isArray(value)
      ? value.map((v, i) => [String(i), v])
      : Object.entries(value).filter(([k]) => k !== '$more_keys')
    const more = isObj(value) && typeof value.$more_keys === 'number' ? value.$more_keys : 0
    const shape = Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`
    return (
      <div className="trajectory-json-row">
        <button className="trajectory-json-fold" aria-expanded={open} onClick={() => setOpen(!open)}>
          <span className="trajectory-json-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
          {label}
          <span className="trajectory-json-shape">{shape}</span>
        </button>
        {open ? (
          <div className="trajectory-json-kids">
            {entries.map(([k, v]) => <JsonNode key={k} name={k} value={v} depth={depth + 1} />)}
            {more > 0 ? <div className="trajectory-ph">{t('gui.trajectory.details.more_keys', { n: more })}</div> : null}
          </div>
        ) : null}
      </div>
    )
  }
  return <div className="trajectory-json-row">{label}<Scalar value={value} /></div>
}

export function JsonView({ value }: { value: unknown }): JSX.Element {
  return <div className="trajectory-json"><JsonNode name={null} value={value} depth={0} /></div>
}

/* ── text ─────────────────────────────────────────────────────────────── */

export function TextView({ text }: { text: string }): JSX.Element {
  if (text === '') return <p className="trajectory-v-none">{t('gui.trajectory.details.empty_string')}</p>
  return <pre className="trajectory-text-body">{text}</pre>
}

/* ── key-values ───────────────────────────────────────────────────────── */

interface KvItem { key: string; value: unknown; source?: string }

const SOURCE_KEYS: Record<string, string> = {
  derived: 'gui.trajectory.details.source_derived',
  attribute: 'gui.trajectory.details.source_attribute',
  artifact: 'gui.trajectory.details.source_artifact',
}

function KvValue({ item, blockId }: { item: KvItem; blockId: string }): JSX.Element {
  const v = item.value
  if (isPlaceholder(v)) return <Placeholder value={v} />
  if (blockId === 'timing' && (item.key === 'duration_ms' || item.key === 'charged_ms') && typeof v === 'number') {
    return <span className="trajectory-v trajectory-v-num">{v} ms ({formatDuration(v)})</span>
  }
  if (blockId === 'timing' && item.key === 'timing_basis' && typeof v === 'string') {
    const key = basisKey(v)
    return <span className="trajectory-v">{v}{key ? <span className="trajectory-kv-note">{t(key)}</span> : null}</span>
  }
  if (Array.isArray(v) || isObj(v)) return <JsonView value={v} />
  return <Scalar value={v} />
}

export function KeyValuesView({ items, blockId }: { items: unknown[]; blockId: string }): JSX.Element {
  const rows = items.filter(isObj) as unknown as KvItem[]
  if (!rows.length) return <p className="trajectory-v-none">{t('gui.trajectory.details.empty_list')}</p>
  return (
    <dl className="trajectory-kv">
      {rows.map((item, i) => {
        if (item.key === '$omitted') {
          return <div key={`omitted-${i}`} className="trajectory-ph">{t('gui.trajectory.details.omitted_n', { n: typeof item.value === 'number' ? item.value : '?' })}</div>
        }
        const label = blockId === 'timing' ? timingLabelKey(item.key) : null
        const source = typeof item.source === 'string' ? SOURCE_KEYS[item.source] : undefined
        return (
          <div key={`${item.key}-${i}`} className="trajectory-kv-row">
            <dt className="trajectory-kv-k" title={item.key}>{label ? t(label) : item.key}</dt>
            <dd className="trajectory-kv-v">
              <KvValue item={item} blockId={blockId} />
              {source ? <span className="trajectory-kv-src">{t(source)}</span> : null}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}

/* ── messages, items, references ──────────────────────────────────────── */

/* One message of a sequence: its metadata in a line, then its content as
   text or as a tree when the content is structured. */
function Message({ item }: { item: unknown }): JSX.Element {
  if (isPlaceholder(item)) return <div className="trajectory-msg"><Placeholder value={item} /></div>
  if (!isObj(item)) return <div className="trajectory-msg"><Scalar value={item} /></div>
  const meta: string[] = []
  for (const k of ['role', 'name', 'tool_call_id', 'id']) if (typeof item[k] === 'string') meta.push(`${k}: ${item[k] as string}`)
  const content = item.content
  const rest = Object.fromEntries(Object.entries(item).filter(([k]) => !['role', 'name', 'tool_call_id', 'id', 'content'].includes(k)))
  return (
    <div className="trajectory-msg">
      <div className="trajectory-msg-meta">{meta.join(' · ')}</div>
      {typeof content === 'string' ? <TextView text={content} /> : content !== undefined ? <JsonView value={content} /> : null}
      {Object.keys(rest).length ? <JsonView value={rest} /> : null}
    </div>
  )
}

function itemHead(item: unknown): string | null {
  if (!isObj(item)) return null
  for (const k of ['name', 'title', 'id', 'key']) if (typeof item[k] === 'string') return item[k] as string
  return null
}

function Item({ item }: { item: unknown }): JSX.Element {
  if (isPlaceholder(item)) return <li className="trajectory-item"><Placeholder value={item} /></li>
  if (!isObj(item) && !Array.isArray(item)) return <li className="trajectory-item"><Scalar value={item} /></li>
  const head = itemHead(item)
  return (
    <li className="trajectory-item">
      {head ? <div className="trajectory-item-head">{head}</div> : null}
      <JsonView value={item} />
    </li>
  )
}

function Reference({ item }: { item: unknown }): JSX.Element {
  if (!isObj(item)) return <li className="trajectory-item"><Scalar value={item} /></li>
  return (
    <li className="trajectory-item trajectory-ref">
      <span className="trajectory-ref-k">{typeof item.key === 'string' ? item.key : ''}</span>
      <span className="trajectory-ref-v">{typeof item.ref === 'string' || typeof item.ref === 'number' ? String(item.ref) : ''}</span>
      <span className="trajectory-ref-kind">{typeof item.kind === 'string' ? item.kind : ''}</span>
    </li>
  )
}

/* A list that may have let its earliest pages go: the surviving first item
   carries its offset, so the pane can put it back under the eye after the
   window slid (see `useWindowAnchor`). */
export function ListView({ renderer, items, offset }: {
  renderer: 'messages' | 'items' | 'references'; items: unknown[]; offset: number
}): JSX.Element {
  if (!items.length) return <p className="trajectory-v-none">{t('gui.trajectory.details.empty_list')}</p>
  if (renderer === 'messages') {
    return (
      <div className="trajectory-msgs">
        {items.map((item, i) => <div key={offset + i} data-offset={offset + i}><Message item={item} /></div>)}
      </div>
    )
  }
  return (
    <ul className="trajectory-items">
      {items.map((item, i) => (
        <div key={offset + i} data-offset={offset + i}>
          {renderer === 'references' ? <Reference item={item} /> : <Item item={item} />}
        </div>
      ))}
    </ul>
  )
}

/* ── previews, for the overview ───────────────────────────────────────── */

/** A block's preview, in the same renderer its body uses. */
export function PreviewView({ block }: { block: TrajectoryBlockDescriptor }): JSX.Element | null {
  const p = block.preview
  if (p === undefined || p === null) return null
  switch (block.renderer) {
    case 'text':
      return <TextView text={typeof p === 'string' ? p : JSON.stringify(p)} />
    case 'json':
      return <JsonView value={p} />
    case 'key_values':
      return <KeyValuesView items={Array.isArray(p) ? p : []} blockId={block.id} />
    case 'messages':
    case 'items':
    case 'references':
      return <ListView renderer={block.renderer} items={Array.isArray(p) ? p : []} offset={0} />
    default:
      return null
  }
}

/* ── the body of one tab ──────────────────────────────────────────────── */

/* After the window slid, the item that is now first goes back under the
   eye: the pane scrolls so that item's top is at its own top, rather than
   keeping a scroll offset that now points into content that is gone. */
function useWindowAnchor(droppedBefore: number, box: HTMLElement | null): void {
  const seen = useRef(droppedBefore)
  useLayoutEffect(() => {
    if (droppedBefore <= seen.current) { seen.current = droppedBefore; return }
    seen.current = droppedBefore
    if (!box) return
    const pane = box.closest<HTMLElement>('.trajectory-pane')
    const first = box.querySelector<HTMLElement>(`[data-offset="${droppedBefore}"]`)
    if (pane && first) pane.scrollTop = first.offsetTop - pane.offsetTop
  }, [droppedBefore, box])
}

function joinedItems(record: details.BlockRecord): unknown[] {
  const out: unknown[] = []
  for (const page of record.pages) {
    const data = page.data
    if (isObj(data) && Array.isArray(data.items)) out.push(...(data.items as unknown[]))
  }
  return out
}

/** What the copy button puts on the clipboard: the text the pane holds, as text. */
export function copyText(record: details.BlockRecord): string {
  const first = record.pages[0]?.data
  switch (record.renderer) {
    case 'text':
      return isObj(first) && typeof first.text === 'string' ? first.text : ''
    case 'json':
      return JSON.stringify(isObj(first) ? first.value : first, null, 2)
    case 'key_values':
      return JSON.stringify(isObj(first) && Array.isArray(first.items) ? first.items : [], null, 2)
    default:
      return JSON.stringify(joinedItems(record), null, 2)
  }
}

function Toolbar({ block, record }: { block: TrajectoryBlockDescriptor; record: details.BlockRecord | null }): JSX.Element | null {
  const lines: JSX.Element[] = []
  const first = record?.pages[0]
  const availability = first?.availability ?? block.availability
  const reason = first?.reason ?? block.reason ?? null
  if (availability !== 'available') {
    const key = (reason && reasonKey(reason)) || availabilityKey(availability)
    lines.push(<span key="avail" className="trajectory-tool-note">{key ? t(key) : (reason ?? availability)}</span>)
  }
  const partial = record ? details.isPartial(record) : false
  if (partial) lines.push(<span key="partial" className="trajectory-tool-note trajectory-tool-warn">{t('gui.trajectory.details.partial')}</span>)
  if (record && record.droppedBefore > 0) {
    lines.push(
      <span key="dropped" className="trajectory-tool-note">
        {t('gui.trajectory.details.earlier_unloaded', { n: record.droppedBefore })}
        {' '}
        <button className="trajectory-link" onClick={() => { void details.reloadBlock(block.id) }}>{t('gui.trajectory.details.reload_from_start')}</button>
      </span>,
    )
  }
  const codes = record ? [...new Set(record.pages.flatMap((p) => p.integrity))] : []
  for (const code of codes) {
    const key = integrityKey(code)
    lines.push(<span key={`i-${code}`} className="trajectory-tool-note">{key ? t(key) : code}</span>)
  }
  if (block.related_operation) {
    lines.push(<span key="from" className="trajectory-tool-note">{t('gui.trajectory.details.from_operation', { op: block.related_operation })}</span>)
  }
  if (block.id === 'frames') lines.push(<span key="frames" className="trajectory-tool-note">{t('gui.trajectory.details.frames_body_unavailable')}</span>)
  const canCopy = record !== null && record.pages.some((p) => p.data !== null)
  return (
    <div className="trajectory-toolbar">
      <div className="trajectory-tool-notes">{lines}</div>
      {canCopy ? (
        <button
          className="trajectory-copy"
          onClick={() => copy(copyText(record), t(partial ? 'gui.trajectory.details.copied_partial' : 'gui.trajectory.details.copied'))}
        >
          {t('gui.trajectory.details.copy')}
        </button>
      ) : null}
    </div>
  )
}

function Skeleton(): JSX.Element {
  return (
    <div className="trajectory-skel" aria-busy="true" aria-label={t('gui.trajectory.details.loading')}>
      <div className="trajectory-skel-line" /><div className="trajectory-skel-line" /><div className="trajectory-skel-line trajectory-skel-short" />
    </div>
  )
}

export function BlockView({ block }: { block: TrajectoryBlockDescriptor }): JSX.Element {
  const s = useSyncExternalStore(details.subscribe, details.get)
  const record = details.block(block.id, s)
  const loading = details.isLoading({ blockId: block.id }, s)
  const more = details.isLoading({ blockId: block.id, more: true }, s)
  const fault = details.fault({ blockId: block.id }, s)
  const moreFault = details.fault({ blockId: block.id, more: true }, s)
  const [box, setBox] = useState<HTMLDivElement | null>(null)
  useWindowAnchor(record?.droppedBefore ?? 0, box)

  const identityKey = s.current ? details.descriptorKey(s.current) : null
  const permitted = details.mayRead(s)
  useEffect(() => {
    if (!record && !loading && !fault && identityKey !== null && permitted) void details.loadBlock(block.id)
  }, [record, loading, fault, identityKey, permitted, block.id])

  let body: JSX.Element | null = null
  if (record) {
    const first = record.pages[0]
    const data = first?.data
    switch (record.renderer) {
      case 'text':
        body = isObj(data) && typeof data.text === 'string' ? <TextView text={data.text} /> : null
        break
      case 'json':
        body = isObj(data) ? <JsonView value={data.value} /> : null
        break
      case 'key_values':
        body = isObj(data) && Array.isArray(data.items) ? <KeyValuesView items={data.items as unknown[]} blockId={block.id} /> : null
        break
      case 'messages':
      case 'items':
      case 'references':
        body = <ListView renderer={record.renderer} items={joinedItems(record)} offset={record.droppedBefore} />
        break
      default:
        body = data === undefined || data === null ? null : <JsonView value={data} />
    }
  }

  return (
    <div className="trajectory-block" ref={setBox}>
      <Toolbar block={block} record={record} />
      {fault ? (
        <p className="trajectory-fault" role="alert">
          {t('gui.trajectory.details.failed', { detail: fault })}
          {' '}
          <button className="trajectory-link" onClick={() => { void details.reloadBlock(block.id) }}>{t('gui.trajectory.details.retry')}</button>
        </p>
      ) : null}
      {!record && loading ? <Skeleton /> : body}
      {record && record.nextCursor !== null ? (
        <div className="trajectory-more">
          {moreFault ? <span className="trajectory-fault">{t('gui.trajectory.details.failed', { detail: moreFault })}</span> : null}
          <button className="trajectory-link" disabled={more} onClick={() => { void details.loadMore(block.id) }}>
            {more ? t('gui.trajectory.details.loading') : t('gui.trajectory.details.load_more')}
          </button>
        </div>
      ) : null}
    </div>
  )
}

/** The scalar renderer, exported for the overview's info rows. */
export { Scalar }

/** A value the gateway may hand back, for callers that only know it is JSON. */
export type Json = JsonValue
