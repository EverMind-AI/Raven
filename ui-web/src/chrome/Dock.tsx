/* The composer dock: the shell src/page.html used to carry as markup -- the two
 * raven bands, the sheet rack, the card with the field and the bar under it, and
 * the popovers that hang off that bar.
 *
 * The bar is four things: the "+" (a file or a deck template, behind one
 * button), the workspace chip (a draft's, gone once a conversation starts),
 * the permission chip and the model chip, then the send button -- plus the
 * environment chip the page is served hidden, the meter and the context ring.
 * The sub-agent tier is a row of the picker the model chip opens, not a chip
 * of its own. The element order, the ids and the classes are what
 * src/test/__golden__/region-app.txt records. The band has to stay one stable
 * node: the composer hangs a ResizeObserver and a MutationObserver on it
 * (features/composer/mount.tsx), and a node rebuilt per render would lose both.
 *
 * Each of the three small popovers is a child of an anchor wrapper (`.chrome-anch`)
 * around its own chip, and the stylesheet hangs it off that chip's top edge:
 * nothing measures an element, nothing leaves the card, and every row keeps a
 * plain onClick because the tree never leaves the container React delegates
 * from. The model picker is the exception -- it is a body-level layer
 * (features/model/ModelPicker.tsx) and measures the chip itself.
 *
 * Words go through t(key) where the markup carries a key; #permName, #envName
 * and #modelName carry none because each is the property of whoever fills it
 * (./PermChip.tsx, state/envChip.ts, ./ModelChip.tsx).
 *
 * Six of the children are files of their own, because each renders the whole
 * of a store: the "+" and its menu (./PlusMenu.tsx), the workspace chip and
 * its popover (./WorkdirChip.tsx, ./WorkdirPopover.tsx), the permission chip
 * and its popover (./PermChip.tsx, ./PermPopover.tsx), the context ring
 * (./CtxChip.tsx) and the model chip (./ModelChip.tsx). Their place in the bar
 * below is the page's, which is the one thing about them this file still
 * decides.
 *
 * What this does NOT own, though it renders the elements:
 *   - textarea#ta. It stays uncontrolled and its four listeners stay native
 *     (features/composer/mount.tsx installs them by id, which resolves because
 *     the page's root commits before that runs): a component owning the field
 *     would re-render a text area the reader is typing into, and React's own
 *     onKeyDown could not see an IME composition the way the native handler
 *     does. The writes to .value and .placeholder are the composer store's for
 *     the same reason (its drafts, its send, its slash commands, its two-state
 *     placeholder).
 *   - div#atts, which is not in the markup: it exists only once something is
 *     staged and the composer inserts it before .field. React never re-orders
 *     these children -- none of them is conditional -- so a node put between
 *     two of them stays between them.
 *   - the children of #sheetRack, #queued and #slashList. Each is shared
 *     ground: the sheet rack and the composer's own roots fill them, and
 *     page.css reads `.dock .sheets:has(>*)` off the rack, so it renders with
 *     no children at all rather than a placeholder.
 *   - #go's icon and disabled state, #meter's text (the composer store),
 *     #envName's label (state/envChip.ts), and #slashPop's data-open.
 * Each of those is still exactly one writer of the value it writes, and React
 * cannot undo any of them: it renders each as the constant the page was served
 * with, and it diffs against the props it rendered last rather than against the
 * document, so a value it never changes is a value it never writes again.
 */

import { useSyncExternalStore } from 'react'

import { TaskRuns } from '../features/tasks/TasksPage'
import { t } from '../i18n/t'
import * as lang from '../state/lang'
import { CtxChip } from './CtxChip'
import { ModelChip } from './ModelChip'
import { PermChip } from './PermChip'
import { PermPopover } from './PermPopover'
import { Plus } from './PlusMenu'
import { WorkdirChip } from './WorkdirChip'

import type { JSX } from 'react'

/* The card: the queued rows, the writing line and the bar under it. The
   staged-attachment tray goes between the first two at runtime. */
function DockIn(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="dock-in">
      <div className="queued" id="queued" />
      <div className="field">
        <textarea id="ta" rows={1} placeholder={t('gui.composer_ph')} />
      </div>
      {/* One bar under a clean writing line: actions and identity on
           the left, session state and the send button on the right --
           nothing shares the row the reader types in. */}
      <div className="under">
        {/* A file or a deck template, behind one button (src/chrome/PlusMenu.tsx);
            then the folder a draft runs in (src/chrome/WorkdirChip.tsx), which
            a conversation says beside its title instead. */}
        <Plus />
        <WorkdirChip />
        <span className="chrome-anch">
          <PermChip />
          <PermPopover />
        </span>
        <span className="chip" id="envChip" hidden><span className="led" /><span id="envName">本机</span></span>
        <span className="meter" id="meter" />
        <CtxChip />
        {/* The sub-agent tier is a row of the picker this chip opens
            (features/model/ModelPicker.tsx), not a chip of its own. */}
        <ModelChip />
        <button className="go" id="go" disabled aria-label={lang.attr('gui.send')} />
      </div>

      <div className="pop slash" id="slashPop" data-open="false" role="listbox" aria-label={lang.attr('gui.commands')}>
        <div className="hd"><span className="lab">{t('gui.session_commands')}</span></div>
        <div id="slashList" />
      </div>
    </div>
  )
}

/* The band and its children, in the order page.html had them. It has to stay
   one node: the composer watches it for every reason its height changes. The
   rack is handed over empty, which is load-bearing: `.dock .sheets:has(> *)`
   is what gives the stack its frost, so an empty rack has to be an element with
   no children rather than a wrapper around none. */
export function Dock(): JSX.Element {
  return (
    <div className="dock">
      {/* Sheets stack here, above the composer card and apart from it:
           each is its own floating glass card over the transcript, not a
           strip embedded in the box the user types in. */}
      <div className="sheets" id="sheetRack" />
      {/* What is running while you type. Above the card and outside it: not
          part of the reader's draft, but the work already under way -- said as
          one count rather than a list, because the desk's tasks tab is the
          list and this chip is the door to it. A component rather than the
          mount point page.html carried, because the strip renders nothing at
          all while nothing is running. */}
      <TaskRuns />
      <DockIn />
    </div>
  )
}
