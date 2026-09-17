/* The workspace pane: the column src/page.html used to carry as markup beside
 * the chat.
 *
 * One file per region of the page, under src/chrome/ -- beside src/features/
 * (islands, each with its own root and its own data), src/state/ and src/chrome/behaviour/ (the modules that own
 * listeners and measurements rather than markup).
 *
 * Every element below is a transcription -- tag, id, class, data-*, role, aria,
 * the svg path data and the text exactly as page.html spelled them, attributes
 * in the same order -- and src/test/__golden__/region-app.txt is what says so.
 * The column itself is here too now: src/App.tsx renders it as one of the two
 * children of #split.
 *
 * Literals go through lang.text(key, literal): the served markup carries
 * data-i18n* keys and state/lang.ts applies a language by walking the document
 * and rewriting them, so a component rendering one of those keyed literals has
 * two writers and has to read the same catalogue to agree with the other one.
 *
 * What this does NOT own, though it renders the elements:
 *   - #wsBody's children. It is shared ground for three islands: the workspace
 *     view roots itself in it, and the browser and sub-agent views are handed
 *     the cleared box (features/workspace/store.ts's draw), so React must not
 *     own that child list.
 *   - the tab strip's aria-selected and #wsWide's four attributes, which the
 *     pane's state writes at the moment it decides them (src/state/ws.ts).
 *   - #wsUnseen's count, written with the header badge's by the same bump, and
 *     #wsAgentRun's dot, which the sub-agents island raises
 *     (features/subagents/store.ts).
 * Each of those is still exactly one writer of the value it writes, and React
 * cannot undo any of them: it renders each as the constant the page was served
 * with, and it diffs against the props it rendered last rather than against the
 * document, so a value it never changes is a value it never writes again.
 *
 * Two controls the pane drives are not its own markup, so it binds them by id:
 * the toggle in the chat header (#wsBtn, a region of its own) and the keyboard
 * shortcuts, which are on aside.ws itself.
 */

import { useEffect, useSyncExternalStore } from 'react'
import { composing } from '../features/composer/store'
import { islands } from '../islands'
import * as lang from '../state/lang'
import * as ws from '../state/ws'

import type { JSX, MouseEvent } from 'react'

/* 1-4 pick a view while the pane has focus, and the toggle in the chat header
   opens the floating desk. Both elements are outside this component's markup --
   the pane is this component's own element and #wsBtn belongs to the chat
   header, and neither listener is a prop -- so both are bound by id, once,
   rather than on every render. */
function useOutsideControls(): void {
  useEffect(() => {
    const pane = document.getElementById('ws')
    const keys: Record<string, string> = { 1: 'diff', 2: 'file', 3: 'browser', 4: 'agents' }
    const keydown = (e: KeyboardEvent): void => {
      if (e.metaKey || e.ctrlKey || e.altKey || composing(e)) return
      const pick = keys[e.key]
      if (!pick) return
      if (/^(INPUT|TEXTAREA)$/.test((e.target as HTMLElement).tagName)) return
      e.preventDefault()
      ws.pick(pick)
    }
    pane?.addEventListener('keydown', keydown)
    const toggle = document.getElementById('wsBtn')
    if (toggle) toggle.onclick = () => islands.workspace.toggleDesk()
    return () => {
      pane?.removeEventListener('keydown', keydown)
      if (toggle) toggle.onclick = null
    }
  }, [])
}

/* The tab strip. One delegated click, as it was: the row is three buttons and
   the handler reads which one off its data-w, so adding a view is markup. */
function WsTabs(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  const click = (e: MouseEvent): void => {
    const b = (e.target as HTMLElement).closest('button')
    if (!b) return
    ws.pick((b as HTMLElement).dataset.w!)
  }
  return (
    /* Segmented icons, not a dropdown: a collapsed menu hides exactly the
       thing you need in your peripheral vision -- that a command is still
       running, or that three more files changed. */
    <div className="wseg" id="wsTabs" role="tablist" onClick={click}>
      <button role="tab" data-w="diff" aria-selected="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <rect x="4" y="4" width="16" height="16" rx="3" /><path d="M12 8.5v7M8.5 12h7" />
        </svg>
        <span className="lb" data-i18n="gui.ws.changes">{lang.text('gui.ws.changes', 'Diff')}</span>
        <span className="bdg" id="wsUnseen" hidden />
      </button>
      <button role="tab" data-w="browser" aria-selected="false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="8" /><path d="M4.5 12h15M12 4.5c-4.5 4.5-4.5 10.5 0 15M12 4.5c4.5 4.5 4.5 10.5 0 15" />
        </svg>
        <span className="lb" data-i18n="gui.ws.browser">{lang.text('gui.ws.browser', '浏览器')}</span>
      </button>
      <button role="tab" data-w="agents" aria-selected="false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <rect x="4" y="8" width="16" height="11" rx="3" /><path d="M12 4.5V8M8.5 13h.01M15.5 13h.01" />
        </svg>
        <span className="lb" data-i18n="gui.ws.agents">{lang.text('gui.ws.agents', '子智能体')}</span>
        <span className="rundot" id="wsAgentRun" hidden />
      </button>
    </div>
  )
}

/* The two buttons in the pane's own corner. Neither carries a literal -- their
   words are a tooltip and a label the language pass writes onto attributes --
   so neither subscribes to anything. */
function WsActs(): JSX.Element {
  return (
    <div className="ws-acts">
      {/* Two glyphs, one button: arrows out to take the window, arrows in
          to give it back. The state is on the button, so which one shows
          is CSS rather than a redraw. */}
      <button
        className="ghost-ic wsfull tipdn"
        id="wsWide"
        aria-pressed="false"
        data-i18n-tip="gui.ws.expand_panel"
        data-i18n-aria="gui.ws.expand_panel"
        data-tip={lang.attr('gui.ws.expand_panel')}
        aria-label={lang.attr('gui.ws.expand_panel')}
        onClick={() => ws.setFull(!ws.wide)}
      >
        <svg className="ex" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M14 5h5v5M10 19H5v-5M19 5l-6 6M5 19l6-6" />
        </svg>
        <svg className="in" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M19 10h-5V5M5 14h5v5M13 11l6-6M11 13l-6 6" />
        </svg>
      </button>
      {/* Same glyph and same corner as the toggle in the chat header, and
          only one of the two is ever on screen: the panel toggle is one
          control at one place that changes state, not a pair that trade
          positions when the panel opens.

          aria-expanded is what paints it as engaged, and nothing has ever
          written it: the served value stands, the way #railBtn's does. */}
      <button
        className="ghost-ic wstog tipdn"
        id="wsClose"
        aria-expanded="true"
        data-i18n-tip="gui.collapse_ws"
        data-i18n-aria="gui.collapse_ws"
        data-tip={lang.attr('gui.collapse_ws')}
        aria-label={lang.attr('gui.collapse_ws')}
        onClick={() => ws.setOpen(false)}
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" /><path d="M14.5 4.5v15" />
        </svg>
      </button>
    </div>
  )
}

/* The column and its two children, in the order page.html had them. #wsBody is
   handed over empty, for the reason the header gives. */
export function WsPane(): JSX.Element {
  useOutsideControls()
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <aside className="ws" id="ws" data-i18n-aria="gui.workspace" aria-label={lang.attr('gui.workspace')}>
      <div className="ws-top">
        <WsTabs />
        <WsActs />
      </div>
      <div className="ws-body" id="wsBody" />
    </aside>
  )
}
