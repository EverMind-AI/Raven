import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { ChannelMark } from '../../components/ChannelMark'
import { Field } from '../../components/SetupSheet'
import {
  TwoPane, TwoPaneFind, TwoPaneGroup, TwoPaneHead, TwoPaneList, TwoPaneNone, TwoPaneRow, TwoPaneSwitch,
  TwoPaneWait,
} from '../../components/TwoPane'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { chanName } from './catalogue'
import * as store from './store'

import type { ConnChannel, ConnField } from './types'
import type { JSX } from 'react'

/* Where Raven receives messages: a section of the settings dialog, drawn as a
   list of the channel catalogue with the picked one beside it. The fields that
   pane draws come from the channel's own Pydantic schema, shipped on
   channels.status, so the form cannot drift from the model.
 */

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

/* An entrance's state: the dot's class plus one line of fact. Five states, five
   things worth saying -- "nobody could be asked" is not the same as "off".
 *
 * The sentence is for the card, which has room for it; the row shows only the
 * dot. Under a row it was a third grey line saying what the group heading, the
 * cost badge and the button had each already said. */
function stateOf(c: ConnChannel): { cls: string; text: string } {
  const live = connState(c)
  if (live === 'live') return { cls: 'ok', text: c.who ? t('gui.conn.as_you', { who: c.who }) : t('gui.conn.st_live') }
  if (live === 'down') return { cls: 'bad', text: t('gui.conn.st_down') }
  if (live === 'unpaired') return { cls: 'warn', text: t('gui.conn.st_unpaired') }
  if (live === 'unknown') return { cls: 'warn', text: t('gui.conn.st_unknown') }
  /* Not in service. What is worth saying is how far off it is -- and a fully
     configured entry that is simply switched off says that, rather than
     nothing. */
  const missing = (c.missing || []).length
  if (missing) return { cls: 'off', text: t('gui.conn.st_missing', { n: missing }) }
  return { cls: 'off', text: isConfigured(c) ? t('gui.conn.st_off') : '' }
}

/* Does this entry sign in rather than get configured?
 *
 * `qrLogin` is the gateway's answer, and it only exists for an adapter that is
 * already running -- the flag rides on channel liveness. Which is backwards for
 * the addable group, whose whole job is to say what it costs to get in before
 * anything is running. So the schema answers instead: an entry with no required
 * field has no form to fill, and the only way into it is signing in. That is
 * derivable, always available, and true of exactly the scan channels.
 */
const scanLogin = (c: ConnChannel): boolean => !!c.qrLogin || (c.fields || []).filter((f) => f.required).length === 0

/* What it costs to get in, which is what the addable group is sorted by. */
const costOf = (c: ConnChannel): number =>
  scanLogin(c) ? 0 : (c.fields || []).filter((f) => f.required).length

/* Where the credentials come from. A channel whose secrets are minted in a
   console gets a jump straight to it -- the alternative is the reader guessing
   which of a vendor's four portals issues the token this form wants. Entries
   with no single place to apply (a mail host is not an open platform) are
   absent on purpose. */
const APPLY: Record<string, string> = {
  feishu: 'https://open.feishu.cn/app',
  slack: 'https://api.slack.com/apps',
  telegram: 'https://t.me/BotFather',
  discord: 'https://discord.com/developers/applications',
  wecom: 'https://work.weixin.qq.com',
  dingtalk: 'https://open-dev.dingtalk.com',
  qq: 'https://q.qq.com',
  matrix: 'https://app.element.io',
}

export function ConnectionsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  const [q, setQ] = useState('')
  const picked = s.viewId ? s.rows.find((c) => c.id === s.viewId) : undefined
  return (
    <TwoPane side={<ConnSide rows={s.rows} loaded={s.loaded} q={q} onQ={setQ} pickedId={s.viewId} />}>
      {picked ? (
        <ConnDetail key={`${picked.id}:${s.epoch}`} c={picked} />
      ) : (
        <TwoPaneNone>{t(s.rows.length ? 'gui.conn.pick' : 'gui.conn.none')}</TwoPaneNone>
      )}
    </TwoPane>
  )
}

