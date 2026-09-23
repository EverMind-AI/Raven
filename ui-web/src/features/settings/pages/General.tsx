/* General: language, theme, notifications. Language goes through the source
   (it is config the agent reads too); theme and notifications are the browser's
   own preferences and go to the modules that already hold them. */
import { code as langCode, t } from '../../../i18n/t'
import * as notifications from '../../../lib/notifications'
import * as look from '../../../state/look'
import { show as toast } from '../../../state/toast'
import { Seg, Switch } from '../Fields'
import * as store from '../store'

import type { JSX, ReactNode } from 'react'

type Theme = 'system' | 'light' | 'dark'

const THEMES: Theme[] = ['system', 'light', 'dark']

const SHOT = { light: 'settings-shot', dark: 'settings-shot settings-shot-dark' }

/* One setting: its name and a line about it on the left, the control on the
   right, or -- for the theme cards -- under both. No card around the list: the
   page is three settings, and the whitespace between them is the division. */
function Setting({ title, sub, below, children }: {
  title: string
  sub: string
  below?: boolean
  children: ReactNode
}): JSX.Element {
  return (
    <section className={'settings-gen' + (below ? ' settings-gen-below' : '')}>
      <div className="settings-gen-k">
        <div className="settings-gen-t">{title}</div>
        <div className="settings-gen-d">{sub}</div>
      </div>
      <div className="settings-gen-ctl">{children}</div>
    </section>
  )
}

/* A miniature of the window in that theme: a sidebar strip, a title bar and a
   block. System is the two halves side by side, light then dark. */
function Preview({ theme }: { theme: Theme }): JSX.Element {
  const win = (tone: 'light' | 'dark'): JSX.Element => (
    <span className={SHOT[tone]} aria-hidden="true">
      <span className="settings-shot-side"><i /></span>
      <span className="settings-shot-main"><i /><b /></span>
    </span>
  )
  if (theme !== 'system') return <span className="settings-shotbox">{win(theme)}</span>
  return (
    <span className="settings-shotbox">
      {win('light')}
      <span className="settings-shot-half">{win('dark')}</span>
    </span>
  )
}

export function General(): JSX.Element {
  const on = notifications.enabled()
  const canNtf = 'Notification' in window
  const theme = look.get().theme as Theme
  const flip = async (v: boolean): Promise<void> => {
    if (v && canNtf && Notification.permission !== 'granted') {
      const r = await Notification.requestPermission()
      if (r !== 'granted') {
        notifications.setEnabled(false)
        store.redraw()
        toast(t('gui.settings.general.notify_denied'))
        return
      }
    }
    notifications.setEnabled(v && canNtf)
    store.redraw()
    if (v && !canNtf) toast(t('gui.settings.general.notify_denied'))
  }
  return (
    <div className="settings-genlist">
      <Setting title={t('gui.settings.general.language')} sub={t('gui.settings.general.language_sub')}>
        <Seg
          opts={[['zh', t('gui.settings.general.lang_zh')], ['en', t('gui.settings.general.lang_en')]]}
          value={langCode}
          onPick={(v) => store.source().setLang(v)}
        />
      </Setting>
      <Setting title={t('gui.settings.general.theme')} sub={t('gui.settings.general.theme_sub')} below>
        <div className="settings-themes" role="radiogroup" aria-label={t('gui.settings.general.theme')}>
          {THEMES.map((v) => (
            <button
              key={v}
              type="button"
              role="radio"
              aria-checked={v === theme}
              className="settings-theme"
              onClick={() => { look.set({ theme: v }); store.redraw() }}
            >
              <Preview theme={v} />
              <span className="settings-theme-foot">
                <span>{t('gui.settings.general.theme_' + v)}</span>
                <span className="settings-theme-dot" aria-hidden="true" />
              </span>
            </button>
          ))}
        </div>
      </Setting>
      <Setting title={t('gui.settings.general.notify')} sub={t('gui.settings.general.notify_sub')}>
        <Switch on={on} label={t('gui.settings.general.notify')} onChange={(v) => void flip(v)} />
      </Setting>
    </div>
  )
}
