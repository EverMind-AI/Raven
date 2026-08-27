import type { MutableRefObject } from 'react'

import type { SlashHandlerContext, UiState } from '../interfaces.js'

export interface SlashRunCtx extends SlashHandlerContext {
  flight: number
  guarded: <T>(fn: (r: T) => void) => (r: null | T) => void
  guardedErr: (e: unknown) => void
  sid: null | string
  slashFlightRef: MutableRefObject<number>
  stale: () => boolean
  ui: UiState
}

export interface SlashCommand {
  aliases?: string[]
  help?: string
  name: string
  run: (arg: string, ctx: SlashRunCtx, cmd: string) => void
  // Absent = shown in the palette (default). `false` = hidden from completion,
  // regardless of why (broken backing OR intentionally not surfaced). The
  // command stays typeable and still runs; only the palette hides it.
  supported?: boolean
  usage?: string
}
