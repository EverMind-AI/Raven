/* The decisions: what the redaction history settled on its own, then every
   value still undecided -- masked on the left, where it was seen on the
   right -- and the four buttons that decide them.

   The left column is the shared two-pane frame's list (components/TwoPane):
   one row per item, the masked value, its category, how often it occurs, the
   current decision and how many items it is linked to. Confirmed items --
   values already replaced, such as a private key -- come first and are only
   acknowledged. The right column asks for the focused item's contexts once
   (bugreport.item) and marks each hit inside them. A long list is paged
   here, not by the gateway: the status already carries every masked row. */

import { TwoPane, TwoPaneHead, TwoPaneList, TwoPaneRow, TwoPaneWait } from '../../components/TwoPane'
import { t } from '../../i18n/t'
import { ReportLost } from './ReportLost'
import { segments } from './spans'
import * as store from './store'

import type { ExportState } from './store'
import type { ReportItem } from './types'
import type { JSX } from 'react'

function History({ s }: { s: ExportState }): JSX.Element | null {
  const history = s.report?.status?.history
  if (!history) return null
  const auto = history.auto_kept + history.auto_redacted
  return (
    <div className="export-history">
      {history.state === 'on' && auto > 0 ? (
        <p>{t('gui.export.history_auto', { n: auto, kept: history.auto_kept, redacted: history.auto_redacted })}</p>
      ) : null}
      {history.state === 'off' ? <p>{t('gui.export.history_off')}</p> : null}
      {history.warning ? <p data-bad="true">{history.warning}</p> : null}
    </div>
  )
}

function rowSub(s: ExportState, item: ReportItem): string {
  if (item.kind === 'confirmed') return t('gui.export.confirmed')
  const parts = [
    item.category,
    t('gui.export.count', { n: item.count }),
    s.choices[item.id] === 'redacted' ? t('gui.export.redacted') : t('gui.export.kept'),
  ]
  if (item.linked.length) parts.push(t('gui.export.linked', { n: item.linked.length }))
  return parts.join(' · ')
}

function Row({ s, item }: { s: ExportState; item: ReportItem }): JSX.Element {
  return (
    <div className="export-item" data-bad={s.marked.includes(item.id) || undefined}>
      <TwoPaneRow
        current={s.focus === item.id}
        name={item.masked_sample}
        sub={rowSub(s, item)}
        tone={item.kind === 'confirmed' ? 'bad' : s.choices[item.id] === 'redacted' ? 'warn' : undefined}
        onOpen={() => store.focusItem(item.id)}
        trailing={item.kind === 'confirmed' ? (
          <label className="export-ack">
            <input
              type="checkbox"
              checked={s.acknowledged.includes(item.id)}
              onChange={(e) => store.acknowledge(item.id, e.currentTarget.checked)}
            />
            <span>{t('gui.export.acknowledge')}</span>
          </label>
        ) : undefined}
      />
    </div>
  )
}

function Contexts({ s, item }: { s: ExportState; item: ReportItem }): JSX.Element {
  const context = s.contexts[item.id]
  let body: JSX.Element
  if (!context || context.state === 'loading') body = <TwoPaneWait rows={3} />
  else if (context.state === 'error') {
    body = (
      <p className="export-lost" role="status">
        <span>{context.detail}</span>
        <button type="button" className="export-btn export-ghost" onClick={() => store.retryContext(item.id)}>
          {t('gui.export.retry')}
        </button>
      </p>
    )
  } else {
    const { groups, total_groups: total } = context.answer
    body = (
      <>
        {groups.map((group, i) => (
          <div key={i} className="export-ctxgroup">
            <div className="export-ctxlab">{group.label} {'·'} {t('gui.export.occurrences', { n: group.count })}</div>
            <pre className="export-ctx">
              {segments(group.text, group.spans).map((piece, j) =>
                piece.hit ? <mark key={j}>{piece.text}</mark> : <span key={j}>{piece.text}</span>)}
            </pre>
          </div>
        ))}
        {total > groups.length ? (
          <p className="export-hint">{t('gui.export.contexts_more', { total, shown: groups.length })}</p>
        ) : null}
      </>
    )
  }
  return (
    <>
      <TwoPaneHead name={item.masked_sample} meta={item.category} />
      <div className="export-ctxs">{body}</div>
    </>
  )
}

export function DecidePage({ s }: { s: ExportState }): JSX.Element {
  const list = store.items(s)
  const pages = Math.max(1, Math.ceil(list.length / store.LIST_PAGE))
  const shown = list.slice(s.listPage * store.LIST_PAGE, (s.listPage + 1) * store.LIST_PAGE)
  const focused = list.find((item) => item.id === s.focus) ?? null
  const single = focused !== null && focused.kind === 'suspected'
  const locked = s.acting
  return (
    <div className="export-decide">
      <History s={s} />
      <ReportLost s={s} />
      <div className="export-tools">
        <button type="button" className="export-btn export-ghost" disabled={locked} onClick={() => store.choose('kept', 'all')}>
          {t('gui.export.keep_all')}
        </button>
        <button type="button" className="export-btn export-ghost" disabled={locked} onClick={() => store.choose('redacted', 'all')}>
          {t('gui.export.redact_all')}
        </button>
        <span className="export-grow" />
        <button type="button" className="export-btn export-ghost" disabled={locked || !single} onClick={() => store.choose('kept', 'focus')}>
          {t('gui.export.keep')}
        </button>
        <button type="button" className="export-btn export-ghost" disabled={locked || !single} onClick={() => store.choose('redacted', 'focus')}>
          {t('gui.export.redact')}
        </button>
      </div>
      <TwoPane
        side={
          <>
            <TwoPaneList>{shown.map((item) => <Row key={item.id} s={s} item={item} />)}</TwoPaneList>
            {pages > 1 ? (
              <div className="export-pager">
                <button type="button" className="export-btn export-ghost" disabled={s.listPage === 0} onClick={() => store.setListPage(s.listPage - 1)}>
                  {t('gui.export.prev')}
                </button>
                <span>{t('gui.export.page_of', { page: s.listPage + 1, pages })}</span>
                <button type="button" className="export-btn export-ghost" disabled={s.listPage >= pages - 1} onClick={() => store.setListPage(s.listPage + 1)}>
                  {t('gui.export.next_page')}
                </button>
              </div>
            ) : null}
          </>
        }
      >
        {focused ? <Contexts s={s} item={focused} /> : <p className="export-empty">{t('gui.export.pick_item')}</p>}
      </TwoPane>
    </div>
  )
}
