import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'
import { current, setCurrent } from '../../shell/session'
import { open as openCron } from '../cron/store'
import * as store from './store'
import { plainTitle } from './title'

import type { MenuItem } from '../../shell/menu'
import type { SessRow } from './types'
import type { JSX, KeyboardEvent, MouseEvent } from 'react'
import { term as findTerm } from '../../shell/find'

/* The row's context/⋯ menu. Opening and acting on a session go through the
   source, while the current pointer is page-scoped modern state, so
   what happens is whatever the installed source can actually do. */
function togglePin(s: SessRow): void {
  s.pin = !s.pin
  store.draw()
  toast(t(s.pin ? 'gui.pinned_ok' : 'gui.unpinned_ok'))
  store.pin(s.id, !!s.pin)
}

function archiveSession(s: SessRow): void {
  store.archive(s)
}

function sessItems(s: SessRow): Array<MenuItem | '-'> {
  const sh = shell()
  return [
    {
      label: t('gui.sess.rename'),
      fn: () => {
        const cur = current()
        if (s.id !== cur) {
          setCurrent(s.id)
          store.open(s)
        }
        store.rename()
      }
    },
    {
      label: t(s.pin ? 'gui.sess.unpin' : 'gui.sess.pin'),
      fn: () => togglePin(s)
    },
    { label: t('gui.sess.archive'), fn: () => archiveSession(s) },
    '-',
    { label: t('gui.sess.delete'), bad: true, fn: () => store.remove(s) }
  ]
}

const enterOrSpace = (fn: () => void) => (e: KeyboardEvent) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    fn()
  }
}

