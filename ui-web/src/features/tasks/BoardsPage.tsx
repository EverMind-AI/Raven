/* The Task Board page: every host conversation's Task Groups in one place, as
 * a board of category columns or as one list. A card unfolds in place into
 * its events and the questions it waits on, the sidebar's own thread; an event
 * is what goes to the conversation. What it shows is features/tasks/boards.ts's;
 * the page is the tasks domain's island in #tasksBoardBody (state/pages.ts).
 */

import { ArrowDown01Icon, BubbleChatIcon, KanbanIcon, ListViewIcon, SparklesIcon } from '@hugeicons/core-free-icons'
import { useEffect, useId, useState, useSyncExternalStore } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { since } from '../../lib/ago'
import { formatDuration } from '../../lib/duration'
import { current as sessionCurrent } from '../../lib/session'
import { useTick } from '../../lib/tick'
import * as lang from '../../state/lang'
import * as page from '../../state/page'
import * as registry from '../../state/session/registry'
import { sess } from '../../state/session/rows'
import * as ws from '../../state/ws'
import { revealTurnAt } from '../transcript/source'
import { MoveOnButton, Thread } from './BoardList'
import * as boards from './boards'
import { CATEGORY_WORD, cardKey, countsOf } from './boards'
import * as tasks from './store'

import type { ThreadVerbs } from './BoardList'
import type { BoardsState, CardThread, Category } from './boards'
import type { ProgressStep, TaskBoardCard, TaskGroup } from './types'
import type { JSX, ReactNode } from 'react'

const COLUMNS: readonly Category[] = ['needs_you', 'running', 'idle', 'completed']

/* The ring ticks from the oldest run still going; every other type's time is
   read off the group's own stamps. */
function Elapsed({ from }: { readonly from: number }): JSX.Element {
  return <>{t('gui.tasks.page.foot_running', { t: formatDuration(useTick(true, from)) })}</>
}

/* A card's type as a mark: amber needs the owner, a turning ring still runs,
   moss is idle and grey is done. Never alone -- `Words` says the same thing in
   text beside or across from it. */
function Mark({ card }: { readonly card: TaskBoardCard }): JSX.Element {
  if (card.category === 'running') return <span className="tkbring" data-st="run" />
  return <span className="tkbdot" data-cat={card.category} />
}

/* The type, then its time, one shape for all four. */
function Words({ card }: { readonly card: TaskBoardCard }): JSX.Element {
  switch (card.category) {
    case 'needs_you':
      return <>{t('gui.tasks.page.foot_needs_you', { t: since(card.updated_at) })}</>
    case 'running':
      return <Elapsed from={card.running_since_ms ?? card.updated_at} />
    case 'idle':
      return <>{t('gui.tasks.page.foot_idle', { t: since(card.updated_at) })}</>
    case 'completed':
      return <>{t('gui.tasks.page.foot_completed', { t: formatDuration(card.updated_at - card.created_at) })}</>
  }
}

/* The counters at the footer's right: how many events the group has, and how
   many sub-agents worked on it -- then whatever rides with them, which drops
   beneath with them, whole, in a narrow column. */
export function Counters({ card, children }: { readonly card: TaskBoardCard; readonly children?: ReactNode }): JSX.Element {
  return (
    <span className="tkbcnt">
      <span>{t('gui.tasks.page.events', { n: card.events })}</span>
      <span className="tkbsep" aria-hidden="true" />
      <span>{t('gui.tasks.page.sub_agents', { n: card.sub_agents })}</span>
      {children}
    </span>
  )
}

/* A card's face, top to bottom: the conversation it lives in, the Task Group
   and where it stands. */
export function CardFace({ card }: { readonly card: TaskBoardCard }): JSX.Element {
  return (
    <>
      <span className="tkbconv"><Icon icon={BubbleChatIcon} size={12} /><span className="tkbconvt">{card.session_title}</span></span>
      <span className="tkbtitle">{card.title}</span>
      {card.summary ? <span className="tkbsum">{card.summary}</span> : null}
    </>
  )
}

/* Under the face and outside it, so Move on is a press of its own: the status,
   the counters, and Move on. */