/* Why an entrance that is in service is not receiving, in the row's own second
   line -- the same line an addable row uses for what it costs to get in. A live
   one needs no reason: the group and the green dot have said it. */
/* Why an entrance is not in service, when something actually went wrong.
 *
 * Only a thing gone wrong earns a reason. An adapter that is up and waiting on a
 * code is not wrong and not a state to advertise -- it is a step of signing in,
 * which happens in the pane the reader has open. An entrance that started and
 * stopped, or has nothing running it, is a different matter: that is why it is
 * not in service, and the row is where the reader looks for it.
 *
 * "State unknown" is the honest answer when nobody could be asked, and the wrong
 * one when we know why nobody answered: with no host running there is no
 * adapter, and naming that is the difference between a reader who thinks their
 * entrance is broken and one who knows nothing is running it. */
function reasonOf(c: ConnChannel): string | null {
  const live = connState(c)
  if (live === 'live' || live === 'unpaired' || live === 'off') return null
  if (live === 'down') return 'tag_down'
  return store.get().host === false ? 'tag_nohost' : 'tag_unknown'
}

/* The row's second line: a reason where there is one, then the state, and for
   an entrance nobody has started what it costs to get in. */
function rowSub(c: ConnChannel): { text: string; tone?: 'warn' | 'live' | 'bad' } {
  const live = connState(c)
  if (live === 'live') return { text: stateOf(c).text, tone: 'live' }
  const reason = reasonOf(c)
  if (reason) return { text: t('gui.conn.' + reason), tone: reason === 'tag_down' ? 'bad' : 'warn' }
  if (live === 'unpaired') return { text: t('gui.conn.st_unpaired'), tone: 'warn' }
  if (scanLogin(c)) return { text: t('gui.conn.cost_scan') }
  const n = (c.fields || []).filter((f) => f.required).length
  const missing = (c.missing || []).length
  return { text: missing ? t('gui.conn.st_missing', { n: missing }) : t('gui.conn.cost_n', { n: String(n) }) }
}

/* The left column: search, then two groups, because there are two answers to
   "is this entrance mine yet".
 *
 * Grouping by the config switch made pressing the button the whole of joining:
 * an entrance moved to "in service" before a code had been scanned, before a
 * credential had been tried, and would have sat there just the same with a
 * made-up token in it. The switch is a decision; being in service is a fact,
 * and only the live adapter can report it. The addable ones are ordered by what
 * it costs to get in -- a scan-login channel is one phone away, a channel
 * wanting six credentials is an afternoon. */
function ConnSide({ rows, loaded, q, onQ, pickedId }: {
  rows: ConnChannel[]
  loaded: boolean
  q: string
  onQ(v: string): void
  pickedId: string | null
}): JSX.Element {
  const term = q.trim().toLowerCase()
  const shown = rows.filter((c) => !term || chanName(c).toLowerCase().includes(term) || c.id.includes(term))
  const on = shown.filter((c) => connState(c) === 'live')
  const off = shown.filter((c) => connState(c) !== 'live').sort((a, b) => costOf(a) - costOf(b))
  const row = (c: ConnChannel): JSX.Element => {
    const cn = chanName(c)
    const sub = rowSub(c)
    return (
      <TwoPaneRow
        key={c.id}
        current={c.id === pickedId}
        off={!c.on}
        icon={<ChannelMark id={c.id} />}
        name={cn}
        sub={sub.text}
        {...(sub.tone ? { tone: sub.tone } : {})}
        onOpen={() => store.openChannel(c)}
        trailing={
          /* The switch only takes an entrance in or out of service. An entrance
             whose credentials are not in yet has nothing to switch on, so the
             way in is the pane beside this list rather than a control that can
             only fail. */
          <TwoPaneSwitch
            on={c.on}
            disabled={!isConfigured(c)}
            label={cn}
            onChange={() => {
              /* A scan entrance switched on from the list has its code in the
                 pane, and the list says nothing about that: opening the card
                 with the switch is the cue, and the reader is where the code
                 appears instead of watching a row that will never turn green
                 on its own. */
              if (!c.on && scanLogin(c)) store.openChannel(c)
              store.toggle(c)
            }}
          />
        }
      />
    )
  }
  return (
    <>
      <TwoPaneFind value={q} onChange={onQ} placeholder={t('gui.conn.search')} />
      <TwoPaneList>
        {!loaded && !rows.length ? <TwoPaneWait /> : shown.length === 0 ? (
          <div className="empty-note">{t('gui.conn.none_match')}</div>
        ) : (
          <>
            {on.length ? <TwoPaneGroup>{t('gui.conn.g_on')}</TwoPaneGroup> : null}
            {on.map(row)}
            {off.length ? <TwoPaneGroup>{t('gui.conn.g_off')}</TwoPaneGroup> : null}
            {off.map(row)}
          </>
        )}
      </TwoPaneList>
    </>
  )
}

