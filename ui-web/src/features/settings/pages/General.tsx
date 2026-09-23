/* General: language and theme. Language goes through the source (it is config
   the agent reads too); theme is the browser's own preference and goes to the
   module that already holds it.

   Desktop notifications are not offered here, because half of what the row
   promised does not exist. Its line read "when tasks finish or need your
   attention", and only the first half was ever sent: an answer landing is
   announced from state/session/runtime.ts, while the three side-channel asks
   that actually stop a turn on the reader -- confirm, the permission gate,
   clarify -- announce nothing. A switch is not offered for a thing that is
   half built.

   Taken out of the page rather than out of the tree: the switch was the only
   writer of the preference src/lib/notifications.ts reads, so with it gone the
   page announces nothing at all, and finishing the other half is putting this
   row back rather than writing the feature again. */
import { code as langCode, t } from '../../../i18n/t'
import * as look from '../../../state/look'
import { Seg } from '../Fields'
import * as store from '../store'

import type { JSX, ReactNode } from 'react'

type Theme = 'system' | 'light' | 'dark'

const THEMES: Theme[] = ['system', 'light', 'dark']

const SHOT = { light: 'settings-shot', dark: 'settings-shot settings-shot-dark' }

/* One setting: its name and a line about it on the left, the control on the
   right, or -- for the theme cards -- under both. No card around the list: the
   page is two settings, and the whitespace between them is the division. */
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
  const theme = look.get().theme as Theme
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
    </div>
  )
}