function CardFoot({ card }: { readonly card: TaskBoardCard }): JSX.Element {
  return (
    <div className="tkbfoot">
      <span className="tkbst"><Mark card={card} /><Words card={card} /></span>
      <Counters card={card}><MoveOnButton onClick={() => moveOnFrom(card)} /></Counters>
    </div>
  )
}

const newestFirst = (a: TaskBoardCard, b: TaskBoardCard): number => b.updated_at - a.updated_at

/* What a card or a row is called when it is read rather than seen: the Task
   Group, where it stands, and the conversation it lives in. */
const cardLabel = (card: TaskBoardCard): string =>
  t('gui.tasks.page.card_label', { title: card.title, cat: t(CATEGORY_WORD[card.category]), conv: card.session_title })

/* Which cards are unfolded, the threads they draw, and the press that folds
   or unfolds one: the store's, handed down to both views. */
interface Unfold {
  readonly unfolded: readonly string[]
  readonly threads: Readonly<Record<string, CardThread>>
  readonly toggle: (card: TaskBoardCard) => void
}

/* An unfolded card's thread, once its conversation's board has come and while
   the board still holds the group. */
function Unfolded({ card, id, thread }: {
  readonly card: TaskBoardCard
  readonly id: string
  readonly thread: CardThread | undefined
}): JSX.Element | null {
  const group = thread?.board.groups.find((g) => g.id === card.group_id)
  if (!thread || !group) return null
  return (
    <Thread
      id={id} group={group} now={Date.now()} verbs={verbsFor(card)}
      linked={(step) => tasks.linkedRows(step, thread.rows, thread.lineage)}
    />
  )
}

function Card({ card, unfold }: { readonly card: TaskBoardCard; readonly unfold: Unfold }): JSX.Element {
  const id = useId()
  const open = unfold.unfolded.includes(cardKey(card))
  return (
    <div className="tkbcard" data-open={open || undefined}>
      <button
        className="tkbface"
        type="button"
        aria-label={cardLabel(card)}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => unfold.toggle(card)}
      >
        <CardFace card={card} />
      </button>
      <CardFoot card={card} />
      {open ? <Unfolded card={card} id={id} thread={unfold.threads[card.session_key]} /> : null}
    </div>
  )
}

export function BoardView({ cards, showCompleted, ...unfold }: {
  readonly cards: TaskBoardCard[]
  readonly showCompleted: boolean
} & Unfold): JSX.Element {
  const shown = showCompleted ? COLUMNS : COLUMNS.filter((cat) => cat !== 'completed')
  return (
    <div className="tkbboard" data-cols={shown.length}>
      {shown.map((cat) => {
        const inCol = cards.filter((c) => c.category === cat).sort(newestFirst)
        return (
          <section className="tkbcol" data-cat={cat} key={cat}>
            <h3 className="tkbhead">
              <Icon icon={SparklesIcon} size={15} />
              <span className="tkbname">{t(CATEGORY_WORD[cat])}</span>
              <span className="tkbn">{inCol.length}</span>
            </h3>
            {inCol.length ? (
              inCol.map((card) => <Card card={card} unfold={unfold} key={cardKey(card)} />)
            ) : (
              <p className="tkbempty">{t('gui.tasks.page.empty_col')}</p>
            )}
          </section>
        )
      })}
    </div>
  )
}

/* One row: the mark, the title with its conversation beside it, the summary
   on one line, and the status and counters at the right; unfolded, its thread
   under it. */
function Row({ card, unfold }: { readonly card: TaskBoardCard; readonly unfold: Unfold }): JSX.Element {
  const id = useId()
  const open = unfold.unfolded.includes(cardKey(card))
  return (
    <div className="tkbrow" data-open={open || undefined}>
      <button
        className="tkbface"
        type="button"
        aria-label={cardLabel(card)}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => unfold.toggle(card)}
      >
        <span className="tkbmark"><Mark card={card} /></span>
        <span className="tkbbody">
          <span className="tkbline">
            <span className="tkbtitle">{card.title}</span>
            <span className="tkbconvt">{card.session_title}</span>
          </span>
          {card.summary ? <span className="tkbsum1">{card.summary}</span> : null}
        </span>
        <span className="tkbmeta"><Words card={card} /><Counters card={card} /></span>
      </button>
      <MoveOnButton onClick={() => moveOnFrom(card)} />
      {open ? <Unfolded card={card} id={id} thread={unfold.threads[card.session_key]} /> : null}
    </div>
  )
}

