// UI language for the TUI, driven by `config.language` on the gateway.
//
// The locale is process-global rather than React state on purpose: slash
// command names are resolved in the registry and in the completion palette,
// neither of which sits under a provider. `setLocale` notifies subscribers so
// the chrome can repaint after `/lang`.

import type { Locale } from './messages.generated.js'

import { SLASH_TEXT, UI_TEXT } from './messages.generated.js'

export type { Locale }

export const LOCALES: Locale[] = ['en', 'zh']

export const isLocale = (value: unknown): value is Locale => value === 'en' || value === 'zh'

let current: Locale = 'en'

const listeners = new Set<(locale: Locale) => void>()

export const getLocale = (): Locale => current

export const setLocale = (locale: Locale): void => {
  if (locale === current) {
    return
  }

  current = locale
  listeners.forEach(fn => fn(locale))
}

export const onLocaleChange = (fn: (locale: Locale) => void): (() => void) => {
  listeners.add(fn)

  return () => {
    listeners.delete(fn)
  }
}

const fill = (text: string, vars?: Record<string, number | string>): string =>
  vars ? text.replace(/\{(\w+)\}/g, (whole, key: string) => (key in vars ? String(vars[key]) : whole)) : text

/** Catalogue lookup: active locale, then English, then the caller's fallback. */
export const t = (key: string, fallback = '', vars?: Record<string, number | string>): string =>
  fill(UI_TEXT[current]?.[key] ?? UI_TEXT.en?.[key] ?? fallback ?? key, vars)

/**
 * Display name for a slash command. English falls back to the canonical name
 * written in code -- the catalogue only carries what differs from it.
 */
export const slashName = (name: string): string => SLASH_TEXT[current]?.[name]?.name ?? name

/** Help line for a slash command, same fallback chain as {@link slashName}. */
export const slashHelp = (name: string, fallback = ''): string => SLASH_TEXT[current]?.[name]?.help ?? fallback

/** Every localized spelling of a command id, for alias registration. */
export const slashAliases = (name: string): string[] => {
  const out: string[] = []

  for (const locale of LOCALES) {
    const localized = SLASH_TEXT[locale]?.[name]?.name

    if (localized && localized !== name) {
      out.push(localized)
    }
  }

  return out
}