/* The picked entrance, in the right column. It used to be a card over the list:
   inside the settings dialog that is a layer over a layer, and the list it
   covered was the one thing a reader comparing entrances needed to keep.
 *
 * Signing in by phone is a sequence, not a form: nothing can be scanned until
 * the entry is running. So a scan channel gets the wizard until it is paired,
 * and the credential form is for the channels that have one. */
function ConnDetail({ c }: { c: ConnChannel }): JSX.Element {
  const st = stateOf(c)
  const signing = scanLogin(c) && connState(c) !== 'live'
  return (
    <>
      <TwoPaneHead
        icon={<ChannelMark id={c.id} />}
        name={chanName(c)}
        meta={signing ? t('gui.conn.cost_scan_line') : <span className={'st ' + st.cls}>{st.text}</span>}
      />
      {signing ? <ScanWizard c={c} /> : <ConnForm c={c} />}
    </>
  )
}

/* One form per channel, built from the fields its own schema declares (they
   ride on channels.status). Secrets never echo back: a set field shows a
   placeholder, and a box left blank means "keep", never "erase". Only the
   required fields show; everything optional folds behind one line, closed,
   with its count. Inputs are uncontrolled; the store's epoch is what reseeds
   them, by keying the dialog subtree. */
function ConnForm({ c }: { c: ConnChannel }): JSX.Element {
  const inputs = useRef(new Map<string, HTMLInputElement>()).current
  const [advOpen, setAdvOpen] = useState(false)
  const [dirty, setDirty] = useState(false)
  const required = (c.fields || []).filter((f) => f.required)
  /* Is there anything to try? A credential the schema requires and nobody has
     supplied cannot be sent, and pressing connect with an empty box wrote
     nothing, started nothing and left the reader looking at a card that had not
     changed -- the press was the only feedback and it meant nothing. So the verb
     is unavailable until every required box has something in it, from config or
     from this card. Recomputed on input because the boxes are uncontrolled: a
     value the reader typed is only in the DOM. */
  const filled = (): boolean =>
    required.every((f) => f.set || (inputs.get(f.key)?.value ?? '').trim() !== '')
  const [ready, setReady] = useState(filled)
  /* Whether this card has handed its credentials over yet. Only after that does
     the state line have anything to report, and only then does the foot say
     "trying" rather than "connect". */
  const [sent, setSent] = useState(false)
  /* Receiving is the only thing that closes this card by itself. Anything else
     -- an adapter that would not start, a gateway that could not be asked -- is
     a reason the reader is owed, so the card stays with the state line up. */
  const live = connState(c) === 'live'
  useEffect(() => {
    if (sent && live) store.closeChannel()
  }, [sent, live])
  const fieldRow = (f: ConnField): JSX.Element => {
    /* The human sentence is the label; the config key rides on its tooltip.
       The catalogue speaks first so the label follows the reader's language,
       the schema's own description backs it up, and the raw key is the floor
       -- printed under the box, it was a second line of grey saying the same
       thing in worse words. */
    const said = t('gui.connf.' + f.key, undefined, f.label && f.label !== f.key ? f.label : f.key)
    /* A schema description is a label when it was written for a reader and a
       paragraph when it was written for a developer. Past this length it has
       stopped being a label, so the key takes the label position and the
       paragraph moves to the tooltip: `workspace` was titled with three lines
       of English prose, which is the standing grey this page was cleared of. */
    const prose = said.length > 44
    return (
      <Field key={f.key} label={prose ? f.key : said} title={prose ? said : f.key}>
        <input
          type={f.secret ? 'password' : 'text'}
          autoComplete="off"
          placeholder={f.set ? t('gui.conn.field_set') : ''}
          onInput={() => {
            setDirty(true)
            setReady(filled())
          }}
          ref={(el) => {
            if (el) inputs.set(f.key, el)
            else inputs.delete(f.key)
          }}
        />
      </Field>
    )
  }
  const optional = (c.fields || []).filter((f) => !f.required)
  const apply = APPLY[c.id]
  const groups = groupFields(c, required)
  /* The card stays up until the entrance is actually receiving.
   *
   * It used to close on the press: the write went out, the card vanished, and
   * the row appeared under "in service" whether or not the credentials were any
   * good -- a made-up token looked exactly like a working one. Now the press
   * hands the credentials over and waits: the adapter starting is the check
   * nobody else can do, and its answer is what closes this card or keeps it
   * open with the reason. */
  const save = (): void => {
    const patch: Record<string, string> = {}
    inputs.forEach((i, k) => {
      if (i.value.trim()) patch[k] = i.value.trim()
    })
    setSent(true)
    setDirty(false)
    /* Always "on". `enable` used to be `!c.on`, which read as a toggle: saving
       a correction to a connected channel turned it off. Disconnecting is its
       own control in the dialog, so this one only ever connects. */
    void store.apply(c, patch, true)
  }
  return (
    <>
      <div className="subody" id="connDlgBody">
        {/* Both sections wear the same caption: a small mono line that says
            what the block below it is, and nothing else. The credential count
            rides on it, which is why the head no longer repeats it. */}
        {required.length ? (
          <div className="sucreds">
            <span className="k">
              {t('gui.conn.creds')}
              <span className="n">{`${required.filter((f) => f.set).length} / ${required.length}`}</span>
            </span>
            {apply ? (
              <a className="jump" href={apply} target="_blank" rel="noreferrer">
                {t('gui.conn.apply')}
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M7 17 17 7M9 7h8v8" />
                </svg>
              </a>
            ) : null}
          </div>
        ) : null}
        {groups.map(([label, fs]) => (
          <div className="sugroup" key={label || '_'}>
            {label ? <div className="sugsub">{t(label)}</div> : null}
            <div className="sufields">{fs.map(fieldRow)}</div>
          </div>
        ))}
        {optional.length > 0 && (
          <div className="suadv">
            <button className="sucap" aria-expanded={advOpen} onClick={() => setAdvOpen(!advOpen)}>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="m9 6 6 6-6 6" />
              </svg>
              {t('gui.conn.advanced', { n: optional.length })}
            </button>
            {/* Only when open. Left in the tree behind `hidden` it still took a
                row of the body's grid, which is a gap under the fold with
                nothing in it. */}
            {advOpen ? <div className="sufields">{optional.map(fieldRow)}</div> : null}
          </div>
        )}
      </div>
      <div className="sufoot">
        {/* Where this form stands, in the one place a form's state belongs:
            beside the button that acts on it. It was an empty span. */}
        <span className="n">
          {dirty
            ? t('gui.conn.foot_dirty')
            : (c.missing || []).length
              ? t('gui.conn.foot_need', { n: String((c.missing || []).length) })
              : t('gui.conn.foot_clean')}
        </span>
        {/* Clearing the switch, for the entrance whose row no longer offers it:
            a row shows "disconnect" only where an adapter is up, so an entrance
            switched on that never started would otherwise have no way back to
            off. Here, beside the form, "disconnect" has the card around it to
            say what is being switched. */}
        {c.on ? (
          <button className="mini ghost" onClick={() => void store.apply(c, {}, false)}>
            {t('gui.conn.disconnect')}
          </button>
        ) : null}
        <button className="mini key" disabled={!ready} onClick={save}>
          {/* Keyed on receiving, not on the flag: a card whose credentials were
              written and refused would otherwise offer to "save" them again. */}
          {t(live ? 'gui.agent.save' : sent ? 'gui.conn.retry' : 'gui.conn.connect')}
        </button>
      </div>
    </>
  )
}

