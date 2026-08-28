/* The served front end's persisted appearance preference and its DOM effect. */

export interface LookState {
  theme: string
  codeFont: string
  motion: string
}

const KEY = 'raven.gui.look'
const DEFAULT: LookState = { theme: 'light', codeFont: 'system', motion: 'on' }

let state: LookState = { ...DEFAULT }

type NativeWindow = Window & {
  webkit?: { messageHandlers?: { raven?: { postMessage(value: unknown): void } } }
}

function tellNativeTheme(): void {
  const value = document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  try {
    const native = window as NativeWindow
    native.webkit?.messageHandlers?.raven?.postMessage({ type: 'theme', value })
  } catch {
    /* The browser host may expose a partial native bridge. */
  }
}

function apply(): void {
  const data = document.documentElement.dataset
  if (state.theme === 'system') delete data.theme
  else data.theme = state.theme
  data.motion = state.motion
  data.font = state.codeFont
  tellNativeTheme()
}

function save(): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state))
  } catch {
    /* Storage may be unavailable in private mode or over quota. */
  }
}

export function load(): void {
  state = { ...DEFAULT }
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(KEY) || 'null')
    if (raw && typeof raw === 'object') {
      const value = raw as Partial<LookState>
      if (value.theme) state.theme = value.theme
      if (value.codeFont) state.codeFont = value.codeFont
      if (value.motion) state.motion = value.motion
    }
  } catch {
    /* The defaults are the fallback for malformed or unavailable storage. */
  }
  apply()
}

export const get = (): LookState => ({ ...state })

export function set(patch: Partial<LookState>): void {
  if (patch.theme) state.theme = patch.theme
  if (patch.codeFont) state.codeFont = patch.codeFont
  if (patch.motion) state.motion = patch.motion
  apply()
  save()
}
