import { afterEach, describe, expect, it } from 'vitest'

import { SLASH_COMMANDS, findSlashCommand } from '../app/slash/registry.js'
import { slashCompletions } from '../hooks/useCompletion.js'
import { getLocale, setLocale, slashHelp, slashName, t } from '../i18n/index.js'
import { SLASH_TEXT, UI_TEXT } from '../i18n/messages.generated.js'

afterEach(() => setLocale('en'))

describe('locale catalogue', () => {
  it('defaults to English and falls back to the name written in code', () => {
    expect(getLocale()).toBe('en')
    expect(slashName('compress')).toBe('compress')
    // No English entry for /compress: the help written in code is the source.
    expect(slashHelp('compress', 'compress transcript')).toBe('compress transcript')
    // With an English entry, the catalogue wins over the code default.
    expect(slashHelp('model', 'change or show model')).toBe('change or show the model')
  })

  it('returns the Chinese name and help once the locale flips', () => {
    setLocale('zh')
    expect(slashName('model')).toBe('切换模型')
    expect(slashName('compress')).toBe('压缩上下文')
    expect(slashHelp('model', 'change or show model')).toBe('切换或查看当前模型')
  })

  it('fills placeholders and falls back through en to the caller default', () => {
    setLocale('zh')
    expect(t('gui.model.count', '', { n: 7 })).toBe('7 个模型')
    expect(t('nope.missing', 'fallback')).toBe('fallback')
  })

  it('keeps both spellings typeable', () => {
    expect(findSlashCommand('切换模型')).toBe(findSlashCommand('model'))
    expect(findSlashCommand('压缩上下文')).toBe(findSlashCommand('compress'))
    expect(findSlashCommand('MODEL')).toBe(findSlashCommand('model'))
  })
})

describe('completion palette', () => {
  it('lists localized names when the locale is Chinese', () => {
    setLocale('zh')
    const items = slashCompletions('/切换', SLASH_COMMANDS)

    expect(items.map(i => i.display)).toContain('/切换模型')
    expect(items.find(i => i.display === '/切换模型')?.meta).toBe('切换或查看当前模型')
  })

  it('still completes the English name under a Chinese locale', () => {
    setLocale('zh')
    expect(slashCompletions('/mod', SLASH_COMMANDS).map(i => i.text)).toContain('/切换模型')
  })
})

describe('catalogue shape', () => {
  it('gives every ui key both locales', () => {
    const missing = Object.keys(UI_TEXT.en).filter(k => !(k in UI_TEXT.zh))

    expect(missing).toEqual([])
  })

  it('only translates command ids the TUI actually has', () => {
    // `gui.*` ids belong to the desktop app's own command list; everything
    // else must resolve here, through a name or an alias.
    const strays = Object.keys(SLASH_TEXT.zh).filter(id => !id.startsWith('gui.') && !findSlashCommand(id))

    expect(strays).toEqual([])
  })
})
