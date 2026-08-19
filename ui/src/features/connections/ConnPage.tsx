import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { shell, t } from '../../shell/bridge'
import * as store from './store'

import type { ConnChannel, ConnField } from './types'
import type { JSX } from 'react'

/* One accessor so a renderer never has to know which of the catalogue's two
   spellings an entry uses (i18n key vs verbatim brand name). */
const chanName = (c: ConnChannel): string => (c.key ? t(c.key) : (c.name ?? c.id))

/* Same stable-hue letter tile as the plugin market. */
function tileHue(cn: string): number {
  let th = 0
  for (let i = 0; i < cn.length; i++) th = (th * 31 + cn.charCodeAt(i)) >>> 0
  return th % 8
}

/* Configured means the schema's required fields are all set. Entries whose
   schema declares no required fields count as configured out of the box. */
const isConfigured = (c: ConnChannel): boolean =>
  (c.fields || []).length ? (c.missing || []).length === 0 : true

/* What this row may honestly claim. The config flag alone used to drive the
   dot, so a channel whose adapter never came up still read as connected -- the
   flag says what was asked for, not what happened. `running` and `connected`
   come from the live gateway, and `undefined` means nobody could be asked,
   which is its own answer and not a negative one. */
function connState(c: ConnChannel): 'off' | 'unknown' | 'down' | 'unpaired' | 'live' {
  if (!c.on) return 'off'
  if (c.running === undefined || c.running === null) return 'unknown'
  if (!c.running) return 'down'
  if (c.connected === false) return 'unpaired'
  return 'live'
}

function connStateText(state: string): string {
  if (state === 'down') return t('gui.conn.st_down')
  if (state === 'unpaired') return t('gui.conn.st_unpaired')
  if (state === 'unknown') return t('gui.conn.st_unknown')
  return ''
}

export function ConnApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  const dialog = s.dialogId ? s.rows.find((c) => c.id === s.dialogId) : undefined
  return (
    <>
      {s.loaded || s.rows.length ? <ConnList rows={s.rows} /> : null}
      {dialog ? <ConnDialog key={`${dialog.id}:${s.epoch}`} c={dialog} /> : null}
    </>
  )
}

/* Flat, catalogue order. The connected/available split spent two headers and
   a count to say what each row's own switch already says. */
function ConnList({ rows }: { rows: ConnChannel[] }): JSX.Element {
  return (
    <>
      <div className="pmhero">
        <h3>{t('gui.page.conn')}</h3>
        <p>{t('gui.conn.hero_sub')}</p>
      </div>
      <div className="clist">
        {rows.map((c) => (
          <ConnRow key={c.id} c={c} />
        ))}
      </div>
    </>
  )
}

/* One row per entry, one state machine: an unconfigured entry offers its configure button and
   nothing else, a configured one carries the on/off switch, and clicking the
   row body opens its dialog either way. Two states, two controls -- a switch
   on an entry with no credentials would promise something the flip cannot
   deliver. */
function ConnRow({ c }: { c: ConnChannel }): JSX.Element {
  const configured = isConfigured(c)
  const cn = chanName(c)
  const live = connState(c)
  /* The sub line answers one question -- can this receive messages, and as
     whom -- or stays empty. It no longer introduces the channel to its user. */
  const sub = !configured
    ? t('gui.conn.unset')
    : live === 'live' && c.who
      ? t('gui.conn.as_you', { who: c.who })
      : connStateText(live)
  const open = (): void => store.openDialog(c)
  return (
    <div
      className="chrow"
      data-on={String(!!c.on)}
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open()
        }
      }}
    >
      <span className={'pmtile th' + tileHue(cn)}>{cn[0]}</span>
      <div className="bd">
        <div className="nm">
          {live !== 'off' && (
            <span className={'led' + (live === 'live' ? '' : live === 'unknown' ? ' bad' : ' warn')} />
          )}
          <span className="tt">{cn}</span>
        </div>
        {sub && <div className={'sub' + (configured && live !== 'unknown' ? '' : ' warn')}>{sub}</div>}
      </div>
      {configured ? (
        <>
          <button
            className="mini ghost"
            onClick={(e) => {
              e.stopPropagation()
              open()
            }}
          >
            {t('gui.conn.configure')}
          </button>
          <button
            className="swi"
            role="switch"
            aria-checked={!!c.on}
            aria-label={cn}
            onClick={(e) => {
              e.stopPropagation()
              store.toggle(c)
            }}
          />
        </>
      ) : (
        <button
          className="mini"
          onClick={(e) => {
            e.stopPropagation()
            open()
          }}
        >
          {t('gui.conn.configure')}
        </button>
      )}
    </div>
  )
}

