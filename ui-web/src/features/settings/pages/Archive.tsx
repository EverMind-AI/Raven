/* Archive: the sessions hidden from the rail, with restore and delete, and the
   auto-archive switch (sessions.autoArchiveAfterDays: 30 or null). */
import { useEffect } from 'react'

import { t } from '../../../i18n/t'
import * as confirm from '../../../state/confirm'
import { refreshList } from '../../../state/session/registry'
import { whenLabel } from '../../rail/source'
import { Card, Row, Rov, Switch } from '../Fields'
import * as store from '../store'

import type { ArchivedSession } from '../types'
import type { JSX } from 'react'

export const AUTO_ARCHIVE_DAYS = 30

function autoArchiveOn(raw: Record<string, unknown>): boolean {
  const sessions = raw.sessions as { autoArchiveAfterDays?: number | null } | undefined
  return !!(sessions && sessions.autoArchiveAfterDays)
}

const rowTitle = (r: ArchivedSession): string => r.title || r.preview || r.id

export function Archive(): JSX.Element {
  const s = store.get()
  useEffect(() => {
    if (s.archived === null) void store.archivedLoad()
  }, [s.archived])
  const rows = s.archived
  const restore = (r: ArchivedSession): void => {
    void store.run(`restore:${r.id}`, () => store.source().restore(r.id).then(() => store.archivedLoad()))
  }
  /* The sweep this switch turns on runs inside session.list, so until something
     lists, turning it on moves nothing: the card kept saying there was nothing
     archived and the rail kept the stale conversations, until a reload made a
     pile of them disappear at once. Both reads below issue one, which is what
     performs the sweep and then shows what it did. */
  const toggleAuto = (on: boolean): void => {
    void store.write('sessions.autoArchiveAfterDays', on ? AUTO_ARCHIVE_DAYS : null)
      .then((applied) => { if (applied) return store.archivedLoad().then(() => refreshList()) })
  }
  const remove = (r: ArchivedSession): void => {
    confirm.ask(
      t('gui.settings.archive.delete_title', { title: rowTitle(r) }),
      t('gui.settings.archive.delete_body'),
      t('gui.settings.archive.delete'),
      () => { void store.run(`delete:${r.id}`, () => store.source().removeSession(r.id).then(() => store.archivedLoad())) },
    )
  }
  return (
    <>
      <Card title={t('gui.settings.archive.title')}>
        {rows === null && <Row><Rov>{t('gui.settings.loading')}</Rov></Row>}
        {rows && rows.length === 0 && <Row><Rov>{t('gui.settings.archive.empty')}</Rov></Row>}
        {rows && rows.map((r) => (
          <Row key={r.id} label={<>{rowTitle(r)} <span className="settings-kk">{whenLabel(r.updated_at)}</span></>}>
            <span className="settings-taglist">
              <button type="button" className="mini" disabled={store.isBusy(`restore:${r.id}`)} onClick={() => restore(r)}>
                {t('gui.settings.archive.restore')}
              </button>
              <button type="button" className="mini ghost" onClick={() => remove(r)}>{t('gui.settings.archive.delete')}</button>
            </span>
          </Row>
        ))}
      </Card>
      <Card>
        <Row label={t('gui.settings.archive.auto')} sub={t('gui.settings.archive.auto_sub', { n: AUTO_ARCHIVE_DAYS })}>
          <Switch
            on={autoArchiveOn(s.snap.raw)}
            label={t('gui.settings.archive.auto')}
            onChange={toggleAuto}
          />
        </Row>
      </Card>
    </>
  )
}
