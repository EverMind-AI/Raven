import { useSyncExternalStore } from 'react'

import { shell, t } from '../../shell/bridge'
import * as store from './store'
import { plainTitle } from './title'

import type { MenuItem } from '../../shell/bridge'
import type { SessRow } from './types'
import type { JSX, KeyboardEvent } from 'react'

/* The row's context/⋯ menu. Every action leaves through the shell by name,
   so the live layer's rebinds (removeSession, renameTitle, openSession,
   pinPersist) win exactly as they did over the legacy renderer. */
function sessItems(s: SessRow): Array<MenuItem | '-'> {
  const sh = shell()
  return [
    {
      label: t('gui.sess.rename'),
      fn: () => {
        let cur: string | null = null
        try {
          cur = store.source().snapshot().cur
        } catch {
          /* keep null */
        }
        if (s.id !== cur) {
          sh.setCur?.(s.id)
          sh.drawList?.()
          sh.openSession?.(s)
        }
        sh.renameTitle?.()
      },
    },
    {
      label: t(s.pin ? 'gui.sess.unpin' : 'gui.sess.pin'),
      fn: () => {
        s.pin = !s.pin
        sh.drawList?.()
        sh.toast(t(s.pin ? 'gui.pinned_ok' : 'gui.unpinned_ok'))
        /* Optimistic: the row moved already; live mode persists the flag in
           session metadata. The demo has no server, so the bridge's guard
           makes this a no-op there. */
        sh.pinPersist?.(s.id, !!s.pin)
      },
    },
    '-',
    { label: t('gui.sess.delete'), bad: true, fn: () => sh.removeSession?.(s) },
  ]
}

const enterOrSpace = (fn: () => void) => (e: KeyboardEvent) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    fn()
  }
}

function Row({ s, cur, busy }: { s: SessRow; cur: string | null; busy: boolean }): JSX.Element {
  const live = s.id === cur && busy ? 'run' : s.status
  // run/done speak from the tail slot instead (see .sess .sig); err and que
  // stay a leading dot -- they are conditions of the session, not of a turn
  // the reader is waiting on.
  const tail = live === 'run' || live === 'done' ? live : null
  // The state is only colour and motion otherwise, and the stamp behind it
  // is visibility:hidden, so name it for a reader who gets the row as text.
  const label = tail ? t(tail === 'run' ? 'gui.sess.running' : 'gui.sess.finished') : undefined
  const go = (): void => {
    const sh = shell()
    sh.showPage(null)
    let now: string | null = cur
    try {
      now = store.source().snapshot().cur
    } catch {
      /* keep the rendered value */
    }
    if (s.id !== now) {
      sh.setCur?.(s.id)
      sh.drawList?.()
      sh.openSession?.(s)
    }
  }
  return (
    <div
      className="sess"
      role="button"
      tabIndex={0}
      aria-current={s.id === cur}
      onClick={go}
      onKeyDown={enterOrSpace(go)}
      ref={(el) => {
        if (el) (el as HTMLElement & { _ctx?: () => Array<MenuItem | '-'> })._ctx = () => sessItems(s)
      }}
    >
      <div className="t">
        {live && !tail ? <span className={'dot ' + live} /> : null}
        <span>{plainTitle(s.title)}</span>
      </div>
      {/* The stamp is always rendered -- it is what gives the tail its width.
          A marker hides the text in place rather than replacing the element,
          so the row does not resize when a turn starts or ends. */}
      <span className="w" data-sig={tail ?? undefined} aria-label={label} title={label}>
        <span className="wt">{s.when}</span>
        {tail ? <i /> : null}
      </span>
      <button
        className="more"
        aria-label={t('gui.cron.menu_aria', { name: plainTitle(s.title) })}
        onClick={(e) => {
          e.stopPropagation()
          const r = e.currentTarget.getBoundingClientRect()
          shell().menuAt(r.right, r.bottom + 4, sessItems(s))
        }}
      >
        ⋯
      </button>
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
  busy,
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
            onClick={(e) => {
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
          {shown.map((s) => (
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
        {[0, 1, 2, 3, 4, 5].map((i) => (
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
  const query = snap.query
  const hit = (x: SessRow): boolean =>
    !query || x.title.toLowerCase().includes(query) || (x.last || '').toLowerCase().includes(query)
  const rows = snap.rows.filter(hit)

  if (query && !rows.length) return <div className="empty-note">{t('gui.rail.no_hits', { q: query })}</div>

  if (query) {
    return (
      <>
        <span className="lab">{t('gui.rail.search_hits', { n: rows.length })}</span>
        {rows.map((x) => (
          <Row key={x.id} s={x} cur={snap.cur} busy={snap.busy} />
        ))}
      </>
    )
  }

  // Straight through, in the order SESS already holds: newest last activity
  // first, which is the same value each row's clock shows.
  const rest = rows.filter((x) => !x.pin && x.from !== 'cron')
  return (
    <>
      <Group label={t('gui.rail.pinned')} items={rows.filter((x) => x.pin)} gid="pin" cur={snap.cur} busy={snap.busy} />
      <Group
        label={t('gui.rail.from_cron')}
        items={rows.filter((x) => !x.pin && x.from === 'cron')}
        action={() => shell().openCron?.()}
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
