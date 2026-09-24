/* Usage: one range, and everything below it drawn from that one answer --
   tiles from the totals, a bar per day, the per-model and per-tool tables. */
import { useEffect } from 'react'

import { t } from '../../../i18n/t'
import { show as toast } from '../../../state/toast'
import { Card, Empty, GLYPH, Row, Seg, fmt } from '../Fields'
import { UsageWait } from '../Skeletons'
import * as store from '../store'
import { isoDay, lastDays } from '../store'

import type { UsageRange, UsageStats } from '../types'
import type { JSX } from 'react'

export const MAX_DAYS = 90

const money = (n: number | null | undefined): string => `$${(n || 0).toFixed(2)}`
/* Whether any call in the range came back with a price at all. A catalogue
   with no rates prices nothing, and every cost surface then reads $0.00 --
   a number, where the honest answer is that there is none. So the cost tile,
   the per-day bars and the model table's cost column are all drawn only when
   at least one call was priced; the per-row "no price" note covers the mixed
   range, where some models are priced and some are not. */
const anyPriced = (u: UsageStats): boolean => u.llm.total.calls > u.llm.total.cost_missing_calls
const hit = (input: number | null | undefined, cached: number | null | undefined): number => {
  const c = cached || 0
  const all = (input || 0) + c
  return all ? c / all : 0
}

/* What the page accepts before asking: both dates inside the 90-day window
   the counter keeps, and from not after to. */
export function clampRange(from: string, to: string): UsageRange | null {
  const { from: earliest, to: today } = lastDays(MAX_DAYS)
  if (from < earliest || to > today || !from || !to) return null
  if (from > to) return null
  return { from, to }
}

function Tiles({ u }: { u: UsageStats }): JSX.Element {
  const tot = u.llm.total
  return (
    <Card raw>
      <div className="settings-tiles4">
        <div className="settings-tl"><div className="settings-n">{tot.calls.toLocaleString()}</div><div className="settings-l">{t('gui.settings.usage.calls')}</div></div>
        {anyPriced(u) && <div className="settings-tl"><div className="settings-n">{money(tot.cost_usd)}</div><div className="settings-l">{t('gui.settings.usage.cost')}</div></div>}
        <div className="settings-tl"><div className="settings-n">{fmt(tot.output_tokens || 0)}</div><div className="settings-l">{t('gui.settings.usage.output')}</div></div>
        <div className="settings-tl settings-good"><div className="settings-n">{(hit(tot.input_tokens, tot.cache_read_tokens) * 100).toFixed(1)}%</div><div className="settings-l">{t('gui.settings.usage.hit')}</div></div>
      </div>
    </Card>
  )
}

function Bars({ u }: { u: UsageStats }): JSX.Element {
  const mx = Math.max(0.01, ...u.daily.map((d) => d.cost_usd || 0))
  return (
    <Card title={t('gui.settings.usage.cost')} raw>
      <div className="settings-bars">
        {u.daily.map((d) => {
          const v = d.cost_usd || 0
          return (
            <div key={d.date} className={v === 0 ? 'settings-bb settings-dim' : 'settings-bb'}
              style={{ height: `${Math.max(2, Math.round((v / mx) * 82))}px` }}
              title={`${d.date} · ${money(v)}`} data-date={d.date} />
          )
        })}
      </div>
      <div className="settings-bax"><span>{u.from}</span><span>{u.to}</span></div>
    </Card>
  )
}

