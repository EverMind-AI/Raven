/* Root island for the Skills & Tools module page (#skillsToolsBody).
 *
 * Uses Raven's standard PageBar header with Scheme C's segmented icon tab bar
 * on the right, renders the active capability tab (Skills, Tools, MCP,
 * Plugins), and portals centered configuration/inspection cards into `#detail`.
 */
import {
  AiContentGenerator01Icon,
  Plug02Icon,
  PuzzleIcon,
  Wrench01Icon,
} from '@hugeicons/core-free-icons'
import { useRef, useSyncExternalStore } from 'react'

import { Icon } from '../../components/Icon'
import { PageBar } from '../../components/PageBar'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { DetailModal } from './DetailModal'
import { tabAttention } from './helpers'
import { McpTab } from './McpTab'
import { WaitView } from './Parts'
import { PluginsTab } from './PluginsTab'
import { SkillsTab } from './SkillsTab'
import * as store from './store'
import { TABS } from './store'
import { ToolsTab } from './ToolsTab'
import './styles.css'

import type { SkillsToolsTab } from './types'
import type { JSX, KeyboardEvent } from 'react'

const TAB_LABEL: Record<SkillsToolsTab, string> = {
  skills: 'gui.skillsTools.tabs.skills',
  tools: 'gui.skillsTools.tabs.tools',
  mcp: 'gui.skillsTools.tabs.mcp',
  plugins: 'gui.skillsTools.tabs.plugins',
}

const TAB_ICON: Record<SkillsToolsTab, JSX.Element> = {
  skills: <Icon icon={AiContentGenerator01Icon} size={14} />,
  tools: <Icon icon={Wrench01Icon} size={14} />,
  mcp: <Icon icon={Plug02Icon} size={14} />,
  plugins: <Icon icon={PuzzleIcon} size={14} />,
}

function SegmentedTabs({ active, attention }: {
  active: SkillsToolsTab
  attention: { tools: boolean; mcp: boolean; plugins: boolean }
}): JSX.Element {
  const refs = useRef<Partial<Record<SkillsToolsTab, HTMLButtonElement | null>>>({})

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>, idx: number): void => {
    let next = -1
    if (e.key === 'ArrowRight') next = (idx + 1) % TABS.length
    else if (e.key === 'ArrowLeft') next = (idx - 1 + TABS.length) % TABS.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = TABS.length - 1
    if (next < 0) return
    e.preventDefault()
    const target = TABS[next]!
    store.setTab(target)
    refs.current[target]?.focus()
  }

  return (
    <div className="skillsTools-segtabs" role="tablist" aria-label={t('gui.skillsTools.tabs.aria')}>
      {TABS.map((id, idx) => {
        const selected = id === active
        const dot = (id === 'tools' && attention.tools)
          || (id === 'mcp' && attention.mcp)
          || (id === 'plugins' && attention.plugins)
        return (
          <button
            key={id}
            ref={(el) => { refs.current[id] = el }}
            type="button"
            role="tab"
            id={`skillsTools-tab-${id}`}
            aria-selected={selected}
            aria-controls={`skillsTools-panel-${id}`}
            tabIndex={selected ? 0 : -1}
            className="skillsTools-segtab"
            onClick={() => store.setTab(id)}
            onKeyDown={(e) => onKeyDown(e, idx)}
          >
            {TAB_ICON[id]}
            <span>{t(TAB_LABEL[id])}</span>
            {dot ? <span className="skillsTools-tab-dot" aria-hidden="true" /> : null}
          </button>
        )
      })}
    </div>
  )
}

export function SkillsToolsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const attention = tabAttention(s.snap, s.pkgs)

  const panel = !s.loaded
    ? <WaitView tab={s.tab} />
    : s.tab === 'skills'
      ? <SkillsTab />
      : s.tab === 'tools'
        ? <ToolsTab />
        : s.tab === 'mcp'
          ? <McpTab />
          : <PluginsTab />

  return (
    <div className="skillsTools-root" data-tab={s.tab}>
      <PageBar
        title={t('gui.skillsTools.title')}
        end={<SegmentedTabs active={s.tab} attention={attention} />}
      />
      <div
        className="skillsTools-panel"
        role="tabpanel"
        id={`skillsTools-panel-${s.tab}`}
        aria-labelledby={`skillsTools-tab-${s.tab}`}
      >
        {panel}
      </div>
      <DetailModal />
    </div>
  )
}
