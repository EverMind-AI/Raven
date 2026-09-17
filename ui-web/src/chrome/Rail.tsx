/* The rail: the column src/page.html used to carry as markup.
 *
 * One file per region of the page, under src/chrome/ -- beside src/features/
 * (islands, each with its own root and its own data) and src/shell/ (behaviour
 * modules that own listeners and measurements rather than markup). What is here
 * is the page's own furniture: elements the document was served with, rendered
 * by the page's root now.
 *
 * Every element below is a transcription -- tag, id, class, data-*, role, aria,
 * the svg path data and the text exactly as page.html spelled them, attributes
 * in the same order -- and src/test/__golden__/region-app.txt is what says so.
 * The column itself is here too now: src/App.tsx renders this region inside the
 * one portal it makes at the body.
 *
 * Words go through lang.text(key, literal) and keyed attributes through
 * lang.attr(key): the literal the page was served with until a language is
 * applied, the catalogue's text afterwards. That pair is what applyI18n's
 * passes over the document used to do, read from the other end -- the data-i18n*
 * keys stay on the elements as the record of which phrase each line speaks, and
 * nothing walks them any more (state/lang.ts).
 *
 * What this does NOT own, though it renders the elements:
 *   - #newBtn's click. Its action belongs to the session rather than to the
 *     chrome that carries it, which is what src/state/install.ts's
 *     installActions() is for; it binds this button by id there, and the dead
 *     second handler the demo layer had is gone.
 *   - #moreFly's rows and the two flags the fold writes (#moreFly[data-open],
 *     #moreBtn[aria-expanded]). The group is shared ground the way #list is:
 *     src/shell/navfly.ts builds the rows from a table and re-decides their
 *     aria-current on demand, and the rail island's markNew() drives that.
 *   - the aria-current marks on the six nav buttons (features/rail/store.ts).
 *   - #meSub and #meKbd's text (src/shell/foot.ts), the update row's text and
 *     its hidden flag (src/state/updates.ts), and #upnote's click.
 *   - #list's children (the rail island's own root) and #railGrip's drag
 *     (src/shell/panes.ts).
 * Each of those is still exactly one writer of the value it writes, and React
 * cannot undo any of them: it renders each as the constant the page was served
 * with, and it diffs against the props it rendered last rather than against the
 * document, so a value it never changes is a value it never writes again.
 */

import { useSyncExternalStore } from 'react'

import { openPlugins, openSkills } from '../legacy/demo/120-capabilities.js'
import { openKb, openMem } from '../legacy/demo/140-schedule.js'
import { openSettings } from '../legacy/demo/150-chrome.js'
import { openPb } from '../legacy/demo/154-playbooks.js'
import * as find from '../shell/find'
import * as lang from '../state/lang'
import * as rail from '../state/rail'

import type { JSX } from 'react'

/* The two icon buttons over the list. Neither carries a literal -- their words
   are a tooltip and a label the language pass writes onto attributes -- so this
   subscribes to the search row alone. */
