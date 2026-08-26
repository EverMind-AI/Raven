import { Fragment, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'
import * as store from './store'

import type { Contribution, DetailEntry, InstalledRow, MarketItem, McpSnapshot } from './types'
import type { CSSProperties, JSX } from 'react'

/* The plugin tab is market-first: the page IS the catalog, and what you
   already have lives one level in (the installed button top-right, back
   arrow to return). Both views share the hub grid so they read as the
   same place. Everything here is a transcription of the legacy renderer
   the live layer used to carry -- class names and structure unchanged. */

function Tile({ name }: { name: string }): JSX.Element {
  // Stable per-name hue: same plugin, same colour, every render and page.
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return <span className={'pmtile th' + (h % 8)}>{(name[0] || '?').toUpperCase()}</span>
}

const Vfd = (): JSX.Element => (
  <svg className="pmvfd" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 2l2.4 2.4 3.4-.5 1 3.3 3.2 1.4-1.3 3.4 1.3 3.4-3.2 1.4-1 3.3-3.4-.5L12 22l-2.4-2.4-3.4.5-1-3.3-3.2-1.4L3.3 12 2 8.6l3.2-1.4 1-3.3 3.4.5z" />
    <path d="M9.5 12.2l1.8 1.8 3.6-3.8" stroke="var(--ink)" strokeWidth="2" fill="none" />
  </svg>
)

/* The category catalogue covers the known ids; an unknown one keeps its raw
   name -- the same fallback the legacy T(key, null, cat) call expressed. */
function catLabel(cat: string): string {
  const key = 'gui.plug.cat_' + cat
  const label = t(key)
  return label === key ? cat : label
}

function StBadge({ m }: { m: McpSnapshot }): JSX.Element {
  const st = store.status(m)
  return (
    <span className={'pmst ' + st.cls} title={st.cls === 'bad' && m.error ? m.error : undefined}>
      <i className="pmdot" />
      <span>{t(st.k)}</span>
    </span>
  )
}

/* No window.confirm (the WKWebView shell has no JS-panel delegate): a
   destructive button arms on first click and fires on the second. */
function ArmButton({
  label,
  onFire,
  className,
  style,
}: {
  label: string
  onFire: () => void
  className: string
  style?: CSSProperties
}): JSX.Element {
  const [armed, setArmed] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )
  return (
    <button
      className={className + (armed ? ' bad' : '')}
      style={style}
      onClick={(e) => {
        e.stopPropagation()
        if (!armed) {
          setArmed(true)
          timer.current = setTimeout(() => setArmed(false), 4000)
          return
        }
        onFire()
      }}
    >
      {armed ? t('gui.plug.confirm_remove') : label}
    </button>
  )
}

export function PlugApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  return (
    <>
      {s.view === 'installed' ? <Installed s={s} /> : <Market s={s} />}
      {s.drawer ? <DrawerHost key={`${s.drawer.kind}:${s.drawer.id}`} drawer={s.drawer} s={s} /> : null}
    </>
  )
}

/* ── market view ─────────────────────────────────────────────────── */

function Market({ s }: { s: store.PlugState }): JSX.Element {
  return (
    <>
      <div className="pmchips">
        <Chip label={t('gui.filter.all')} val="" cur={s.cat} />
        {s.cats.map((cat) => (
          <Chip key={cat} label={catLabel(cat)} val={cat} cur={s.cat} />
        ))}
      </div>
      {s.err ? (
        <div className="empty-note">
          <div>{t('gui.plug.market_down')}</div>
          <br />
          <button className="mini ghost" onClick={() => void store.search()}>
            {t('gui.plug.retry')}
          </button>
        </div>
      ) : s.marketState === 'loading' && !s.items.length ? (
        <div className="empty-note">{t('gui.hub.reading')}</div>
      ) : !s.items.length ? (
        <div className="empty-note">{t('gui.plug.none_found', { q: s.query })}</div>
      ) : (
        <div className="hubgrid">
          {s.items.map((it) => (
            <MarketCard key={it.id} it={it} s={s} />
          ))}
        </div>
      )}
    </>
  )
}

function Chip({ label, val, cur }: { label: string; val: string; cur: string }): JSX.Element {
  return (
    <button className="pill" aria-pressed={cur === val} onClick={() => store.setCat(val)}>
      {label}
    </button>
  )
}

