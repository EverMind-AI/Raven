/* Archive: the sessions hidden from the rail, with restore and delete, and the
   auto-archive switch (sessions.autoArchiveAfterDays: 30 or null). */
import { useEffect } from 'react'

import { t } from '../../../i18n/t'
import * as confirm from '../../../state/confirm'
import { whenLabel } from '../../rail/source'
import { Card, Row, Rov, Switch } from '../Fields'
import * as store from '../store'

import type { ArchivedSession } from '../types'
import type { JSX } from 'react'

export const AUTO_ARCHIVE_DAYS = 30

export function autoArchiveOn(raw: Record<string, unknown>): boolean {
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
            onChange={(v) => void store.write('sessions.autoArchiveAfterDays', v ? AUTO_ARCHIVE_DAYS : null)}
          />
        </Row>
      </Card>
    </>
  )
}
