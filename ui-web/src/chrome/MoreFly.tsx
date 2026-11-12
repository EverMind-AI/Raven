/* The More group of the rail: the row that opens it, and the popover it opens.
 *
 * The strip keeps its length. What is behind the group is not a place the
 * reader goes to every day -- it is an action, once in a while -- so it waits
 * behind one row instead of taking a row of the column of its own.
 *
 * The popover opens beside the column rather than under the row: the group is
 * not part of the list the strip is, and a second list under one row reads as
 * a fold rather than as a group with a door.
 *
 * The row inside the popover is a .navi row, the same shape as the strip's own:
 * same glyph slot, same label, same lift. One row shape for the whole column.
 *
 * Importing a conversation is not a page the reader goes to, so it is an
 * action rather than a destination, and the group marks nothing: there is no
 * page behind it to light.
 *
 * While a package is on its way the row names the file, and the group stands
 * open for as long as it does -- that name is the only place the reader can
 * see it, and the group cannot be folded away from under it.
 */

import { FileImportIcon, Layers01Icon } from '@hugeicons/core-free-icons'
import { useLayoutEffect, useRef, useSyncExternalStore } from 'react'

import { Icon } from '../components/Icon'
import * as imported from '../features/imported/store'
import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as navfly from '../state/navfly'

import type { JSX } from 'react'

export function MoreFly(): JSX.Element {
  const s = useSyncExternalStore(navfly.subscribe, navfly.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const { busy } = useSyncExternalStore(imported.subscribe, imported.get)
  /* A package on its way keeps the group open whatever the reader did with
     the row before it: the file's name is in here, and it is the only place
     the reader can see it. */
  const open = s.open || busy !== null
  const row = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const wasOpen = useRef(false)
  useLayoutEffect(() => {
    if (open) {
      wasOpen.current = true
      return
    }
    if (!wasOpen.current) return
    wasOpen.current = false
    /* The panel is hidden by the stylesheet rather than unmounted, so a row
       focused inside it would drop focus to <body>; the row that opened the
       panel takes it back before the browser does. */
    const active = document.activeElement
    if (active instanceof Node && panel.current?.contains(active)) row.current?.focus()
  }, [open])
  const toggle = (): void => {
    if (busy !== null) return
    navfly.toggle()
  }
  return (
    <span className="chrome-navfly-anch">
      <button
        className="navi chrome-more"
        id="navMoreBtn"
        ref={row}
        aria-expanded={open}
        aria-haspopup="true"
        aria-controls="chrome-navfly"
        onClick={toggle}
      >
        <Icon icon={Layers01Icon} />
        <span>{t('gui.nav.more')}</span>
      </button>
      <div
        className="chrome-navfly"
        id="chrome-navfly"
        ref={panel}
        data-open={open ? 'true' : 'false'}
        role="dialog"
        aria-label={t('gui.nav.more')}
      >
        <button
          className="navi"
          id="importedBtn"
          disabled={busy !== null}
          aria-busy={busy !== null ? true : undefined}
          onClick={() => imported.pick()}
        >
          <Icon icon={FileImportIcon} />
          <span>{busy !== null ? t('gui.nav.importing', { name: busy }) : t('gui.nav.import')}</span>
        </button>
      </div>
    </span>
  )
}
