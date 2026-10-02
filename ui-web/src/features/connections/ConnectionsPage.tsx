import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { ChannelMark } from '../../components/ChannelMark'
import { HubHead } from '../../components/HubHead'
import { Field } from '../../components/SetupSheet'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { CHANNELS, chanName } from './catalogue'
import * as store from './store'

import type { ConnChannel, ConnField } from './types'
import type { JSX } from 'react'
import './styles.css'

/* Where Raven receives messages: the channel half of the connections hub
   (state/hub.ts), drawn the way its agent half is -- tabs over a grid of
   cards, and the picked one in the shared drawer.
   The fields the sheet draws come from the channel's own Pydantic schema,
   shipped on channels.status, so the form cannot drift from the model.
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

/* What the reader is told, which is three things rather than the five above:
 * it works, it does not work yet, or it was set up and failed.
 *
 * The other two collapse on purpose. An adapter up and waiting on a code is
 * signing in, and signing in happens in the sheet; to the reader it is simply
 * not connected yet. And "nobody could be asked" is a fact about the page, not
 * about one entrance: with no host running every entrance is equally deaf, so
 * the page says that once (the notice over the grid) and no card claims it.
 * Only an adapter the host tried and could not start is this entrance's own
 * trouble, and the one worth a red light.
 */
type Shown = 'live' | 'broken' | 'idle'

function shownOf(c: ConnChannel): Shown {
  const live = connState(c)
  if (live === 'live') return 'live'
  if (live === 'down' && store.get().host !== false) return 'broken'
  return 'idle'
}

/* The sheet's own line: the same three, with room for the reason. The reason
   is the gateway's word from this page's last write (the source keeps it on
   the row); a status read has none to give. */
function stateOf(c: ConnChannel): { cls: string; text: string } {
  const shown = shownOf(c)
  if (shown === 'live') return { cls: 'ok', text: c.who ? t('gui.conn.as_you', { who: c.who }) : t('gui.conn.st_live') }
  if (shown === 'broken') {
    return { cls: 'bad', text: c.refusal ? t('gui.conn.st_bad_why', { why: c.refusal }) : t('gui.conn.st_bad') }
  }
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

/* What a card says under its name: what to do about a failure, how far this
   entrance is from receiving, or, once it receives, who it receives as. */
function lineOf(c: ConnChannel): string {
  const shown = shownOf(c)
  if (shown === 'live') return c.who ? t('gui.conn.as_you', { who: c.who }) : t('gui.conn.line_live')
  if (shown === 'broken') return t(scanLogin(c) ? 'gui.conn.line_bad_scan' : 'gui.conn.line_bad_creds')
  if (scanLogin(c)) return t('gui.conn.line_scan')
  const missing = (c.missing || []).length
  if (missing) return t('gui.conn.line_creds', { n: String(missing) })
  /* Switched on and still not in: whatever is holding it (no host, a code not
     scanned yet) the page or the sheet says -- "not switched on" would be
     false here. */
  return t(c.on ? 'gui.conn.line_saved' : 'gui.conn.line_ready')
}

/* The card's foot: one word for the three, in the same quiet grey for two of
   them, so a grid of twelve can be scanned for the one in trouble. */
function footOf(c: ConnChannel): { tone: 'quiet' | 'bad'; text: string } {
  const shown = shownOf(c)
  if (shown === 'live') return { tone: 'quiet', text: t('gui.conn.foot_on') }
  if (shown === 'broken') return { tone: 'bad', text: t('gui.conn.foot_bad') }
  return { tone: 'quiet', text: t('gui.conn.foot_off') }
}

const FOOT_CLASS = { quiet: 'su-foot su-foot-quiet', bad: 'su-foot su-foot-bad' } as const

const PLUS = 'M12 5v14M5 12h14'

/* One entrance. The whole card opens its sheet, and so does the corner: that
   is the only press a card has, because getting in is scanning a code or
   handing over credentials, and both happen in the sheet. */
function ChanCard({ c, current }: { c: ConnChannel; current: boolean }): JSX.Element {
  const shown = shownOf(c)
  const live = shown === 'live'
  const foot = footOf(c)
  const name = chanName(c)
  const open = (): void => store.openChannel(c)
  const led = live ? 'su-led' : shown === 'broken' ? 'su-led su-led-bad' : null
  return (
    <div
      className="su-card"
      role="button"
      tabIndex={0}
      aria-current={current ? 'true' : undefined}
      onClick={open}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open()
        }
      }}
    >
      <div className="su-ctop">
        <ChannelMark id={c.id} />
        <div className="su-nm">
          <span className="su-t">{name}</span>
          {led ? <span className={led} /> : null}
        </div>
        {live ? null : (
          <button
            aria-label={t('gui.conn.connect')}
            className="su-cbtn"
            onClick={(e) => {
              e.stopPropagation()
              open()
            }}
            title={t('gui.conn.connect')}
            type="button"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" aria-hidden="true">
              <path d={PLUS} />
            </svg>
          </button>
        )}
      </div>
      <div className="su-one">{lineOf(c)}</div>
      <div className="su-foot-slot">
        <div className={FOOT_CLASS[foot.tone]}>{foot.text}</div>
      </div>
    </div>
  )
}

