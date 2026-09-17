/* The page's own React root: the dialog shells src/page.html used to carry as
 * markup.
 *
 * Every element below is a transcription -- tag, id, class, data-*, role, aria
 * and text exactly as page.html spelled them, attributes in the same order --
 * and the goldens under src/test/__golden__/ are what says so.
 *
 * The containers stay in page.html and the interiors portal into them, which is
 * the mechanism for every region stage C converts: while a container is static
 * markup, the order React commits into it is the order these portals are
 * written, so an interior cannot end up appended after something the page put
 * there first. React clears a container it is given as a ROOT (measured: a root
 * at document.body deletes #splash, #noJs and every static region on its first
 * commit), which is why the root in main.tsx is a detached element and
 * everything here is a portal.
 *
 * The data-i18n* attributes stay on the markup rather than becoming t() calls.
 * state/lang.ts applies a language by walking the document and rewriting them,
 * and none of these components subscribes to the lang store, so a flip is never
 * undone by a re-render putting a served literal back over text it has already
 * moved. A re-render is harmless anyway -- React diffs against the props it
 * rendered last, not against the document -- which is what lets the drawer
 * below subscribe to its own store while its rewritten attributes stay put.
 *
 * Not here, and not later: #splash and #noJs. Both are pre-JavaScript shells --
 * the splash is the literal first frame, painted while this bundle is still
 * being evaluated, and #noJs is what a reader gets when it never runs -- so
 * neither can be something React puts on screen. They stay in page.html and are
 * taken down at boot (state/splash.ts, legacy/demo/010-kernel.js).
 */
import { useEffect, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import * as detail from './state/detail'

import type { JSX } from 'react'

/* The confirm dialog. The asker fills #cfTitle / #cfBody and the label on
   #cfYes, and the chrome binds both buttons (legacy/demo/040-state.js). */
function ConfirmSheet(): JSX.Element {
  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="cfTitle">
      <header id="cfTitle">确认</header>
      <div className="body" id="cfBody" />
      <footer>
        <button className="btn" id="cfNo" data-i18n="gui.cancel">取消</button>
        <button className="btn bad" id="cfYes">确认</button>
      </footer>
    </div>
  )
}

/* The shared detail drawer. #dBody renders empty on purpose: state/detail.ts
   keeps one host per owner under it and each island renders its card into its
   own, so React must not own that child list.

   The title is the store's. It is the served em-dash until a card claims the
   header, and every card blanks it -- an empty <b> is what turns the header row
   into a floating close control (`.detail header:has(b:empty)`, page.css:3592),
   so the emptiness is load-bearing and no card ever puts the dash back. */
function DetailPanel(): JSX.Element {
  const s = useSyncExternalStore(detail.subscribe, detail.get)
  /* The scrim is the container, which src/page.html still renders, so this
     listener cannot be an onClick. Same handler the close button has: the
     drawer's own panel is a child, so a click inside it is not the scrim. */
  useEffect(() => {
    const el = document.getElementById('detail')
    if (!el) return
    const away = (e: Event): void => {
      if (e.target === el) detail.close()
    }
    el.addEventListener('click', away)
    return () => el.removeEventListener('click', away)
  }, [])
  return (
    <div className="dpanel">
      <header>
        <b id="dTitle">{s.title ?? '—'}</b>
        <button className="dx" id="dClose" data-i18n-aria="gui.close" aria-label="关闭" onClick={() => detail.close()}>
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </header>
      <div className="body" id="dBody" />
    </div>
  )
}

/* The settings dialog's frame. #snavList and #spanels render empty for the same
   reason as #dBody: the settings island portals its nav into the first and
   roots its panels in the second. */
function SettingsModal(): JSX.Element {
  return (
    <div className="smodal" id="setModal" role="dialog" aria-modal="true" data-i18n-aria="gui.page.set">
      <nav className="snav" id="snav">
        {/* A block row with one inline child, so the whitespace page.html had
            around it is reproduced: it collapses at both line edges either way,
            but the rule that makes it harmless is the line edge, not a flex or
            grid parent (.snav .brandrow, src/styles/page.css:3667). */}
        <div className="brandrow">
          {' '}
          <span className="wm" data-i18n="gui.page.set">设置</span>{' '}
        </div>
        <div className="snavlist" id="snavList" />
      </nav>
      <div className="sbody">
        <header className="shd">
          <div className="ttl">
            <h3 id="setTitle">设置</h3>
            <p className="sub" id="setSub" />
          </div>
          <button className="icb" id="setClose" data-i18n-tip="gui.close" data-i18n-aria="gui.close">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
          </button>
        </header>
        <div className="spanels" id="spanels" />
      </div>
    </div>
  )
}

/* Each interior into the container page.html still provides. Guarded the way
   the island mounts are: a document without the container renders nothing
   rather than throwing. */
export function App(): JSX.Element {
  const veil = document.getElementById('veil')
  const detail = document.getElementById('detail')
  const setVeil = document.getElementById('setVeil')
  return (
    <>
      {detail ? createPortal(<DetailPanel />, detail) : null}
      {setVeil ? createPortal(<SettingsModal />, setVeil) : null}
      {veil ? createPortal(<ConfirmSheet />, veil) : null}
    </>
  )
}
