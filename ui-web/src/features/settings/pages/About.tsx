/* About: the running version, the update check, and where the config and the
   workspace live. The check is the chrome's (features/settings/wire.ts); it
   answers with the newer version when there is one, and the row then offers
   the upgrade -- the two steps A47 asks for. */
import { useState } from 'react'

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
  /* Held here rather than acted on inside the check: the design has the row
     offer the upgrade once a newer version is known (A47), and starting it
     from the check made it a second action the reader did not ask for. */
  const [newer, setNewer] = useState<string | null>(null)
  return (
    <Card>
      <Row label={t('gui.settings.about.version')}>
        <Rov>{version || t('gui.settings.about.version_unknown')}</Rov>
      </Row>
      <Row label={t('gui.settings.about.update')}>
        <button
          type="button"
          className="mini ghost"
          onClick={(e) => { void store.source().checkUpdate(e.currentTarget).then(setNewer) }}
        >
          {t('gui.settings.about.check')}
        </button>
        {newer ? (
          <button type="button" className="mini" onClick={() => store.source().upgrade()}>
            {t('gui.settings.about.upgrade')} {newer}
          </button>
        ) : null}
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
