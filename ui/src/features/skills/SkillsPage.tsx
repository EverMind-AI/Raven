import { useEffect, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { shell, t } from '../../shell/bridge'
import * as store from './store'

import type { SkillsState } from './store'
import type { HubItem } from './types'
import type { JSX } from 'react'

/* The skill tab mirrors the plugin tab exactly: the market IS the page,
   what you already have lives one level in (the installed button top-right
   is legacy chrome, back arrow to return), a category chip row filters,
   and every card opens the shared detail drawer. */
const HUB_CATS: Array<[string, string]> = [
  ['', 'gui.hubcat.all'],
  ['DEV', 'gui.hubcat.DEV'],
  ['FRONTEND-UI', 'gui.hubcat.FRONTEND-UI'],
  ['DEVOPS-INFRA', 'gui.hubcat.DEVOPS-INFRA'],
  ['DATA', 'gui.hubcat.DATA'],
  ['AI-ML', 'gui.hubcat.AI-ML'],
  ['TESTING', 'gui.hubcat.TESTING'],
  ['SECURITY', 'gui.hubcat.SECURITY'],
  ['AUTH', 'gui.hubcat.AUTH'],
  ['MULTIMEDIA', 'gui.hubcat.MULTIMEDIA'],
  ['WRITING', 'gui.hubcat.WRITING'],
  ['DOC-PROC', 'gui.hubcat.DOC-PROC'],
  ['COMMS', 'gui.hubcat.COMMS'],
  ['WORKFLOW', 'gui.hubcat.WORKFLOW'],
  ['PRODUCTIVITY', 'gui.hubcat.PRODUCTIVITY'],
  ['META', 'gui.hubcat.META'],
  ['OTHER', 'gui.hubcat.OTHER'],
]

const hubCatLabel = (key: string): string => {
  const c = HUB_CATS.find((x) => x[0] === key)
  return c ? t(c[1]) : key
}

const hubNum = (n: number): string => (n >= 10000 ? `${(n / 1000).toFixed(0)}k` : n.toLocaleString('en-US'))

/* Same stable-hue letter tile as the plugin market (hash duplicated so the
   island stays free of the plugin layer's helpers). */
function Tile({ name }: { name: string }): JSX.Element {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return <span className={'pmtile th' + (h % 8)}>{(name[0] || '?').toUpperCase()}</span>
}

/* quality_score is 0-1; the site shows it out of five, so do the same. */
function Stars({ score }: { score?: number | null }): JSX.Element {
  const val = Math.round((score || 0) * 5 * 10) / 10
  return (
    <div className="stars">
      {[1, 2, 3, 4, 5].map((i) => {
        const fill = Math.max(0, Math.min(1, val - i + 1))
        return (
          <span
            key={i}
            className="st"
            ref={(el) => {
              if (el) el.style.setProperty('--f', `${Math.round(fill * 100)}%`)
            }}
          >
            ★
          </span>
        )
      })}
      <span className="sv">{val.toFixed(1)}</span>
    </div>
  )
}

/* Placeholder card shown the instant a chip / query flips, so the page
   answers the click immediately instead of freezing on stale results. */
export function Skeleton(): JSX.Element {
  return (
    <div className="hubcard skel" aria-hidden="true">
      <div className="top">
        <div className="pmhead">
          <span className="sk" style={{ width: 34, height: 34, borderRadius: 10, flex: 'none' }} />
          <div className="pmid" style={{ display: 'grid', gap: 6 }}>
            <span className="sk" style={{ width: 110, height: 12 }} />
            <span className="sk" style={{ width: 70, height: 9 }} />
          </div>
        </div>
      </div>
      <div style={{ display: 'grid', gap: 7 }}>
        <span className="sk" style={{ width: '100%', height: 10 }} />
        <span className="sk" style={{ width: '72%', height: 10 }} />
      </div>
      <div className="foot">
        <span className="sk" style={{ width: 58, height: 22, marginLeft: 'auto', borderRadius: 8 }} />
      </div>
    </div>
  )
}

/* No window.confirm (the WKWebView shell has no JS-panel delegate): a
   destructive button arms on first click and fires on the second. */
function ArmRemove({ fn }: { fn: () => void }): JSX.Element {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const timer = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(timer)
  }, [armed])
  return (
    <button
      className={'mini ghost' + (armed ? ' bad' : '')}
      style={{ marginTop: 8 }}
      onClick={(e) => {
        e.stopPropagation()
        if (!armed) {
          setArmed(true)
          return
        }
        fn()
      }}
    >
      {t(armed ? 'gui.plug.confirm_remove' : 'gui.plug.uninstall')}
    </button>
  )
}

