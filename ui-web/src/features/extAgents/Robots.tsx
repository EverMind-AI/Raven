/* Personal robot records and explicit health checks, without an execution node. */
import { useEffect, useRef, useState } from 'react'

import { t } from '../../i18n/t'
import { ds } from '../../state/sources'

import type { RobotDraft, RobotRow } from './types'
import type { JSX } from 'react'

const TYPE_KEYS = {
  sparkbot: 'gui.extAgents.robot_sparkbot',
  http_device: 'gui.extAgents.robot_http_device',
  robot_dog: 'gui.extAgents.robot_dog',
  agv: 'gui.extAgents.robot_agv',
  mobile_manipulator: 'gui.extAgents.robot_mobile_manipulator',
  other: 'gui.extAgents.robot_other',
} satisfies Record<RobotDraft['type'], string>

const STATUS_KEYS = {
  reachable: 'gui.extAgents.robot_reachable',
  not_connected: 'gui.extAgents.robot_disconnected',
  unknown: 'gui.extAgents.robot_unknown',
  error: 'gui.extAgents.robot_error',
}

export function robotStatus(row: RobotRow, now: number): RobotRow['status'] {
  const age = row.observation ? now / 1000 - row.observation.checked_epoch : null
  return age !== null && (age > 60 || age < -5) ? 'unknown' : row.status
}

function RobotForm({ row, saved, close }: { row: RobotRow | null; saved: () => Promise<void>; close: () => void }): JSX.Element {
  const form = useRef<HTMLFormElement>(null)
  const [kind, setKind] = useState<RobotDraft['type']>(row?.type ?? 'sparkbot')
  const [mode, setMode] = useState<RobotDraft['connection_mode']>(row?.connection_mode ?? 'metadata')
  const [company, setCompany] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const httpAllowed = kind === 'sparkbot' || kind === 'http_device'
  const http = httpAllowed && mode === 'http_status'
  return (
    <form
      aria-label={t(row ? 'gui.extAgents.robot_edit' : 'gui.extAgents.robot_add')}
      className="extAgents-robot-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (company || busy || !form.current) return
        const data = new FormData(form.current)
        const text = (key: string): string => String(data.get(key) ?? '')
        const device: RobotDraft = {
          domain: 'personal', confirm_personal: data.get('confirm_personal') === 'on',
          name: text('name'), type: kind, connection_mode: http ? 'http_status' : 'metadata',
          model: text('model'), location: text('location'), note: text('note'),
          status_url: http ? text('status_url') : '', expected_project: http ? text('expected_project') : '',
        }
        setBusy(true)
        setError('')
        void ds('robots').save(device, row?.id, row?.revision).then(saved).catch(() => {
          setError(t('gui.extAgents.robot_save_failed'))
        }).finally(() => setBusy(false))
      }}
      ref={form}
    >
      <h3>{t(row ? 'gui.extAgents.robot_edit' : 'gui.extAgents.robot_add')}</h3>
      <label>{t('gui.extAgents.robot_domain')}
        <select onChange={(event) => setCompany(event.target.value === 'company')} defaultValue="personal">
          <option value="personal">{t('gui.extAgents.robot_personal')}</option>
          <option value="company">{t('gui.extAgents.robot_company')}</option>
        </select>
      </label>
      {company ? <p role="alert">{t('gui.extAgents.robot_company_blocked')}</p> : (
        <fieldset disabled={busy}>
          <div className="extAgents-robot-pair">
            <label>{t('gui.extAgents.robot_name')}<input defaultValue={row?.name ?? ''} maxLength={80} name="name" required /></label>
            <label>{t('gui.extAgents.robot_type')}
              <select defaultValue={kind} onChange={(event) => setKind(event.target.value as RobotDraft['type'])}>
                {Object.entries(TYPE_KEYS).map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}
              </select>
            </label>
            <label>{t('gui.extAgents.robot_model')}<input defaultValue={row?.model ?? ''} maxLength={100} name="model" /></label>
            <label>{t('gui.extAgents.robot_location')}<input defaultValue={row?.location ?? ''} maxLength={100} name="location" /></label>
          </div>
          <label>{t('gui.extAgents.robot_mode')}
            <select value={httpAllowed ? mode : 'metadata'} onChange={(event) => setMode(event.target.value as RobotDraft['connection_mode'])}>
              <option value="metadata">{t('gui.extAgents.robot_metadata')}</option>
              <option disabled={!httpAllowed} value="http_status">{t('gui.extAgents.robot_http')}</option>
            </select>
          </label>
          {!httpAllowed ? <p>{t('gui.extAgents.robot_metadata_only')}</p> : null}
          {http ? <div className="extAgents-robot-http">
            <label>{t('gui.extAgents.robot_status_url')}<input defaultValue={row?.status_url ?? ''} maxLength={300} name="status_url" required type="url" /></label>
            <label>{t('gui.extAgents.robot_identity')}<input defaultValue={row?.expected_project ?? ''} maxLength={80} name="expected_project" /></label>
            <p>{t('gui.extAgents.robot_url_hint')}</p>
            <label className="extAgents-robot-checkbox"><input defaultChecked={row?.connection_mode === 'http_status'} required type="checkbox" />{t('gui.extAgents.robot_readonly_confirm')}</label>
          </div> : null}
          <label>{t('gui.extAgents.robot_note')}<textarea defaultValue={row?.note ?? ''} maxLength={500} name="note" rows={2} /></label>
          <label className="extAgents-robot-checkbox"><input defaultChecked={!!row} name="confirm_personal" required type="checkbox" />{t('gui.extAgents.robot_personal_confirm')}</label>
        </fieldset>
      )}
      {error ? <p role="alert">{error}</p> : null}
      <div className="extAgents-robot-actions">
        <button disabled={busy} onClick={close} type="button">{t('gui.cancel')}</button>
        <button disabled={busy || company} type="submit">{t('gui.save')}</button>
      </div>
    </form>
  )
}

