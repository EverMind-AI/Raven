/* The composer dock: the shell src/page.html used to carry as markup -- the two
 * raven bands, the sheet rack, the card with the field and the bar under it, and
 * the three popovers that hang off that bar.
 *
 * Every element below is a transcription -- tag, id, class, data-*, role, aria,
 * the svg path data and the text exactly as page.html spelled them, attributes
 * in the same order -- and src/test/__golden__/region-app.txt is what says so.
 * The band itself is here too now: src/App.tsx renders div.chat and this is its
 * sixth and last child. It has to
 * stay one stable node for a second reason too: the composer hangs a
 * ResizeObserver and a MutationObserver on it (features/composer/mount.tsx), and
 * a node rebuilt per render would lose both.
 *
 * Words go through t(key) where the markup carries a key;
 * the four that carry none (#permName, #envName, #tierName, #modelName) carry
 * none because each is the property of whoever fills it. Two of the four are
 * their owner's store now (./PermChip.tsx, ./TierChip.tsx, each falling back to
 * the served word until that store's first paint); the other two are rendered
 * as the page serves them and written over by hand.
 *
 * Five of the children are files of their own, because each renders the whole
 * of a store: the context ring (./CtxChip.tsx) and the three chips with the
 * popovers they open (./PermChip.tsx, ./PermPopover.tsx, ./TierChip.tsx,
 * ./TierPopover.tsx, ./WorkdirChip.tsx, ./WorkdirPopover.tsx). Their place in the two child lists below is the page's,
 * which is the one thing about them this file still decides.
 *
 * What this does NOT own, though it renders the elements:
 *   - textarea#ta. It stays uncontrolled and its four listeners stay native
 *     (features/composer/mount.tsx installs them by id, which resolves because
 *     the page's root commits before that runs): a component owning the field
 *     would re-render a text area the reader is typing into, and React's own
 *     onKeyDown could not see an IME composition the way the native handler
 *     does. The three writes to .value are the composer store's for the same
 *     reason (its drafts, its send, its slash commands).
 *   - div#atts, which is not in the markup: it exists only once something is
 *     staged and the composer inserts it before .field. React never re-orders
 *     these children -- none of them is conditional -- so a node put between
 *     two of them stays between them.
 *   - the children of #sheetRack, #queued and #slashList. Each is shared
 *     ground: the sheet rack and the composer's own roots fill them, and
 *     page.css reads `.dock .sheets:has(>*)` off the rack, so it renders with
 *     no children at all rather than a placeholder.
 *   - #go's icon and disabled state, #meter's text, #attBtn's click and
 *     #tplBtn's click and hidden flag (the composer store), #modelName and #modelChip's title
 *     (features/model/chip.ts), #envName's label (state/envChip.ts), and
 *     #slashPop's data-open.
 *   - where #permPop and #tierPop stand. Both stores move the node to the body
 *     the first time it opens, because the card's entrance animation makes the
 *     card a containing block and re-bases the popover's fixed coordinates. A
 *     child moved out from under a portal is safe as long as React never
 *     reconciles that child list, which it does not: see above.
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
import { PermChip } from './PermChip'
import { PermPopover } from './PermPopover'
import { TierChip } from './TierChip'
import { TierPopover } from './TierPopover'
import { WorkdirChip } from './WorkdirChip'
import { WorkdirPopover } from './WorkdirPopover'

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
        {/* A button is neither a flex nor a grid parent here and its one child
            is inline, so the whitespace page.html had around the icon is
            reproduced: it collapses at both line edges either way, but the rule
            that makes it harmless is the line edge, not the parent (.tool-btn,
            src/styles/page.css:2135). */}
        <button className="tool-btn" id="attBtn" data-tip={lang.attr('gui.attach')} aria-label={lang.attr('gui.attach')}>
          {' '}
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
            <path d="M15 7l-6.2 6.2a2.6 2.6 0 0 0 3.7 3.7L19 10a4.4 4.4 0 0 0-6.2-6.2L6 10.5a6.2 6.2 0 0 0 8.8 8.8l3.4-3.4" />
          </svg>
          {' '}
        </button>
        {/* Hidden until the composer says a template can be picked here
            (features/composer/mount.tsx): the demo canvas has no server to
            list them from. */}
        <button className="tool-btn" id="tplBtn" hidden data-tip={lang.attr('gui.tpl.pick')} aria-label={lang.attr('gui.tpl.pick')}>
          {' '}
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
            <rect x="3" y="4.5" width="18" height="12" rx="2" />
            <path d="M8 20.5h8M12 16.5v4M7 9h6M7 12.5h10" />
          </svg>
          {' '}
        </button>
        <PermChip />
        {/* Where the conversation will run. Live on a draft, a report once the
            conversation exists (src/chrome/WorkdirChip.tsx). */}
        <WorkdirChip />
        <span className="chip" id="envChip" hidden><span className="led" /><span id="envName">本机</span></span>
        <span className="meter" id="meter" />
        <CtxChip />
        <TierChip />
        <button className="chip" id="modelChip"><span id="modelName">minimax-m3</span></button>
        <button className="go" id="go" disabled aria-label={lang.attr('gui.send')} />
      </div>

      <div className="pop slash" id="slashPop" data-open="false" role="listbox" aria-label={lang.attr('gui.commands')}>
        <div className="hd"><span className="lab">{t('gui.session_commands')}</span></div>
        <div id="slashList" />
      </div>

      <PermPopover />
      <TierPopover />
      <WorkdirPopover />
    </div>
  )
}

/* The band and its four children, in the order page.html had them. It has to
   stay one node: the composer watches it for every reason its height changes.
   The rack is handed over empty, which is load-bearing: `.dock .sheets:has(> *)`
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