function Row({ s, cur, busy }: { s: SessRow; cur: string | null; busy: boolean }): JSX.Element {
  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState(s.title)
  const inputRef = useRef<HTMLInputElement>(null)
  const finishing = useRef(false)
  useEffect(() => {
    if (!editing) return
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [editing])
  const live = s.id === cur && busy ? 'run' : s.status
  // run/done/err all speak from the tail slot (see .sess .w[data-sig]). A turn
  // that failed is the outcome of the same turn `run` was reporting, so it
  // belongs in the slot the reader is already watching; splitting it onto a
  // leading dot moved the row's state to the other end on the one transition
  // where the reader cares most. `que` stays a leading dot -- it is a
  // condition of the session, not the state of a turn just watched.
  const tail = live === 'run' || live === 'done' || live === 'err' ? live : null
  // The state is only colour and motion otherwise, and the stamp behind it
  // is visibility:hidden, so name it for a reader who gets the row as text.
  const label = tail
    ? t(tail === 'run' ? 'gui.sess.running' : tail === 'err' ? 'gui.sess.failed' : 'gui.sess.finished')
    : undefined
  const go = (): void => {
    if (editing) return
    const sh = shell()
    sh.showPage(null)
    const now = current()
    if (s.id !== now) {
      setCurrent(s.id)
      store.open(s)
    }
  }
  const beginEdit = (e: MouseEvent): void => {
    e.preventDefault()
    e.stopPropagation()
    finishing.current = false
    setDraftTitle(s.title)
    setEditing(true)
  }
  const finishEdit = (commit: boolean): void => {
    if (finishing.current) return
    finishing.current = true
    const title = draftTitle.trim()
    setEditing(false)
    if (commit && title) store.renameRow(s, title)
  }
  return (
    <div
      className="sess"
      role="button"
      tabIndex={0}
      aria-current={s.id === cur}
      onClick={go}
      onDoubleClick={beginEdit}
      onKeyDown={editing ? undefined : enterOrSpace(go)}
      ref={el => {
        if (el) (el as HTMLElement & { _ctx?: () => Array<MenuItem | '-'> })._ctx = () => sessItems(s)
      }}
    >
      <div className="t">
        {live && !tail ? <span className={'dot ' + live} /> : null}
        {editing ? (
          <input
            ref={inputRef}
            className="ren"
            aria-label={t('gui.sess.rename')}
            value={draftTitle}
            onChange={e => setDraftTitle(e.target.value)}
            onClick={e => e.stopPropagation()}
            onDoubleClick={e => e.stopPropagation()}
            onBlur={() => finishEdit(true)}
            onKeyDown={e => {
              if (e.nativeEvent.isComposing) return
              if (e.key === 'Enter') {
                e.preventDefault()
                finishEdit(true)
              } else if (e.key === 'Escape') {
                e.preventDefault()
                finishEdit(false)
              }
            }}
          />
        ) : s.naming ? (
          /* The title is being generated. A fixed width, not a per-row one:
             there is a single bar here and a varying width would make it jump
             sideways as the real title replaces it. The shimmer is the rail's
             own loading primitive, which already stands still under
             prefers-reduced-motion. */
          <span className="skel">
            <span className="sk" style={{ width: '40%', height: '11px' }} aria-label={t('gui.sess.naming')} />
          </span>
        ) : (
          <span>{plainTitle(s.title)}</span>
        )}
      </div>
      {/* The stamp is always rendered -- it is what gives the tail its width.
          A marker hides the text in place rather than replacing the element,
          so the row does not resize when a turn starts or ends. */}
      <span className="w" data-sig={tail ?? undefined} aria-label={label} title={label}>
        <span className="wt">{s.when}</span>
        {tail ? <i /> : null}
      </span>
      <div className="quick" onDoubleClick={e => e.stopPropagation()}>
        <button
          className="quick-pin"
          data-active={s.pin || undefined}
          aria-label={t(s.pin ? 'gui.sess.unpin' : 'gui.sess.pin')}
          onClick={e => {
            e.stopPropagation()
            togglePin(s)
          }}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m14.5 4.5 5 5-3 2.5v3l-2 2-3-3-5 5-1.5-1.5 5-5-3-3 2-2h3z" />
          </svg>
        </button>
        <button
          aria-label={t('gui.sess.archive')}
          onClick={e => {
            e.stopPropagation()
            archiveSession(s)
          }}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 8h14v11H5zM4 4h16v4H4zm5 8h6" />
          </svg>
        </button>
      </div>
    </div>
  )
}

/* Every group gets the same collapsible eyebrow: caret + label + count +
   hairline. The cron and recent groups are permanent fixtures of the rail
   (rendered even when empty); pinned only exists while something is pinned. */
function Group({
  label,
  items,
  action,
  cap,
  gid,
  always,
  cur,
  busy
}: {
  label: string
  items: SessRow[]
  action?: () => void
  cap?: number
  gid: string
  always?: boolean
  cur: string | null
  busy: boolean
}): JSX.Element | null {
  if (!items.length && !always) return null
  const folded = store.isFolded(gid)
  const open = store.isOpen(gid)
  // A long tail of old sessions buries the rail's other groups, so a group
  // with a cap shows its head and folds the rest behind one row.
  const shown = cap && !open ? items.slice(0, cap) : items
  const flip = (): void => store.flipFold(gid)
  return (
    <>
      <div
        className="grp"
        role="button"
        tabIndex={0}
        aria-expanded={!folded}
        onClick={flip}
        onKeyDown={enterOrSpace(flip)}
      >
        <span className="car">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M8.5 5.5 15 12l-6.5 6.5" />
          </svg>
        </span>
        <span className="lab">{label}</span>
        <span className="n">{String(items.length)}</span>
        <span className="rule" />
        {action ? (
          <button
            className="grp-go"
            onClick={e => {
              e.stopPropagation()
              action()
            }}
          >
            {t('gui.rail.manage')}
          </button>
        ) : null}
      </div>
      {folded ? null : !items.length ? (
        <div className="grp-empty">{t('gui.rail.none')}</div>
      ) : (
        <>
          {shown.map(s => (
            <Row key={s.id} s={s} cur={cur} busy={busy} />
          ))}
          {cap && items.length > cap ? (
            <button className="grp-more" aria-expanded={open} onClick={() => store.flipOpen(gid)}>
              {open ? t('gui.rail.collapse') : t('gui.rail.expand_rest', { n: items.length - cap })}
            </button>
          ) : null}
        </>
      )}
    </>
  )
}

export function RailApp(): JSX.Element | null {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  if (s.skel) {
    /* The live boot's skeleton rows, exactly the shapes the boot guard drew. */
    return (
      <>
        {[0, 1, 2, 3, 4, 5].map(i => (
          <div key={i} className="sess skel">
            <span className="sk" style={{ width: `${52 + ((i * 17) % 30)}%`, height: '11px' }} />
            <span className="sk" style={{ width: '28px', height: '9px' }} />
          </div>
        ))}
      </>
    )
  }
  const snap = s.snap
  if (!snap) return null
  /* Not a snapshot field: the search row owns the term (shell/find.ts), and
     neither the demo nor the live source can produce it. */
  const query = findTerm()
  const hit = (x: SessRow): boolean =>
    !query || x.title.toLowerCase().includes(query) || (x.last || '').toLowerCase().includes(query)
  const rows = snap.rows.filter(hit)

  if (query && !rows.length) return <div className="empty-note">{t('gui.rail.no_hits', { q: query })}</div>

  if (query) {
    return (
      <>
        <span className="lab">{t('gui.rail.search_hits', { n: rows.length })}</span>
        {rows.map(x => (
          <Row key={x.id} s={x} cur={snap.cur} busy={snap.busy} />
        ))}
      </>
    )
  }

  // Straight through, in the order the source already holds: newest last activity
  // first, which is the same value each row's clock shows.
  const rest = rows.filter(x => !x.pin && x.from !== 'cron')
  return (
    <>
      <Group label={t('gui.rail.pinned')} items={rows.filter(x => x.pin)} gid="pin" cur={snap.cur} busy={snap.busy} />
      <Group
        label={t('gui.rail.from_cron')}
        items={rows.filter(x => !x.pin && x.from === 'cron')}
        action={() => openCron()}
        cap={3}
        gid="cron"
        always
        cur={snap.cur}
        busy={snap.busy}
      />
      <Group label={t('gui.rail.recent')} items={rest} cap={15} gid="recent" always cur={snap.cur} busy={snap.busy} />
    </>
  )
}