/* ── market view: same card grammar as pmMarketCard ─────────────── */

function HubCard({ it, busy }: { it: HubItem; busy: string | null }): JSX.Element {
  const pub = it.source || hubCatLabel(it.category || '')
  return (
    <div
      className={'hubcard pmcard' + (it.installed ? ' dim' : '')}
      tabIndex={0}
      role="button"
      onClick={() => store.openDetail('market', it.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') store.openDetail('market', it.id)
      }}
    >
      <div className="top">
        <div className="pmhead">
          <Tile name={it.name} />
          <div className="pmid">
            <div className="pmnm">
              <span>{it.name}</span>
            </div>
            {pub ? <div className="pmpub">{pub}</div> : null}
          </div>
        </div>
        <Stars score={it.quality_score} />
      </div>
      <p className="one">{it.description || ''}</p>
      <div className="foot">
        <div className="act">
          {busy === it.id ? (
            <span className="pnote">{t('gui.hub.working')}</span>
          ) : it.installed ? (
            <span className="okpill">{t('gui.hub.installed')}</span>
          ) : (
            <button
              className="mini gold"
              onClick={(e) => {
                // Two hit zones: the button installs right away, the card opens the sheet.
                e.stopPropagation()
                store.install(it)
              }}
            >
              {t('gui.hub.install')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

function Pager({ page, total }: { page: number; total: number }): JSX.Element | null {
  const pages = Math.max(1, Math.ceil(total / store.HUB_PAGE))
  if (pages <= 1) return null
  return (
    <div className="hubpage">
      <button className="mini ghost" disabled={page <= 1} onClick={() => store.search(page - 1)}>
        {t('gui.hub.prev')}
      </button>
      <span className="pnote">{t('gui.hub.page', { p: page, n: hubNum(pages) })}</span>
      <button className="mini ghost" disabled={page >= pages} onClick={() => store.search(page + 1)}>
        {t('gui.hub.next')}
      </button>
    </div>
  )
}

function SkillMarket({ s }: { s: SkillsState }): JSX.Element {
  return (
    <>
      <div className="pmchips">
        {HUB_CATS.map(([key, label]) => (
          <button key={key || 'all'} className="pill" aria-pressed={s.cat === key} onClick={() => store.setCat(key)}>
            {t(label)}
          </button>
        ))}
      </div>
      {s.err ? (
        <div className="empty-note">
          <div>{t('gui.hub.err', { err: s.err })}</div>
          <br />
          <button className="mini ghost" onClick={() => store.search(s.page)}>
            {t('gui.plug.retry')}
          </button>
        </div>
      ) : s.hub === 'loading' ? (
        <div className="hubgrid">
          {Array.from({ length: 9 }, (_, i) => (
            <Skeleton key={i} />
          ))}
        </div>
      ) : s.items.length === 0 ? (
        <div className="empty-note">{t('gui.hub.empty_filter')}</div>
      ) : (
        <>
          <div className="hubgrid">
            {s.items.map((it) => (
              <HubCard key={it.id} it={it} busy={s.busy} />
            ))}
          </div>
          <Pager page={s.page} total={s.total} />
        </>
      )}
    </>
  )
}

/* ── installed view: same shape as drawPlugInstalled ────────────── */

function SkillInstalled(): JSX.Element {
  const rows = store.installedRows()
  return (
    <>
      <div className="pmback">
        <button className="mini ghost" onClick={() => store.toggleView()}>
          {'← ' + t('gui.plug.back')}
        </button>
        <b>{t('gui.plug.installed_title')}</b>
      </div>
      {rows.length === 0 ? (
        <div className="empty-note">{t('gui.hub.empty_installed')}</div>
      ) : (
        <div className="hubgrid">
          {rows.map((c) => (
            <div
              key={c.id}
              className="hubcard pmcard"
              tabIndex={0}
              role="button"
              onClick={() => store.openDetail('inst', c.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') store.openDetail('inst', c.id)
              }}
            >
              <div className="top">
                <div className="pmhead">
                  <Tile name={c.name} />
                  <div className="pmid">
                    <div className="pmnm">
                      <span>{c.name}</span>
                    </div>
                    {c.src ? <div className="pmpub">{c.src}</div> : null}
                  </div>
                </div>
              </div>
              <p className="one">{c.one || ''}</p>
              <div className="foot">
                <span />
                <div className="act">
                  <button
                    className="mini gold"
                    onClick={(e) => {
                      e.stopPropagation()
                      shell().useInTask?.('gui.hub.use_prompt', c.name)
                    }}
                  >
                    {t('gui.hub.use')}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

/* ── detail drawer (market entries + installed skills) ──────────── */

/* One renderer for both entry points: an installed skill opens the exact
   hub detail the market shows (fetched via the id its marker recorded);
   the only delta is the action set -- use in the header, uninstall under
   manage. Hand-written/builtin skills have no hub entry to show, so they
   get the same head/about/manage skeleton without the hub sections. */
function SkillDetail({ s, drawer }: { s: SkillsState; drawer: NonNullable<SkillsState['drawer']> }): JSX.Element | null {
  const inst = drawer.kind === 'inst' ? (store.installedRows().find((x) => x.id === drawer.id) ?? null) : null
  const it =
    drawer.kind === 'market'
      ? (s.items.find((x) => x.id === drawer.id) ?? null)
      : inst
        ? (s.items.find((x) => x.name === inst.name || x.installed_name === inst.name) ?? null)
        : null
  const hubId = drawer.kind === 'market' ? drawer.id : inst?.hubId || it?.id || ''
  const d = hubId ? s.details[hubId] : undefined
  useEffect(() => {
    if (hubId && !d) store.fetchDetail(hubId)
  }, [hubId, d])
  if (drawer.kind === 'inst' && !inst) return null

  const installed = Boolean(inst || it?.installed)
  const busy = s.busy != null && (s.busy === it?.id || (inst != null && s.busy === inst.id))
  const name = inst?.name || it?.name || drawer.id

  const header = (metaBits: Array<string | undefined>, sourceUrl?: string): JSX.Element => (
    <div className="pmdhead">
      <Tile name={name} />
      <div className="pmdmeta">
        <div className="l1">
          <b>{name}</b>
        </div>
        <div className="l2">
          {metaBits.filter(Boolean).join(' · ') + (sourceUrl ? ' · ' : '')}
          {sourceUrl ? (
            <a href={sourceUrl} target="_blank" rel="noreferrer">
              {t('gui.plug.homepage') + ' ↗'}
            </a>
          ) : null}
        </div>
      </div>
      <div className="dact">
        {busy ? (
          <span className="pnote">{t('gui.hub.working')}</span>
        ) : installed ? (
          <button className="mini gold" onClick={() => shell().useInTask?.('gui.hub.use_prompt', name)}>
            {t('gui.hub.use')}
          </button>
        ) : (
          <button className="mini gold" onClick={() => store.install(it ?? { id: hubId, name })}>
            {t('gui.hub.install')}
          </button>
        )}
      </div>
    </div>
  )

  const manage = (removable: boolean | undefined, fn: () => void): JSX.Element | null =>
    !removable || busy ? null : (
      <div className="pmsec">
        <div className="cap">{t('gui.plug.sec_manage')}</div>
        <div>
          <div className="pnote">{t('gui.hub.uninstall_note')}</div>
          <ArmRemove fn={fn} />
        </div>
      </div>
    )

  if (!hubId) {
    return (
      <>
        {header([inst?.src, inst?.reach ? shell().reachText?.(inst.reach) : undefined])}
        {inst?.one ? (
          <div className="pmsec">
            <div className="cap">{t('gui.plug.sec_about')}</div>
            <div className="pmdesc">{inst.one}</div>
          </div>
        ) : null}
        {manage(inst?.hub, () => {
          if (inst) store.removeInstalled(inst)
        })}
      </>
    )
  }

  if (!d) return <div className="pnote">{t('gui.hub.reading')}</div>

  const tags = (d.tags && d.tags.length ? d.tags : it?.tags) || []
  const sub = d.subscores || {}
  const files = d.files || []
  return (
    <>
      {header([hubCatLabel(d.category || '') || d.category, d.source || it?.source, d.license], it?.source_url)}
      <div className="pmsec">
        <div className="cap">{t('gui.plug.sec_about')}</div>
        <div>
          <div className="pmdesc">{d.description || it?.description || inst?.one || ''}</div>
          {tags.length ? (
            <div className="tags" style={{ marginTop: 8 }}>
              {tags.map((x) => (
                <span key={x} className="tag">
                  {x}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      </div>
      <div className="pmsec">
        <div className="cap">{t('gui.hub.sec_rating')}</div>
        <div>
          <Stars score={d.quality_score != null ? d.quality_score : it?.quality_score} />
          {sub.utility || sub.robustness || sub.safety ? (
            <div className="pnote">
              {t('gui.hub.rating', { u: sub.utility ?? '', r: sub.robustness ?? '', s: sub.safety ?? '' })}
            </div>
          ) : null}
          {sub.flags && sub.flags.length ? (
            <div className="perr">{t('gui.hub.flags', { flags: sub.flags.join(', ') })}</div>
          ) : null}
        </div>
      </div>
      {files.length ? (
        <div className="pmsec">
          <div className="cap">{t('gui.hub.n_files', { n: files.length })}</div>
          <div>
            <div className="pnote">
              {files.slice(0, 12).join('  ·  ') +
                (files.length > 12 ? '  ·  ' + t('gui.hub.more_files', { n: files.length - 12 }) : '')}
            </div>
            {d.body_tokens ? <div className="pnote">{`${hubNum(d.body_tokens)} tokens`}</div> : null}
          </div>
        </div>
      ) : null}
      {d.skill_md ? (
        <div className="pmsec">
          <div className="cap">{t('gui.hub.sec_preview')}</div>
          <pre className="mdprev">{d.skill_md.slice(0, 1600)}</pre>
        </div>
      ) : null}
      {manage(installed, () => (inst ? store.removeInstalled(inst) : it ? store.removeMarket(it) : undefined))}
    </>
  )
}

/* The drawer's own host node, re-attached under #dBody whenever a skill
   sheet opens: the plugin and memory drawers clear #dBody with innerHTML,
   which must never tear down nodes React owns. */
const drawerHost = document.createElement('div')

export function SkillsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  useEffect(() => {
    if (!s.drawer) return
    const dBody = document.getElementById('dBody')
    const detail = document.getElementById('detail')
    if (!dBody || !detail) return
    if (drawerHost.parentElement !== dBody) {
      dBody.innerHTML = ''
      dBody.appendChild(drawerHost)
    }
    const dTitle = document.getElementById('dTitle')
    if (dTitle) dTitle.textContent = ''
    detail.dataset.open = 'true'
  }, [s.drawer])
  return (
    <>
      {s.view === 'installed' ? <SkillInstalled /> : <SkillMarket s={s} />}
      {s.drawer ? createPortal(<SkillDetail s={s} drawer={s.drawer} />, drawerHost) : null}
    </>
  )
}
