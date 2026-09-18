/* The settings dialog's root: the section nav, portalled into the shell's
   #snavList, and the open section's page in #spanels. The shell (App.tsx)
   owns the veil, the modal and the two columns; this island renders into them
   and writes the section's name to #setTitle on every draw. */
import { useEffect, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { InlineErr } from './Fields'
import { About } from './pages/About'
import { Archive } from './pages/Archive'
import { General } from './pages/General'
import { Model } from './pages/Model'
import { Plugins } from './pages/Plugins'
import { Skills } from './pages/Skills'
import { Tools } from './pages/Tools'
import { Usage } from './pages/Usage'
import * as store from './store'
import { SECTIONS } from './store'
import './styles.css'

import type { SectionId } from './store'
import type { JSX } from 'react'

/* One glyph per section, the prototype's. */
const ICON: Record<SectionId, JSX.Element> = {
  general: <><circle cx="12" cy="12" r="8" /><path d="M12 4a8 8 0 0 0 0 16Z" fill="currentColor" stroke="none" /></>,
  usage: <><path d="M4 19h16" /><path d="M7 19v-6M12 19V6M17 19v-9" /></>,
  model: <><path d="M12 3.5 20 8v8l-8 4.5L4 16V8l8-4.5Z" /><path d="M12 12v8.5M12 12 4 8M12 12l8-4" /></>,
  skills: <><path d="M6 4.5h9.5L20 9v10.5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1Z" /><path d="M15 4.5V9h5M8.5 13h7M8.5 16.5h5" /></>,
  tools: <><path d="M14.5 4.5a4.2 4.2 0 0 0 5.5 5.6L14 16.2l-4-4 4.5-7.7Z" /><path d="m9 13-4.5 4.5a1.8 1.8 0 0 0 2.5 2.5L11.5 16" /></>,
  plugins: <><path d="M9 3.5v4M15 3.5v4" /><path d="M6.5 7.5h11v3.5a5.5 5.5 0 0 1-11 0Z" /><path d="M12 16.5v4" /></>,
  archive: <path d="M5 8h14v11H5zM4 4h16v4H4zm5 8h6" />,
  about: <><circle cx="12" cy="12" r="8" /><path d="M12 11v5M12 8h.01" /></>,
}

/* Literal keys, so the i18n gate can read each one. */
const NAV: Record<SectionId, string> = {
  general: 'gui.settings.nav.general',
  usage: 'gui.settings.nav.usage',
  model: 'gui.settings.nav.model',
  skills: 'gui.settings.nav.skills',
  tools: 'gui.settings.nav.tools',
  plugins: 'gui.settings.nav.plugins',
  archive: 'gui.settings.nav.archive',
  about: 'gui.settings.nav.about',
}

export const navLabel = (id: SectionId): string => t(NAV[id])

function Nav({ tab }: { tab: SectionId }): JSX.Element {
  return (
    <>
      {SECTIONS.map((id) => (
        <button key={id} type="button" className="settings-nitem" aria-current={id === tab} onClick={() => store.setTab(id)}>
          <svg viewBox="0 0 24 24" aria-hidden="true">{ICON[id]}</svg>
          <span>{navLabel(id)}</span>
        </button>
      ))}
    </>
  )
}

const PAGE: Record<SectionId, () => JSX.Element> = {
  general: General, usage: Usage, model: Model, skills: Skills,
  tools: Tools, plugins: Plugins, archive: Archive, about: About,
}

export function SettingsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const title = navLabel(s.tab)
  /* The heading is the shell's element; the island writes the section's name
     there rather than rendering a heading of its own. */
  useEffect(() => {
    const el = document.getElementById('setTitle')
    if (el) el.textContent = title
    const sub = document.getElementById('setSub')
    if (sub) sub.textContent = ''
  }, [title])
  const navHost = document.getElementById('snavList')
  const Page = PAGE[s.tab]
  return (
    <>
      {navHost && createPortal(<Nav tab={s.tab} />, navHost)}
      <div className="settings-panel" data-section={s.tab} key={s.epoch}>
        {s.loaded ? <Page /> : <div className="settings-soonbox"><div className="settings-t">{t('gui.settings.loading')}</div></div>}
        <InlineErr text={s.err} />
      </div>
    </>
  )
}