/* The grid before the first answer: one placeholder per catalogue entry, the
   shape a real card has, so the cards land in place rather than after a jump. */
function WaitGrid(): JSX.Element {
  return (
    <div className="su-grid" aria-busy="true">
      {CHANNELS.map((c) => (
        <div className="su-card su-wcard" key={c.id}>
          <div className="su-ctop">
            <span className="su-wbar su-wtile" />
            <span className="su-wbar su-wname" />
          </div>
          <span className="su-wbar su-wline" />
          <div className="su-foot-slot">
            <span className="su-wbar su-wfoot" />
          </div>
        </div>
      ))}
    </div>
  )
}

/* The picked entrance, in the shared drawer. Signing in by phone is a
   sequence, not a form: nothing can be scanned until the entry is running. So
   a scan channel gets the wizard until it is paired, and the credential form
   is for the channels that have one. */
const SHEET_LED: Record<Shown, string | null> = { live: 'su-led', broken: 'su-led su-led-bad', idle: null }

function ChanSheet({ c }: { c: ConnChannel }): JSX.Element {
  const st = stateOf(c)
  const shown = shownOf(c)
  const signing = scanLogin(c) && connState(c) !== 'live'
  /* A scan entrance still to sign in says how it signs in; one that failed
     says it failed, like any other. */
  const led = SHEET_LED[shown]
  const by = signing && shown !== 'broken'
    ? <span className="st off">{t('gui.conn.cost_scan_line')}</span>
    : <span className={'st ' + st.cls}>{st.text}</span>
  return createPortal(
    <div className="su-sheet" aria-label={chanName(c)} role="document">
      <div className="su-head">
        <ChannelMark id={c.id} />
        <div className="su-meta">
          <h3>{chanName(c)}</h3>
          <div className="su-by">
            {led ? <span className={led} /> : null}
            {by}
          </div>
        </div>
      </div>
      {signing ? <ScanWizard c={c} /> : <ConnForm c={c} />}
    </div>,
    store.detailHost(),
  )
}

type Tab = 'all' | 'on' | 'off'

const TABS: ReadonlyArray<{ tab: Tab; label: string; empty: string }> = [
  { tab: 'all', label: 'gui.conn.tab_all', empty: 'gui.conn.none' },
  { tab: 'on', label: 'gui.conn.tab_on', empty: 'gui.conn.none_on' },
  { tab: 'off', label: 'gui.conn.tab_off', empty: 'gui.conn.none_off' },
]

/* Two groups, because there are two answers to "is this entrance mine yet".
   Connected is a fact only the live adapter can report, so a switch flipped on
   for an entrance that never came up stays with the ones still to connect.
   Those are ordered by what it costs to get in: a scan-login channel is one
   phone away, a channel wanting six credentials is an afternoon. */
