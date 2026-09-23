/* The model chip in the bar under the field: the provider's mark, the model's
 * display name and a chevron saying a list opens here.
 *
 * Rendered from two stores rather than written by id, which is what the chip
 * was before: the model store says which model this conversation runs on
 * (features/model/store.ts), and features/model/chip.ts's `paint` is bumped by
 * every path that changes what the chip should say without changing the model
 * -- a provider refresh, a language pick -- so the label reads the provider
 * list as it stands. The full id stays on the title, because the row is 12px
 * type with an icon in it and the reader who needs the exact string hovers.
 *
 * The display name is the registry's label where the provider carries one,
 * and the id without its vendor half otherwise -- the same rule the picker's
 * rows follow, so the chip and the row it was picked from say the same thing.
 *
 * The click is the chip's: the picker against the provider list the page
 * really has, and a build with no provider configured is sent to Models first,
 * which is what that guard answers.
 */

import { useSyncExternalStore } from 'react'

import { ProviderIcon } from '../components/ProviderMark'
import { paint } from '../features/model/chip'
import { openModelsForMissingProvider } from '../features/model/source'
import * as model from '../features/model/store'
import { sameModel } from '../features/model/types'

import type { Provider } from '../features/model/types'
import type { JSX } from 'react'

const CHEVRON = 'M6 9l6 6 6-6'

/* The provider serving the current model and the id as that provider spells it,
   or null while no list is installed (the served frame, and a test that never
   installs one).

   By the backend's identity rather than by string, the way the column is built
   (features/model/store.ts): a list mixes ids added by hand with ids the vendor
   reports, so the conversation's spelling and the account's need not match. The
   account's spelling comes back with it because `labels` is keyed by that one --
   looking the label up under the conversation's spelling misses and falls
   through to the raw id. */
function serving(current: string): { provider: Provider; id: string } | null {
  try {
    const rows = model.source().providers()
    const named = model.currentProvider()
    /* The account a pick or the gateway named first, because two accounts can
       list one id and only that one answers which the conversation is on. The
       scan behind it is for a page that has been told neither. */
    const order = named ? [...rows.filter((p) => p.id === named), ...rows.filter((p) => p.id !== named)] : rows
    for (const p of order) {
      const id = model.column(p).find((m) => sameModel(p, m, current))
      if (id !== undefined) return { provider: p, id }
    }
    return null
  } catch {
    return null
  }
}

export function ModelChip(): JSX.Element {
  useSyncExternalStore(model.subscribe, model.version)
  useSyncExternalStore(paint.subscribe, paint.get)
  const current = model.current()
  const at = serving(current)
  const label = at?.provider.labels?.[at.id]?.label || model.short(current)
  return (
    <button
      className="chip model"
      id="modelChip"
      title={current}
      aria-haspopup="true"
      onClick={() => {
        if (openModelsForMissingProvider()) return
        model.open(null)
      }}
    >
      {at ? <ProviderIcon id={at.provider.id} name={at.provider.name} /> : null}
      <span id="modelName">{label}</span>
      <svg className="chrome-model-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d={CHEVRON} />
      </svg>
    </button>
  )
}
