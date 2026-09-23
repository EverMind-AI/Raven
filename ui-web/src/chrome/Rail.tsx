/* The rail: the column src/page.html used to carry as markup.
 *
 * One file per region of the page, under src/chrome/ -- beside src/features/
 * (islands, each with its own root and its own data), src/state/ and src/chrome/behaviour/ (the modules that own
 * listeners and measurements rather than markup).
 * What is here is the page's own furniture: elements the document was served with, rendered
 * by the page's root now.
 *
 * Every element below is a transcription -- tag, id, class, data-*, role, aria,
 * the svg path data and the text exactly as page.html spelled them, attributes
 * in the same order -- and src/test/__golden__/region-app.txt is what says so.
 * The column itself is here too now: src/App.tsx renders this region inside the
 * one portal it makes at the body.
 *
 * Words go through t(key), which reads the language the page resolved before
 * its first frame, and keyed attributes through lang.attr(key), which stays
 * absent until a pick lands. That pair is what applyI18n's passes over the
 * document used to do, read from the other end: the key each line speaks is the
 * argument beside its words, and nothing walks the document for it any more
 * (state/lang/store.ts).
 *
 * What this does NOT own, though it renders the elements:
 *   - #newBtn's click. Its action belongs to the session rather than to the
 *     chrome that carries it, which is what src/app/install.ts's
 *     installActions() is for; it binds this button by id there, and the dead
 *     second handler the demo layer had is gone.
 *   - the aria-current marks on the nav buttons (features/rail/store.ts).
 *   - the update row's text and its hidden flag (src/app/updates.ts), and
 *     #upnote's click.
 *   - #list's children (the rail island's own root) and #railGrip's drag
 *     (src/chrome/behaviour/panes.ts).
 * Each of those is still exactly one writer of the value it writes, and React
 * cannot undo any of them: it renders each as the constant the page was served
 * with, and it diffs against the props it rendered last rather than against the
 * document, so a value it never changes is a value it never writes again.
 */

import { useSyncExternalStore } from 'react'

import { RavenMark } from '../components/RavenMark'
import { open as openExtAgents } from '../features/extAgents/store'
import { open as openSettings } from '../features/settings/store'
import { t } from '../i18n/t'
import * as find from '../state/find'
import * as lang from '../state/lang'
import * as rail from '../state/rail'

import type { NavButton } from '../state/pages'
import type { JSX } from 'react'

/* The mark and the name, then the two icon chores. Neither button carries a
   literal -- their words are a tooltip and a label the language pass writes
   onto attributes -- so this subscribes to the search row alone. */
function RailTop(): JSX.Element {
  const s = useSyncExternalStore(find.subscribe, find.get)
  return (
    <div className="railtop">
      {/* The name is the product's, not a translated string: it reads the same
          in every language the page has. */}
      <span className="wordmark"><RavenMark />RAVEN</span>
      {/* aria-expanded is what paints it as engaged: the toggle never moves, so
          its own state is the only thing that says whether the rail is open.
          Nothing has ever written it, so the served value stands. */}
      <button
        className="ghost-ic tipdn"
        id="railBtn"
        aria-expanded="true"
        data-tip={lang.attr('gui.collapse_rail')}
        aria-label={lang.attr('gui.collapse_rail')}
        onClick={() => rail.set(false)}
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" /><path d="M9.5 4.5v15" />
        </svg>
      </button>
      <button
        className="ghost-ic tipdn"
        id="findBtn"
        aria-expanded={s.open}
        data-tip={lang.attr('gui.search_sessions')}
        aria-label={lang.attr('gui.search_sessions')}
        onClick={() => find.toggle()}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
          <circle cx="11" cy="11" r="7" /><path d="M20 20l-4.3-4.3" />
        </svg>
      </button>
    </div>
  )
}

/* The rows that open a module page, in the order the strip renders them.
   `button` is typed against the page table (state/pages.ts), so a row can only
   light a button some page declares -- and the mark the rail writes is read off
   that same table (features/rail/store.ts's markNew), which is the pair a page
   used to be able to miss in silence.

   One destination, not five. Schedules, channels and memory are set up once
   and then left alone, so they are sections of the settings dialog now, which
   the foot opens; a playbook is not a place a reader goes at all. What is left
   on the strip is the one module you go TO. */
