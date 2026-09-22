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

import type { Provider } from '../features/model/types'
import type { JSX } from 'react'

const CHEVRON = 'M6 9l6 6 6-6'

/* The provider serving the current model, or null while no list is installed
   (the served frame, and a test that never installs one). */
function serving(current: string): Provider | null {
  try {
    return model.source().providers().find((p) => model.column(p).includes(current)) ?? null
  } catch {
    return null
  }
}

export function ModelChip(): JSX.Element {
  useSyncExternalStore(model.subscribe, model.version)
  useSyncExternalStore(paint.subscribe, paint.get)
  const current = model.current()
  const provider = serving(current)
  const label = provider?.labels?.[current]?.label || model.short(current)
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
      {provider ? <ProviderIcon id={provider.id} name={provider.name} /> : null}
      <span id="modelName">{label}</span>
      <svg className="chrome-model-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d={CHEVRON} />
      </svg>
    </button>
  )
}