function RailTop(): JSX.Element {
  const s = useSyncExternalStore(find.subscribe, find.get)
  return (
    <div className="railtop">
      {/* aria-expanded is what paints it as engaged: the toggle never moves, so
          its own state is the only thing that says whether the rail is open.
          Nothing has ever written it, so the served value stands. */}
      <button
        className="ghost-ic tipdn"
        id="railBtn"
        aria-expanded="true"
        data-i18n-tip="gui.collapse_rail"
        data-i18n-aria="gui.collapse_rail"
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
        data-i18n-tip="gui.search_sessions"
        data-i18n-aria="gui.search_sessions"
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

/* The nav strip: the draft row, the four module rows, and the fold that holds
   the three set-up-once ones. */
function RailNav(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <nav className="rail-nav">
      <button className="navi newrun" id="newBtn">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" aria-hidden="true">
          <path d="M12 5v14M5 12h14" />
        </svg>
        <span data-i18n="gui.new_task">{lang.text('gui.new_task', '新任务')}</span>
      </button>
      <button className="navi" id="skillBtn" onClick={() => openSkills()}>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M12 4l1.9 5.3L19 11l-5.1 1.7L12 18l-1.9-5.3L5 11l5.1-1.7Z" />
        </svg>
        <span data-i18n="gui.tab.skills">{lang.text('gui.tab.skills', '技能')}</span>
      </button>
      <button className="navi" id="plugBtn" onClick={() => openPlugins()}>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M9 3.5v4.5M15 3.5v4.5M7 8h10v4.5a5 5 0 0 1-10 0zM12 17.5v3" />
        </svg>
        <span data-i18n="gui.tab.plugins">{lang.text('gui.tab.plugins', '插件')}</span>
      </button>
      <button className="navi" id="pbBtn" onClick={() => openPb()}>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <circle cx="5.5" cy="7" r="2" /><circle cx="5.5" cy="17" r="2" /><circle cx="18.5" cy="12" r="2" />
          <path d="M7.5 7.6c5 1.4 6.5 2.6 9 3.9M7.5 16.4c5-1.4 6.5-2.6 9-3.9" />
        </svg>
        <span data-i18n="gui.nav.pb">{lang.text('gui.nav.pb', '剧本')}</span>
      </button>
      <button className="navi" id="kbBtn" onClick={() => openKb()}>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M5 4.5h9.5a2 2 0 0 1 2 2v13H7a2 2 0 0 1-2-2zM16.5 6.5H19v13h-2.5M8 8.5h5M8 12h5" />
        </svg>
        <span data-i18n="gui.nav.kb">{lang.text('gui.nav.kb', '知识库')}</span>
      </button>
      <button className="navi" id="memBtn" onClick={() => openMem()}>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M12 3l8 4.5-8 4.5-8-4.5zM4 12.4l8 4.5 8-4.5M4 16.6l8 4.5 8-4.5" />
        </svg>
        <span data-i18n="gui.nav.mem">{lang.text('gui.nav.mem', '记忆')}</span>
      </button>
      {/* Sub-agents / entrances / schedules live one level in: they are
           set-up-once surfaces,
           not daily destinations, and seven top-level rows buried the four
           that are. The group drawn by navfly's draw() is their home.

           The rows land above this button, not below it, because the button is
           the fold: it reads "More" while they are hidden and "Less" once they
           stand in the list, which only works if it sits at the list's end. */}
      <div className="moresub" id="moreFly" data-open="false" role="group" data-i18n-aria="gui.nav.more" aria-label={lang.attr('gui.nav.more')} />
      <button className="navi more" id="moreBtn" aria-expanded="false" aria-controls="moreFly">
        <svg className="chev" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M7 9.8l5 5.4 5-5.4" />
        </svg>
        <span className="l-more" data-i18n="gui.nav.more">{lang.text('gui.nav.more', '更多')}</span>
        <span className="l-less" data-i18n="gui.nav.less">{lang.text('gui.nav.less', '收起')}</span>
      </button>
    </nav>
  )
}

/* The search row. The field is deliberately uncontrolled and its listeners are
   native (src/shell/find.ts); what is state here is the row's own showing and
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
        data-i18n-ph="gui.search_sessions"
        data-i18n-aria="gui.search_sessions"
        placeholder={lang.text('gui.search_sessions', '搜索会话')}
        aria-label={lang.attr('gui.search_sessions')}
      />
      {' '}
      <button className="clr" id="sclr" hidden={!s.query} data-i18n-aria="gui.clear_search" aria-label={lang.attr('gui.clear_search')} onClick={() => find.clear()}>&#10005;</button>
      {' '}
    </div>
  )
}

/* The foot row. The update notice above it and the two slots inside the door --
   the running build and this platform's shortcut for it -- are written by the
   modules that know those facts (state/updates.ts, shell/foot.ts), so this
   renders them as the page serves them: empty. */
function RailFoot(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="rail-foot">
      <button className="upnote" id="upnote" hidden>
        <span className="pip" />
        <span className="t" data-i18n="gui.update.note">{lang.text('gui.update.note', '界面有更新')}</span>
        <span className="rl" data-i18n="gui.update.reload">{lang.text('gui.update.reload', '刷新')}</span>
      </button>
      {/* The foot is the door to settings, and only that: accounts are not a
           thing this product has, so nothing down here pretends to be one.
           The version under the label is drawn by drawFoot(). */}
      <button className="me" id="meBtn" data-i18n-aria="gui.nav.set" aria-label={lang.attr('gui.nav.set')} onClick={() => openSettings()}>
        <span className="av anon">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.2" />
            <path d="M12 3v2.2M12 18.8V21M4.6 7.8l1.9 1.1M17.5 15.1l1.9 1.1M4.6 16.2l1.9-1.1M17.5 8.9l1.9-1.1M3 12h2.2M18.8 12H21" /></svg>
        </span>
        <span className="who">
          <span className="n" data-i18n="gui.nav.set">{lang.text('gui.nav.set', '设置')}</span>
          <span className="s" id="meSub" />
        </span>
        <span className="kbd" id="meKbd" />
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
        data-i18n-title="gui.resize_rail"
        data-i18n-aria="gui.resize_rail"
        title={lang.attr('gui.resize_rail')}
        aria-label={lang.attr('gui.resize_rail')}
      />
    </aside>
  )
}
