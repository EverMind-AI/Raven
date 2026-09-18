/* Model: the connected providers and the model roles, or one provider's
   detail when a row is opened. */
import { ProviderDetail } from '../providers/ProviderDetail'
import { Providers } from '../providers/Providers'
import { Roles } from '../providers/Roles'
import * as store from '../store'

import type { JSX } from 'react'

export function Model(): JSX.Element {
  const s = store.get()
  return (
    <>{s.provider ? <ProviderDetail slug={s.provider} /> : <><Providers /><Roles /></>}</>
  )
}
