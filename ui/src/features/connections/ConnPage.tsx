import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { t } from '../../shell/bridge'
import { SetupGroup, SetupRow, Tile } from '../../shell/setuprow'
import { Field, SheetHead, StateLine } from '../../shell/setupsheet'
import * as store from './store'

import type { ConnChannel, ConnField } from './types'
import type { JSX } from 'react'

/* Where Raven receives messages. One row per entry of the channel catalogue;
   the fields each row's dialog draws come from the channel's own Pydantic
   schema, shipped on channels.status, so the form cannot drift from the model.
 *
 * Grouped by whether the entry is in service, and the addable ones ordered by
 * what it costs to get in -- a scan-login channel is one phone away, a channel
 * wanting six credentials is an afternoon. That ordering is the page's only
 * opinion, and it replaces a flat list of twelve where the cheapest way in sat
 * wherever the catalogue happened to put it.
 */

/* One accessor so a renderer never has to know which of the catalogue's two
   spellings an entry uses (i18n key vs verbatim brand name). */
const chanName = (c: ConnChannel): string => (c.key ? t(c.key) : (c.name ?? c.id))

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

/* The same cost, said as a sentence. The badge is for a row being scanned in a
   list of twelve; the sheet has room to say it plainly. */
function CostBadge({ c }: { c: ConnChannel }): JSX.Element {
  if (scanLogin(c)) return <span className="kd auth">{t('gui.conn.cost_scan')}</span>
  const n = (c.fields || []).filter((f) => f.required).length
  return <span className="kd">{t('gui.conn.cost_n', { n: String(n) })}</span>
}

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

/* One green line for what is actually receiving, and nothing else.
 *
 * It counts live adapters, never the config switch: counting the switch is how
 * the page came to say "receiving on 1 entrance" over a card that said, in the
 * same breath, that the adapter had never started. The unfinished ones are not
 * named here either -- they have a group of their own now, with a count on it,
 * and saying it twice made the top of the page a place for bad news. */
function GatewayBar({ rows }: { rows: ConnChannel[] }): JSX.Element | null {
  const live = rows.filter((c) => connState(c) === 'live').length
  if (!live) return null
  return (
    <div className="sustate">
      <span className="led" />
      <span>{t('gui.conn.gw_live', { n: String(live) })}</span>
    </div>
  )
}

/* Why an entrance that is in service is not receiving, in the row's own badge
   slot -- the same slot an addable row uses for what it costs to get in. The
   group heading says the entrance was handed to Raven; this says how far that
   actually got. A live one needs no badge: the group and the green dot have
   said it. */
function StateBadge({ c }: { c: ConnChannel }): JSX.Element | null {
  const live = connState(c)
  if (live === 'live') return null
  const key = live === 'down' ? 'tag_down' : live === 'unpaired' ? 'tag_unpaired' : 'tag_unknown'
  return <span className={live === 'down' ? 'kd bad' : 'kd warn'}>{t('gui.conn.' + key)}</span>
}

/* Three groups, because there are three answers to "is this entrance mine yet".
 *
 * Grouping by the config switch made pressing the button the whole of joining:
 * an entrance moved to "in service" before a code had been scanned, before a
 * credential had been tried, and would have sat there just the same with a
 * made-up token in it. The switch is a decision; being in service is a fact,
 * and only the live adapter can report it. So the flag now buys a place in
 * "connecting", and the entrance earns "in service" by receiving. */
function ConnList({ rows }: { rows: ConnChannel[] }): JSX.Element {
  const s = store.getState()
  const on = rows.filter((c) => connState(c) === 'live')
  const pending = rows.filter((c) => c.on && connState(c) !== 'live')
  const off = rows.filter((c) => !c.on).sort((a, b) => costOf(a) - costOf(b))
  const list = (items: ConnChannel[]): JSX.Element => (
    <div className="sulist">
      {items.map((c) => (
        <ConnRow key={c.id} c={c} sel={s.dialogId === c.id} />
      ))}
    </div>
  )
  return (
    <>
      <div className="pmhero">
        <h3>{t('gui.page.conn')}</h3>
      </div>
      <GatewayBar rows={rows} />
      {/* Nothing in service yet is the ordinary first run, and a heading over an
          empty box saying so was the page explaining itself. The addable group
          below is the whole answer. */}
      {on.length ? (
        <SetupGroup label={t('gui.conn.g_on')} count={on.length}>
          {list(on)}
        </SetupGroup>
      ) : null}
      {pending.length ? (
        <SetupGroup label={t('gui.conn.g_pending')} count={pending.length}>
          {list(pending)}
        </SetupGroup>
      ) : null}
      {off.length ? (
        <SetupGroup label={t('gui.conn.g_off')} count={off.length}>
          {list(off)}
        </SetupGroup>
      ) : null}
    </>
  )
}

/* One row, one verb, and the same pair the agents page offers: connect or
   disconnect. It used to be a switch on one side and two different way-in labels
   on the other, which named the mechanism (a flag, an enable) instead of the
   errand. Connect opens the card, because what an entrance needs next is a form
   or a code; disconnect is the write itself, since there is nothing to ask. */