/* The configuration dialog, rendered into the static #connVeil container the
   page markup keeps. Lifting the form out of the card is what lets the page
   be a list; the veil's open flag, click-outside and focus behaviour are
   managed here because nothing legacy owns them any more. */
function ConnDialog({ c }: { c: ConnChannel }): JSX.Element | null {
  const veil = document.getElementById('connVeil')
  useEffect(() => {
    if (!veil) return
    veil.dataset.open = 'true'
    const body = veil.querySelector('#connDlgBody')
    const first = body?.querySelector('input')
    if (first) first.focus()
    else if ((c.missing || []).length > 0) (body?.querySelector('button') as HTMLElement | null)?.focus()
    const onClick = (e: MouseEvent): void => {
      if (e.target === veil) store.closeDialog()
    }
    veil.addEventListener('click', onClick)
    return () => {
      veil.dataset.open = 'false'
      veil.removeEventListener('click', onClick)
    }
  }, [veil])
  if (!veil) return null
  /* Identity or nothing. The old subtitle recited what the channel is, which
     the reader knew before they clicked its name. */
  const sub = c.on && c.who ? t('gui.conn.as_you', { who: c.who }) : ''
  const disconnect = (): void => {
    store.closeDialog()
    shell().confirmAsk(
      t('gui.conn.disconnect'),
      t('gui.conn.disc_body', { name: chanName(c) }),
      t('gui.conn.disconnect'),
      () => void store.apply(c, {}, false),
    )
  }
  return createPortal(
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="connDlgTitle">
      <header id="connDlgTitle">{chanName(c)}</header>
      <div className="dsub" id="connDlgSub" hidden={!sub}>
        {sub}
      </div>
      <div className="body" id="connDlgBody">
        {/* Scanning is how these channels sign in, and it only exists while
            the adapter is up and unpaired -- so it sits above the form, where
            the reader is already looking, rather than behind a second click. */}
        {c.qrLogin && c.on ? <QrPanel c={c} /> : null}
        <ConnForm c={c} />
        {c.on ? (
          /* Disconnecting belongs with configuring, not on the row: the row is
             a list of twelve, and a destructive control repeated twelve times
             down a page is one mis-click waiting to happen. */
          <div className="dngrow">
            <button className="mini ghost danger" onClick={disconnect}>
              {t('gui.conn.disconnect')}
            </button>
          </div>
        ) : null}
      </div>
    </div>,
    veil,
  )
}

/* One form per channel, built from the fields its own schema declares (they
   ride on channels.status). Secrets never echo back: a set field shows a
   placeholder, and a box left blank means "keep", never "erase". Only the
   required fields show; everything optional folds behind one line, closed,
   with its count. Inputs are uncontrolled, as the legacy form kept them. */