function Models({ u }: { u: UsageStats }): JSX.Element {
  const rows = u.llm.models
  const priced = anyPriced(u)
  const anyWrite = rows.some((m) => (m.cache_write_tokens || 0) > 0)
  const most = Math.max(1, ...rows.map((m) => m.calls))
  return (
    <Card title={t('gui.settings.usage.by_model')} raw>
      {/* A table of headers over one "nothing" row said the range was empty
          in eight columns; the empty card says it once. */}
      {!rows.length ? <Empty icon={GLYPH.chart}>{t('gui.settings.usage.none')}</Empty> : (
      <div style={{ overflowX: 'auto' }}>
        <table className="settings-utab">
          <thead>
            <tr>
              <th>{t('gui.settings.usage.model')}</th><th>{t('gui.settings.usage.calls')}</th>
              <th>{t('gui.settings.usage.input_fresh')}</th><th>{t('gui.settings.usage.input_cached')}</th>
              {anyWrite && <th>{t('gui.settings.usage.cache_write')}</th>}
              <th>{t('gui.settings.usage.output')}</th><th>{t('gui.settings.usage.hit')}</th>
              {priced && <th>{t('gui.settings.usage.cost')}</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr key={m.model}>
                <td>{m.model}<span className="settings-share" style={{ width: `${Math.round((m.calls / most) * 100)}%` }} /></td>
                <td>{m.calls}</td>
                <td>{fmt(m.input_tokens || 0)}</td>
                <td>{fmt(m.cache_read_tokens || 0)}</td>
                {anyWrite && <td>{m.cache_write_tokens ? fmt(m.cache_write_tokens) : '—'}</td>}
                <td>{fmt(m.output_tokens || 0)}</td>
                <td>{Math.round(hit(m.input_tokens, m.cache_read_tokens) * 100)}%</td>
                {priced && <td>{m.cost_usd == null ? <span className="settings-dim">{t('gui.settings.usage.no_price')}</span> : money(m.cost_usd)}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}
    </Card>
  )
}

function ToolsTable({ u }: { u: UsageStats }): JSX.Element {
  return (
    <Card title={t('gui.settings.usage.by_tool')} raw>
      {!u.tools.counts.length ? <Empty icon={GLYPH.chart}>{t('gui.settings.usage.none')}</Empty> : (
      <div style={{ overflowX: 'auto' }}>
        <table className="settings-utab">
          <thead><tr><th>{t('gui.settings.usage.tool')}</th><th>{t('gui.settings.usage.count')}</th></tr></thead>
          <tbody>
            {u.tools.counts.map((c) => <tr key={c.name}><td>{c.name}</td><td>{c.count}</td></tr>)}
          </tbody>
        </table>
      </div>
      )}
    </Card>
  )
}

export function Usage(): JSX.Element {
  const s = store.get()
  useEffect(() => {
    if (s.usage === undefined) void store.usageLoad(s.range)
  }, [s.usage, s.range])
  const pick = (kind: string): void => {
    if (kind === 'custom') { store.set({ range: { ...s.range, kind } }); return }
    void store.usageLoad({ kind, ...lastDays(Number(kind)) })
  }
  const custom = (which: 'from' | 'to', value: string): void => {
    const next = { ...s.range, [which]: value }
    const ok = clampRange(next.from, next.to)
    if (!ok) { toast(t('gui.settings.usage.bad_range', { n: MAX_DAYS })); return }
    void store.usageLoad({ kind: 'custom', ...ok })
  }
  const { from: earliest, to: today } = lastDays(MAX_DAYS)
  const u = s.usage
  const days = u ? u.days : 0
  return (
    <>
      <Card raw>
        <div className="settings-rows">
          <Row label={t('gui.settings.usage.range')}>
            <Seg
              opts={[
                ['1', t('gui.settings.usage.today')], ['7', t('gui.settings.usage.days', { n: 7 })],
                ['30', t('gui.settings.usage.days', { n: 30 })], ['90', t('gui.settings.usage.days', { n: 90 })],
                ['custom', t('gui.settings.usage.custom')],
              ]}
              value={s.range.kind}
              onPick={pick}
            />
          </Row>
        </div>
        {s.range.kind === 'custom' && (
          <div className="settings-tkrow">
            <span className="settings-fl2">{t('gui.settings.usage.from')}</span>
            <input className="settings-tbox" type="date" aria-label={t('gui.settings.usage.from')} defaultValue={s.range.from}
              min={earliest} max={today} onChange={(e) => custom('from', e.currentTarget.value)} />
            <span className="settings-fl2">{t('gui.settings.usage.to')}</span>
            <input className="settings-tbox" type="date" aria-label={t('gui.settings.usage.to')} defaultValue={s.range.to}
              min={earliest} max={today} onChange={(e) => custom('to', e.currentTarget.value)} />
            {u && <span className="settings-rov">{t('gui.settings.usage.n_days', { n: days })}</span>}
          </div>
        )}
      </Card>
      {u === undefined && <UsageWait />}
      {u === null && (
        <Card raw>
          <Empty
            icon={GLYPH.offline}
            title={t('gui.settings.usage.unavailable')}
            /* Cleared rather than reloaded here: the effect above reads whenever
               the answer is missing, so calling the load as well asked twice. */
            action={{ label: t('gui.plug.retry'), onClick: () => store.set({ usage: undefined }) }}
          >
            {t('gui.settings.usage.unavailable_sub')}
          </Empty>
        </Card>
      )}
      {/* A range with nothing in it: the gateway answers zeros rather than
          nothing, and drawn as is that was a row of 0 tiles over two tables
          saying "nothing" each. One card says it once. */}
      {u && !u.llm.total.calls && !u.tools.counts.length && (
        <Card raw>
          <Empty icon={GLYPH.chart} title={t('gui.settings.usage.empty_t')}>{t('gui.settings.usage.empty_sub')}</Empty>
        </Card>
      )}
      {u && (u.llm.total.calls > 0 || u.tools.counts.length > 0) && (
        <>
          <Tiles u={u} />
          {anyPriced(u) && <Bars u={u} />}
          <Models u={u} />
          <ToolsTable u={u} />
        </>
      )}
    </>
  )
}

export { isoDay }