function ConnRow({ c, sel }: { c: ConnChannel; sel: boolean }): JSX.Element {
  const cn = chanName(c)
  const open = (): void => store.openDialog(c)
  const act = c.on ? (
    /* No confirm: this only takes the entrance out of service. The credentials
       stay in config and connect puts it back, so a dialog would be asking
       permission for a switch. */
    <button className="mini ghost" aria-label={cn} onClick={() => store.toggle(c)}>
      {t('gui.conn.disconnect')}
    </button>
  ) : (
    <button className="mini" aria-label={cn} onClick={open}>
      {t('gui.conn.connect')}
    </button>
  )
  return (
    <SetupRow
      name={cn}
      /* Dot only: SetupRow draws the second line when there is text for it, and
         here there is not. */
      state={{ cls: stateOf(c).cls, text: '' }}
      /* In service: why it is not receiving yet, or nothing. Not in service:
         what it costs to get in. */
      tags={c.on ? <StateBadge c={c} /> : <CostBadge c={c} />}
      act={act}
      sel={sel}
      onOpen={open}
    />
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
    /* Click-away. The card is modal: the scrim covers the page and takes the
       press, so a click outside closes this card and does not also operate what
       it was covering -- pressing the rail behind an open card must not
       navigate, and pressing another row's disconnect must not disconnect it.
     *
     * Listened for on the document rather than on the scrim, because the press
     * that closes the card may also land on the page's own chrome above the
     * veil's stacking context. Guarded on the card itself, so a press inside it
     * (focusing a credential box) is not a click-away. */
    const onDown = (e: MouseEvent): void => {
      const t = e.target as Node | null
      if (t && veil.firstElementChild?.contains(t)) return
      store.closeDialog()
    }
    document.addEventListener('mousedown', onDown, true)
    return () => {
      veil.dataset.open = 'false'
      document.removeEventListener('mousedown', onDown, true)
    }
  }, [veil])
  if (!veil) return null
  const st = stateOf(c)
  /* Signing in by phone is a sequence, not a form: nothing can be scanned
     until the entry is running. So a scan channel gets the wizard until it is
     paired, and the credential form is for the channels that have one. */
  const signing = scanLogin(c) && connState(c) !== 'live'
  /* The centred card every module's detail is, not a surface of its own: an
     entrance and an agent are the same errand, and the reader should not have to
     learn two shapes for it.

     Three parts, and only the middle one scrolls: the head names what is being
     edited and the foot carries the verb, so neither can be scrolled out of
     reach. Both used to ride inside the scroller -- with mail's twenty-one
     optional fields unfolded, the button that saves them sat a thousand pixels
     down, and pinning it there with `sticky` left the field under it showing
     through the gutter. A form's own state and its action belong to the card,
     not to its scroll position. */
  return createPortal(
    <div className="sheet" role="dialog" aria-modal="true" aria-label={chanName(c)}>
      <SheetHead
        tile={<Tile name={chanName(c)} />}
        name={chanName(c)}
        /* One fact, once. Scanning is the only way in that the body does not
           already spell out: a credential form heads its own section with the
           count, and a channel in service has the state line. */
        facts={signing ? t('gui.conn.cost_scan_line') : undefined}
        onClose={() => store.closeDialog()}
        closeLabel={t('gui.conn.close')}
      />
      {signing ? <ScanWizard c={c} /> : <ConnForm c={c} state={st} />}
    </div>,
    veil,
  )
}

/* One form per channel, built from the fields its own schema declares (they
   ride on channels.status). Secrets never echo back: a set field shows a
   placeholder, and a box left blank means "keep", never "erase". Only the
   required fields show; everything optional folds behind one line, closed,
   with its count. Inputs are uncontrolled, as the legacy form kept them. */
function ConnForm({ c, state }: { c: ConnChannel; state: { cls: string; text: string } }): JSX.Element {
  const inputs = useRef(new Map<string, HTMLInputElement>()).current
  const [advOpen, setAdvOpen] = useState(false)
  const [dirty, setDirty] = useState(false)
  /* Whether this card has handed its credentials over yet. Only after that does
     the state line have anything to report, and only then does the foot say
     "trying" rather than "connect". */
  const [sent, setSent] = useState(false)
  /* Receiving is the only thing that closes this card by itself. Anything else
     -- an adapter that would not start, a gateway that could not be asked -- is
     a reason the reader is owed, so the card stays with the state line up. */
  const live = connState(c) === 'live'
  useEffect(() => {
    if (sent && live) store.closeDialog()
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
          onInput={() => setDirty(true)}
          ref={(el) => {
            if (el) inputs.set(f.key, el)
            else inputs.delete(f.key)
          }}
        />
      </Field>
    )
  }
  const required = (c.fields || []).filter((f) => f.required)
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
        {c.on || sent ? <StateLine cls={state.cls} text={state.text} /> : null}
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
        <button className="mini key" onClick={save}>
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
     gone quiet -- the dead end this wizard exists to remove. */
  const stalled = !!c.on && c.running !== true
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
             current, not above the wizard. */
          s2 === 'now' ? <QrPanel c={c} /> : stalled ? t('gui.conn.w2_blocked') : null,
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
          <button className="mini key" onClick={() => store.closeDialog()}>
            {t('gui.conn.w_done')}
          </button>
        ) : (
          <button className="mini ghost" onClick={() => void store.apply(c, {}, false)}>
            {t('gui.conn.disconnect')}
          </button>
        )}
      </div>
    </>
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
