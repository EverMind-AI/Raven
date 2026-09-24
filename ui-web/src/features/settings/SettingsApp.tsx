/* The settings dialog's root: the section nav, portalled into the shell's
   #snavList, and the open section's page in #spanels. The shell (App.tsx)
   owns the veil, the modal and the two columns; this island renders into them
   and writes the section's name to #setTitle on every draw. */
import {
  AiContentGenerator01Icon, BrainIcon, InformationCircleIcon, Message02Icon, Plug02Icon, Settings03Icon,
  Settings05Icon, TimeQuarter02Icon, Wrench01Icon,
} from '@hugeicons/core-free-icons'
import { useEffect, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { ArchiveGlyph, CubeGlyph, Icon, UsageGlyph } from '../../components/Icon'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { InlineErr } from './Fields'
import { About } from './pages/About'
import { Archive } from './pages/Archive'
import { General } from './pages/General'
import { Model } from './pages/Model'
import { Plugins } from './pages/Plugins'
import { Provider } from './pages/Provider'
import { Skills } from './pages/Skills'
import { Tools } from './pages/Tools'
import { Usage } from './pages/Usage'
import { AddModelLayer } from './providers/AddModelPop'
import { SectionWait } from './Skeletons'
import * as store from './store'
import { HOSTED, SECTIONS } from './store'
import './styles.css'

import type { SectionId } from './store'
import type { JSX } from 'react'

/* One glyph per section, the prototype's. */
/* The design's set (Figma: Raven / Setting): HugeIcons where it has the
   glyph, the design's own drawing where it does not. */
const ICON: Record<SectionId, JSX.Element> = {
  general: <Icon icon={Settings03Icon} />,
  usage: <UsageGlyph />,
  provider: <CubeGlyph />,
  model: <Icon icon={Settings05Icon} />,
  skills: <Icon icon={AiContentGenerator01Icon} />,
  tools: <Icon icon={Wrench01Icon} />,
  plugins: <Icon icon={Plug02Icon} />,
  channels: <Icon icon={Message02Icon} />,
  cron: <Icon icon={TimeQuarter02Icon} />,
  memory: <Icon icon={BrainIcon} />,
  archive: <ArchiveGlyph />,
  about: <Icon icon={InformationCircleIcon} />,
}

/* Literal keys, so the i18n gate can read each one. */
const NAV: Record<SectionId, string> = {
  general: 'gui.settings.nav.general',
  usage: 'gui.settings.nav.usage',
  provider: 'gui.settings.nav.provider',
  model: 'gui.settings.nav.model',
  skills: 'gui.settings.nav.skills',
  tools: 'gui.settings.nav.tools',
  plugins: 'gui.settings.nav.plugins',
  channels: 'gui.settings.nav.channels',
  cron: 'gui.settings.nav.cron',
  memory: 'gui.settings.nav.memory',
  archive: 'gui.settings.nav.archive',
  about: 'gui.settings.nav.about',
}

export const navLabel = (id: SectionId): string => t(NAV[id])

function Nav({ tab }: { tab: SectionId }): JSX.Element {
  return (
    <>
      {SECTIONS.map((id) => (
        <button key={id} type="button" className="settings-nitem" aria-current={id === tab} onClick={() => store.setTab(id)}>
          {ICON[id]}
          <span>{navLabel(id)}</span>
        </button>
      ))}
    </>
  )
}

/* One component per section this island draws itself. The three another
   domain fills are absent on purpose: HOSTED names the box beside this root
   that each of them is rooted in, and the panel below draws nothing for
   them. */
const PAGE: Partial<Record<SectionId, () => JSX.Element>> = {
  general: General, usage: Usage, provider: Provider, model: Model, skills: Skills,
  tools: Tools, plugins: Plugins, archive: Archive, about: About,
}

export function SettingsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const title = navLabel(s.tab)
  /* The heading is the shell's element; the island writes the section's name
     there rather than rendering a heading of its own -- and the flag the
     stylesheet picks a hosted pane off is the same write (src/App.tsx lists
     it among what it renders and does not own). */
  useEffect(() => {
    const el = document.getElementById('setTitle')
    if (el) el.textContent = title
    const sub = document.getElementById('setSub')
    if (sub) sub.textContent = ''
    const veil = document.getElementById('setVeil')
    if (veil) veil.dataset.section = s.tab
  }, [title, s.tab])
  const navHost = document.getElementById('snavList')
  const Page = PAGE[s.tab]
  return (
    <>
      {navHost && createPortal(<Nav tab={s.tab} />, navHost)}
      {/* Nothing for a hosted section: the box beside this one is its pane. */}
      {HOSTED[s.tab] || !Page ? null : (
        <div className="settings-panel" data-section={s.tab} key={s.epoch}>
          {s.loaded ? <Page /> : <SectionWait id={s.tab} />}
          {/* An open add form says it beside its own button instead, and the
              provider section says it inside its own pane -- this foot is a
              screen below that pane's controls (ProviderDetail's PaneErr). */}
          <InlineErr text={s.provAdd === null && s.tab !== 'provider' ? s.err : ''} />
        </div>
      )}
      {/* Beside the panel, not in it: the epoch key above replaces that box on
          every write, and this popover has to outlive one. */}
      <AddModelLayer />
    </>
  )
}