const NAV_ROWS: ReadonlyArray<{
  readonly button: NavButton
  readonly key: string
  readonly open: () => void
  readonly icon: JSX.Element
}> = [
  {
    button: 'agentsBtn',
    key: 'gui.nav.agents',
    open: () => openExtAgents(),
    icon: (
      <>
        <rect x="3.5" y="4" width="7" height="7" rx="1.6" /><rect x="13.5" y="13" width="7" height="7" rx="1.6" /><path d="M10.5 7.5h3.5a3 3 0 0 1 3 3v2.5" />
      </>
    ),
  },
]

/* The nav strip: the draft row and the module rows. */
function RailNav(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <nav className="rail-nav">
      <button className="navi newrun" id="newBtn">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" aria-hidden="true">
          <path d="M12 5v14M5 12h14" />
        </svg>
        <span>{t('gui.new_task')}</span>
      </button>
      {NAV_ROWS.map((row) => (
        <button className="navi" id={row.button} key={row.button} onClick={row.open}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
            {row.icon}
          </svg>
          <span>{t(row.key)}</span>
        </button>
      ))}
    </nav>
  )
}

/* The search row. The field is deliberately uncontrolled and its listeners are
   native (src/state/find.ts); what is state here is the row's own showing and
   the clear button's, which are the same term read two ways.

   The whitespace page.html had between these three is reproduced: .find is
   neither a flex nor a grid parent and its middle child is an inline-block
   (src/styles/page.css), so the rule that makes the gaps harmless is the line
   edge rather than the parent -- a leading or trailing space on a line is not
   painted, and the two absolutely positioned siblings leave nothing else on it. */
function FindRow(): JSX.Element {
  const s = useSyncExternalStore(find.subscribe, find.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="find" id="findBox" hidden={!s.open}>
      {' '}
      <svg className="ic" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="11" cy="11" r="7" /><path d="M20 20l-4.3-4.3" />
      </svg>
      {' '}
      <input
        id="sfind"
        placeholder={t('gui.search_sessions')}
        aria-label={lang.attr('gui.search_sessions')}
      />
      {' '}
      <button className="clr" id="sclr" hidden={!s.query} aria-label={lang.attr('gui.clear_search')} onClick={() => find.clear()}>&#10005;</button>
      {' '}
    </div>
  )
}

/* The foot row. The update notice above it is written by the module that knows
   that fact (app/updates.ts), so this renders it as the page serves it: empty
   and hidden. */
function RailFoot(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="rail-foot">
      <button className="upnote" id="upnote" hidden>
        <span className="pip" />
        <span className="t">{t('gui.update.note')}</span>
        <span className="rl">{t('gui.update.reload')}</span>
      </button>
      {/* The cold-start import the wizard's last step starts, followed here
           because it outlives the wizard: hours for a full one, and the reader
           has to be able to find it, stop it, and start it again after a
           gateway restart. The slot is the foot's; the row in it is the
           importSync domain's, mounted by src/main.tsx from its manifest. */}
      <div id="importRow" />
      {/* The foot is the door to settings, and only that: accounts are not a
           thing this product has, so nothing down here pretends to be one. The
           build number and the shortcut that used to sit under the label went
           with state/foot.ts -- a version string is what an About dialog is
           for, not the one row that has to stay legible at a glance. */}
      <button className="me" id="meBtn" aria-label={lang.attr('gui.nav.set')} onClick={() => void openSettings()}>
        <span className="av anon">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.2" />
            <path d="M12 3v2.2M12 18.8V21M4.6 7.8l1.9 1.1M17.5 15.1l1.9 1.1M4.6 16.2l1.9-1.1M17.5 8.9l1.9-1.1M3 12h2.2M18.8 12H21" /></svg>
        </span>
        <span className="who">
          <span className="n">{t('gui.nav.set')}</span>
        </span>
      </button>
    </div>
  )
}

/* The column and its six children, in the order page.html had them. #list is
   handed over empty: the rail island roots itself in it (src/main.tsx), so
   React must not own that child list. */
export function Rail(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <aside className="rail">
      <RailTop />
      <RailNav />
      <FindRow />
      <div className="list" id="list" />
      <RailFoot />
      <div
        className="grip"
        id="railGrip"
        role="separator"
        aria-orientation="vertical"
        title={lang.attr('gui.resize_rail')}
        aria-label={lang.attr('gui.resize_rail')}
      />
    </aside>
  )
}
