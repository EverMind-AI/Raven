import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { shell, t } from '../../shell/bridge'
import { copy } from '../../shell/clipboard'
import { language } from '../../shell/platform'
import * as store from './store'

import type { BrowserState } from './store'
import type { BrowserTabRow, LinksSource, UrlRow } from './types'
import type { CSSProperties, JSX, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'

/* Mirrors the glyph paths the legacy renderer drew with (ICO in
   ui-web/src/demo/100-workspace.js plus the inline ones in the old live part). */
const ICO = {
  web: 'M4.5 12h15M12 4.5c-4.5 4.5-4.5 10.5 0 15M12 4.5c4.5 4.5 4.5 10.5 0 15',
  ext: 'M10 6H6.5A2.5 2.5 0 0 0 4 8.5v9A2.5 2.5 0 0 0 6.5 20h9a2.5 2.5 0 0 0 2.5-2.5V14M14 4h6v6M20 4l-9 9',
  up: 'M14.5 6.5 9 12l5.5 5.5',
  cross: 'M6 6l12 12M18 6L6 18',
  reload: 'M4.5 12a7.5 7.5 0 1 0 2.6-5.7M4.5 5.5V10h4.5',
  popout: 'M9 5H5v14h14v-4M14 4h6v6M20 4l-9 9',
}

function Ico({ d, cls, style }: { d: string; cls?: string; style?: CSSProperties }): JSX.Element {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
      className={cls}
      style={style}
    >
      <path d={d} />
    </svg>
  )
}

export function BrowserApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  const src = store.source()
  if (!src.embedded) return <LinkList src={src} />
  return <Chromium s={s} />
}

/* ── the fixture shape: where the agent went on the web ──────────────── */

function LinkList({ src }: { src: LinksSource }): JSX.Element {
  const urls = src.urls()
  if (!urls.length) return <div className="wsnote">{t('gui.ws.no_web')}</div>
  return (
    <>
      {urls.map((u, i) => (
        <LinkRow key={i} u={u} src={src} />
      ))}
    </>
  )
}

