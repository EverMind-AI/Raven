/* The first-run provider and model setup flow rendered into #onb. */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import { KeyInput } from '../../shell/key-input'
import { ModelTagDefs, ModelTags } from '../../shell/model-tags'
import { ProviderIcon, ProviderLink, ProviderStatus } from '../../shell/provider-mark'
import * as store from './store'

import type { OnboardProvider, OnboardSource } from './types'
import type { JSX } from 'react'

type Step = 'welcome' | 'provider' | 'creds' | 'model' | 'done'

const DOT_STEPS = ['provider', 'creds', 'model'] as const

const errOf = (value: unknown): string => {
  const e = value as { data?: { detail?: string; reason?: string }; message?: string } | null
  return e?.data?.detail || e?.data?.reason || e?.message || String(value)
}

function Raven(): JSX.Element {
  return <img className="ob-rv" src="assets/ravens/main-agent.webp" alt="" />
}

function Dots({ current }: { current: (typeof DOT_STEPS)[number] }): JSX.Element {
  return (
    <div className="ob-dots">
      {DOT_STEPS.map(name => (
        <i key={name} className={name === current ? 'on' : undefined} />
      ))}
    </div>
  )
}

function ErrorLine({ error }: { error: string }): JSX.Element {
  return <div className="ob-err">{error}</div>
}

function CopyCommand({ provider }: { provider: OnboardProvider }): JSX.Element {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | null>(null)
  const command = `raven provider login ${provider.slug.replace(/_/g, '-')}`

  useEffect(
    () => () => {
      if (timer.current != null) window.clearTimeout(timer.current)
    },
    []
  )

  return (
    <div className="ob-cmd">
      <code>{command}</code>
      <button
        type="button"
        onClick={() => {
          navigator.clipboard?.writeText(command).catch(() => {})
          setCopied(true)
          if (timer.current != null) window.clearTimeout(timer.current)
          timer.current = window.setTimeout(() => setCopied(false), 1600)
        }}
      >
        {t(copied ? 'gui.onb.copied' : 'gui.onb.copy')}
      </button>
    </div>
  )
}

export function OnboardApp(): JSX.Element | null {
  const opening = useSyncExternalStore(store.subscribe, store.snapshot)
  if (!opening.source) return null
  return <Flow key={opening.epoch} source={opening.source} />
}

