/* General: language, theme, notifications. Language goes through the source
   (it is config the agent reads too); theme and notifications are the browser's
   own preferences and go to the modules that already hold them. */
import { code as langCode, t } from '../../../i18n/t'
import * as notifications from '../../../lib/notifications'
import * as look from '../../../state/look'
import { show as toast } from '../../../state/toast'
import { Card, Row, Seg, Switch } from '../Fields'
import * as store from '../store'

import type { JSX } from 'react'

export function General(): JSX.Element {
  const on = notifications.enabled()
  const canNtf = 'Notification' in window
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
    <Card>
      <Row label={t('gui.settings.general.language')}>
        <Seg
          opts={[['zh', t('gui.settings.general.lang_zh')], ['en', t('gui.settings.general.lang_en')]]}
          value={langCode}
          onPick={(v) => store.source().setLang(v)}
        />
      </Row>
      <Row label={t('gui.settings.general.theme')}>
        <Seg
          opts={[
            ['system', t('gui.settings.general.theme_system')],
            ['light', t('gui.settings.general.theme_light')],
            ['dark', t('gui.settings.general.theme_dark')],
          ]}
          value={look.get().theme}
          onPick={(v) => { look.set({ theme: v }); store.redraw() }}
        />
      </Row>
      <Row label={t('gui.settings.general.notify')}>
        <Switch on={on} label={t('gui.settings.general.notify')} onChange={(v) => void flip(v)} />
      </Row>
    </Card>
  )
}
