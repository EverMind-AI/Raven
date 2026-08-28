/* Browser and host facts shared by page chrome and feature islands. */

export const isMac = (): boolean => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)

export const modKey = (): string => (isMac() ? '⌘' : 'Ctrl +')

export const language = (): 'zh' | 'en' =>
  document.documentElement.lang.startsWith('zh') ? 'zh' : 'en'