function Flow({ source }: { source: OnboardSource }): JSX.Element {
  const [step, setStep] = useState<Step>('welcome')
  const [providers, setProviders] = useState<OnboardProvider[]>([])
  const [selected, setSelected] = useState<OnboardProvider | null>(null)
  const [model, setModel] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [ready, setReady] = useState(false)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  const keyInput = useRef<HTMLInputElement>(null)
  const baseInput = useRef<HTMLInputElement>(null)
  const closeTimer = useRef<number | null>(null)
  const doneTimer = useRef<number | null>(null)

  useEffect(() => {
    const host = document.getElementById('onb')
    if (!host) return
    host.hidden = false
    delete host.dataset.off
    return () => {
      host.hidden = true
    }
  }, [])

  useEffect(
    () => () => {
      if (closeTimer.current != null) window.clearTimeout(closeTimer.current)
      if (doneTimer.current != null) window.clearTimeout(doneTimer.current)
    },
    []
  )

  const run = async (fn: () => Promise<void>): Promise<void> => {
    setWorking(true)
    try {
      await fn()
    } finally {
      setWorking(false)
    }
  }

  const fail = (value: unknown): void => setError(t('gui.onb.err_generic', { err: errOf(value) }))

  const welcome = (): void => {
    setError('')
    setStep('welcome')
  }

  const chooseProvider = (): void => {
    setError('')
    setStep('provider')
  }

  const chooseModel = (provider: OnboardProvider): void => {
    setSelected(provider)
    setModel(null)
    setQuery('')
    setReady(provider.auth_type === 'local' && !!(provider.api_base || provider.default_api_base))
    setError('')
    setStep(provider.authenticated ? 'model' : 'creds')
  }

  const finish = (): void => {
    setStep('done')
    doneTimer.current = window.setTimeout(() => {
      const host = document.getElementById('onb')
      if (host) host.dataset.off = '1'
      closeTimer.current = window.setTimeout(store.finish, 500)
    }, 1500)
  }

  const checkInputs = (): void => {
    const local = selected?.auth_type === 'local'
    const needsBase = local || !!selected?.needs_api_base
    const hasKey = local || !!keyInput.current?.value.trim()
    const hasBase = !needsBase || !!baseInput.current?.value.trim()
    setReady(hasKey && hasBase)
  }

  let body: JSX.Element
  if (step === 'welcome') {
    body = (
      <div className="ob-step" data-center="1">
        <Raven />
        <div className="ob-t">{t('gui.onb.welcome_t')}</div>
        <div className="ob-s">{t('gui.onb.welcome_s')}</div>
        <button
          className="ob-btn"
          disabled={working}
          onClick={() =>
            void run(async () => {
              setError('')
              try {
                const result = await source.options()
                setProviders(result?.providers || [])
                chooseProvider()
              } catch (e) {
                fail(e)
              }
            })
          }
        >
          {t('gui.onb.start')}
        </button>
        <ErrorLine error={error} />
      </div>
    )
  } else if (step === 'provider') {
    body = (
      <div className="ob-step">
        <div className="ob-t">{t('gui.onb.provider_t')}</div>
        <div className="ob-s">{t('gui.onb.provider_s')}</div>
        <div className="ob-list">
          {providers.map(provider => (
            <div key={provider.slug} className="ob-row provider-choice-row">
              <button
                className="provider-choice-action"
                type="button"
                aria-label={provider.name || provider.slug}
                onClick={() => chooseModel(provider)}
              />
              <span className="nm">
                <ProviderIcon id={provider.slug} name={provider.name || provider.slug} />
                <ProviderLink homepage={provider.homepage} name={provider.name || provider.slug} />
              </span>
              <span className={'bd' + (provider.authenticated ? ' on' : '')}>
                {provider.authenticated
                  ? t('gui.onb.connected')
                  : provider.auth_type === 'oauth'
                    ? 'OAuth'
                    : provider.auth_type === 'local'
                      ? 'URL'
                      : 'API Key'}
              </span>
              <ProviderStatus
                connected={provider.authenticated}
                label={t(provider.authenticated ? 'gui.model.state.connected' : 'gui.model.not_connected')}
              />
            </div>
          ))}
        </div>
        <button className="ob-ghost" onClick={welcome}>
          {t('gui.onb.back')}
        </button>
        <Dots current="provider" />
      </div>
    )
  } else if (step === 'creds' && selected) {
    const local = selected.auth_type === 'local'
    const acceptsApiKey = selected.accepts_api_key ?? !local
    const needsBase = local || selected.auth_type === 'endpoint' || !!selected.needs_api_base
    body = (
      <div className="ob-step">
        <div className="ob-t">{t('gui.onb.key_t', { name: selected.name || selected.slug })}</div>
        {selected.auth_type === 'oauth' ? (
          <>
            <div className="ob-s">{t('gui.onb.oauth_s')}</div>
            <CopyCommand provider={selected} />
            <button
              className="ob-btn"
              disabled={working}
              onClick={() =>
                void run(async () => {
                  setError('')
                  try {
                    const result = await source.options()
                    const next = result?.providers || providers
                    setProviders(next)
                    const fresh = next.find(provider => provider.slug === selected.slug)
                    if (fresh?.authenticated) chooseModel(fresh)
                    else setError(t('gui.onb.oauth_wait'))
                  } catch (e) {
                    fail(e)
                  }
                })
              }
            >
              {t('gui.onb.oauth_check')}
            </button>
          </>
        ) : (
          <>
            <div className="ob-s">{t(local ? 'gui.onb.local_s' : 'gui.onb.key_s')}</div>
            <div className="ob-form">
              {acceptsApiKey ? (
                <KeyInput
                  ref={keyInput}
                  className="ob-in"
                  placeholder={t('gui.onb.key_ph')}
                  aria-label={t('gui.onb.key_ph')}
                  onInput={checkInputs}
                />
              ) : null}
              {needsBase ? (
                <input
                  ref={baseInput}
                  className="ob-in"
                  type="text"
                  defaultValue={selected.api_base || selected.default_api_base || ''}
                  placeholder={t('gui.onb.base_ph')}
                  autoComplete="off"
                  spellCheck={false}
                  onInput={checkInputs}
                />
              ) : null}
            </div>
            <button
              className="ob-btn"
              disabled={working || !ready}
              onClick={() =>
                void run(async () => {
                  setError('')
                  try {
                    const result = await source.saveKey(
                      selected.slug,
                      keyInput.current?.value.trim() || '',
                      baseInput.current?.value.trim() || ''
                    )
                    chooseModel(result?.provider || selected)
                  } catch (e) {
                    fail(e)
                  }
                })
              }
            >
              {t('gui.onb.next')}
            </button>
          </>
        )}
        <ErrorLine error={error} />
        <button className="ob-ghost" onClick={chooseProvider}>
          {t('gui.onb.back')}
        </button>
        <Dots current="creds" />
      </div>
    )
  } else if (step === 'model' && selected) {
    const models = selected.models || []
    const shown = models.filter(name => !query || name.toLowerCase().includes(query))
    body = (
      <div className="ob-step">
        <div className="ob-t">{t('gui.onb.model_t')}</div>
        <div className="ob-s">{t('gui.onb.model_s')}</div>
        {models.length > 8 ? (
          <input
            className="ob-in"
            placeholder={t('gui.onb.search_ph')}
            style={{ marginTop: '18px' }}
            autoComplete="off"
            spellCheck={false}
            onInput={e => setQuery(e.currentTarget.value.trim().toLowerCase())}
          />
        ) : null}
        <div className="ob-list">
          <ModelTagDefs />
          {shown.map(name => {
            const facts = selected.model_labels?.[name]
            return (
              <button
                key={name}
                className="ob-row"
                type="button"
                data-sel={name === model ? '1' : undefined}
                title={facts?.label ? `${name}${facts.description ? ` -- ${facts.description}` : ''}` : name}
                onClick={() => setModel(name)}
              >
                <span className="nm">{facts?.label || name}</span>
                <ModelTags facts={facts} />
              </button>
            )
          })}
        </div>
        <button
          className="ob-btn"
          disabled={working || !model}
          onClick={() =>
            void run(async () => {
              setError('')
              try {
                await source.setModel(model as string, selected.slug)
                if (await source.recheck()) finish()
                else fail('provider not configured')
              } catch (e) {
                fail(e)
              }
            })
          }
        >
          {t('gui.onb.finish')}
        </button>
        <ErrorLine error={error} />
        <button className="ob-ghost" onClick={chooseProvider}>
          {t('gui.onb.back')}
        </button>
        <Dots current="model" />
      </div>
    )
  } else {
    body = (
      <div className="ob-step" data-center="1">
        <Raven />
        <div className="ob-t">{t('gui.onb.done_t')}</div>
        <div className="ob-s">{t('gui.onb.done_s')}</div>
      </div>
    )
  }

  return <div className="ob">{body}</div>
}