/* Channels that are two of something. Mail is a receiving server and a sending
   server, and one flat column of six boxes left the reader counting which three
   belonged to which. Split by key prefix rather than by position, so a schema
   that grows a field keeps its halves. Anything unprefixed leads, unlabelled.
   A channel absent from here is one group and no heading. */
const FIELD_GROUPS: Record<string, Array<[string, string]>> = {
  email: [
    ['imap_', 'gui.conn.g_imap'],
    ['smtp_', 'gui.conn.g_smtp'],
  ],
}

function groupFields(c: ConnChannel, fields: ConnField[]): Array<[string | null, ConnField[]]> {
  const table = FIELD_GROUPS[c.id]
  if (!table) return [[null, fields]]
  const rest = fields.filter((f) => !table.some(([pre]) => f.key.startsWith(pre)))
  const out: Array<[string | null, ConnField[]]> = rest.length ? [[null, rest]] : []
  table.forEach(([pre, label]) => {
    const hit = fields.filter((f) => f.key.startsWith(pre))
    if (hit.length) out.push([label, hit])
  })
  return out
}

/* Signing in by phone, as the three moments it actually has: the entry has to
   be on, a code has to be scanned, and only then do messages arrive. The old
   sheet showed the middle one and nothing else, so a channel that was merely
   switched off presented an empty form -- no code, no way to ask for one,
   nothing said about why.

   Turning it on is a config write, and the adapter it starts belongs to the
   app process, which builds its channel set at launch. So step 2 waits on
   `running` rather than on the write, and says so: "reopen Raven App" is the
   real remaining step, and printing it here is the whole point of drawing the
   sequence instead of a form. */
