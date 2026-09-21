/* The provider catalogue: every vendor the gateway knows, searchable and
   filtered, with the selected one drawn beside it.
 *
 * The whole list rather than the connected ones: "which accounts do I have" and
 * "which could I have" are one question asked twice, and the page used to
 * answer only the first -- fifty-odd vendors Raven supports were reachable
 * from a dropdown inside an "add" form and nowhere else. A column of
 * fifty-five needs a search box and a filter, which is what turns the full
 * list from noise into an index.
 *
 * Connected first because that is the question asked most often; registry
 * order within each half, which is the order the wire sends and the priority
 * the registry declares.
 */
import { ProviderIcon } from '../../../components/ProviderMark'
import { t } from '../../../i18n/t'
import * as store from '../store'

import type { ProvFilter } from '../store'
import type { ProviderRow } from '../types'
import type { JSX } from 'react'

/* The six the prototype offers. `direct` is the remainder rather than a fact of
   its own: a vendor you hold an account with directly is one that is not a
   reseller, not an OAuth sign-in and not something you run yourself. */
const FILTERS: Array<[ProvFilter, string]> = [
  ['all', 'gui.model.prov_filter.all'],
  ['on', 'gui.settings.providers.connected'],
  ['direct', 'gui.settings.providers.filter_direct'],
  ['gateway', 'gui.settings.providers.filter_gateway'],
  ['oauth', 'gui.settings.providers.filter_oauth'],
  ['local', 'gui.model.kind.local'],
]

export function provMatch(p: ProviderRow, q: string, f: ProvFilter): boolean {
  const needle = q.trim().toLowerCase()
  if (needle && !`${p.name} ${p.id}`.toLowerCase().includes(needle)) return false
  if (f === 'all') return true
  if (f === 'on') return p.on
  if (f === 'gateway') return !!p.gateway
  if (f === 'oauth') return p.kind === 'oauth'
  if (f === 'local') return p.kind === 'local'
  return !p.gateway && (p.kind === 'key' || p.kind === 'endpoint')
}

export const provRows = (rows: ProviderRow[], q: string, f: ProvFilter): ProviderRow[] => {
  const kept = rows.filter((p) => provMatch(p, q, f))
  return [...kept.filter((p) => p.on), ...kept.filter((p) => !p.on)]
}

export function ProviderSide(): JSX.Element {
  const s = store.get()
  const rows = provRows(s.snap.providers, s.provQ, s.provFilt)
  return (
    <div className="settings-tp-side">
      <div className="settings-tp-find">
        <input
          value={s.provQ}
          placeholder={t('gui.model.prov_search_ph')}
          aria-label={t('gui.model.prov_search_ph')}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => store.set({ provQ: e.currentTarget.value })}
        />
        {/* A select, not a menu of our own: it is a list of exclusive choices,
            which is what the element is for, and it arrives keyboard-reachable
            and screen-reader-labelled without a line of our code. */}
        <select
          value={s.provFilt}
          aria-label={t('gui.model.prov_filter_tip')}
          onChange={(e) => store.set({ provFilt: e.currentTarget.value as ProvFilter })}
        >
          {FILTERS.map(([id, key]) => <option key={id} value={id}>{t(key)}</option>)}
        </select>
      </div>
      {rows.length ? (
        <div className="settings-tp-list">
          {rows.map((p) => (
            <div key={p.id} className="settings-tp-row" data-id={p.id} aria-current={s.provider === p.id}>
              <button type="button" className="settings-tp-hit" onClick={() => store.set({ provider: p.id, err: '' })}>
                <ProviderIcon id={p.id} name={p.name} />
                <span className="settings-tp-nm">{p.name}</span>
                {s.snap.curProvider === p.id && <span className="settings-tp-def">{t('gui.model.is_default')}</span>}
                {p.on && <span className="settings-tp-dot" />}
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="settings-tp-empty">{t('gui.model.prov_no_match')}</div>
      )}
    </div>
  )
}