export function ConnectionsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  const [tab, setTab] = useState<Tab>('all')
  const on = s.rows.filter((c) => connState(c) === 'live')
  /* The ones in trouble lead the rest: they are the only cards asking for
     something. */
  const broken = (c: ConnChannel): number => (shownOf(c) === 'broken' ? 0 : 1)
  const off = s.rows
    .filter((c) => connState(c) !== 'live')
    .sort((a, b) => broken(a) - broken(b) || costOf(a) - costOf(b))
  const rows: Record<Tab, ConnChannel[]> = { all: [...on, ...off], on, off }
  const current = TABS.find((x) => x.tab === tab)!
  const waiting = !s.loaded && !s.rows.length
  const picked = s.viewId ? s.rows.find((c) => c.id === s.viewId) : undefined
  return (
    <>
      <HubHead current="channels" />
      <div className="su-tabs" role="tablist">
        {TABS.map((x) => (
          <button
            aria-selected={tab === x.tab}
            className="su-tab"
            key={x.tab}
            onClick={() => setTab(x.tab)}
            role="tab"
            type="button"
          >
            {t(x.label)}
            {waiting ? <span className="su-wbar su-wtn" /> : <span className="su-tn">{String(rows[x.tab].length)}</span>}
          </button>
        ))}
      </div>
      {/* Nothing running that could host an entrance: every one of them is
          deaf for the same reason, so it is said once, here, and no card
          repeats it. */}
      {s.host === false ? (
        <div className="su-notice" role="status">
          {t('gui.conn.host_down')}
        </div>
      ) : null}
      {waiting ? (
        <WaitGrid />
      ) : rows[tab].length ? (
        <div className="su-grid">
          {rows[tab].map((c) => <ChanCard c={c} current={c.id === s.viewId} key={c.id} />)}
        </div>
      ) : (
        <div className="su-empty">{t(current.empty)}</div>
      )}
      {picked ? <ChanSheet c={picked} key={`${picked.id}:${s.epoch}`} /> : null}
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
       own control in the dialog, so this one only ever connects.
     *
     * The card closes when the write has been applied and the entrance reads
     * live after it, not before. A card opened on an entrance that was already
     * receiving used to close on the press itself, reporting an answer nothing
     * had given yet; and a rebuild that finishes before the status is re-read
     * shows no intermediate state at all, so the answer has to be read off the
     * write's own completion rather than waited for as a transition. A write
     * the gateway refused leaves the row as it was, live included, and that is
     * not an answer either: the card stays with the failure the source toasted. */
    void store.apply(c, patch, true).then((applied) => {
      if (applied && connState(c) === 'live') store.closeChannel()
    })
  }
  return (
    <>
      <div className="subody" id="connDlgBody">
        {/* Where the credentials come from, for an entrance still to get in:
            the first question a reader has, answered above the boxes it is
            about, with the console one press away. The count of what is
            missing is the head's, so it is not said here again. */}
        {apply && !live ? (
          <a className="su-guide" href={apply} target="_blank" rel="noreferrer">
            <span className="su-guide-t">{t('gui.conn.guide', { name: spaced(chanName(c)) })}</span>
            <span className="su-guide-go">
              {t('gui.conn.apply')}
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M7 17 17 7M9 7h8v8" />
              </svg>
            </span>
          </a>
        ) : null}
        {/* Said once for the form, not in every box: the boxes of a mail
            entrance read "set, leave blank to keep" six times over. */}
        {(c.fields || []).some((f) => f.set) ? <p className="su-keep">{t('gui.conn.keep_hint')}</p> : null}
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
        {/* The way back to off, at the far end from the verb that connects:
            side by side the two were one slip apart. */}
        {c.on ? (
          <button className="mini ghost su-off" onClick={() => void store.apply(c, {}, false)}>
            {t('gui.conn.disconnect')}
          </button>
        ) : null}
        {/* Only what the head cannot say: that the boxes hold something not
            yet saved. What is still missing is the head's line, and a second
            count down here said it twice. */}
        <span className="n">{dirty ? t('gui.conn.foot_dirty') : ''}</span>
        <button className="mini key" disabled={!ready} onClick={save}>
          {/* Keyed on receiving, not on the flag: a card whose credentials were
              written and refused would otherwise offer to "save" them again. */}
          {t(live ? 'gui.agent.save' : sent ? 'gui.conn.retry' : 'gui.conn.connect')}
        </button>
      </div>
    </>
  )
}