function ScanWizard({ c }: { c: ConnChannel }): JSX.Element {
  const up = c.running === true
  const paired = c.connected === true
  /* Not `=== false`. A gateway that could not be asked reports nothing, and
     the reader who just turned the entry on is owed the same sentence either
     way: there is no code yet, and reopening the app is what produces one.
     Testing for an explicit no left that reader looking at a step that had
     gone quiet -- the dead end this wizard exists to remove.
   *
   * And it is owed BEFORE the press, not only after it, wherever we already
   * know nothing is running: pressing connect with no host mints no code, so a
   * card that waits for the press to mention that spends the reader's press to
   * tell them something it knew all along. */
  const host = store.get().host
  const stalled = c.running !== true && (!!c.on || host === false)
  const s1 = c.on ? 'done' : 'idle'
  const s2 = paired ? 'done' : up ? 'now' : 'idle'
  const s3 = paired ? 'now' : 'idle'
  const step = (n: string, state: string, title: string, sub?: JSX.Element | string | null): JSX.Element => (
    <div className="step" data-state={state}>
      <span className="n">{state === 'done' ? '\u2713' : n}</span>
      <div>
        <div className="st">{title}</div>
        {sub ? <div className="sd">{sub}</div> : null}
      </div>
    </div>
  )
  return (
    <>
      <div className="subody suwiz" id="connDlgBody">
        {step('1', s1, c.on ? t('gui.conn.w1_done') : t('gui.conn.w1_idle'))}
        {step(
          '2',
          s2,
          paired ? t('gui.conn.w2_done') : t('gui.conn.w2'),
          /* The panel only polls where a code can exist, and unmounting it is
             what stops the poll -- so it lives inside the step that is
             current, not above the wizard.
           *
           * Two ways for there to be no code, and one sentence for both was
           * wrong in the more common one: "Raven is not running" printed over a
           * page the gateway itself was serving. Only a KNOWN host shifts the
           * blame to the adapter -- something is running it, so it started and
           * gave up, and that is the case worth trying again. Not knowing keeps
           * the old advice, because "open the app" is still the useful thing to
           * say to a reader whose gateway may well be down. */
          s2 === 'now' ? (
            <QrPanel c={c} />
          ) : stalled ? (
            t(host === true ? 'gui.conn.w2_down' : 'gui.conn.w2_blocked')
          ) : null,
        )}
        {step('3', s3, t('gui.conn.w3'))}
      </div>
      <div className="sufoot">
        <span className="n">{up && !paired ? t('gui.conn.w_wait') : ''}</span>
        {/* The list's two verbs, not two more of their own: the wizard's first
            button does what the row's does, and backing out is the same
            disconnect. It read "turn the entry on" -- the words step 1 above it
            already carries -- and "cancel connecting". */}
        {!c.on ? (
          <button className="mini key" onClick={() => void store.apply(c, {}, true)}>
            {t('gui.conn.connect')}
          </button>
        ) : paired ? (
          <button className="mini key" onClick={() => store.closeChannel()}>
            {t('gui.conn.w_done')}
          </button>
        ) : (
          <>
            <button className="mini ghost" onClick={() => void store.apply(c, {}, false)}>
              {t('gui.conn.disconnect')}
            </button>
            {/* On and not up: the entrance gave up, or nothing started it. The
                row's connect is the retry, and this card is covering it -- so
                the card carries one, or the only way to try again is to close
                this and find the row underneath. */}
            {!up ? (
              <button className="mini key" onClick={() => void store.apply(c, {}, true)}>
                {t('gui.conn.w_retry')}
              </button>
            ) : null}
          </>
        )}
      </div>
    </>
  )
}

