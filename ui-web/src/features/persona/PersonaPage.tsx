import { useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import * as store from './store'
import './styles.css'

import type { PersonaLine, PersonaRow } from './types'
import type { JSX } from 'react'

/* One Persona on the wall. The card says what it is and who it can hand work
   to, and its only action starts a conversation on it -- there is no "preview"
   or "open", because a Persona is a thing to talk to and reading its fields is
   what the playbook library is for. */
function Card({ row, confirming }: { row: PersonaRow; confirming: boolean }): JSX.Element {
  return (
    <article className="persona-card">
      <header>
        <h3>{row.name}</h3>
        {row.disabled ? <span className="persona-chip">{t('gui.persona.disabled')}</span> : null}
      </header>
      <p className="persona-desc">{row.description || t('gui.persona.no_description')}</p>
      <Seats row={row} />
      {confirming ? (
        <div className="persona-acts persona-confirm">
          <span className="persona-muted">{t('gui.persona.delete_sure', { name: row.name })}</span>
          <button className="persona-drop persona-danger" onClick={() => void store.remove(row.name)}>
            {t('gui.persona.delete_yes')}
          </button>
          <button className="persona-drop" onClick={() => store.cancelRemove()}>
            {t('gui.persona.delete_no')}
          </button>
        </div>
      ) : (
        <div className="persona-acts">
          <button className="persona-start" disabled={row.disabled} onClick={() => store.start(row.name)}>
            {t('gui.persona.start')}
          </button>
          <button className="persona-drop" onClick={() => store.askRemove(row.name)}>
            {t('gui.persona.delete')}
          </button>
        </div>
      )}
    </article>
  )
}

/* What was said, so far. Two speakers and no tool rows: the transcript island
   draws a turn's work, and this page draws the conversation about a Persona. */
function Said({ lines }: { lines: PersonaLine[] }): JSX.Element | null {
  if (!lines.length) return null
  return (
    <div className="persona-said">
      {lines.map((line, i) => {
        /* Read out of the className expression on purpose: a role compared
           inside one reads as a class literal to the namespace check. */
        const mine = line.role === 'user'
        return (
          <p key={i} className={mine ? 'persona-mine' : 'persona-theirs'}>
            <span className="persona-who">{t(mine ? 'gui.persona.you' : 'gui.persona.raven')}</span>
            {line.text}
          </p>
        )
      })}
    </div>
  )
}

/* The seats: the one identity the reader talks to, and the sub-agents it hands
   work to. Both rows name where the seat comes from, because a card that drew
   the delegates alone read as "two agents" -- the coordinator was not on it at
   all, and which registered sub-agent each delegate dispatched to lived in a
   `title` nobody hovers. */
function Seats({ row }: { row: PersonaRow }): JSX.Element {
  const workers = row.workers || []
  return (
    <ul className="persona-workers">
      {row.coordinator ? (
        <li className="persona-seat persona-lead">
          <span className="persona-chip">{t('gui.persona.seat_main')}</span>
          <span className="persona-to" aria-hidden="true">→</span>
          <span className="persona-agent">{t('gui.persona.host')}</span>
          {row.coordinator_brief ? <span className="persona-brief">{row.coordinator_brief}</span> : null}
        </li>
      ) : null}
      {workers.length === 0 && !row.coordinator ? (
        <li className="persona-muted">{t('gui.persona.no_workers')}</li>
      ) : null}
      {workers.map(w => (
        <li key={w.label} className="persona-seat">
          <span className="persona-chip">{w.label}</span>
          <span className="persona-to" aria-hidden="true">→</span>
          <span className="persona-agent">{w.agent}</span>
        </li>
      ))}
    </ul>
  )
}

/* What the turn is doing, off its own frames. The page drew a static line
   before this and read as frozen on a two-minute generation -- which is what a
   reader called it: no progress, no status. */
function Progress({ s }: { s: store.PersonaState }): JSX.Element {
  const step = s.doing
  const doing = !step || step.kind === 'thinking'
    ? t('gui.persona.step_thinking')
    : step.kind === 'tool'
      ? t('gui.persona.step_tool', { name: step.label })
      : step.kind === 'saying'
        ? t('gui.persona.step_saying')
        : step.kind === 'error'
          ? t('gui.persona.step_error')
          : t('gui.persona.step_done')
  return (
    <div className="persona-progress" role="status" aria-live="polite">
      <p className="persona-doing">
        <span className="persona-spin" aria-hidden="true" />
        {doing}
        {s.waited > 2 ? <span className="persona-secs">{t('gui.persona.working_for', { secs: s.waited })}</span> : null}
      </p>
      {s.ran.length ? (
        <ul className="persona-ran">
          <li className="persona-who">{t('gui.persona.ran')}</li>
          {s.ran.map((name, i) => <li key={i} className="persona-chip">{name}</li>)}
        </ul>
      ) : null}
    </div>
  )
}

/* The question the engine is blocked on, answered here rather than on a sheet
   above a composer the reader is not looking at (state/clarifyClaim.ts). */
function Asked({ ask }: { ask: store.PendingAsk }): JSX.Element {
  return (
    <div className="persona-asked" role="group" aria-label={t('gui.persona.asked')}>
      <p className="persona-question">{ask.question}</p>
      {ask.choices.length ? (
        <ul className="persona-choices">
          {ask.choices.map((choice, i) => (
            <li key={i}>
              <button className="persona-choice" onClick={() => void store.answer(choice)}>{choice}</button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

/* The generated Persona: what the turn made, named so the reader can find it
   later, kept or let go here and nowhere else. */
function Draft({ row, name, saving }: { row: PersonaRow; name: string; saving: boolean }): JSX.Element {
  return (
    <article className="persona-card persona-draft">
      <header><h3>{t('gui.persona.draft_head')}</h3></header>
      <p className="persona-desc">{row.description || t('gui.persona.no_description')}</p>
      <Seats row={row} />
      <label className="persona-name">
        <span>{t('gui.persona.draft_name')}</span>
        <input
          type="text"
          value={name}
          aria-label={t('gui.persona.draft_name')}
          onChange={e => store.rename(e.target.value)}
        />
      </label>
      <div className="persona-acts">
        <button className="persona-start" disabled={saving} onClick={() => void store.save()}>
          {t('gui.persona.save')}
        </button>
        <button className="persona-drop" disabled={saving} onClick={() => void store.discard()}>
          {t('gui.persona.discard')}
        </button>
      </div>
    </article>
  )
}

/* The page opens here: one conversation about one Persona, from the first
   sentence to the save. */
function Maker({ s }: { s: store.PersonaState }): JSX.Element {
  const started = s.lines.length > 0
  const label = started ? t('gui.persona.reply') : t('gui.persona.ask')
  return (
    <div className="persona-maker">
      <Said lines={s.lines} />
      {s.busy ? <Progress s={s} /> : null}
      {s.ask ? <Asked ask={s.ask} /> : null}
      {s.draft ? <Draft row={s.draft} name={s.name} saving={s.saving} /> : null}
      <textarea
        className="persona-ask"
        value={s.text}
        placeholder={label}
        aria-label={label}
        disabled={s.busy}
        onChange={e => store.type(e.target.value)}
      />
      <div className="persona-acts">
        <button
          className="persona-start"
          disabled={s.busy || !s.text.trim()}
          onClick={() => void (s.ask ? store.answer(s.text) : store.send())}
        >
          {s.ask ? t('gui.persona.answer') : t('gui.persona.send')}
        </button>
        {started ? (
          <button className="persona-drop" onClick={() => store.reset()}>{t('gui.persona.restart')}</button>
        ) : null}
      </div>
      {s.makeErr ? (
        <div className="persona-lapsed">
          <p className="err">{s.makeErr}</p>
          {s.session ? (
            <button className="persona-drop" disabled={s.busy} onClick={() => void store.recheck()}>
              {t('gui.persona.recheck')}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/* The wall of what was kept. */
function Wall({ s }: { s: store.PersonaState }): JSX.Element {
  const rows = store.visible()
  if (s.err) return <p className="err">{s.err}</p>
  /* Not yet read is not the same as empty: one is a wait, the other is an
     answer, and drawing "no personas" during the read tells the reader
     something untrue for as long as the round trip takes. */
  if (s.rows === null) return <p className="persona-muted">{t('gui.persona.loading')}</p>
  return (
    <>
      <div className="persona-toolbar">
        <input
          type="search"
          value={s.query}
          placeholder={t('gui.persona.search')}
          aria-label={t('gui.persona.search')}
          onChange={e => store.search(e.target.value)}
        />
      </div>
      {rows.length === 0 ? (
        <p className="persona-muted">{s.rows.length === 0 ? t('gui.persona.empty') : t('gui.persona.no_match')}</p>
      ) : (
        <div className="persona-wall">
          {rows.map(r => <Card key={r.name} row={r} confirming={s.confirming === r.name} />)}
        </div>
      )}
    </>
  )
}

export function PersonaApp(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  const s = useSyncExternalStore(store.subscribe, store.get)

  return (
    <>
      <div className="persona-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={s.view === 'make'}
          className="persona-tab"
          onClick={() => store.show('make')}
        >
          {t('gui.persona.tab_make')}
        </button>
        <button
          role="tab"
          aria-selected={s.view === 'saved'}
          className="persona-tab"
          onClick={() => store.show('saved')}
        >
          {t('gui.persona.tab_saved')}
        </button>
      </div>
      {s.view === 'make' ? <Maker s={s} /> : <Wall s={s} />}
    </>
  )
}