function ConnForm({ c }: { c: ConnChannel }): JSX.Element {
  const inputs = useRef(new Map<string, HTMLInputElement>()).current
  const [advOpen, setAdvOpen] = useState(false)
  const fieldRow = (f: ConnField): JSX.Element => {
    /* The human sentence is the label; the config key is the fine print. The
       catalogue speaks first so the label follows the reader's language; the
       schema's own description backs it up, and the raw key is the floor. */
    const human = t('gui.connf.' + f.key, undefined, f.label && f.label !== f.key ? f.label : f.key)
    return (
      <div className="agf" key={f.key}>
        <label title={human}>{human}</label>
        <input
          type={f.secret ? 'password' : 'text'}
          autoComplete="off"
          placeholder={f.set ? t('gui.conn.field_set') : ''}
          ref={(el) => {
            if (el) inputs.set(f.key, el)
            else inputs.delete(f.key)
          }}
        />
        {human !== f.key && <div className="hint mono">{f.key}</div>}
      </div>
    )
  }
  const required = (c.fields || []).filter((f) => f.required)
  const optional = (c.fields || []).filter((f) => !f.required)
  const save = (): void => {
    const patch: Record<string, string> = {}
    inputs.forEach((i, k) => {
      if (i.value.trim()) patch[k] = i.value.trim()
    })
    /* Always "on". `enable` used to be `!c.on`, which read as a toggle: saving
       a correction to a connected channel turned it off. Disconnecting is its
       own control in the dialog, so this one only ever connects. */
    store.closeDialog()
    void store.apply(c, patch, true)
  }
  return (
    <div className="pform">
      {required.map(fieldRow)}
      {optional.length > 0 && (
        <>
          <button className="mini ghost fold" aria-expanded={advOpen} onClick={() => setAdvOpen(!advOpen)}>
            {t('gui.conn.advanced', { n: optional.length })}
          </button>
          <div className="adv" hidden={!advOpen}>
            {optional.map(fieldRow)}
          </div>
        </>
      )}
      <div className="ctl">
        <button className="mini" onClick={save}>
          {t(c.on ? 'gui.agent.save' : 'gui.conn.connect')}
        </button>
        <button className="mini ghost" onClick={() => store.closeDialog()}>
          {t('gui.agent.cancel')}
        </button>
      </div>
    </div>
  )
}

/* The scan panel. `channels.qr` is a live read off the adapter, so it is
   polled while the dialog is open and stopped the moment it is not: the code
   rotates, and a poll left running after the dialog closed would keep a
   socket busy for a picture nobody is looking at. Unmounting is the stop. */
type QrView = { phase: 'wait' | 'scan' | 'done' | 'noenc'; img: string | null }

function QrPanel({ c }: { c: ConnChannel }): JSX.Element {
  const [view, setView] = useState<QrView>({ phase: 'wait', img: null })
  useEffect(() => {
    let dead = false
    let timer: ReturnType<typeof setInterval> | null = null
    const stop = (): void => {
      if (timer) {
        clearInterval(timer)
        timer = null
      }
    }
    const paint = async (): Promise<void> => {
      let r
      try {
        r = await store.source().qr(c)
      } catch {
        return /* the connection banner already covers an unreachable gateway */
      }
      if (dead || !r) return
      if (r.connected) {
        stop()
        setView({ phase: 'done', img: null })
        void store.refresh()
        return
      }
      if (r.qr) {
        setView({ phase: 'scan', img: r.qr })
        return
      }
      /* A payload with no picture: the server could not rasterise it. Say
         which install is missing rather than showing an empty frame -- the
         reader cannot scan a URL, and has no way to guess why the box is
         blank. */
      setView({ phase: r.qr_text ? 'noenc' : 'wait', img: null })
    }
    void paint()
    timer = setInterval(() => void paint(), 3000)
    return () => {
      dead = true
      stop()
    }
  }, [c.id])
  const say =
    view.phase === 'done'
      ? t('gui.conn.qr_done')
      : view.phase === 'scan'
        ? t('gui.conn.qr_scan')
        : view.phase === 'noenc'
          ? t('gui.conn.qr_noenc')
          : t('gui.conn.qr_wait')
  return (
    <div className="qrbox">
      <div className="qrshot">{view.img ? <img src={view.img} alt={t('gui.conn.qr_alt')} /> : null}</div>
      <div className={view.phase === 'done' ? 'qrsay ok' : 'qrsay'}>{say}</div>
    </div>
  )
}