/* The scan panel. `channels.qr` is a live read off the adapter, so it is
   polled while the dialog is open and stopped the moment it is not: the code
   rotates, and a poll left running after the dialog closed would keep a
   socket busy for a picture nobody is looking at. Unmounting is the stop. */
type QrView = { phase: 'wait' | 'scan' | 'done' | 'noenc' | 'down'; img: string | null }

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
      /* The adapter is gone -- it gave up on the login, or nothing is running
         it any more -- and any code it left pending expired with it. The row
         behind this panel still reads as up until the list is re-read, which
         is what puts the wizard's own retry out of reach, so ask for that read
         and carry the verb here until it lands. */
      if (r.running === false) {
        setView({ phase: 'down', img: null })
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
  /* Down reads the host the same way the wizard does: only a known host shifts
     the blame to the adapter, and not knowing keeps the advice to open the app. */
  const say =
    view.phase === 'done'
      ? t('gui.conn.qr_done')
      : view.phase === 'scan'
        ? t('gui.conn.qr_scan')
        : view.phase === 'noenc'
          ? t('gui.conn.qr_noenc')
          : view.phase === 'down'
            ? t(store.get().host === true ? 'gui.conn.w2_down' : 'gui.conn.w2_blocked')
            : t('gui.conn.qr_wait')
  return (
    <div className="qrbox">
      {view.phase === 'down' ? null : (
        <div className="qrshot">{view.img ? <img src={view.img} alt={t('gui.conn.qr_alt')} /> : null}</div>
      )}
      <div className={view.phase === 'done' ? 'qrsay ok' : 'qrsay'}>{say}</div>
      {view.phase === 'down' ? (
        <button
          className="mini key"
          onClick={() => {
            /* The press is answered in the panel it was made in, not three
               seconds later by the poll: a start that did not take reads as down
               again on the next tick anyway. */
            setView({ phase: 'wait', img: null })
            void store.apply(c, {}, true)
          }}
        >
          {t('gui.conn.w_retry')}
        </button>
      ) : null}
    </div>
  )
}