export function Robots(): JSX.Element {
  const [rows, setRows] = useState<RobotRow[]>([])
  const [trash, setTrash] = useState(false)
  const [editing, setEditing] = useState<RobotRow | null | undefined>(undefined)
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<{ id: string; checking: boolean } | null>(null)
  const [query, setQuery] = useState('')
  const [type, setType] = useState('all')
  const [clock, setClock] = useState(Date.now)
  const epoch = useRef(0)
  const blocked = !!busy || editing !== undefined
  useEffect(() => {
    let live = true
    const ticket = ++epoch.current
    setLoading(true)
    void ds('robots').list(trash).then((items) => {
      if (live && ticket === epoch.current) { setRows(items); setNotice('') }
    }).catch(() => {
      if (live) setNotice(t('gui.extAgents.robot_unavailable'))
    }).finally(() => { if (live) setLoading(false) })
    const timer = setInterval(() => setClock(Date.now()), 5000)
    return () => { live = false; clearInterval(timer); epoch.current += 1 }
  }, [trash])
  const reload = async (): Promise<void> => {
    const ticket = ++epoch.current
    const items = await ds('robots').list(trash)
    if (ticket === epoch.current) setRows(items)
  }
  const act = async (id: string, operation: () => Promise<unknown>, checking = false): Promise<void> => {
    setBusy({ id, checking })
    setNotice('')
    try { await operation(); await reload() }
    catch { setNotice(t('gui.extAgents.robot_operation_failed')) }
    finally { setBusy(null) }
  }
  const visible = rows.filter((row) => (type === 'all' || row.type === type) &&
    [row.name, row.model, row.location].join(' ').toLowerCase().includes(query.toLowerCase()))
  return (
    <section aria-label={t('gui.extAgents.robot_title')} className="extAgents-robots">
      <div className="extAgents-robot-toolbar">
        <span>{t('gui.extAgents.robot_personal')}</span>
        <input aria-label={t('gui.extAgents.robot_search')} placeholder={t('gui.extAgents.robot_search')} onChange={(event) => setQuery(event.target.value)} type="search" />
        <select aria-label={t('gui.extAgents.robot_filter')} defaultValue="all" onChange={(event) => setType(event.target.value)}>
          <option value="all">{t('gui.extAgents.robot_all_types')}</option>
          {Object.entries(TYPE_KEYS).map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}
        </select>
        <button disabled={blocked} onClick={() => { setTrash(!trash); setEditing(undefined) }} type="button">{t(trash ? 'gui.extAgents.robot_back' : 'gui.extAgents.robot_trash')}</button>
        {!trash ? <button disabled={blocked} onClick={() => setEditing(null)} type="button">{t('gui.extAgents.robot_add')}</button> : null}
      </div>
      <p className="extAgents-robot-boundary">{t('gui.extAgents.robot_boundary')}</p>
      <p aria-live="polite" role="status">{notice}</p>
      {editing !== undefined ? <RobotForm close={() => setEditing(undefined)} key={editing?.id ?? 'new'} row={editing}
        saved={async () => { await reload(); setEditing(undefined) }} /> : null}
      {loading ? <p>{t('gui.extAgents.robot_loading')}</p> : !visible.length ? <p>{t('gui.extAgents.robot_empty')}</p> : null}
      <div className="extAgents-robot-grid">
        {!loading ? visible.map((row) => {
          const status = robotStatus(row, clock)
          return <article aria-label={row.name} className="extAgents-robot-card" key={row.id}>
            <div className="extAgents-robot-heading"><h3>{row.name}</h3><span data-status={status}>{t(STATUS_KEYS[status])}</span></div>
            <p>{t(TYPE_KEYS[row.type])}</p>
            <dl>
              <dt>{t('gui.extAgents.robot_mode')}</dt><dd>{t(row.connection_mode === 'metadata' ? 'gui.extAgents.robot_metadata' : 'gui.extAgents.robot_http')}</dd>
              {row.model ? <><dt>{t('gui.extAgents.robot_model')}</dt><dd>{row.model}</dd></> : null}
              {row.location ? <><dt>{t('gui.extAgents.robot_location')}</dt><dd>{row.location}</dd></> : null}
              {row.status_url ? <><dt>{t('gui.extAgents.robot_status_url')}</dt><dd>{row.status_url}</dd></> : null}
              {row.observation ? <><dt>{t('gui.extAgents.robot_checked')}</dt><dd>{new Date(row.observation.checked_at).toLocaleString()}</dd></> : null}
            </dl>
            {row.note ? <p className="extAgents-robot-note">{row.note}</p> : null}
            {row.observation ? <p>{row.observation.detail}</p> : <p>{t('gui.extAgents.robot_unverified')}</p>}
            <div className="extAgents-robot-actions">
              {trash ? <button disabled={blocked} onClick={() => void act(row.id, () => ds('robots').restore(row.id))} type="button">{t('gui.extAgents.robot_restore')}</button> : <>
                {row.connection_mode === 'http_status' ? <button disabled={blocked} onClick={() => void act(row.id, () => ds('robots').check(row.id), true)} type="button">{t(busy?.id === row.id && busy.checking ? 'gui.extAgents.robot_checking' : 'gui.extAgents.robot_check')}</button> : null}
                <button disabled={blocked} onClick={() => setEditing(row)} type="button">{t('gui.extAgents.robot_edit')}</button>
                <button disabled={blocked} onClick={() => void act(row.id, () => ds('robots').remove(row.id))} type="button">{t('gui.extAgents.robot_remove')}</button>
              </>}
            </div>
          </article>
        }) : null}
      </div>
      <p className="extAgents-robot-foot">{t('gui.extAgents.robot_safety')}</p>
    </section>
  )
}
