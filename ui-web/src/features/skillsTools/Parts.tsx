/* Tactile Bento Studio UI primitives for the Skills & Tools page: switches,
 * colored soft-tint icon tiles, semantic dots and pills, search box, empty
 * state, copy action, and Bento shimmer skeletons.
 */
import {
  Copy01Icon,
  LinkSquare02Icon,
  Search01Icon,
  Tick01Icon,
} from '@hugeicons/core-free-icons'
import { useEffect, useState } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { copy } from '../../lib/clipboard'
import { show as toast } from '../../state/toast'

import type { TileTone } from './helpers'
import type { SkillsToolsTab } from './types'
import type { JSX, ReactNode } from 'react'

export function Switch({ on, onChange, label, disabled, stop }: {
  on: boolean
  onChange(v: boolean): void
  label: string
  disabled?: boolean
  stop?: boolean
}): JSX.Element {
  return (
    <button
      type="button"
      className="skillsTools-swi"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={(e) => {
        if (stop) e.stopPropagation()
        onChange(!on)
      }}
    />
  )
}

export function FixedSwitch({ on, label }: { on: boolean; label: string }): JSX.Element {
  return (
    <span
      className="skillsTools-swi skillsTools-swi-fixed"
      role="switch"
      aria-checked={on}
      aria-disabled="true"
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation()
        toast(t('gui.skillsTools.tools.builtin_locked'))
      }}
    />
  )
}

export function IconTile({ tone, size = 'md', children }: {
  tone: TileTone
  size?: 'sm' | 'md' | 'lg'
  children: ReactNode
}): JSX.Element {
  return (
    <span className="skillsTools-itile" data-tone={tone} data-size={size} aria-hidden="true">
      {children}
    </span>
  )
}

export function StatusDot({ tone }: { tone: 'ok' | 'bad' | 'warn' | 'off' }): JSX.Element {
  return <span className="skillsTools-led" data-tone={tone} aria-hidden="true" />
}

export function Pill({ tone = 'neutral', children }: {
  tone?: 'neutral' | 'hub' | 'ok' | 'bad' | 'warn' | 'mono'
  children: ReactNode
}): JSX.Element {
  return <span className="skillsTools-pill" data-tone={tone}>{children}</span>
}

export function SearchBox({ id, value, placeholder, onChange, compact }: {
  id: string
  value: string
  placeholder: string
  onChange(v: string): void
  compact?: boolean
}): JSX.Element {
  const cls = compact ? 'skillsTools-srch skillsTools-srch-compact' : 'skillsTools-srch'
  return (
    <div className={cls}>
      <Icon icon={Search01Icon} size={14} />
      <input
        id={id}
        value={value}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => onChange(e.currentTarget.value)}
      />
    </div>
  )
}

export function EmptyBox({ icon, title, action, children }: {
  icon: ReactNode
  title?: string
  action?: { label: string; onClick(): void }
  children?: ReactNode
}): JSX.Element {
  return (
    <div className="skillsTools-empty">
      <span className="skillsTools-empty-ico" aria-hidden="true">{icon}</span>
      {title ? <div className="skillsTools-empty-t">{title}</div> : null}
      {children ? <div className="skillsTools-empty-s">{children}</div> : null}
      {action ? (
        <button type="button" className="skillsTools-btn skillsTools-btn-primary skillsTools-empty-act" onClick={action.onClick}>
          {action.label}
        </button>
      ) : null}
    </div>
  )
}

export function KeyLink({ url }: { url?: string | null }): JSX.Element | null {
  if (!url) return null
  const label = t('gui.skillsTools.get_key')
  return (
    <a className="skillsTools-keylink" href={url} target="_blank" rel="noopener" title={label} aria-label={label}>
      <span>{label}</span>
      <Icon icon={LinkSquare02Icon} size={12} />
    </a>
  )
}

/* The name on a card, as the control that opens the card.
 *
 * The card itself is not a button: a role="button" flattens its descendants,
 * and the switch beside the name is a control of its own -- a screen reader
 * would meet a plain label where the switch should be. The name carries the
 * click and the keyboard, and the card keeps the pointer affordance. */
export function CardName({ name, mono = false, onOpen }: {
  name: string
  mono?: boolean
  onOpen(): void
}): JSX.Element {
  return (
    <button
      type="button"
      className={mono ? 'skillsTools-cname mono' : 'skillsTools-cname'}
      onClick={(e) => {
        e.stopPropagation()
        onOpen()
      }}
    >
      {name}
    </button>
  )
}