type Chip = 'all' | Category

export function ListView({ cards, showCompleted, ...unfold }: {
  readonly cards: TaskBoardCard[]
  readonly showCompleted: boolean
} & Unfold): JSX.Element {
  const [chip, setChip] = useState<Chip>('all')
  const [foldOpen, setFoldOpen] = useState(false)
  const counts = countsOf(cards)
  const offered: readonly Chip[] = ['all', ...(showCompleted ? COLUMNS : COLUMNS.filter((cat) => cat !== 'completed'))]
  /* A chip the switch took away (completed, with the switch now off) falls
     back to All rather than leaving an empty list behind a hidden chip. */
  const at: Chip = offered.includes(chip) ? chip : 'all'
  const of = (cat: Category) => cards.filter((c) => c.category === cat).sort(newestFirst)
  const sections = at === 'all' ? COLUMNS.filter((cat) => cat !== 'completed') : [at]
  const filled = sections.filter((cat) => of(cat).length)
  const fold = at === 'all' && showCompleted && counts.completed > 0
  return (
    <>
      <div className="tkbchips">
        {offered.map((c) => (
          <button className="tkbchip" type="button" key={c} data-cat={c} aria-pressed={c === at} onClick={() => setChip(c)}>
            {t(c === 'all' ? 'gui.tasks.page.all' : CATEGORY_WORD[c])}
            <span className="tkbchipn">{c === 'all' ? counts.needs_you + counts.running + counts.idle : counts[c]}</span>
          </button>
        ))}
      </div>
      {filled.map((cat) => (
        <section className="tkbsec" data-cat={cat} key={cat}>
          <h3 className="tkbsechead"><span className="tkbname">{t(CATEGORY_WORD[cat])}</span><span className="tkbsecn">{of(cat).length}</span></h3>
          <div className="tkbrows">{of(cat).map((card) => <Row card={card} unfold={unfold} key={cardKey(card)} />)}</div>
        </section>
      ))}
      {!filled.length && !fold ? <p className="tkbempty">{t('gui.tasks.page.empty_col')}</p> : null}
      {fold ? (
        <section className="tkbsec" data-cat="completed">
          <button className="tkbfold" type="button" aria-expanded={foldOpen} onClick={() => setFoldOpen(!foldOpen)}>
            <Icon icon={ArrowDown01Icon} size={14} />
            <span className="tkbname">{t(CATEGORY_WORD.completed)}</span><span className="tkbsecn">{counts.completed}</span>
          </button>
          {foldOpen ? (
            <div className="tkbrows">{of('completed').map((card) => <Row card={card} unfold={unfold} key={cardKey(card)} />)}</div>
          ) : null}
        </section>
      ) : null}
    </>
  )
}

/* Going to a card: the page steps aside, its conversation opens -- listed
   first when the rail has not seen it yet -- and the sidebar's tasks tab opens
   with that Task Group expanded (BoardList spends the request). The tab is
   opened after the switch because the desk keys what it opens by the
   conversation on screen. Answers whether that conversation is the one on
   screen when it is done. */
export async function openCard(card: TaskBoardCard): Promise<boolean> {
  tasks.requestFocus(card.session_key, card.group_id)
  page.show(null)
  if (card.session_key !== sessionCurrent()) {
    let row = sess(card.session_key)
    if (!row) {
      await registry.refreshList()
      row = sess(card.session_key)
    }
    const target = row ?? { id: card.session_key, title: card.session_title }
    await registry.switchTo(target)
    /* Another open overtook this one while the conversation loaded (the switch
       answers the canonical id on `target` when it lands): the tab and the
       request were for a conversation no longer on screen, and a newer
       request is not this one's to take back. */
    if (target.id !== sessionCurrent()) {
      if (tasks.focusFor(card.session_key) === card.group_id) tasks.clearFocus()
      return false
    }
  }
  ws.pick('tasks')
  return true
}