function LinkRow({ u, src }: { u: UrlRow; src: LinksSource }): JSX.Element {
  /* The right-click menu keeps the shell's own contextmenu protocol:
     ctxMenu() is just `el._ctx = items`, so the ref plays that part. */
  const ctx = (el: HTMLButtonElement | null): void => {
    if (!el) return
    ;(el as HTMLButtonElement & { _ctx?: () => unknown })._ctx = () => [
      { label: t('gui.ws.open_url'), fn: () => src.openUrl(u.url) },
      { label: t('gui.ws.copy_url'), fn: () => copy(u.url, t('gui.ws.copied_url')) },
    ]
  }
  return (
    <button className="urow" title={u.url} ref={ctx} onClick={() => src.openUrl(u.url)}>
      <Ico d={ICO.web} />
      <span className="u">
        {u.url.replace(/^https?:\/\//, '')}
        <small>{`${t(u.kind === 'search' ? 'gui.ws.web_searched' : 'gui.ws.web_fetched')} · ${u.at}`}</small>
      </span>
      <Ico d={ICO.ext} cls="ext" />
    </button>
  )
}

/* ── the live shape: one Chromium behind the RPC ─────────────────────── */

function Chromium({ s }: { s: BrowserState }): JSX.Element {
  useEffect(() => {
    if (store.getState().avail === null) void store.poll(true)
  }, [])
  const gate = s.absent || s.avail === false
  const strip = !gate && s.started && !s.headful
  useEffect(() => {
    if (strip) {
      void store.tabsSync()
      store.tabTick(true)
    } else {
      store.tabTick(false)
    }
  }, [strip])
  useEffect(() => {
    /* Poll cheaply while there is no stage to stream to -- the agent may
       open a page any moment, and popped-out chrome reports through it. */
    if (!gate && (!s.started || s.headful)) store.tick(true)
  }, [gate, s.started, s.headful])

  /* Absent is not the same as uninstalled, and the difference is the whole
     point of telling the reader anything: chromium is worth an install
     command, a server without the surface is not. */
  if (s.absent) {
    return (
      <div className="bnote">
        <div className="h">{t('gui.br.absent_h')}</div>
        <div className="w">{t('gui.br.absent')}</div>
      </div>
    )
  }
  if (s.avail === false) {
    return (
      <div className="bnote">
        <div className="h">{t('gui.br.unavail')}</div>
        <div className="w">{t('gui.br.unavail_w')}</div>
        <code>uv sync --extra browser && uv run playwright install chromium</code>
        {s.reason ? <div className="w">{s.reason}</div> : null}
      </div>
    )
  }
  return (
    <>
      {strip && <TabStrip tabs={s.tabs} />}
      <BBar s={s} strip={strip} />
      {s.started && s.headful ? <Popped /> : !s.started ? <Idle s={s} /> : <Stage s={s} />}
    </>
  )
}

function BBar({ s, strip }: { s: BrowserState; strip: boolean }): JSX.Element {
  /* Address field: security glyph inside, select-all on focus, Esc restores,
     and Enter routes -- URL-shaped input navigates, anything else searches. */
  const urlKey = (e: ReactKeyboardEvent<HTMLInputElement>): void => {
    const el = e.currentTarget
    if (e.key === 'Escape') {
      el.value = store.getState().url
      el.blur()
      return
    }
    if (e.key !== 'Enter') return
    e.preventDefault()
    const v = el.value.trim()
    if (!v) return
    const urlish =
      /^[a-z][a-z0-9+.-]*:\/\//i.test(v) ||
      (!/\s/.test(v) &&
        (/^localhost(:\d+)?([/?#]|$)/i.test(v) || /^[\w-]+(\.[\w-]+)+/.test(v) || /^\d{1,3}(\.\d{1,3}){3}/.test(v)))
    const search =
      language() === 'zh'
        ? `https://www.baidu.com/s?wd=${encodeURIComponent(v)}`
        : `https://duckduckgo.com/?q=${encodeURIComponent(v)}`
    void store.open(urlish ? v : search)
    el.blur()
  }
  return (
    <div className="bbar">
      <div className="nav">
        <button
          className="ghost-ic bk"
          title={t('gui.br.back')}
          aria-label={t('gui.br.back')}
          disabled={!s.started || !s.canBack}
          onClick={() => void store.go('back')}
        >
          <Ico d={ICO.up} />
        </button>
        <button
          className="ghost-ic fw"
          title={t('gui.br.forward')}
          aria-label={t('gui.br.forward')}
          disabled={!s.started || !s.canFwd}
          onClick={() => void store.go('forward')}
        >
          <Ico d={ICO.up} style={{ transform: 'rotate(180deg)' }} />
        </button>
        <button
          className="ghost-ic brl"
          title={t(s.loading ? 'gui.br.stop' : 'gui.br.reload')}
          aria-label={t(s.loading ? 'gui.br.stop' : 'gui.br.reload')}
          disabled={!s.started}
          onClick={() => void store.go(s.loading ? 'stop' : 'reload')}
        >
          <Ico d={s.loading ? ICO.cross : ICO.reload} />
        </button>
      </div>
      <span className="burl">
        <Sec url={s.url} />
        <input
          className="url"
          type="text"
          defaultValue={s.url}
          placeholder={t('gui.br.url_ph')}
          aria-label={t('gui.br.url_ph')}
          autoComplete="off"
          spellCheck={false}
          ref={store.setUrlEl}
          onFocus={(e) => e.currentTarget.select()}
          onKeyDown={urlKey}
        />
      </span>
      <div className="bprog" hidden={!s.loading}>
        <i />
      </div>
      {strip && (
        <button
          className="ghost-ic"
          title={t('gui.br.popout')}
          aria-label={t('gui.br.popout')}
          onClick={() => void store.popout()}
        >
          <Ico d={ICO.popout} />
        </button>
      )}
      {s.started && (
        <button
          className="ghost-ic"
          title={t('gui.br.close')}
          aria-label={t('gui.br.close')}
          onClick={() => void store.closeBrowser()}
        >
          <Ico d={ICO.cross} />
        </button>
      )}
    </div>
  )
}

function Sec({ url }: { url: string }): JSX.Element {
  const https = /^https:/i.test(url)
  const http = /^http:/i.test(url)
  return (
    <span className={'sec' + (https ? ' ok' : http ? ' warn' : '')}>
      {https ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
          <path d="M8 11V8a4 4 0 0 1 8 0v3M6 11h12v9H6z" />
        </svg>
      ) : http ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
          <path d="M12 5v8M12 17.5v.5" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="11" cy="11" r="7" />
          <path d="M20 20l-4.3-4.3" />
        </svg>
      )}
    </span>
  )
}

function TabStrip({ tabs }: { tabs: BrowserTabRow[] }): JSX.Element {
  return (
    <div className="btabs">
      {tabs.map((tab) => (
        <Tab key={tab.index} tab={tab} />
      ))}
      <button
        className="btab-new"
        title={t('gui.br.tab_new')}
        aria-label={t('gui.br.tab_new')}
        onClick={() => void store.tabsAct('new', {})}
      >
        +
      </button>
    </div>
  )
}

function Tab({ tab }: { tab: BrowserTabRow }): JSX.Element {
  return (
    <div
      className="btab"
      role="tab"
      aria-current={tab.active ? 'true' : 'false'}
      title={tab.title || tab.url}
      onClick={() => {
        if (!tab.active) void store.tabsAct('activate', { index: tab.index })
      }}
      onAuxClick={(e) => {
        if (e.button === 1) {
          e.preventDefault()
          void store.tabsAct('close', { index: tab.index })
        }
      }}
    >
      {tab.loading ? <span className="ld" /> : <Fav url={tab.url} />}
      <span className="tt">{tab.title || tab.url.replace(/^https?:\/\//, '') || t('gui.br.tab_blank')}</span>
      <button
        className="bx"
        title={t('gui.br.tab_close')}
        aria-label={t('gui.br.tab_close')}
        onClick={(e) => {
          e.stopPropagation()
          void store.tabsAct('close', { index: tab.index })
        }}
      >
        ✕
      </button>
    </div>
  )
}

function Fav({ url }: { url: string }): JSX.Element {
  /* Origins whose /favicon.ico 404'd live in the store, so a strip redraw
     never re-asks; the bump only swaps this one image for its letter. */
  const [, bump] = useState(0)
  let host = ''
  let origin = ''
  try {
    const u = new URL(url)
    host = u.host
    origin = u.origin
  } catch {
    /* about:blank */
  }
  if (host && !store.noFavHas(origin)) {
    return (
      <img
        className="fav"
        alt=""
        src={`${origin}/favicon.ico`}
        onError={() => {
          store.noFavAdd(origin)
          bump((n) => n + 1)
        }}
      />
    )
  }
  return <span className="fav ltr">{host ? (host[0] || '?').toUpperCase() : '·'}</span>
}

/* Popped out: the page lives in a real Chromium window. The panel keeps the
   address bar working and offers the way back. */
function Popped(): JSX.Element {
  return (
    <div className="bnote">
      <div className="h">{t('gui.br.popped')}</div>
      <div className="w">{t('gui.br.popped_w')}</div>
      <button className="btn" onClick={() => void store.popin()}>
        {t('gui.br.popin')}
      </button>
    </div>
  )
}

function Idle({ s }: { s: BrowserState }): JSX.Element {
  const urls = store.source().urls()
  return (
    <div className="bnote">
      <div className="h">{t('gui.br.idle')}</div>
      <div className="w">{t('gui.br.idle_w')}</div>
      {s.err ? <div className="w">{s.err}</div> : null}
      {urls.length > 0 && (
        <div className="urls">
          {urls.slice(0, 8).map((u, i) => (
            <button key={i} className="urow" title={u.url} onClick={() => void store.open(u.url)}>
              <Ico d={ICO.web} />
              <span className="u">{u.url.replace(/^https?:\/\//, '')}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/* Pointer position in page coordinates. The viewport tracks the stage, so
   this is normally 1:1 -- the math only earns its keep in the beat between a
   resize and the restream, when the frame is letterboxed inside the stage. */
function toPage(stage: HTMLElement, e: { clientX: number; clientY: number }): { x: number; y: number } {
  const r = stage.getBoundingClientRect()
  const vp = store.getState().vp
  const vw = vp[0] || r.width || 1
  const vh = vp[1] || r.height || 1
  const sc = Math.min(r.width / vw, r.height / vh) || 1
  const ox = r.left + (r.width - vw * sc) / 2
  const oy = r.top + (r.height - vh * sc) / 2
  return {
    x: Math.round(Math.min(vw, Math.max(0, (e.clientX - ox) / sc))),
    y: Math.round(Math.min(vh, Math.max(0, (e.clientY - oy) / sc))),
  }
}

/* Live: the stage owns the rest of the panel (flex column, no scroll) and
   the page's viewport is resized to the stage, so pointer geometry is 1:1. */
function Stage({ s }: { s: BrowserState }): JSX.Element {
  const stageRef = useRef<HTMLDivElement | null>(null)
  const kbRef = useRef<HTMLInputElement | null>(null)
  const composing = useRef(false)
  const mv = useRef<{ pend: { x: number; y: number } | null; raf: number }>({ pend: null, raf: 0 })

  useEffect(() => {
    const stage = stageRef.current
    if (!stage) return
    store.setStage(stage)
    /* The stage view stamps the panel's layout mode, as the legacy draw did. */
    const host = store.hostEl()
    if (host) host.dataset.view = 'web'
    store.repaintLast()
    void store.watch(true)
    /* A dragged seam or window resize means a new viewport: re-lease with the
       new size once it settles, and the server restreams at that size. */
    let rsz = 0
    const ro =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver(() => {
            clearTimeout(rsz)
            rsz = window.setTimeout(() => {
              if (store.showing() && store.getState().started) void store.watch(true)
            }, 250)
          })
    ro?.observe(stage)
    /* Raw deltas, not rounded: trackpad momentum is made of fractional steps.
       The canvas also shifts locally on the spot, so scrolling does not wait
       a round-trip for Chromium's next frame. React's wheel listener is
       passive, and this one must preventDefault, so it goes on the node. */
    const wheel = (e: WheelEvent): void => {
      e.preventDefault()
      store.input({ kind: 'wheel', dx: e.deltaX, dy: e.deltaY })
      const cv = store.canvas()
      const vp = store.getState().vp
      if (!cv || !cv.width || !vp[1]) return
      const d = Math.max(-cv.height, Math.min(cv.height, Math.round(e.deltaY * (cv.height / vp[1]))))
      if (!d) return
      const c2 = cv.getContext('2d')!
      const w = cv.width
      const h = cv.height
      const a = Math.abs(d)
      if (d > 0) {
        c2.drawImage(cv, 0, a, w, h - a, 0, 0, w, h - a)
        c2.drawImage(cv, 0, h - 1, w, 1, 0, h - a, w, a)
      } else {
        c2.drawImage(cv, 0, 0, w, h - a, 0, a, w, h - a)
        c2.drawImage(cv, 0, 0, w, 1, 0, 0, w, a)
      }
    }
    stage.addEventListener('wheel', wheel, { passive: false })
    return () => {
      ro?.disconnect()
      clearTimeout(rsz)
      stage.removeEventListener('wheel', wheel)
      store.setStage(null)
      if (host && host.dataset.view === 'web') delete host.dataset.view
    }
  }, [])

  const btnOf = (e: ReactPointerEvent): 'left' | 'middle' | 'right' =>
    e.button === 2 ? 'right' : e.button === 1 ? 'middle' : 'left'
  const down = (e: ReactPointerEvent<HTMLDivElement>): void => {
    e.preventDefault()
    const stage = e.currentTarget
    try {
      stage.setPointerCapture(e.pointerId)
    } catch {
      /* gone mid-gesture */
    }
    kbRef.current?.focus({ preventScroll: true })
    store.input({ kind: 'down', button: btnOf(e), count: e.detail || 1, ...toPage(stage, e) })
  }
  const up = (e: ReactPointerEvent<HTMLDivElement>): void => {
    store.input({ kind: 'up', button: btnOf(e), count: e.detail || 1, ...toPage(e.currentTarget, e) })
  }
  /* Moves coalesce per animation frame -- the newest position wins. */
  const move = (e: ReactPointerEvent<HTMLDivElement>): void => {
    mv.current.pend = toPage(e.currentTarget, e)
    if (mv.current.raf) return
    mv.current.raf = requestAnimationFrame(() => {
      mv.current.raf = 0
      if (mv.current.pend) {
        store.input({ kind: 'move', ...mv.current.pend })
        mv.current.pend = null
      }
    })
  }

  const kbKey = (e: ReactKeyboardEvent<HTMLInputElement>): void => {
    if (composing.current) return
    if (e.metaKey || e.ctrlKey) {
      const k = e.key.toLowerCase()
      /* The browser's own chrome shortcuts, same keys as the native thing:
         L focuses the address bar, R reloads, T/W manage tabs, [ ] walk
         history. Everything else editing-shaped belongs to the page. */
      if (k === 'l') {
        e.preventDefault()
        const u = store.urlInput()
        if (u) {
          u.focus()
          u.select()
        }
        return
      }
      if (k === 'r') {
        e.preventDefault()
        void store.go(store.getState().loading ? 'stop' : 'reload')
        return
      }
      if (k === 't') {
        e.preventDefault()
        void store.tabsAct('new', {})
        return
      }
      if (k === 'w') {
        e.preventDefault()
        const act = store.getState().tabs.find((x) => x.active)
        if (act) void store.tabsAct('close', { index: act.index })
        return
      }
      if (k === '[') {
        e.preventDefault()
        void store.go('back')
        return
      }
      if (k === ']') {
        e.preventDefault()
        void store.go('forward')
        return
      }
      /* Editing shortcuts belong to the page. Paste is the exception:
         letting Cmd+V land in the sink turns it into an input event, which
         is already the IME text path. */
      if (k.length === 1 && 'aczyx'.includes(k)) {
        e.preventDefault()
        store.input({ kind: 'key', key: 'ControlOrMeta+' + (e.shiftKey ? 'Shift+' : '') + k.toUpperCase() })
      }
      return
    }
    if (e.key.length === 1) return
    e.preventDefault()
    const mods: string[] = []
    if (e.shiftKey) mods.push('Shift')
    if (e.altKey) mods.push('Alt')
    store.input({ kind: 'key', key: mods.concat(e.key).join('+') })
  }

  return (
    <div className="bstage" ref={stageRef} onPointerDown={down} onPointerUp={up} onPointerMove={move}>
      <canvas className="shot" role="img" aria-label={s.title || s.url} ref={store.setCanvas} />
      {/* Keystrokes land in an invisible input, not on the stage div: that is
          what lets an IME compose, and the composed string goes over whole. */}
      <input
        className="kbsink"
        type="text"
        autoCapitalize="off"
        autoComplete="off"
        spellCheck={false}
        aria-hidden="true"
        tabIndex={-1}
        ref={kbRef}
        onCompositionStart={() => {
          composing.current = true
        }}
        onCompositionEnd={(e) => {
          composing.current = false
          const v = e.data
          e.currentTarget.value = ''
          if (v) store.input({ kind: 'text', text: v })
        }}
        onInput={(e) => {
          if (composing.current) return
          const v = e.currentTarget.value
          e.currentTarget.value = ''
          if (v) store.input({ kind: 'text', text: v })
        }}
        onKeyDown={kbKey}
        onFocus={() => stageRef.current?.classList.add('hot')}
        onBlur={() => stageRef.current?.classList.remove('hot')}
      />
      {!s.hasFrame && <div className="waiting">{t('gui.br.waiting')}</div>}
      {/* A navigation that failed gets a page, not a status-line mumble. */}
      {s.err ? (
        <div className="berr">
          <div className="h">{t('gui.br.error')}</div>
          <div className="w">{s.err}</div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="mini gold" onClick={() => store.retry()}>
              {t('gui.plug.retry')}
            </button>
            <button className="mini ghost" onClick={() => store.dismissErr()}>
              {t('gui.cancel')}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