function MarketCard({ it, s }: { it: MarketItem; s: store.PlugState }): JSX.Element {
  const bits: string[] = []
  if (it.tool_preview_count) bits.push(t('gui.plug.tools_n', { n: it.tool_preview_count }))
  if (it.skill_count) bits.push(t('gui.plug.skills_n', { n: it.skill_count }))
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
              {it.verified && (
                <span title={t('gui.plug.verified')}>
                  <Vfd />
                </span>
              )}
            </div>
            {it.publisher ? <div className="pmpub">{it.publisher}</div> : null}
          </div>
        </div>
      </div>
      <p className="one">{it.summary || ''}</p>
      <div className="foot">
        <span className="pmcnt">
          {bits.join(' · ')}
          {it.risk_tier === 2 && <span className="pmsign gold">{t('gui.plug.local_run')}</span>}
          {(it.risk_tier ?? 0) >= 3 && (
            <span className="pmsign faint">{t('gui.plug.needs_restart')}</span>
          )}
        </span>
        <div className="act">
          {s.busy === it.id ? (
            <span className="pnote">{t('gui.hub.working')}</span>
          ) : store.pendingHas(it.id) ? (
            <span className="pnote">{t('gui.plug.st_wait')}</span>
          ) : it.installed ? (
            <span className="okpill">{t('gui.hub.installed')}</span>
          ) : (
            // Two hit zones: the button installs right away (or lands on the
            // form when one is needed), the card opens the sheet.
            <button
              className="mini gold"
              onClick={(e) => {
                e.stopPropagation()
                store.quickInstall(it)
              }}
            >
              {t('gui.plug.install')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/* ── installed view ──────────────────────────────────────────────── */

function Installed({ s }: { s: store.PlugState }): JSX.Element {
  // A pending-auth install is not installed yet: it stays a market-side
  // waiting card until its authentication settles.
  const py = store.pyRows()
  const mcps = store.mcpRows().filter((p) => p.m && !store.pendingHas(p.m.name))
  return (
    <>
      <div className="pmback">
        <button className="mini ghost" onClick={() => store.backToMarket()}>
          {'← ' + t('gui.plug.back')}
        </button>
        <b>{t('gui.plug.installed_title')}</b>
      </div>
      <Section label={t('gui.plug.grp_builtin')}>
        {py.map((row) => (
          <PyCard key={row.id} row={row} />
        ))}
      </Section>
      <Section label={t('gui.plug.grp_market')}>
        {mcps.map((row) => (
          <McpCard key={row.id} row={row} />
        ))}
      </Section>
      {!py.length && !mcps.length && (
        <div className="empty-note">
          {t(store.rowsLoaded() ? 'gui.plug.empty_installed' : 'gui.plug.empty_installed_off')}
        </div>
      )}
    </>
  )
}

function Section({ label, children }: { label: string; children: JSX.Element[] }): JSX.Element | null {
  if (!children.length) return null
  return (
    <div className="csec">
      <div className="hd">
        <b>{label}</b>
        <span className="n">{String(children.length)}</span>
      </div>
      <div className="hubgrid">{children}</div>
    </div>
  )
}

function McpCard({ row }: { row: InstalledRow }): JSX.Element | null {
  const m = row.m
  if (!m) return null
  const st = store.status(m)
  const bits = [store.fromMarket(m.name) ? t('gui.plug.from_market') : t('gui.plug.manual'), m.transport ?? '']
  if (m.tool_count) bits.push(t('gui.plug.tools_n', { n: m.tool_count }))
  const wait = store.authUrl(m.name)
  return (
    <div
      className={'hubcard pmcard' + (st.cls === 'bad' ? ' pmbad' : '')}
      tabIndex={0}
      role="button"
      onClick={() => store.openDetail('inst', m.name)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') store.openDetail('inst', m.name)
      }}
    >
      <div className="top">
        <div className="pmhead">
          <Tile name={m.name} />
          <div className="pmnm">
            <span>{m.name}</span>
          </div>
        </div>
        <button
          className="swi"
          role="switch"
          aria-checked={m.enabled}
          aria-label={t('gui.caps.toggle_aria', { name: m.name })}
          onClick={(e) => {
            e.stopPropagation()
            store.toggleMcp(m.name, !m.enabled)
          }}
        />
      </div>
      <p className="one">{bits.join(' · ')}</p>
      <div className="foot">
        <StBadge m={m} />
        <div className="act">
          {/* The card keeps only the high-frequency verbs (the enable switch
              and one context action); uninstall lives in the detail. */}
          {wait ? (
            <button
              className="mini"
              onClick={(e) => {
                e.stopPropagation()
                window.open(wait, '_blank')
              }}
            >
              {t('gui.plug.reopen')}
            </button>
          ) : m.state === 'auth_required' || m.state === 'error' ? (
            <button
              className="mini"
              onClick={(e) => {
                e.stopPropagation()
                store.auth(m.name)
              }}
            >
              {t(m.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair')}
            </button>
          ) : m.enabled ? (
            <button
              className="mini gold"
              onClick={(e) => {
                e.stopPropagation()
                shell().useInTask?.('gui.plug.use_prompt', m.name)
              }}
            >
              {t('gui.hub.use')}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}

function PyCard({ row }: { row: InstalledRow }): JSX.Element {
  const st = row.state === 'on' ? store.PM_ST.connected! : store.PM_ST.off!
  return (
    <div className="hubcard pmcard">
      <div className="top">
        <div className="pmhead">
          <Tile name={row.name} />
          <div className="pmnm">
            <span>{row.name}</span>
          </div>
        </div>
        <button
          className="swi"
          role="switch"
          aria-checked={row.state === 'on'}
          aria-label={t('gui.caps.toggle_aria', { name: row.name })}
          onClick={(e) => {
            e.stopPropagation()
            store.togglePy(row)
          }}
        />
      </div>
      <p className="one">
        {[row.src, row.ver && row.ver !== '—' ? 'v' + row.ver : ''].filter(Boolean).join(' · ')}
      </p>
      <div className="foot">
        <span className={'pmst ' + st.cls}>
          <i className="pmdot" />
          <span>{t(st.k)}</span>
        </span>
        <span className="pmcnt">
          <span className="pmsign faint">{t('gui.plug.needs_restart')}</span>
        </span>
      </div>
    </div>
  )
}

/* ── detail drawer (market entries + installed servers) ──────────── */

/* The drawer renders into the shared #detail chrome through a portal host
   the island owns: other openers clear #dBody with innerHTML, which only
   detaches this host -- React keeps rendering into it unharmed. */
function DrawerHost({ drawer, s }: { drawer: store.Drawer; s: store.PlugState }): JSX.Element {
  const host = useMemo(() => document.createElement('div'), [])
  useEffect(() => {
    const body = document.getElementById('dBody')
    const detail = document.getElementById('detail')
    const title = document.getElementById('dTitle')
    if (!body || !detail) return
    body.innerHTML = ''
    if (title) title.textContent = ''
    body.appendChild(host)
    detail.dataset.open = 'true'
    return () => {
      host.remove()
      detail.dataset.open = 'false'
    }
  }, [host])
  return createPortal(
    drawer.kind === 'progress' ? (
      <Progress s={s} />
    ) : drawer.kind === 'market' ? (
      <MarketDetail id={drawer.id} s={s} />
    ) : (
      <InstDetail id={drawer.id} />
    ),
    host,
  )
}

function Sec({ label, children }: { label: string; children: JSX.Element }): JSX.Element {
  return (
    <div className="pmsec">
      <div className="cap">{label}</div>
      {children}
    </div>
  )
}

function ConnInfo({ m }: { m: McpSnapshot }): JSX.Element {
  const st = store.status(m)
  return (
    <div>
      <StBadge m={m} />
      {m.error && st.cls === 'bad' ? <div className="perr">{m.error}</div> : null}
      {m.tool_count ? <div className="pnote">{t('gui.plug.tools_n', { n: m.tool_count })}</div> : null}
    </div>
  )
}

/* Install progress sheet: the install drives this sheet through the whole
   transaction -- write config -> connect -> (browser authorization) -> done.
   Closing it leaves the install running; the card reopens it. */
function Progress({ s }: { s: store.PlugState }): JSX.Element | null {
  const pg = s.prog
  if (!pg) return null
  const authPhase = pg.state === 'run' && pg.mode === 'oauth' && pg.step >= 2
  const wait = store.authUrl(pg.id)
  const keys = ['prog_write', 'prog_conn', ...(pg.mode === 'oauth' ? ['prog_auth'] : []), 'prog_finish']
  return (
    <>
      <div className="pmdhead">
        <Tile name={pg.name} />
        <div className="pmdmeta">
          <div className="l1">
            <b>{pg.name}</b>
          </div>
          <div className="l2">{t('gui.plug.prog_cap')}</div>
        </div>
        <div className="dact">
          {authPhase ? (
            <>
              {wait && (
                <button className="mini" onClick={() => window.open(wait, '_blank')}>
                  {t('gui.plug.reopen')}
                </button>
              )}
              <button className="mini ghost" onClick={() => store.cancelPending(pg.id, pg.name)}>
                {t('gui.plug.cancel')}
              </button>
            </>
          ) : pg.state === 'run' ? (
            <span className="pnote">{t('gui.hub.working')}</span>
          ) : (
            <>
              {pg.state === 'done' && (
                <button
                  className="mini gold"
                  onClick={() => shell().useInTask?.('gui.plug.use_prompt', pg.name)}
                >
                  {t('gui.hub.use')}
                </button>
              )}
              <button className="mini ghost" onClick={() => store.drawerClosed()}>
                {t('gui.close')}
              </button>
            </>
          )}
        </div>
      </div>
      <div className="pmsteps">
        {keys.map((k, i) => {
          const done = pg.state === 'done' || i < pg.step
          const cur = pg.state !== 'done' && !done && i === Math.min(pg.step, keys.length - 1)
          const cls = done ? ' done' : cur ? (pg.state === 'fail' ? ' fail' : ' cur') : ''
          return (
            <div key={k} className={'pstep' + cls}>
              <i className="ic">{done ? '✓' : cur && pg.state === 'fail' ? '✕' : ''}</i>
              <span>{t('gui.plug.' + k)}</span>
            </div>
          )
        })}
      </div>
      {pg.state === 'done' ? (
        <div className="pnote">
          {pg.tools == null
            ? t('gui.plug.installed_conn', { name: pg.name })
            : t('gui.plug.prog_ok', { n: pg.tools })}
        </div>
      ) : pg.state === 'fail' ? (
        <>
          <div className="pnote">{t('gui.plug.auth_fail_rm', { name: pg.name })}</div>
          {pg.err ? <div className="perr">{pg.err}</div> : null}
        </>
      ) : authPhase ? (
        <div className="pnote">
          {t(wait ? 'gui.plug.prog_auth_hint' : 'gui.plug.prog_auth_soon', { host: pg.host })}
          {store.authEndsAt(pg.id) ? (
            <>
              {' '}
              <span className="pcd">
                {t('gui.plug.auth_left') + ' '}
                <span className="pcd b" data-authcd={pg.id}>
                  {store.authLeft(pg.id)}
                </span>
              </span>
            </>
          ) : null}
        </div>
      ) : pg.mode === 'oauth' ? (
        <div className="pnote">{t('gui.plug.prog_auth_soon', { host: pg.host })}</div>
      ) : null}
    </>
  )
}

function Perms({ entry }: { entry: DetailEntry }): JSX.Element {
  const mcp = store.entryMcp(entry)
  const lines: string[] = []
  if (mcp) {
    const mode = (mcp.auth || {}).mode || 'none'
    const local = Boolean((mcp.connection || {}).command)
    if (mode === 'oauth') lines.push(t('gui.plug.perm_oauth', { host: store.hostOf((mcp.connection || {}).url) }))
    if (mode === 'apikey') lines.push(t('gui.plug.perm_key'))
    if (local) lines.push(t('gui.plug.perm_local'))
    // Where the data goes is already stated by the oauth line for oauth servers.
    else if (mode !== 'oauth') lines.push(t('gui.plug.perm_net', { host: store.hostOf((mcp.connection || {}).url) }))
  }
  const skills = (entry.contributes || []).filter((c) => c.kind === 'skill').length
  if (skills) lines.push(t('gui.plug.perm_skill', { n: skills }))
  return (
    <div className="pmperm">
      {lines.map((txt, i) => (
        <div key={i} className="row">
          <i className="pmdot" />
          <span>{txt}</span>
        </div>
      ))}
    </div>
  )
}

/* A plugin is a package of contributions (today: MCP servers and skills).
   Each piece renders as name line -> connection meta -> what it brings. */
function Contents({ entry }: { entry: DetailEntry }): JSX.Element {
  const cts = entry.contributes || []
  return (
    <div>
      {cts.map((co, i) => (
        <div key={i} className="pmperm" style={i ? { marginTop: 14 } : undefined}>
          <div className="row">
            <span className="kd">{co.kind === 'skill' ? t('gui.plug.ct_skill') : 'MCP'}</span>
            <b>{co.kind === 'mcp' ? entry.id : co.name || co.skillhub_id || entry.id}</b>
          </div>
          {co.kind === 'mcp' ? <McpPiece co={co} /> : null}
        </div>
      ))}
    </div>
  )
}

function McpPiece({ co }: { co: Contribution }): JSX.Element {
  const conn = co.connection || {}
  const meta = (conn.command
    ? ['stdio', [conn.command].concat(conn.args || []).join(' ').slice(0, 60)]
    : [conn.type || 'http', store.hostOf(conn.url)]
  )
    .filter(Boolean)
    .join(' · ')
  const tools = co.tools_preview || []
  return (
    <>
      <div className="pnote" style={{ margin: '4px 0 0 2px' }}>
        {meta}
      </div>
      {tools.length > 0 && (
        <>
          <div className="cap" style={{ margin: '8px 0 4px 2px' }}>
            {t('gui.plug.tools_n', { n: tools.length })}
          </div>
          <div style={{ margin: '0 0 0 2px' }}>
            {tools.map((x) => (
              <div key={x} className="pmtool">
                {x}
              </div>
            ))}
          </div>
        </>
      )}
    </>
  )
}

function StdioWarn({
  entry,
  collect,
}: {
  entry: DetailEntry
  collect: (() => Record<string, string> | null) | null
}): JSX.Element | null {
  const mcp = store.entryMcp(entry)
  if (!mcp || !mcp.connection) return null
  const cmd = [mcp.connection.command, ...(mcp.connection.args || [])].join(' ')
  return (
    <div className="pmwarn">
      <div className="wt">{t('gui.plug.stdio_warn')}</div>
      <pre className="pmcmd">{cmd}</pre>
      <div className="pnote">{t('gui.plug.stdio_trust')}</div>
      <div className="pmrow">
        <button className="mini ghost" onClick={() => store.foldForms(entry.id)}>
          {t('gui.plug.cancel')}
        </button>
        <button
          className="mini"
          onClick={() => {
            const form = collect ? collect() : {}
            if (form) store.install(entry, form)
          }}
        >
          {t('gui.plug.still_install')}
        </button>
      </div>
    </div>
  )
}

function InstallControls({
  entry,
  host,
  s,
}: {
  entry: DetailEntry
  host: string
  s: store.PlugState
}): JSX.Element {
  const mcp = store.entryMcp(entry)
  const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none'
  const stdio = Boolean(mcp && (mcp.connection || {}).command)
  const inputs = useRef(new Map<string, HTMLInputElement>())
  const collect = (): Record<string, string> | null => {
    const form: Record<string, string> = {}
    let missing = false
    inputs.current.forEach((inp, k) => {
      form[k] = inp.value.trim()
      if (!form[k]) missing = true
    })
    if (missing) {
      toast(t('gui.plug.need_key'))
      return null
    }
    return form
  }

  if (s.form && mode === 'apikey' && mcp) {
    const fields = (mcp.auth || {}).fields || []
    return (
      <div>
        {fields.map((f) => (
          <Fragment key={f.key}>
            <label className="pmlab">
              <span>{f.label || f.key}</span>
              {f.help_url && (
                <a className="pmhelp" href={f.help_url} target="_blank" rel="noreferrer">
                  {t('gui.plug.how_get')}
                </a>
              )}
            </label>
            <input
              type={f.secret ? 'password' : 'text'}
              className="pminp"
              autoComplete="off"
              ref={(el) => {
                if (el) inputs.current.set(f.key, el)
                else inputs.current.delete(f.key)
              }}
            />
          </Fragment>
        ))}
        {stdio ? (
          // The key form and the run-locally warning show together: one
          // scroll, one decision -- the warning block owns the action row.
          <StdioWarn entry={entry} collect={collect} />
        ) : (
          <div className="pmrow">
            <button className="mini ghost" onClick={() => store.foldForms(entry.id)}>
              {t('gui.plug.cancel')}
            </button>
            <button
              className="mini gold"
              onClick={() => {
                const form = collect()
                if (form) store.install(entry, form)
              }}
            >
              {t('gui.plug.connect')}
            </button>
          </div>
        )}
      </div>
    )
  }

  if (s.confirm && stdio) {
    return (
      <div>
        <StdioWarn entry={entry} collect={null} />
      </div>
    )
  }

  return (
    <div>
      <button
        className="mini gold"
        disabled={s.busy === entry.id}
        onClick={() => {
          if (mode === 'apikey' && !s.form) {
            store.unfoldForm(entry.id)
            return
          }
          if (stdio && !s.confirm && mode !== 'apikey') {
            store.unfoldConfirm(entry.id)
            return
          }
          store.install(entry, {})
        }}
      >
        {s.busy === entry.id ? t('gui.hub.working') : t('gui.plug.install')}
      </button>
      {host ? <div className="pnote">{t('gui.plug.oauth_note', { host })}</div> : null}
    </div>
  )
}

/* An installed market plugin opens the same detail the market shows -- full
   package content -- with connection state and manage actions layered in. */
function MarketDetail({ id, s }: { id: string; s: store.PlugState }): JSX.Element {
  const [got, setGot] = useState<{ entry: DetailEntry; installed: boolean } | null>(null)
  useEffect(() => {
    let stale = false
    store
      .source()
      .detail(id)
      .then((r) => {
        if (!stale) setGot(r)
      })
      // The source already toasted; the drawer keeps its reading note.
      .catch(() => {})
    return () => {
      stale = true
    }
  }, [id])
  if (!got) return <div className="pnote">{t('gui.hub.reading')}</div>

  const entry = got.entry
  const it = s.items.find((x) => x.id === id)
  const mcp = store.entryMcp(entry)
  const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none'
  const stdio = Boolean(mcp && (mcp.connection || {}).command)
  // A pending-auth install never reads as installed: the ledger entry exists
  // on disk, but the install completes (or rolls back) with auth.
  const isPending = store.pendingHas(entry.id)
  const installed = !isPending && (got.installed || Boolean(it && it.installed))
  const lrow = installed || isPending ? store.mcpRows().find((p) => p.m && p.m.name === entry.id) : null
  const lm = lrow && lrow.m
  const wait = store.authUrl(entry.id)
  const busy = s.busy === entry.id
  const oauthHost = mcp && mode === 'oauth' ? store.hostOf((mcp.connection || {}).url) : ''
  const cts = entry.contributes || []

  return (
    <>
      <div className="pmdhead">
        <Tile name={entry.name} />
        <div className="pmdmeta">
          <div className="l1">
            <b>{entry.name}</b>
            {(entry.publisher || {}).verified && (
              <span>
                <Vfd />
              </span>
            )}
          </div>
          <div className="l2">
            {[(entry.publisher || {}).name, entry.version ? 'v' + entry.version : '']
              .filter(Boolean)
              .join(' · ') + (entry.homepage ? ' · ' : '')}
            {entry.homepage && (
              <a href={entry.homepage} target="_blank" rel="noreferrer">
                {t('gui.plug.homepage') + ' ↗'}
              </a>
            )}
          </div>
        </div>
        {/* The decisive control rides the identity row; forms unfold below. */}
        <div className="dact">
          {busy ? (
            <span className="pnote">{t('gui.hub.working')}</span>
          ) : isPending ? (
            <>
              {wait && (
                <button className="mini" onClick={() => window.open(wait, '_blank')}>
                  {t('gui.plug.reopen')}
                </button>
              )}
              <button className="mini ghost" onClick={() => store.cancelPending(entry.id, entry.name)}>
                {t('gui.plug.cancel')}
              </button>
            </>
          ) : installed ? (
            wait ? (
              <button className="mini" onClick={() => window.open(wait, '_blank')}>
                {t('gui.plug.reopen')}
              </button>
            ) : lm && (lm.state === 'auth_required' || lm.state === 'error') ? (
              <button className="mini" onClick={() => store.auth(entry.id)}>
                {t(lm.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair')}
              </button>
            ) : !lm || lm.enabled ? (
              <button
                className="mini gold"
                onClick={() => shell().useInTask?.('gui.plug.use_prompt', entry.name)}
              >
                {t('gui.hub.use')}
              </button>
            ) : (
              <span className="okpill">{t('gui.hub.installed')}</span>
            )
          ) : !s.form && !s.confirm ? (
            <button
              className="mini gold"
              onClick={() => {
                if (mode === 'apikey') {
                  store.unfoldForm(entry.id)
                  return
                }
                if (stdio) {
                  store.unfoldConfirm(entry.id)
                  return
                }
                store.install(entry, {})
              }}
            >
              {t('gui.plug.install')}
            </button>
          ) : null}
        </div>
      </div>
      {isPending ? (
        <div className="pnote">{t('gui.plug.wait_auth', { name: entry.name })}</div>
      ) : !installed && (s.form || s.confirm) ? (
        <Sec label={t(s.form ? 'gui.plug.sec_cred' : 'gui.plug.sec_confirm')}>
          <InstallControls entry={entry} host={oauthHost} s={s} />
        </Sec>
      ) : !installed && mode === 'oauth' ? (
        <div className="pnote">{t('gui.plug.oauth_note', { host: oauthHost })}</div>
      ) : null}
      {lm ? (
        <Sec label={t('gui.plug.sec_conn')}>
          <ConnInfo m={lm} />
        </Sec>
      ) : null}
      <Sec label={t('gui.plug.sec_perm')}>
        <Perms entry={entry} />
      </Sec>
      <Sec label={t('gui.plug.sec_about')}>
        <div className="pmdesc">{entry.description || entry.summary || ''}</div>
      </Sec>
      {cts.length > 0 && (
        <Sec label={t('gui.plug.sec_contents')}>
          <Contents entry={entry} />
        </Sec>
      )}
      {installed && !busy && (
        <Sec label={t('gui.plug.sec_manage')}>
          <div>
            <div className="pnote">{t('gui.plug.uninstall_note')}</div>
            <ArmButton
              className="mini ghost"
              style={{ marginTop: 8 }}
              label={t('gui.plug.uninstall')}
              onFire={() => store.remove(entry.id, entry.name)}
            />
          </div>
        </Sec>
      )}
    </>
  )
}

/* Manually-configured servers (no catalog entry): the slim config drawer. */
function InstDetail({ id }: { id: string }): JSX.Element | null {
  const row = store.mcpRows().find((p) => p.m && p.m.name === id)
  const m = row && row.m
  if (!m) return null
  const wait = store.authUrl(m.name)
  return (
    <>
      <div className="pmdhead">
        <Tile name={m.name} />
        <div className="pmdmeta">
          <div className="l1">
            <b>{m.name}</b>
          </div>
          <div className="l2">
            {[store.fromMarket(m.name) ? t('gui.plug.from_market') : t('gui.plug.manual'), m.transport ?? ''].join(' · ')}
          </div>
        </div>
        <div className="dact">
          {wait ? (
            <button className="mini" onClick={() => window.open(wait, '_blank')}>
              {t('gui.plug.reopen')}
            </button>
          ) : m.state === 'auth_required' || m.state === 'error' ? (
            <button className="mini" onClick={() => store.auth(m.name)}>
              {t(m.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair')}
            </button>
          ) : m.enabled ? (
            <button
              className="mini gold"
              onClick={() => shell().useInTask?.('gui.plug.use_prompt', m.name)}
            >
              {t('gui.hub.use')}
            </button>
          ) : null}
        </div>
      </div>
      <Sec label={t('gui.plug.sec_conn')}>
        <ConnInfo m={m} />
      </Sec>
      <Sec label={t('gui.plug.sec_manage')}>
        <div>
          <div className="pnote">{t('gui.plug.uninstall_note')}</div>
          <ArmButton
            className="mini ghost"
            style={{ marginTop: 8 }}
            label={t('gui.plug.uninstall')}
            onFire={() => store.remove(m.name, m.name)}
          />
        </div>
      </Sec>
    </>
  )
}
