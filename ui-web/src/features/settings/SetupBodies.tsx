/* The onboarding wizard's two step bodies: the settings dialog's own model
   page and web-search controls, drawn without the dialog around them. The
   wizard (src/app/install.ts) renders these inside its own scroll pane and
   drives the store itself -- subscribe/get and refresh() -- the way
   SettingsApp.tsx does for the dialog; nothing here opens or closes the
   dialog, and neither pane owns a source.ts call of its own. */
import { useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { Card, Chip, InlineErr, Row, Rov } from './Fields'
import { KeyRow, VendorSelect } from './pages/Tools'
import { Providers } from './providers/Providers'
import { Roles } from './providers/Roles'
import { FETCH_KEYLESS, WEB_VENDOR, WEB_VENDOR_LABEL, WEB_VENDOR_URL, keySet, legacyKey, vendorKey, webVendor } from './source'
import * as store from './store'

import type { JSX } from 'react'

export function ModelStepBody(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="settings-panel settings-setup" data-section="model" key={s.epoch}>
      {s.loaded ? (
        <>
          <Providers setup />
          <Roles />
        </>
      ) : (
        <div className="settings-soonbox"><div className="settings-t">{t('gui.settings.loading')}</div></div>
      )}
      <InlineErr text={s.err} />
    </div>
  )
}

export function WebStepBody(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const raw = s.snap.raw
  const searchPick = WEB_VENDOR.web_search!
  const fetchPick = WEB_VENDOR.web_fetch!
  const searchVendor = webVendor('web_search', raw)
  const fetchVendor = webVendor('web_fetch', raw)
  const searchReady = keySet('web_search', searchVendor, raw)
  const fetchOptional = FETCH_KEYLESS.has(fetchVendor)
  const fetchReady = fetchOptional || keySet('web_fetch', fetchVendor, raw)
  const fetchLabel = fetchOptional
    ? `${t('gui.settings.tools.vendor_key', { name: WEB_VENDOR_LABEL[fetchVendor] || fetchVendor })} · ${t('gui.settings.roles.optional')}`
    : t('gui.settings.tools.vendor_key', { name: WEB_VENDOR_LABEL[fetchVendor] || fetchVendor })
  return (
    <div className="settings-panel settings-setup" data-section="tools" key={s.epoch}>
      {s.loaded ? (
        <>
          <Card title={t('gui.settings.setup.web_search')}>
            <Row label={<>
              {t('gui.settings.tools.search_vendor')}{' '}
              <Chip state={searchReady ? 'on' : 'off'}>{searchReady ? t('gui.settings.setup.ready') : t('gui.settings.setup.unset')}</Chip>
            </>}
            >
              <VendorSelect keyName={searchPick.path} value={searchVendor} opts={searchPick.vendors.map((v) => [v, WEB_VENDOR_LABEL[v] || v])} />
            </Row>
            <KeyRow
              label={t('gui.settings.tools.vendor_key', { name: WEB_VENDOR_LABEL[searchVendor] || searchVendor })}
              keyName={vendorKey(searchVendor)} legacy={legacyKey('web_search', searchVendor)} url={WEB_VENDOR_URL[searchVendor]} raw={raw}
            />
          </Card>
          <Card title={t('gui.settings.setup.web_fetch')}>
            <Row label={<>
              {t('gui.settings.tools.fetch_vendor')}{' '}
              <Chip state={fetchReady ? 'on' : 'off'}>{fetchReady ? t('gui.settings.setup.ready') : t('gui.settings.setup.unset')}</Chip>
            </>}
            >
              <VendorSelect keyName={fetchPick.path} value={fetchVendor} opts={fetchPick.vendors.map((v) => [v, WEB_VENDOR_LABEL[v] || v])} />
            </Row>
            <KeyRow label={fetchLabel} keyName={vendorKey(fetchVendor)} legacy={legacyKey('web_fetch', fetchVendor)} url={WEB_VENDOR_URL[fetchVendor]} raw={raw} />
            {fetchOptional && <Row><Rov>{t('gui.settings.setup.keyless_hint')}</Rov></Row>}
          </Card>
        </>
      ) : (
        <div className="settings-soonbox"><div className="settings-t">{t('gui.settings.loading')}</div></div>
      )}
      <InlineErr text={s.err} />
    </div>
  )
}
