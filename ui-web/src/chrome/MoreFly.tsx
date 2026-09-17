/* The More group's rows: #moreFly and everything inside it.
 *
 * The group is the gap above the fold's own button in the rail's nav strip
 * (src/chrome/Rail.tsx renders this beside the six module rows). Its three rows
 * come from the table in src/state/navfly.ts and are drawn on the way open --
 * `rows` is empty until then, which is how the page is served -- and they are
 * .navi rows, the same shape as the modules above them, so unfolding the group
 * makes one list longer rather than opening a second, indented one under it.
 *
 * Names only. These three rows are places the reader already knows by name; a
 * sentence under each turned a three-item group into a panel.
 *
 * Words through t(), like every other line of chrome: these rows carry no
 * served literal to fall back to, because nothing draws them until a reader
 * unfolds the group.
 *
 * Two flags on these elements are not this component's, and React leaves both
 * alone because it never renders them: `data-open` on the group, which the
 * stylesheet unfolds it off, and `aria-current` on each row, which is decided
 * together with the marks on the nav buttons above (state/navfly.ts).
 */

import { useSyncExternalStore } from 'react'

import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as navfly from '../state/navfly'

import type { JSX } from 'react'

/* The glyphs, in the shape and the attribute order page.css and the sibling nav
   rows have them: one 24x24 box, stroked by the rule rather than by the tag. */
const GLYPH: Record<string, JSX.Element> = {
  xaPage: (
    <>
      <rect x="3.5" y="4" width="7" height="7" rx="1.6" /><rect x="13.5" y="13" width="7" height="7" rx="1.6" /><path d="M10.5 7.5h3.5a3 3 0 0 1 3 3v2.5" />
    </>
  ),
  connPage: <path d="M9.5 14.5 6.8 17.2a3.3 3.3 0 0 1-4.7-4.7l2.7-2.7M14.5 9.5l2.7-2.7a3.3 3.3 0 0 1 4.7 4.7l-2.7 2.7M9 15l6-6" />,
  cronPage: (
    <>
      <circle cx="12" cy="12.5" r="7.5" /><path d="M12 8.5v4.2l2.6 1.6M9 2.5h6" />
    </>
  ),
}

export function MoreFly(): JSX.Element {
  const s = useSyncExternalStore(navfly.subscribe, navfly.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="moresub" id="moreFly" data-open="false" role="group" data-i18n-aria="gui.nav.more" aria-label={lang.attr('gui.nav.more')}>
      {s.rows.map((row) => (
        <button key={row.page} className="navi" onClick={() => navfly.pick(row)}>
          <svg viewBox="0 0 24 24" aria-hidden="true">{GLYPH[row.page]}</svg>
          <div className="nm">{t(row.nameKey)}</div>
        </button>
      ))}
    </div>
  )
}