export function CopyCmd({ command }: { command: string }): JSX.Element {
  const [done, setDone] = useState(false)
  /* The tick that un-ticks the button: cleared when the card goes away, so a
     copy in a modal the reader has already closed does not set state on a
     component that is no longer there. */
  const [timer, setTimer] = useState<number | null>(null)
  useEffect(() => () => {
    if (timer !== null) window.clearTimeout(timer)
  }, [timer])
  const trigger = (e: React.MouseEvent): void => {
    e.stopPropagation()
    copy(command, t('gui.skillsTools.copied'))
    setDone(true)
    if (timer !== null) window.clearTimeout(timer)
    setTimer(window.setTimeout(() => setDone(false), 1600))
  }
  return (
    <div className="skillsTools-cmdbox" onClick={(e) => e.stopPropagation()}>
      <code className="skillsTools-cmdtxt">{command}</code>
      <button
        type="button"
        className="skillsTools-cmdcopy"
        aria-label={t('gui.skillsTools.copy_cmd')}
        title={t('gui.skillsTools.copy_cmd')}
        onClick={trigger}
      >
        <Icon icon={done ? Tick01Icon : Copy01Icon} size={13} />
      </button>
    </div>
  )
}

export function InlineErr({ text }: { text: string }): JSX.Element | null {
  return text ? <div className="skillsTools-err" role="alert">{text}</div> : null
}

function WBar({ w, h = 11 }: { w?: string; h?: number }): JSX.Element {
  return <span className="skillsTools-wbar" style={{ width: w, height: `${h}px` }} />
}

export function WaitView({ tab }: { tab: SkillsToolsTab }): JSX.Element {
  if (tab === 'tools') {
    return (
      <div className="skillsTools-wait" role="status" aria-busy="true" aria-label={t('gui.skillsTools.loading')}>
        <div className="skillsTools-cfg-grid">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="skillsTools-cfg-card skillsTools-wcard">
              <div className="skillsTools-ctop">
                <WBar w="32px" h={32} />
                <WBar w="48%" h={13} />
              </div>
              <WBar w="82%" h={11} />
              <WBar w="56%" h={11} />
            </div>
          ))}
        </div>
        <div className="skillsTools-matrix-card">
          <div className="skillsTools-matrix-head"><WBar w="220px" h={14} /></div>
          <div className="skillsTools-matrix">
            {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
              <div key={i} className="skillsTools-mcell">
                <WBar w="55%" h={12} />
                <WBar w="34px" h={19} />
              </div>
            ))}
          </div>
        </div>
      </div>
    )
  }
  if (tab === 'plugins') {
    return (
      <div className="skillsTools-wait" role="status" aria-busy="true" aria-label={t('gui.skillsTools.loading')}>
        <div className="skillsTools-pkg-grid">
          {[0, 1].map((i) => (
            <div key={i} className="skillsTools-tray-card skillsTools-wcard">
              <div className="skillsTools-tray-body">
                <div className="skillsTools-ctop">
                  <WBar w="36px" h={36} />
                  <WBar w="44%" h={14} />
                </div>
                <WBar w="78%" h={11} />
                <WBar w="60%" h={11} />
              </div>
              <div className="skillsTools-tray-foot"><WBar w="38%" h={11} /></div>
            </div>
          ))}
        </div>
      </div>
    )
  }
  return (
    <div className="skillsTools-wait" role="status" aria-busy="true" aria-label={t('gui.skillsTools.loading')}>
      {tab === 'skills' ? (
        <div className="skillsTools-bento-hero">
          <div className="skillsTools-hub-bento skillsTools-wcard">
            <WBar w="42px" h={42} />
            <div className="skillsTools-wcol">
              <WBar w="52%" h={14} />
              <WBar w="84%" h={11} />
            </div>
          </div>
          <div className="skillsTools-summary-bento skillsTools-wcard">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skillsTools-stat-cell">
                <WBar w="48%" h={18} />
                <WBar w="66%" h={10} />
              </div>
            ))}
          </div>
        </div>
      ) : null}
      <div className="skillsTools-grid">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="skillsTools-tray-card skillsTools-wcard">
            <div className="skillsTools-tray-body">
              <div className="skillsTools-ctop">
                <WBar w="34px" h={34} />
                <WBar w="46%" h={13} />
              </div>
              <WBar w="86%" h={11} />
              <WBar w="64%" h={11} />
            </div>
            <div className="skillsTools-tray-foot"><WBar w="40%" h={11} /></div>
          </div>
        ))}
      </div>
    </div>
  )
}
