/* Reusable settings API-key row and vendor dropdown used by the onboarding
 * wizard's web step (SetupBodies.tsx) and the EverOS Cloud card (CloudCard.tsx).
 */
import { useState, useSyncExternalStore } from 'react'

import { KeyInput } from '../../components/KeyInput'
import { t } from '../../i18n/t'
import { KeyLink, Row } from './Fields'
import { str } from './source'
import * as store from './store'

import type { JSX } from 'react'

export function KeyRow({ label, keyName, legacy, url, raw }: {
  label: string
  keyName: string
  legacy?: string | null
  url?: string
  raw: Record<string, unknown>
}): JSX.Element {
  const [value, setValue] = useState('')
  const isSet = !!str(raw, keyName) || (!!legacy && !!str(raw, legacy))
  const busy = useSyncExternalStore(store.subscribe, () => store.isBusy(`set:${keyName}`))
  const save = (): void => {
    if (busy) return
    const v = value.trim()
    if (!v) { store.refuse(t('gui.settings.tools.key_first')); return }
    void store.write(keyName, v)
  }
  const clear = async (): Promise<void> => {
    if (str(raw, keyName)) await store.write(keyName, '')
    if (legacy && str(raw, legacy)) await store.write(legacy, '')
  }
  return (
    <Row stack label={<>{label}<KeyLink url={url} /></>}>
      <span className="settings-taglist">
        <KeyInput className="settings-tbox settings-tbox-wide" value={value} aria-label={label} disabled={busy}
          placeholder={isSet ? t('gui.settings.key_set_ph') : t('gui.settings.key_ph')}
          onChange={(e) => setValue(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
        <button type="button" className="mini" disabled={busy} onClick={save}>
          {busy ? (
            <><span className="settings-spin" aria-hidden="true" />{t('gui.set.checking')}</>
          ) : (
            isSet ? t('gui.settings.update') : t('gui.save')
          )}
        </button>
        {isSet && <button type="button" className="mini ghost" disabled={busy} onClick={() => void clear()}>{t('gui.settings.clear')}</button>}
      </span>
    </Row>
  )
}

export function VendorSelect({ keyName, value, opts }: { keyName: string; value: string; opts: Array<[string, string]> }): JSX.Element {
  return (
    <span className="selw">
      <select className="sel" value={value} aria-label={t('gui.settings.tools.vendor')} onChange={(e) => void store.write(keyName, e.currentTarget.value)}>
        {opts.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
      </select>
    </span>
  )
}
