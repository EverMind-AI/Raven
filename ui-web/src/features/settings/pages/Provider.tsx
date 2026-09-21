/* Model providers: which accounts this install holds, and how each connects.
   Two columns, because the answer to "which do I have" is a list to scan and
   the answer to "how is this one set up" is a form -- paging between them was
   the shape this replaces, and twenty rows in it is already too many. */
import { t } from '../../../i18n/t'
import { ProviderDetail } from '../providers/ProviderDetail'
import { ProviderSide } from '../providers/ProviderSide'
import * as store from '../store'

import type { JSX } from 'react'

export function Provider(): JSX.Element {
  const s = store.get()
  /* Opens on the provider serving the chat model, which is the row a reader
     coming here to change something is most often after. */
  const slug = s.provider ?? s.snap.curProvider
  const shown = s.snap.providers.some((p) => p.id === slug) ? slug : null
  return (
    <div className="settings-tp">
      <ProviderSide />
      {/* Keyed by the provider: the pane holds a key field and an address field
          in local state, seeded from the provider it mounted on. Re-rendering
          the same instance for a different vendor kept the first one's values,
          which on a page where the list is one click away means the address of
          the provider you just left, sitting in the box of the one you opened.
          A page that reached the pane by navigation never had this. */}
      {shown
        ? <ProviderDetail key={shown} slug={shown} />
        : <div className="settings-tp-none">{t('gui.settings.providers.pick_one')}</div>}
    </div>
  )
}