/* A Latin name set inside a Chinese sentence takes a space either side, the
   way the catalogue writes "Raven" into its own sentences; a Chinese name or
   an English sentence (whose template has its spaces) takes none. */
function spaced(name: string): string {
  return lang.get().lang === 'zh' && /^[\x20-\x7e]+$/.test(name) ? ` ${name} ` : name
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
  /* Pressing connect is the step there is before anything is on, so it is
     the current one, not a grey line like the two after it. */
  const s1 = c.on ? 'done' : 'now'
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
      <div className="subody suwiz su-scan" id="connDlgBody">
        {/* The code's place, drawn before there is a code: the reader sees
            where it will appear, and what brings it. The panel itself only
            mounts where a code can exist, and unmounting it is what stops the
            poll. */}
        <div className="su-qr">
          {s2 === 'now' ? (
            <QrPanel c={c} />
          ) : (
            <div className={paired ? 'su-qrph su-qrph-ok' : 'su-qrph'}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                {paired ? (
                  <path d="m5 12.5 4.5 4.5L19 7.5" />
                ) : (
                  <>
                    <rect x="4" y="4" width="6" height="6" rx="1" />
                    <rect x="14" y="4" width="6" height="6" rx="1" />
                    <rect x="4" y="14" width="6" height="6" rx="1" />
                    <path d="M14 14h2v2h-2zM18 18h2v2h-2zM14 18h2M18 14h2" />
                  </>
                )}
              </svg>
              <span>{t(paired ? 'gui.conn.qr_done' : 'gui.conn.qr_idle')}</span>
            </div>
          )}
        </div>
        <div className="su-steps">
        {step('1', s1, c.on ? t('gui.conn.w1_done') : t('gui.conn.w1_idle'))}
        {step(
          '2',
          s2,
          paired ? t('gui.conn.w2_done') : t('gui.conn.w2'),
          /* Two ways for there to be no code, and one sentence for both was
           * wrong in the more common one: "Raven is not running" printed over a
           * page the gateway itself was serving. Only a KNOWN host shifts the
           * blame to the adapter -- something is running it, so it started and
           * gave up, and that is the case worth trying again. Not knowing keeps
           * the old advice, because "open the app" is still the useful thing to
           * say to a reader whose gateway may well be down. */
          s2 !== 'now' && stalled ? t(host === true ? 'gui.conn.w2_down' : 'gui.conn.w2_blocked') : null,
        )}
        {step('3', s3, t('gui.conn.w3'))}
        </div>
      </div>
      <div className="sufoot">
        {/* Backing out sits at the far end, as it does under the form. */}
        {c.on && !paired ? (
          <button className="mini ghost su-off" onClick={() => void store.apply(c, {}, false)}>
            {t('gui.conn.disconnect')}
          </button>
        ) : null}
        <span className="n">{up && !paired ? t('gui.conn.w_wait') : ''}</span>
        {/* The list's two verbs, not two more of their own: the wizard's first
            button does what the row's does, and backing out is the same
            disconnect. */}
        {!c.on ? (
          <button className="mini key" onClick={() => void store.apply(c, {}, true)}>
            {t('gui.conn.connect')}
          </button>
        ) : paired ? (
          <button className="mini key" onClick={() => store.closeChannel()}>
            {t('gui.conn.w_done')}
          </button>
        ) : !up ? (
          /* On and not up: the entrance gave up, or nothing started it. The
             card carries the retry, or the only way to try again is to close
             this and find the card underneath. */
          <button className="mini key" onClick={() => void store.apply(c, {}, true)}>
            {t('gui.conn.w_retry')}
          </button>
        ) : null}
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
