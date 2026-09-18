/* About: the running version, the update check, and where the config and the
   workspace live. The check is the chrome's (features/settings/wire.ts), which
   already hands a newer version to the upgrade flow. */
import { t } from '../../../i18n/t'
import { open as copyOrOpen } from '../../../lib/openUrl'
import { Card, PathVal, Row, Rov } from '../Fields'
import * as store from '../store'

import type { JSX } from 'react'

/* The configured workspace, else the default beside the config file: the
   home the gateway runs from, not the operator's. */
export function workspacePath(raw: Record<string, unknown>, configPath: string): string {
  const agents = raw.agents as { defaults?: { workspace?: string } } | undefined
  const stated = agents && agents.defaults && agents.defaults.workspace
  if (stated) return stated
  const dir = configPath.replace(/[\\/][^\\/]*$/, '')
  return dir ? `${dir}/workspace` : '~/.raven/workspace'
}

export function About(): JSX.Element {
  const s = store.get()
  const version = store.source().version()
  return (
    <Card>
      <Row label={t('gui.settings.about.version')}>
        <Rov>{version || t('gui.settings.about.version_unknown')}</Rov>
      </Row>
      <Row label={t('gui.settings.about.update')}>
        <button type="button" className="mini ghost" onClick={(e) => void store.source().checkUpdate(e.currentTarget)}>
          {t('gui.settings.about.check')}
        </button>
      </Row>
      <Row label={t('gui.settings.about.config')}>
        <PathVal path={s.snap.configPath} onCopy={() => copyOrOpen(s.snap.configPath)} />
      </Row>
      <Row label={t('gui.settings.about.storage')}>
        <PathVal path={workspacePath(s.snap.raw, s.snap.configPath)} onCopy={() => copyOrOpen(workspacePath(s.snap.raw, s.snap.configPath))} />
      </Row>
    </Card>
  )
}
