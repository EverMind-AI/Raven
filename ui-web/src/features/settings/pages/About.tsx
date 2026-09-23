/* About: the running version, the update check, and where the config and the
   workspace live. The check is the chrome's (features/settings/wire.ts); once a
   newer version is known the row offers the upgrade beside it -- the two steps
   A47 asks for. */
import { t } from '../../../i18n/t'
import { open as copyOrOpen } from '../../../lib/openUrl'
import { hostIsLocal, hostPlatform } from '../../../lib/platform'
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
  /* Read, not held: a successful check redraws the settings dialog, and the
     panel is keyed by that redraw's epoch (SettingsApp.tsx), so this component
     is replaced between the check resolving and any setter it left behind.
     The page already retains the version the check found -- app/updates.ts sets
     it for the rail's own notice -- so the replacement reads the same fact and
     draws the button its predecessor would have.
     It also means a version found by the background poll offers the upgrade
     here, which is what A47 asks for: the row offers it once a newer version is
     known, not only when this button was the one that found it. */
  const newer = store.source().newerVersion()
  const config = s.snap.configPath
  const storage = workspacePath(s.snap.raw, config)
  /* A location is somewhere to go, so its button takes the reader there -- in
     the host's file manager, which is only their own while the gateway is this
     desktop. Served from elsewhere it would open a window on another machine,
     so there the button copies the path instead. */
  const local = hostIsLocal()
  const platform = hostPlatform()
  const goLabel = !local
    ? t('gui.settings.copy_path')
    : t(platform === 'mac' ? 'gui.ws.reveal_finder' : platform === 'windows' ? 'gui.ws.reveal_explorer' : 'gui.ws.reveal_folder')
  const go = (place: 'config' | 'workspace', path: string): void => {
    if (local) void store.source().revealPlace(place)
    else copyOrOpen(path)
  }
  return (
    <Card>
      <Row label={t('gui.settings.about.version')}>
        <Rov>{version || t('gui.settings.about.version_unknown')}</Rov>
      </Row>
      <Row label={t('gui.settings.about.update')}>
        <button
          type="button"
          className="mini ghost"
          onClick={(e) => { void store.source().checkUpdate(e.currentTarget) }}
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
        <PathVal path={config} label={goLabel} onAct={() => go('config', config)} />
      </Row>
      <Row label={t('gui.settings.about.storage')}>
        <PathVal path={storage} label={goLabel} onAct={() => go('workspace', storage)} />
      </Row>
    </Card>
  )
}
