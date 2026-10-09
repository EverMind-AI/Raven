/* The EverOS Cloud card: whether the hosted memory backend answers, which key
   it uses, where it points. Drawn only when settings.everosCloud says the
   backend is the selected one; the EverOS role slots it replaces are hidden
   by then (Roles.tsx). Its own file rather than a corner of Roles.tsx because
   it borrows KeyRow from the tools page, which imports Roles. */
import { useSyncExternalStore } from 'react'

import { t } from '../../../i18n/t'
import { Card, Chip, Row, Rov, Spin } from '../Fields'
import { KeyRow } from '../pages/Tools'
import * as store from '../store'

import type { EverosCloudInfo } from '../types'
import type { JSX } from 'react'

/* The settings.set key the card's key row writes; the server admits it against
   the plugin's own manifest. */
export const CLOUD_KEY = 'plugins.config.everos-cloud-memory.api_key'
const KEYS_URL = 'https://everos.evermind.ai'

function CloudChip({ c }: { c: EverosCloudInfo }): JSX.Element {
  if (!c.api_key_set) return <Chip state="warn">{t('gui.settings.cloud.chip_needs_key')}</Chip>
  if (c.status === null || c.status === undefined) return <Spin>{t('gui.settings.cloud.chip_probing')}</Spin>
  if (c.status === 'ok') return <Chip state="on">{t('gui.settings.cloud.chip_connected')}</Chip>
  if (c.status === 'degraded') return <Chip state="warn">{t('gui.settings.cloud.chip_rate_limited')}</Chip>
  /* The backend's own sentence: which key to check, which account to fix. */
  return <Chip state="off">{c.hint || t('gui.settings.cloud.chip_needs_key')}</Chip>
}

export function EverosCloudCard(): JSX.Element | null {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const c = s.snap.everosCloud
  if (!c?.selected) return null
  return (
    <Card title={t('gui.settings.cloud.title')}>
      <Row label={t('gui.settings.cloud.status')}><CloudChip c={c} /></Row>
      <KeyRow label={t('gui.settings.cloud.key')} keyName={CLOUD_KEY} url={KEYS_URL} raw={s.snap.raw} />
      {c.key_source === 'env' && <Row><Rov>{t('gui.settings.cloud.key_env')}</Rov></Row>}
      <Row label={t('gui.settings.cloud.endpoint')}><Rov>{c.base_url}</Rov></Row>
      <Row><Rov>{t('gui.settings.cloud.restart_hint')}</Rov></Row>
    </Card>
  )
}
