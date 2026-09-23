/* The "+" at the left of the composer bar, and the menu it opens.
 *
 * Both render the whole of state/plus.ts: whether the button is drawn, which
 * of the two rows the menu offers -- upload a file, pick a deck template --
 * and whether it is up. The same two rows in a draft and in a conversation:
 * a file or a template can go with any message, so nothing here asks which
 * state the page is in.
 *
 * The menu is a child of the anchor wrapper around the button (`.chrome-anch`), and
 * stays one: the stylesheet hangs it off the button's top edge, so nothing
 * here measures an element, and every row carries a plain onClick because the
 * tree never leaves the container React delegates from. The verbs the rows
 * run are the composer island's (features/composer/mount.tsx), which owns the
 * file input and the template sheet.
 */

import { PlusSignIcon } from '@hugeicons/core-free-icons'
import { useSyncExternalStore } from 'react'

import { Icon } from '../components/Icon'
import * as composer from '../features/composer/mount'
import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as plus from '../state/plus'

import type { JSX } from 'react'

const CLIP = 'M15 7l-6.2 6.2a2.6 2.6 0 0 0 3.7 3.7L19 10a4.4 4.4 0 0 0-6.2-6.2L6 10.5a6.2 6.2 0 0 0 8.8 8.8l3.4-3.4'

function Glyph({ d, className }: { d: string; className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className={className}>
      <path d={d} />
    </svg>
  )
}

export function PlusBtn(): JSX.Element {
  const s = useSyncExternalStore(plus.subscribe, plus.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const word = lang.attr('gui.plus.add')
  return (
    <button
      className="plus"
      id="plusBtn"
      aria-expanded={s.open ? 'true' : 'false'}
      aria-haspopup="true"
      data-tip={word}
      aria-label={word}
      onClick={() => plus.toggle()}
    >
      <Icon icon={PlusSignIcon} size={18} stroke={1.33} />
    </button>
  )
}

export function PlusPopover(): JSX.Element {
  const s = useSyncExternalStore(plus.subscribe, plus.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div
      className="pop"
      id="plusPop"
      data-open={s.open ? 'true' : 'false'}
      role="dialog"
      aria-label={lang.attr('gui.plus.add')}
    >
      {/* Only while it stands: a closed popover has no rows, so nothing under
          it can take a click, and the served tree carries the shell alone. */}
      {s.open ? (
        <>
          {s.upload ? (
            <button className="prow chrome-plus-row" onClick={() => { plus.close(); composer.pickFiles() }}>
              <Glyph d={CLIP} className="pico" />
              <span className="nm">{t('gui.plus.upload')}</span>
            </button>
          ) : null}
          {s.template ? (
            <button className="prow chrome-plus-row" onClick={() => { plus.close(); composer.pickTemplate() }}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className="pico">
                <rect x="3" y="4.5" width="18" height="12" rx="2" /><path d="M8 20.5h8M12 16.5v4M7 9h6M7 12.5h10" />
              </svg>
              <span className="nm">{t('gui.plus.template')}</span>
            </button>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

/* The button and its menu in one anchor, hidden together: a wrapper left
   standing for a hidden button would still take the bar's gap. */
export function Plus(): JSX.Element {
  const s = useSyncExternalStore(plus.subscribe, plus.get)
  return (
    <span className="chrome-anch" hidden={!s.shown}>
      <PlusBtn />
      <PlusPopover />
    </span>
  )
}