/* Move on from the page: the card's conversation opens as an event's does,
   and its next message is about the group. Not held when another switch
   overtook the open: the bar would name a conversation no longer on screen. */
function moveOnFrom(card: TaskBoardCard): void {
  void openCard(card).then((there) => {
    if (there) tasks.moveOn(card.session_key, { id: card.group_id, title: card.title, summary: card.summary })
  })
}

/* An unfolded card's thread, wired to the card's own conversation: an event
   goes to it and on to the reply its turn ended on (the switch answers once
   the history is drawn), Answer drafts the reply there once it is open, and
   the group's writes go to it whichever conversation is on screen. */
function verbsFor(card: TaskBoardCard): ThreadVerbs {
  return {
    reveal: (step: ProgressStep) => {
      void openCard(card).then((there) => { if (there) revealTurnAt(step.turn_at, step.turn_id) })
    },
    setStatus: (group, status) => { void boards.setGroupStatus(card.session_key, group.id, status) },
    ignore: (group, step, question) => { void boards.ignoreQuestion(card.session_key, group.id, step.id, question) },
    answer: (group: TaskGroup) => {
      void openCard(card).then((there) => { if (there) tasks.answer(group) })
    },
  }
}

/* The title, which conversations to show, the completed switch and the view. */
function Toolbar({ s }: { readonly s: BoardsState }): JSX.Element {
  const conversations = [...new Map(s.cards.map((c) => [c.session_key, c.session_title])).entries()]
  return (
    <div className="tkbbar">
      <h2 className="tkbh">{t('gui.tasks.page.title')}</h2>
      <select
        className="tkbconvsel"
        aria-label={t('gui.tasks.page.conversation')}
        value={s.filter ?? ''}
        onChange={(e) => boards.setFilter(e.target.value || null)}
      >
        <option value="">{t('gui.tasks.page.all_conversations')}</option>
        {conversations.map(([key, title]) => <option value={key} key={key}>{title}</option>)}
      </select>
      <button
        className="tkbswitch"
        type="button"
        role="switch"
        aria-checked={s.showCompleted}
        onClick={() => boards.setShowCompleted(!s.showCompleted)}
      >
        <span className="tkbknob" aria-hidden="true" />
        {t('gui.tasks.page.show_completed', { n: s.completedCount })}
      </button>
      <div className="tkbseg" role="group">
        <button type="button" data-view="board" aria-pressed={s.view === 'board'} onClick={() => boards.setView('board')}>
          <Icon icon={KanbanIcon} size={14} />{t('gui.tasks.page.view_board')}
        </button>
        <button type="button" data-view="list" aria-pressed={s.view === 'list'} onClick={() => boards.setView('list')}>
          <Icon icon={ListViewIcon} size={14} />{t('gui.tasks.page.view_list')}
        </button>
      </div>
    </div>
  )
}

export function TasksApp(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  const s = useSyncExternalStore(boards.subscribe, boards.get)
  /* A conversation picked in the filter whose cards have all gone -- its last
     open group completed with the switch off -- would leave an empty page
     under a filter that no longer lists it. */
  useEffect(() => {
    if (s.loaded && s.filter !== null && !s.cards.some((c) => c.session_key === s.filter)) boards.setFilter(null)
  }, [s.loaded, s.filter, s.cards])
  const cards = boards.visible(s.cards)
  return (
    <div className="tkbpage">
      <Toolbar s={s} />
      {s.error ? <p className="tkberr" role="status">{t('gui.tasks.page.error')}</p> : null}
      {!s.loaded ? null : s.view === 'list' ? (
        <ListView cards={cards} showCompleted={s.showCompleted} unfolded={s.unfolded} threads={s.threads} toggle={boards.toggleCard} />
      ) : (
        <BoardView cards={cards} showCompleted={s.showCompleted} unfolded={s.unfolded} threads={s.threads} toggle={boards.toggleCard} />
      )}
    </div>
  )
}
